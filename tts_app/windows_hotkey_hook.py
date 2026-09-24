from __future__ import annotations

from .runtime import *

class WindowsHotkeyHookMixin:
    _HOOK_MODIFIER_VKS = {
        "CTRL": (0x11, 0xA2, 0xA3),
        "SHIFT": (0x10, 0xA0, 0xA1),
        "ALT": (0x12, 0xA4, 0xA5),
    }
    _HOOK_INJECTED_FLAG = 0x10

    def _hook_modifiers_match(self):
        """Match modifiers from hook events, not GetAsyncKeyState()."""
        required = set(self.hotkey_modifiers)
        for modifier in self.MODIFIER_ORDER:
            pressed = any(
                vk in self._pressed_vks
                for vk in self._HOOK_MODIFIER_VKS[modifier]
            )
            if (modifier in required) != pressed:
                return False
        return True

    @classmethod
    def _hook_event_is_injected(cls, flags):
        # Our synthetic Ctrl+C must never trigger another tab hotkey monitor.
        return bool(int(flags) & cls._HOOK_INJECTED_FLAG)

    def _tk_sequence_for_hotkey(self):
        parts = []
        if "CTRL" in self.hotkey_modifiers:
            parts.append("Control")
        if "ALT" in self.hotkey_modifiers:
            parts.append("Alt")
        if "SHIFT" in self.hotkey_modifiers:
            parts.append("Shift")

        # Tkinter не умеет глобально ловить Q+W без сторонних библиотек.
        # В локальном запасном режиме берём последнюю клавишу сочетания.
        key = self.hotkey_main_keys[-1]
        if len(key) == 1:
            key = key.lower()
        parts.append(key)

        return "<" + "-".join(parts) + ">"

    def _enable_local_binding(self):
        self._disable_local_binding()
        sequence = self._tk_sequence_for_hotkey()
        self.root.bind_all(sequence, self._local_hotkey, add="+")
        self._local_binding_sequence = sequence

    def _disable_local_binding(self):
        if not self._local_binding_sequence:
            return
        try:
            self.root.unbind_all(self._local_binding_sequence)
        except Exception:
            pass
        self._local_binding_sequence = None

    def _local_hotkey(self, event=None):
        if self.enabled:
            context = self._foreground_capture_context()
            self.root.after(
                70,
                lambda: self._copy_selection_then_run_callback(
                    context
                ),
            )
        # Не возвращаем "break", чтобы обычное копирование не ломалось.
        return None

    def _windows_keyboard_hook_loop(self):
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WH_KEYBOARD_LL = 13
        HC_ACTION = 0
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        WM_SYSKEYDOWN = 0x0104
        WM_SYSKEYUP = 0x0105
        GA_ROOT = 2

        ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong)
        HHOOK = getattr(wintypes, "HHOOK", wintypes.HANDLE)
        HINSTANCE = getattr(wintypes, "HINSTANCE", wintypes.HANDLE)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
        LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
            LRESULT,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelKeyboardProc, HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = HHOOK
        user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = HINSTANCE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        try:
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetForegroundWindow.restype = wintypes.HWND
        except Exception:
            pass

        def foreground_is_our_app():
            try:
                if not self._root_hwnd:
                    return False
                fg = user32.GetForegroundWindow()
                if not fg:
                    return False
                root_fg = user32.GetAncestor(fg, GA_ROOT)
                return int(fg) == int(self._root_hwnd) or int(root_fg) == int(self._root_hwnd)
            except Exception:
                return False

        def hotkey_is_pressed():
            return (
                self.hotkey_vks.issubset(self._pressed_vks)
                and self._hook_modifiers_match()
            )

        def should_suppress(vk):
            # Plain Ctrl+C не глушим: пусть Telegram/браузер/Word копируют штатно.
            if self._hotkey_is_plain_ctrl_c():
                return False
            if vk not in self.hotkey_vks:
                return False
            return self._hook_modifiers_match() or len(self.hotkey_vks) > 1

        def hook_proc(n_code, w_param, l_param):
            try:
                if n_code == HC_ACTION:
                    kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

                    # keybd_event/SendInput-generated Ctrl+C is visible to a
                    # WH_KEYBOARD_LL hook as injected input.  Ignoring it here
                    # prevents an F8/F9 capture from recursively firing a
                    # different tab that is bound to Ctrl+C.
                    if self._hook_event_is_injected(kb.flags):
                        return user32.CallNextHookEx(
                            self._hook_id,
                            n_code,
                            w_param,
                            l_param,
                        )

                    vk = int(kb.vkCode)
                    is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    is_up = w_param in (WM_KEYUP, WM_SYSKEYUP)

                    if is_down:
                        self._pressed_vks.add(vk)
                    elif is_up:
                        # На keyup проверка уже не нужна, но клавишу из набора убираем.
                        self._pressed_vks.discard(vk)

                    if self.enabled and not foreground_is_our_app():
                        if is_down and vk in self.hotkey_vks and hotkey_is_pressed():
                            now = time.monotonic()
                            if now - self._last_trigger_time > 0.35:
                                self._last_trigger_time = now
                                context = self._foreground_capture_context()
                                self._last_trigger_context = dict(
                                    context
                                )
                                self._events.put(context)
                            if not self._hotkey_is_plain_ctrl_c():
                                return 1

                        if should_suppress(vk):
                            return 1
            except Exception:
                pass

            return user32.CallNextHookEx(self._hook_id, n_code, w_param, l_param)

        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook_callback = LowLevelKeyboardProc(hook_proc)
        module_handle = kernel32.GetModuleHandleW(None)
        self._hook_id = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_callback, module_handle, 0)

        if not self._hook_id:
            try:
                self._hook_install_error = int(
                    kernel32.GetLastError()
                )
            except Exception:
                self._hook_install_error = None
            self._hook_ready_event.set()
            return

        self._hook_install_error = None
        self._hook_ready_event.set()

        msg = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._hook_ready_event.set()
            if self._hook_id:
                user32.UnhookWindowsHookEx(self._hook_id)
                self._hook_id = None
