from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *

class AppWorkspaceInitMixin:
    def __init__(self, root: tk.Tk) -> None:
        ensure_help_files()

        self.root = root
        self.settings = load_settings()
        self.logger = ProblemLogger()

        try:
            removed_backups, failed_backups = (
                cleanup_old_mp3_text_backups()
            )
            self.logger.event(
                "mp3_text_backup_cleanup",
                retention_days=(
                    MP3_TEXT_BACKUP_RETENTION_DAYS
                ),
                removed=removed_backups,
                failed=failed_backups,
                backup_dir=str(MP3_TEXT_BACKUPS_DIR),
            )
        except Exception as exc:
            self.logger.error(
                task_id="app",
                stage="mp3_text_backup_cleanup",
                exc=exc,
                traceback_text=traceback.format_exc(),
                context={
                    "backup_dir": str(
                        MP3_TEXT_BACKUPS_DIR
                    ),
                    "retention_days": (
                        MP3_TEXT_BACKUP_RETENTION_DAYS
                    ),
                },
            )

        # One Speed value for the whole application and all tabs.
        self.global_rate_var = tk.IntVar(
            value=clamp_int(self.settings.get("rate"), RATE_MIN, RATE_MAX, 0)
        )
        self._global_rate_syncing = False
        self.global_rate_var.trace_add("write", self._on_global_rate_changed)

        # One Pitch value for the whole application and all tabs.
        self.global_pitch_var = tk.IntVar(
            value=clamp_int(self.settings.get("pitch"), PITCH_MIN, PITCH_MAX, 0)
        )
        self._global_pitch_syncing = False
        self.global_pitch_var.trace_add("write", self._on_global_pitch_changed)

        self.global_audio_output_var = tk.StringVar(
            value=str(
                self.settings.get("audio_output")
                or DEFAULT_AUDIO_OUTPUT_LABEL
            )
        )
        self.global_audio_output_var.trace_add(
            "write",
            self._on_global_audio_output_changed,
        )
        self.audio_outputs: list[str] = []
        self.audio_output_choices: list[str] = [DEFAULT_AUDIO_OUTPUT_LABEL]

        self.root.title(
            f"{APP_TITLE} — {APP_VERSION}"
        )
        self.root.minsize(900, 680)

        try:
            self.root.geometry(
                str(self.settings.get("window_geometry") or "1200x820")
            )
        except tk.TclError:
            self.root.geometry("1200x820")

        self.events: queue.Queue[
            tuple[str, str, object]
        ] = queue.Queue()

        self.voices: list[str] = []
        self.tabs: dict[str, TaskTab] = {}
        self.task_counter = 0
        self.settings_after_id: str | None = None
        self.workspace_after_id: str | None = None
        self.workspace_save_due_perf: float | None = None
        self.workspace_last_saved_perf = time.perf_counter()
        self.workspace_save_failure_count = 0
        self.context_tab_index: int | None = None
        self.shutdown_in_progress = False
        self.shutdown_deadline = 0.0

        # One low-level monitor per ASSIGNED tab hotkey. A monitor's callback is
        # permanently bound to that tab's workspace_id, so changing the visible
        # active tab cannot redirect captured text to the wrong place.
        self.tab_hotkey_monitors: dict[
            str,
            GlobalCopyHotkeyMonitor,
        ] = {}

        # Состояние берём из реальной записи Windows, а не только из JSON.
        autostart_enabled = False
        if os.name == "nt":
            autostart_enabled = windows_autostart_is_enabled()
            if autostart_enabled:
                # Если файл/EXE перенесли, при ручном запуске обновляем путь
                # существующей записи автозапуска на текущую копию программы.
                try:
                    set_windows_autostart(True)
                except Exception:
                    pass
        self.autostart_windows_var = tk.BooleanVar(
            value=autostart_enabled
        )
        self.settings["autostart_windows"] = autostart_enabled

        self._build_ui()
        self._restore_workspace()
        self._activate_restored_tab_hotkeys()

        legacy_removed = cleanup_legacy_temp_workdirs()
        if legacy_removed:
            self.logger.event(
                "legacy_temp_cleanup",
                removed_dirs=legacy_removed,
                retention_days=RECOVERY_RETENTION_DAYS,
            )

        self.root.after(100, self._process_events)
        self.root.after(900, self._offer_recovery_jobs)
        self._load_voices_async()
        self._load_audio_outputs_async()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 7))

        ttk.Label(
            top,
            text=f"Версия: {APP_VERSION}",
        ).pack(side="right")

        ttk.Button(
            top,
            text="＋ Новая вкладка",
            command=self.new_tab,
        ).pack(side="left")

        self.autostart_checkbutton = ttk.Checkbutton(
            top,
            text="Автозапуск с Windows",
            variable=self.autostart_windows_var,
            command=self.toggle_windows_autostart,
        )
        self.autostart_checkbutton.pack(
            side="left",
            padx=(14, 0),
        )
        if os.name != "nt":
            self.autostart_checkbutton.configure(state="disabled")

        self.notebook = ClosableNotebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self.notebook.bind(
            "<<NotebookCloseRequested>>",
            self._on_notebook_close_requested,
            add="+",
        )
        self.notebook.bind(
            "<Button-3>",
            self._on_tab_right_click,
            add="+",
        )
        self.notebook.bind(
            "<<NotebookTabChanged>>",
            lambda _event: self.schedule_workspace_save(),
            add="+",
        )

        self.tab_menu = tk.Menu(self.root, tearoff=False)
        self.tab_menu.add_command(
            label="Переименовать вкладку",
            command=self.rename_context_tab,
        )
        self.tab_menu.add_command(
            label="Закрыть вкладку",
            command=self.close_context_tab,
        )

    def _restore_workspace(self) -> None:
        entries: list[dict] = []
        selected_index = 0

        if WORKSPACE_FILE.exists():
            try:
                payload = json.loads(
                    WORKSPACE_FILE.read_text(encoding="utf-8")
                )
                if isinstance(payload, dict):
                    raw_entries = payload.get("tabs", [])
                    if isinstance(raw_entries, list):
                        entries = [
                            item for item in raw_entries
                            if isinstance(item, dict)
                        ]
                    selected_index = clamp_int(
                        payload.get("selected_index", 0),
                        0,
                        max(0, len(entries) - 1),
                        0,
                    )
            except Exception as exc:
                self.logger.error(
                    task_id="app",
                    stage="restore_workspace_manifest",
                    exc=exc,
                    traceback_text=traceback.format_exc(),
                    context={"workspace_file": str(WORKSPACE_FILE)},
                )

        has_per_tab_hotkeys = any(
            "copy_hotkey" in entry
            for entry in entries
        )
        legacy_hotkey = TAB_HOTKEY_NONE_LABEL
        if (
            entries
            and not has_per_tab_hotkeys
            and bool(
                self.settings.get(
                    "global_copy_enabled",
                    False,
                )
            )
        ):
            legacy_hotkey = str(
                self.settings.get("global_copy_hotkey")
                or TAB_HOTKEY_NONE_LABEL
            ).strip() or TAB_HOTKEY_NONE_LABEL

        for entry_index, entry in enumerate(entries):
            workspace_id = str(
                entry.get("workspace_id") or uuid.uuid4().hex
            )
            text_file_name = str(
                entry.get("text_file") or f"{workspace_id}.txt"
            )
            text_path = WORKSPACE_TEXT_DIR / Path(text_file_name).name

            try:
                restored_text = (
                    text_path.read_text(encoding="utf-8")
                    if text_path.exists()
                    else ""
                )
            except Exception as exc:
                restored_text = ""
                self.logger.error(
                    task_id="app",
                    stage="restore_workspace_text",
                    exc=exc,
                    traceback_text=traceback.format_exc(),
                    context={"text_path": str(text_path)},
                )

            restored_settings = {
                "voice": entry.get("voice", self.settings["voice"]),
                # Speed and Pitch are global now. Old per-tab values are
                # intentionally ignored during migration.
                "rate": self.settings["rate"],
                "pitch": self.settings["pitch"],
                "audio_output": self.settings["audio_output"],
                "volume": entry.get("volume", self.settings["volume"]),
                "bitrate": entry.get("bitrate", self.settings["bitrate"]),
                "delete_read_text": bool(
                    entry.get("delete_read_text", False)
                ),
                "auto_read_hotkey_text": bool(
                    entry.get("auto_read_hotkey_text", True)
                ),
                "copy_hotkey": (
                    entry.get(
                        "copy_hotkey",
                        TAB_HOTKEY_NONE_LABEL,
                    )
                    if has_per_tab_hotkeys
                    else (
                        legacy_hotkey
                        if entry_index == selected_index
                        else TAB_HOTKEY_NONE_LABEL
                    )
                ),
            }

            raw_title = str(entry.get("title") or "")
            stored_custom_title = entry.get("custom_title")
            if isinstance(stored_custom_title, bool):
                custom_title = stored_custom_title
            else:
                # Migration from 2.2: "Задача 37", "Вкладка 18", etc. were
                # automatic names and should be renumbered, not preserved.
                custom_title = not is_automatic_tab_title(raw_title)

            self.new_tab(
                title=raw_title,
                workspace_id=workspace_id,
                restored_text=restored_text,
                restored_settings=restored_settings,
                restored_preview_bookmark=(
                    entry.get("preview_bookmark")
                    if isinstance(entry.get("preview_bookmark"), dict)
                    else None
                ),
                custom_title=custom_title,
                save_workspace=False,
            )

        if not self.tabs:
            self.new_tab(save_workspace=False)

        # Normalize all non-custom tabs after restoration. This also fixes old
        # titles such as "Задача 37" / "Задача 38".
        self.renumber_default_tabs()

        try:
            if self.notebook.tabs():
                self.notebook.select(selected_index)
        except tk.TclError:
            pass

        self.settings["global_copy_enabled"] = False
        self.schedule_workspace_save()
