"""Persistent top-level engine page. Background work reports through a Tk-owned queue."""
from pathlib import Path
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from .engine_installation import engine_status
from .engine_runtime import engine_python, engine_environment, program_root


def engine_operation_busy(app) -> bool:
    manager = getattr(app, 'engine_manager', None)
    return bool(manager and manager.busy)


def audio_active(app) -> bool:
    return any((getattr(task, 'worker', None) and task.worker.is_alive()) or
               (getattr(task, 'preview_thread', None) and task.preview_thread.is_alive())
               for task in app.tabs.values())


class EngineManager:
    def __init__(self, app, parent):
        self.app = app
        self.root = app.root
        self.frame = ttk.Frame(parent, padding=18)
        self.messages = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.closed = False
        self.thread = None
        self.after_id = None
        self.statuses = {}
        self.buttons = []
        ttk.Label(self.frame, text='Голосовые движки', font=('Segoe UI', 16, 'bold')).pack(anchor='w')
        ttk.Label(self.frame, text='Устанавливайте недостающие движки или удаляйте их из программы.\n'
                  'Установленные компоненты не обновляются и не заменяются автоматически.',
                  wraplength=720).pack(anchor='w', pady=(8, 16))
        for engine, title, detail in [
            ('silero', 'Silero · 5 русских голосов', 'Без интернета после установки. Модель около 148 МБ; при отсутствии PyTorch потребуется дополнительная загрузка.'),
            ('edge', 'Microsoft Edge · Светлана и Дмитрий', 'Нужен интернет. При озвучке текст передаётся Microsoft. Устанавливается отдельная копия для этой программы.'),
        ]:
            card = ttk.LabelFrame(self.frame, text=title, padding=12)
            card.pack(fill='x', pady=(0, 12))
            ttk.Label(card, text=detail, wraplength=650).pack(anchor='w')
            value = tk.StringVar(value='Проверяю…')
            self.statuses[engine] = value
            ttk.Label(card, textvariable=value).pack(anchor='w', pady=6)
            row = ttk.Frame(card)
            row.pack(anchor='w')
            for action, label in [('install', 'Установить'), ('remove', 'Удалить')]:
                button = ttk.Button(row, text=label, command=lambda e=engine, a=action: self.start(a, e))
                button.pack(side='left', padx=(0, 8))
                self.buttons.append(button)
        ttk.Label(self.frame, text='Windows SAPI и сторонние системные голоса остаются без изменений.\n'
                  'При удалении сохраняется резервная копия. Ваши тексты, MP3 и настройки не удаляются.',
                  wraplength=720).pack(anchor='w', pady=4)
        self.message = tk.StringVar(value='')
        ttk.Label(self.frame, textvariable=self.message, wraplength=720).pack(anchor='w', pady=10)
        self.progress = ttk.Progressbar(self.frame, mode='indeterminate')
        self.progress.pack(fill='x')
        self.cancel = ttk.Button(self.frame, text='Отменить операцию', command=self.stop.set, state='disabled')
        self.cancel.pack(anchor='w', pady=8)
        self.frame.bind('<Destroy>', self._destroyed, add='+')
        self.refresh()
        self.after_id = self.root.after(100, self.poll)

    def _destroyed(self, event):
        if event.widget == self.frame:
            self.close()

    def refresh(self):
        def inspect():
            try:
                result = {engine: engine_status(engine) for engine in ('silero', 'edge')}
            except Exception:
                result = {engine: 'Не удалось проверить файлы' for engine in ('silero', 'edge')}
            self.messages.put(('status', result))
        threading.Thread(target=inspect, daemon=True, name='engine-status').start()

    def start(self, action, engine):
        if self.closed or self.busy or getattr(self.app, 'shutdown_in_progress', False):
            return
        if audio_active(self.app):
            messagebox.showinfo('Голосовые движки', 'Сначала остановите прослушивание и создание MP3.', parent=self.root)
            return
        if action == 'remove' and not messagebox.askyesno('Удалить движок?',
                'Удалить выбранный движок из программы? Резервная копия будет сохранена.\n'
                'Windows-голоса, настройки и ваши тексты останутся без изменений.', parent=self.root, default='no'):
            return
        # A confirmation dialog runs a nested event loop: hotkeys/close may have run.
        if self.closed or self.busy or getattr(self.app, 'shutdown_in_progress', False) or audio_active(self.app):
            return
        self.busy = True
        self.stop.clear()
        for button in self.buttons:
            button.configure(state='disabled')
        self.cancel.configure(state='normal')
        self.progress.start()
        self.message.set('Устанавливаю… Для загрузки нужен интернет.' if action == 'install' else 'Удаляю с сохранением копии…')
        self.thread = threading.Thread(target=self.run, args=(action, engine), daemon=True, name='engine-management')
        self.thread.start()

    def run(self, action, engine):
        process = None
        result = 'Операция не завершена. Проверьте интернет и доступ к папке программы.'
        try:
            script = program_root() / 'tools' / 'manage_voice_engine.py'
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen([engine_python(), str(script), action, engine], stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.DEVNULL, env=engine_environment(),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                deadline = time.monotonic() + 1800
                while process.poll() is None:
                    if self.stop.wait(0.05) or time.monotonic() >= deadline:
                        break
                if process.poll() is None:
                    if sys.platform == 'win32':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    else:
                        process.terminate()
                    process.wait(timeout=5)
                    result = 'Операция остановлена. Успевшие установиться компоненты сохранены; состояние обновлено.'
                else:
                    output.seek(0)
                    payload = json.loads(output.read(4096))
                    result = str(payload['message'])[:1000]
        except Exception:
            pass
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            self.messages.put(('done', result))

    def poll(self):
        self.after_id = None
        if self.closed:
            return
        for _ in range(8):
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'status':
                for engine, value in payload.items():
                    self.statuses[engine].set(value)
            elif kind == 'done':
                self.busy = False
                self.message.set(payload)
                self.progress.stop()
                self.cancel.configure(state='disabled')
                for button in self.buttons:
                    button.configure(state='normal')
                self.refresh()
        self.after_id = self.root.after(100, self.poll)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
