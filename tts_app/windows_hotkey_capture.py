from __future__ import annotations

from .runtime import *

class WindowsHotkeyCaptureMixin:
    def _is_our_app_focused(self):
        """Нужно, чтобы глобальная автовставка не мешала обычному Ctrl+C/Ctrl+V внутри программы."""
        try:
            return self.root.focus_get() is not None
        except Exception:
            return False

    def _clipboard_sequence_number(self) -> int | None:
        if not self.is_windows:
            return None
        try:
            return int(
                ctypes.windll.user32.GetClipboardSequenceNumber()
            )
        except Exception:
            return None

    @staticmethod
    def _process_name_from_pid(pid: int) -> str:
        if os.name != "nt" or not pid:
            return ""
        try:
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            kernel32.QueryFullProcessImageNameW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_wchar),
                ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [
                ctypes.c_void_p,
            ]

            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                int(pid),
            )
            if not handle:
                return ""
            try:
                size = ctypes.c_ulong(32768)
                buffer = ctypes.create_unicode_buffer(
                    size.value
                )
                ok = kernel32.QueryFullProcessImageNameW(
                    handle,
                    0,
                    buffer,
                    ctypes.byref(size),
                )
                if not ok:
                    return ""
                return Path(buffer.value).name
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return ""

    def _foreground_capture_context(self) -> dict:
        # Keep this method very fast because it can run inside WH_KEYBOARD_LL.
        # Process-name resolution is deferred to the Tk thread.
        if not self.is_windows:
            return {
                "source_process": "",
                "source_pid": None,
                "clipboard_sequence_before": None,
                "trigger_perf": time.perf_counter(),
            }

        try:
            user32 = ctypes.windll.user32
            hwnd = int(user32.GetForegroundWindow() or 0)
            pid = ctypes.c_ulong()
            if hwnd:
                user32.GetWindowThreadProcessId(
                    hwnd,
                    ctypes.byref(pid),
                )
            pid_value = int(pid.value or 0)
            return {
                "source_process": "",
                "source_pid": pid_value or None,
                "clipboard_sequence_before": (
                    self._clipboard_sequence_number()
                ),
                "trigger_perf": time.perf_counter(),
            }
        except Exception:
            return {
                "source_process": "",
                "source_pid": None,
                "clipboard_sequence_before": (
                    self._clipboard_sequence_number()
                ),
                "trigger_perf": time.perf_counter(),
            }

    def _poll_events(self):
        try:
            while True:
                trigger_context = self._events.get_nowait()
                if not isinstance(trigger_context, dict):
                    trigger_context = dict(
                        self._last_trigger_context
                    )

                # A short delay only lets the physical hotkey keys come up.
                # Clipboard readiness itself is detected by sequence number.
                self.root.after(
                    70,
                    lambda context=dict(trigger_context):
                        self._copy_selection_then_run_callback(
                            context
                        ),
                )
        except queue.Empty:
            pass

        if not self._stop_event.is_set():
            self.root.after(100, self._poll_events)

    def _copy_selection_then_run_callback(
        self,
        trigger_context: dict | None = None,
    ):
        if not self.enabled:
            return

        # Если фокус внутри самой программы, не делаем автовставку —
        # так обычное копирование/вставка в полях программы не ломается.
        if self._is_our_app_focused():
            return

        context = dict(trigger_context or {})
        if (
            not context.get("source_process")
            and context.get("source_pid")
        ):
            context["source_process"] = (
                self._process_name_from_pid(
                    int(context["source_pid"])
                )
            )

        if "clipboard_sequence_before" not in context:
            context["clipboard_sequence_before"] = (
                self._clipboard_sequence_number()
            )
        if "trigger_perf" not in context:
            context["trigger_perf"] = time.perf_counter()

        # Для обычного Ctrl+C копирование уже сделал Telegram/браузер/другая программа.
        # Для других сочетаний программа сама отправляет Ctrl+C активному окну.
        if not self._hotkey_is_plain_ctrl_c():
            self._send_ctrl_c_to_active_window()

        self._wait_for_clipboard_change(
            context,
            deadline=time.perf_counter()
            + CLIPBOARD_COPY_TIMEOUT_SEC,
        )

    def _wait_for_clipboard_change(
        self,
        context: dict,
        *,
        deadline: float,
    ) -> None:
        if not self.enabled:
            return

        before = context.get("clipboard_sequence_before")
        after = self._clipboard_sequence_number()
        changed = bool(
            before is not None
            and after is not None
            and int(after) != int(before)
        )

        # If sequence numbers are unavailable, keep compatibility by allowing
        # a small delay and then trying the clipboard once.
        sequence_unavailable = (
            before is None or after is None
        )
        elapsed = max(
            0.0,
            time.perf_counter()
            - float(
                context.get("trigger_perf")
                or time.perf_counter()
            ),
        )

        if changed or (
            sequence_unavailable
            and elapsed >= 0.20
        ):
            capture_info = {
                **context,
                "clipboard_changed": (
                    True if changed else None
                ),
                "clipboard_sequence_after": after,
                "copy_latency_ms": round(
                    elapsed * 1000.0,
                    1,
                ),
            }
            self._last_capture_info = capture_info
            self._run_callback(capture_info)
            return

        if time.perf_counter() >= deadline:
            capture_info = {
                **context,
                "clipboard_changed": False,
                "clipboard_sequence_after": after,
                "copy_latency_ms": round(
                    elapsed * 1000.0,
                    1,
                ),
            }
            self._last_capture_info = capture_info
            self._run_callback(capture_info)
            return

        self.root.after(
            CLIPBOARD_POLL_INTERVAL_MS,
            lambda: self._wait_for_clipboard_change(
                context,
                deadline=deadline,
            ),
        )

    def _run_callback(self, capture_info: dict | None = None):
        if self.enabled:
            self.callback(capture_info or {})

    def _hotkey_is_plain_ctrl_c(self):
        return (
            len(self.hotkey_main_keys) == 1
            and self.hotkey_main_keys[0] == "C"
            and self.hotkey_modifiers == {"CTRL"}
        )

    def _send_ctrl_c_to_active_window(self):
        if not self.is_windows:
            return

        try:
            import ctypes
            user32 = ctypes.windll.user32

            VK_CONTROL = 0x11
            VK_C = 0x43
            KEYEVENTF_KEYUP = 0x0002

            # Отпускаем клавиши выбранной горячей клавиши и модификаторы,
            # чтобы Q+W/F8/Ctrl+Shift+C не мешали отправить обычный Ctrl+C.
            for vk in sorted(self.hotkey_vks):
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

            for vk in (0x10, 0x12):  # Shift, Alt
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_C, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass
