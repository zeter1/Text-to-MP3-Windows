from __future__ import annotations

from .runtime import *

class WindowsHotkeyConfigMixin:
    def __init__(self, root, callback, hotkey="Ctrl+C"):
        self.root = root
        self.callback = callback
        self.enabled = False
        self.is_windows = (os.name == "nt")
        self._stop_event = threading.Event()
        self._events = queue.Queue()
        self._thread = None
        self._thread_id = None
        self._hook_id = None
        self._hook_callback = None
        self._hook_ready_event = threading.Event()
        self._hook_install_error: int | None = None
        self._local_binding_sequence = None
        self._last_trigger_time = 0
        self._last_text = None
        self._last_text_time = 0
        self._pressed_vks = set()
        self._last_trigger_context: dict = {}
        self._last_capture_info: dict = {}
        try:
            self._root_hwnd = int(self.root.winfo_id())
        except Exception:
            self._root_hwnd = None

        self.hotkey_text = "Ctrl+C"
        self.hotkey_modifiers = {"CTRL"}
        self.hotkey_main_keys = ("C",)
        self.hotkey_main = "C"          # оставлено для совместимости со старой логикой
        self.hotkey_vks = {self.VK_BY_NAME["C"]}
        self.hotkey_vk = self.VK_BY_NAME["C"]
        self.set_hotkey(hotkey)

        # Очередь нужна, чтобы обработчик клавиатуры не трогал Tkinter напрямую.
        self.root.after(100, self._poll_events)

        if self.is_windows:
            self._thread = threading.Thread(target=self._windows_keyboard_hook_loop, daemon=True)
            self._thread.start()

    @classmethod
    def parse_hotkey(cls, hotkey):
        raw = (hotkey or "").strip()
        if not raw:
            raise ValueError("Введите горячую клавишу, например F8, Q+W или Ctrl+Shift+C.")

        prepared = (
            raw.replace("Control", "Ctrl")
               .replace("CONTROL", "Ctrl")
               .replace("control", "Ctrl")
               .replace("контрол", "Ctrl")
               .replace("кнтрл", "Ctrl")
               .replace("альт", "Alt")
               .replace("шифт", "Shift")
               .replace(" ", "")
        )
        parts = [p for p in prepared.split("+") if p]
        if not parts:
            raise ValueError("Не удалось прочитать горячую клавишу.")

        modifiers = set()
        main_keys = []

        aliases = {
            "CTRL": "CTRL",
            "CONTROL": "CTRL",
            "SHIFT": "SHIFT",
            "ALT": "ALT",
            "OPTION": "ALT",
        }

        for part in parts:
            token = part.upper()
            token = cls.CYRILLIC_TO_LATIN.get(token, token)
            if token in aliases:
                modifiers.add(aliases[token])
                continue

            if token not in cls.VK_BY_NAME:
                raise ValueError(
                    "Эта клавиша пока не поддерживается. Используйте буквы, цифры, F1-F24 "
                    "или сочетания вроде Q+W, Й+Ц, Ctrl+Alt+X."
                )
            if token not in main_keys:
                main_keys.append(token)

        if not main_keys:
            raise ValueError("Добавьте основную клавишу: F8, Q+W, C, X и т.д.")

        if len(main_keys) > 3:
            raise ValueError("В горячей клавише можно использовать максимум 3 основные клавиши.")

        normalized_parts = []
        for mod in cls.MODIFIER_ORDER:
            if mod in modifiers:
                normalized_parts.append({"CTRL": "Ctrl", "SHIFT": "Shift", "ALT": "Alt"}[mod])

        def display_key(key):
            if key.startswith("F") and key[1:].isdigit():
                return key
            if key == "SPACE":
                return "Space"
            if key == "TAB":
                return "Tab"
            if key == "ENTER":
                return "Enter"
            return key

        normalized_parts.extend(display_key(k) for k in main_keys)
        normalized = "+".join(normalized_parts)

        return {
            "text": normalized,
            "modifiers": modifiers,
            "main_keys": tuple(main_keys),
            "vks": {cls.VK_BY_NAME[k] for k in main_keys},
        }

    def set_hotkey(self, hotkey):
        parsed = self.parse_hotkey(hotkey)
        self.hotkey_text = parsed["text"]
        self.hotkey_modifiers = parsed["modifiers"]
        self.hotkey_main_keys = parsed["main_keys"]
        self.hotkey_main = self.hotkey_main_keys[0]
        self.hotkey_vks = parsed["vks"]
        self.hotkey_vk = next(iter(self.hotkey_vks))
        self._pressed_vks.clear()

        if not self.is_windows and self.enabled:
            self._enable_local_binding()

        return self.hotkey_text

    def get_hotkey(self):
        return self.hotkey_text

    def enable(self):
        if self.is_windows:
            # Constructor starts the dedicated WH_KEYBOARD_LL thread.  Do not
            # report the hotkey as active until Windows has actually accepted
            # the hook; previously the UI could say "... → только эта вкладка"
            # even when SetWindowsHookExW had failed in the background thread.
            if not self._hook_ready_event.wait(timeout=1.5):
                self.enabled = False
                raise RuntimeError(
                    "Windows не подтвердил запуск глобальной горячей клавиши. "
                    "Попробуйте назначить её ещё раз или перезапустить программу."
                )
            if not self._hook_id:
                self.enabled = False
                error_code = self._hook_install_error
                suffix = (
                    f" (WinError {error_code})"
                    if error_code
                    else ""
                )
                raise RuntimeError(
                    "Не удалось зарегистрировать глобальный перехватчик клавиатуры"
                    + suffix
                    + "."
                )

        self.enabled = True
        if not self.is_windows:
            self._enable_local_binding()

    def disable(self):
        self.enabled = False
        if not self.is_windows:
            self._disable_local_binding()

    def stop(self):
        self.disable()
        self._stop_event.set()
        if self.is_windows and self._thread_id:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                WM_QUIT = 0x0012
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass

    def remember_text_and_check_duplicate(self, text):
        """Защита от случайного двойного срабатывания на один и тот же текст."""
        now = time.monotonic()
        if text == self._last_text and now - self._last_text_time < 1.2:
            return True
        self._last_text = text
        self._last_text_time = now
        return False
