from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk


def notification_work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    """Use the main window's monitor, excluding the Windows taskbar."""
    if os.name == "nt":
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = user32.MonitorFromWindow(root.winfo_id(), 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        rect = info.rcWork
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def reveal_audio_file(path: Path) -> None:
    """Select the output without creating directories or changing any files."""
    path = path.resolve()
    if os.name == "nt" and path.is_file():
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    elif path.parent.is_dir():
        if os.name == "nt":
            os.startfile(str(path.parent))
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    else:
        raise FileNotFoundError("Файл и его папка больше недоступны:\n" + str(path))


class ConversionNotifications:
    """Independent permanent windows; every method runs on the owning Tk thread.

    No timer, grab, transient owner, or focus request: other tasks keep running
    and minimizing the main window does not hide the notifications.
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.thread_id = threading.get_ident()
        self.windows: list[tk.Toplevel] = []

    def show(self, output: Path, note: str = "") -> None:
        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Уведомления должны создаваться в потоке Tk.")
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        # Ignore WM close / Alt+F4: only the visible cross dismisses this item.
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        window.title("MP3 готов")
        self.windows.append(window)

        body = ttk.Frame(window, padding=12, relief="solid", borderwidth=1)
        body.pack(fill="both", expand=True)
        header = ttk.Frame(body)
        header.pack(fill="x")
        ttk.Label(header, text="Аудиофайл готов", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(header, text="×", width=3,
                   command=lambda: self.dismiss(window)).pack(side="right")
        left, top, right, bottom = notification_work_area(self.root)
        width = min(400, right - left - 24)
        # Keep controls reachable for long Windows paths; the action retains
        # the complete path, while the label shows the identifying suffix.
        name = output.name if len(output.name) <= 100 else output.name[:80] + "…" + output.suffix
        folder = str(output.parent)
        if len(folder) > 120:
            folder = "…" + folder[-120:]
        for text in (name, folder, note):
            if text:
                ttk.Label(body, text=text, wraplength=max(100, width - 32),
                          justify="left").pack(fill="x", pady=(6, 0))
        ttk.Button(body, text="Показать файл",
                   command=lambda: self.reveal(window, output)).pack(anchor="w", pady=(10, 0))
        window.geometry(f"{width}x1")
        window.update_idletasks()
        self._reflow((left, top, right, bottom))

    def dismiss(self, window: tk.Toplevel) -> None:
        if window not in self.windows:
            return
        self.windows.remove(window)
        window.destroy()
        self._reflow()

    def reveal(self, window: tk.Toplevel, output: Path) -> None:
        try:
            reveal_audio_file(output)
        except OSError as exc:
            messagebox.showerror("Не удалось открыть расположение файла", str(exc), parent=window)
        # Opening Explorer, including a failed attempt, never dismisses the item.

    def _reflow(self, area: tuple[int, int, int, int] | None = None) -> None:
        if not self.windows:
            return
        left, top, right, bottom = area or notification_work_area(self.root)
        margin = 12
        width = min(400, right - left - margin * 2)
        x, y = right - width - margin, bottom - margin
        for window in reversed(self.windows):
            height = window.winfo_reqheight()
            if y - height < top + margin:
                x = max(left + margin, x - width - margin)
                y = bottom - margin
            y -= height
            window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
            window.deiconify()
            if os.name == "nt":
                # Tk negative geometry means distance from the right/bottom edge.
                # Native coordinates also handle monitors left/above the primary.
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.GetParent.argtypes = [wintypes.HWND]
                user32.GetParent.restype = wintypes.HWND
                user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                               ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
                user32.SetWindowPos.restype = wintypes.BOOL
                hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
                if not user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0015):
                    raise ctypes.WinError(ctypes.get_last_error())
            y -= margin
