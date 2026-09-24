from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskUiBuildMixin:
    def __init__(
        self,
        app: "TTSApp",
        number: int,
        *,
        workspace_id: str | None = None,
        restored_title: str | None = None,
        restored_text: str = "",
        restored_settings: dict | None = None,
        restored_preview_bookmark: dict | None = None,
        custom_title: bool = False,
    ) -> None:
        self.app = app
        self.number = number
        self.workspace_id = workspace_id or uuid.uuid4().hex
        self.task_id = f"{number:02d}_{uuid.uuid4().hex[:6]}"

        tab_settings = dict(app.settings)
        if isinstance(restored_settings, dict):
            tab_settings.update(restored_settings)

        self.frame = ttk.Frame(app.notebook, padding=8)
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.preview_thread: threading.Thread | None = None
        self.preview_stop_event = threading.Event()
        self.preview_pause_event = threading.Event()
        self.preview_is_paused = False
        self.current_preview_segment_text = ""
        self.current_preview_run_id: str | None = None
        self.current_preview_base_index = "1.0"
        self.current_preview_base_text_offset = 0
        self.current_preview_source_mode = ""
        self.current_preview_segment_start = 0
        self.current_preview_segment_end = 0
        self.current_preview_range_end = 0
        self.current_preview_snapshot_end = 0
        self.current_preview_follow_end = False
        self.current_preview_segment_index = 0
        self.current_preview_segments_total = 0
        self.current_preview_word_length = 0
        self.preview_keep_bookmark_after_stop = False
        self.preview_resume_previous_pending = False

        mark_suffix = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            self.workspace_id[:16],
        )
        self.preview_base_mark = f"_preview_base_{mark_suffix}"
        self.preview_snapshot_end_mark = f"_preview_snapshot_end_{mark_suffix}"
        self.preview_pending_delete_start_mark = f"_preview_delete_start_{mark_suffix}"
        self.preview_pending_delete_end_mark = f"_preview_delete_end_{mark_suffix}"
        self.preview_deleted_chars = 0
        self.preview_pending_delete_active = False

        # A running MP3 task owns one immutable source snapshot. Marks keep its
        # exact widget range stable while hotkey text is appended after it.
        self.mp3_source_start_mark = f"_mp3_source_start_{mark_suffix}"
        self.mp3_source_end_mark = f"_mp3_source_end_{mark_suffix}"
        self.mp3_source_run_id: str | None = None
        self.mp3_source_chars = 0
        self.mp3_source_sha256 = ""
        self.mp3_source_separator_pending = False

        # Compact read/delete diagnostics and per-tab queue counters.
        self._delete_log_segments = 0
        self._delete_log_chars = 0
        self._delete_log_last_flush_perf = time.perf_counter()
        self._text_state_last_log_perf = 0.0
        self.session_captured_chars = 0
        self.session_read_segments = 0
        self.session_read_chars = 0
        self.session_deleted_segments = 0
        self.session_deleted_chars = 0
        self.queue_status_after_id: str | None = None

        # Preview finish cause is logged separately from SAPI's stopped=True/False.
        self.current_preview_finish_reason: str | None = None
        self.preview_resume_from_run_id: str | None = None

        self.preview_bookmark: dict | None = (
            dict(restored_preview_bookmark)
            if isinstance(restored_preview_bookmark, dict)
            else None
        )
        self.current_output_path: Path | None = None
        self.current_mp3_text_backup_path: Path | None = None
        self.current_run_id: str | None = None
        self.current_job_stage = "idle"

        self.voice_var = tk.StringVar(value=str(tab_settings.get("voice") or DEFAULT_VOICE_HINT))

        # Speed and Pitch are intentionally ONE shared Tk variable each for the
        # whole application. Every tab's sliders point to these same variables,
        # so changing either value in one tab instantly updates every other tab.
        self.rate_var = app.global_rate_var
        self.pitch_var = app.global_pitch_var
        self.audio_output_var = app.global_audio_output_var

        self.volume_var = tk.IntVar(value=clamp_int(tab_settings.get("volume"), 0, 100, 100))
        self.bitrate_var = tk.StringVar(value=str(tab_settings.get("bitrate") or "96k"))
        self.delete_read_text_var = tk.BooleanVar(
            value=bool(tab_settings.get("delete_read_text", False))
        )
        self.auto_read_hotkey_var = tk.BooleanVar(
            value=bool(tab_settings.get("auto_read_hotkey_text", True))
        )

        # Hotkey belongs to THIS tab. It is intentionally not inherited from
        # application settings: every newly created tab starts with "Нет".
        restored_copy_hotkey = str(
            tab_settings.get("copy_hotkey")
            or TAB_HOTKEY_NONE_LABEL
        ).strip() or TAB_HOTKEY_NONE_LABEL
        self.copy_hotkey_var = tk.StringVar(
            value=restored_copy_hotkey
        )
        self.copy_hotkey_status_var = tk.StringVar(
            value="Горячая клавиша не назначена."
        )
        self.applied_copy_hotkey = TAB_HOTKEY_NONE_LABEL

        self.status_var = tk.StringVar(value="Готово к работе.")
        self.queue_status_var = tk.StringVar(value="Очередь: считаю…")
        self.rate_value_var = tk.StringVar(value=str(self.rate_var.get()))
        self.pitch_value_var = tk.StringVar(value=str(self.pitch_var.get()))

        self.workspace_dirty = False
        self.custom_title = bool(custom_title)
        self.restored_title = restored_title or f"Вкладка {number}"

        self._build_ui()

        if restored_text:
            self.text_box.insert("1.0", restored_text)

        self._restore_preview_bookmark_ui()
        self.text_box.edit_modified(False)
        self.text_box.bind("<<Modified>>", self._on_text_modified, add="+")
        self.schedule_queue_status_refresh()

        # Speed and Pitch have one application-level trace each. Do not
        # attach one trace per tab to shared variables, otherwise a single slider
        # movement would cause duplicate callbacks from every open tab.
        for variable in (
            self.voice_var,
            self.volume_var,
            self.bitrate_var,
            self.delete_read_text_var,
            self.auto_read_hotkey_var,
        ):
            variable.trace_add("write", self._settings_changed)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x")

        ttk.Button(
            toolbar,
            text="Вставить текст",
            command=self.paste_text_from_clipboard,
        ).pack(side="left")

        ttk.Button(
            toolbar,
            text="Очистить",
            command=self.clear_text,
        ).pack(side="left", padx=(6, 0))

        self.start_button = ttk.Button(
            toolbar,
            text="Создать MP3",
            command=self.start_job,
        )
        self.start_button.pack(side="left", padx=(18, 0))

        self.cancel_button = ttk.Button(
            toolbar,
            text="Остановить создание MP3",
            command=self.cancel_job,
            state="disabled",
        )
        self.cancel_button.pack(side="left", padx=(6, 0))

        self.preview_button = ttk.Button(
            toolbar,
            text="▶ Воспроизвести текст",
            command=self.start_preview,
        )
        self.preview_button.pack(side="left", padx=(18, 0))

        self.preview_cursor_button = ttk.Button(
            toolbar,
            text="▶ Читать с курсора",
            command=self.start_preview_from_cursor,
        )
        self.preview_cursor_button.pack(side="left", padx=(6, 0))

        self.pause_preview_button = ttk.Button(
            toolbar,
            text="⏸ Пауза",
            command=self.pause_preview,
            state="disabled",
        )
        self.pause_preview_button.pack(side="left", padx=(10, 0))

        self.resume_preview_button = ttk.Button(
            toolbar,
            text="▶ Продолжить",
            command=self.resume_preview,
            state="disabled",
        )
        self.resume_preview_button.pack(side="left", padx=(6, 0))

        self.stop_preview_button = ttk.Button(
            toolbar,
            text="■ Остановить",
            command=self.stop_preview,
            state="disabled",
        )
        self.stop_preview_button.pack(side="left", padx=(6, 0))

        capture_bar = ttk.Frame(self.frame)
        capture_bar.pack(fill="x", pady=(6, 0))

        ttk.Label(
            capture_bar,
            text="Горячая клавиша чтения:",
        ).pack(side="left")

        self.copy_hotkey_combo = ttk.Combobox(
            capture_bar,
            textvariable=self.copy_hotkey_var,
            values=(
                TAB_HOTKEY_NONE_LABEL,
                *GlobalCopyHotkeyMonitor.PRESET_HOTKEYS,
            ),
            state="normal",
            width=16,
        )
        self.copy_hotkey_combo.pack(
            side="left",
            padx=(6, 0),
        )
        self.copy_hotkey_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.apply_copy_hotkey(),
        )
        self.copy_hotkey_combo.bind(
            "<FocusOut>",
            lambda _event: self.apply_copy_hotkey(
                silent=True
            ),
        )
        self.copy_hotkey_combo.bind(
            "<Return>",
            lambda _event: self.apply_copy_hotkey(),
        )

        ttk.Label(
            capture_bar,
            textvariable=self.copy_hotkey_status_var,
        ).pack(side="left", padx=(10, 0))

        ttk.Checkbutton(
            capture_bar,
            text="Удалять прочитанные предложения",
            variable=self.delete_read_text_var,
        ).pack(side="left", padx=(18, 0))

        ttk.Checkbutton(
            capture_bar,
            text="Сразу читать добавленный текст",
            variable=self.auto_read_hotkey_var,
        ).pack(side="left", padx=(18, 0))

        ttk.Label(
            capture_bar,
            textvariable=self.queue_status_var,
            anchor="e",
        ).pack(side="right", padx=(16, 0))

        ttk.Label(
            self.frame,
            text="Текст для озвучки:",
        ).pack(anchor="w", pady=(10, 4))

        text_frame = ttk.Frame(self.frame)
        text_frame.pack(fill="both", expand=True)

        self.text_box = tk.Text(
            text_frame,
            wrap="word",
            undo=True,
            font=("Segoe UI", 10),
        )

        scrollbar = ttk.Scrollbar(
            text_frame,
            orient="vertical",
            command=self.text_box.yview,
        )
        self.text_box.configure(yscrollcommand=scrollbar.set)

        # Отдельный тег текущего читаемого предложения. Он не меняет текст,
        # не мешает обычному выделению мышью и снимается после остановки.
        self.text_box.tag_configure(
            "preview_current",
            background="#fff2a8",
            relief="solid",
            borderwidth=1,
        )

        # Instance-level binding runs before Tk's Text class binding.
        # This makes Ctrl+C/V/X/A/Z/Y work by the same physical keys under
        # English and Russian keyboard layouts without double copy/paste.
        self.text_box.bind(
            "<Control-KeyPress>",
            lambda event: handle_text_ctrl_shortcut(
                self.text_box,
                event,
            ),
            add="+",
        )

        self.text_box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        voice_group = ttk.LabelFrame(
            self.frame,
            text="Параметры голоса",
            padding=10,
        )
        voice_group.pack(fill="x", pady=(10, 0))

        voice_group.columnconfigure(1, weight=1)
        voice_group.columnconfigure(4, weight=1)

        ttk.Label(voice_group, text="Голос:").grid(
            row=0,
            column=0,
            sticky="w",
        )

        self.voice_combo = ttk.Combobox(
            voice_group,
            textvariable=self.voice_var,
            state="readonly",
        )
        self.voice_combo.grid(
            row=0,
            column=1,
            columnspan=5,
            sticky="ew",
            padx=(8, 0),
        )
        self.voice_combo["values"] = self.app.voices

        ttk.Label(voice_group, text="Скорость:").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(9, 0),
        )

        self.rate_scale = tk.Scale(
            voice_group,
            from_=RATE_MIN,
            to=RATE_MAX,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            variable=self.rate_var,
            length=300,
        )
        self.rate_scale.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(8, 6),
            pady=(3, 0),
        )

        ttk.Spinbox(
            voice_group,
            from_=RATE_MIN,
            to=RATE_MAX,
            textvariable=self.rate_var,
            width=6,
        ).grid(
            row=1,
            column=2,
            sticky="w",
            pady=(9, 0),
        )

        ttk.Label(voice_group, text="Pitch:").grid(
            row=1,
            column=3,
            sticky="w",
            padx=(22, 0),
            pady=(9, 0),
        )

        self.pitch_scale = tk.Scale(
            voice_group,
            from_=PITCH_MIN,
            to=PITCH_MAX,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            variable=self.pitch_var,
            length=300,
        )
        self.pitch_scale.grid(
            row=1,
            column=4,
            sticky="ew",
            padx=(8, 6),
            pady=(3, 0),
        )

        ttk.Spinbox(
            voice_group,
            from_=PITCH_MIN,
            to=PITCH_MAX,
            textvariable=self.pitch_var,
            width=6,
        ).grid(
            row=1,
            column=5,
            sticky="w",
            pady=(9, 0),
        )

        ttk.Label(voice_group, text="Громкость:").grid(
            row=2,
            column=0,
            sticky="w",
            pady=(8, 0),
        )

        ttk.Spinbox(
            voice_group,
            from_=0,
            to=100,
            textvariable=self.volume_var,
            width=7,
        ).grid(
            row=2,
            column=1,
            sticky="w",
            padx=(8, 0),
            pady=(8, 0),
        )

        ttk.Label(voice_group, text="Качество MP3:").grid(
            row=2,
            column=3,
            sticky="w",
            padx=(22, 0),
            pady=(8, 0),
        )

        ttk.Combobox(
            voice_group,
            textvariable=self.bitrate_var,
            values=("64k", "96k", "128k", "160k", "192k"),
            state="readonly",
            width=9,
        ).grid(
            row=2,
            column=4,
            sticky="w",
            padx=(8, 0),
            pady=(8, 0),
        )

        ttk.Label(
            voice_group,
            text="Устройство воспроизведения:",
        ).grid(
            row=3,
            column=0,
            sticky="w",
            pady=(8, 0),
        )

        self.audio_output_combo = ttk.Combobox(
            voice_group,
            textvariable=self.audio_output_var,
            values=self.app.audio_output_choices,
            state="readonly",
        )
        self.audio_output_combo.grid(
            row=3,
            column=1,
            columnspan=4,
            sticky="ew",
            padx=(8, 0),
            pady=(8, 0),
        )

        ttk.Button(
            voice_group,
            text="↻ Обновить",
            command=self.app.refresh_audio_outputs,
        ).grid(
            row=3,
            column=5,
            sticky="e",
            padx=(8, 0),
            pady=(8, 0),
        )

        progress_group = ttk.Frame(self.frame)
        progress_group.pack(fill="x", pady=(10, 0))

        self.progress = ttk.Progressbar(
            progress_group,
            maximum=100,
            mode="determinate",
        )
        self.progress.pack(fill="x")

        ttk.Label(
            progress_group,
            textvariable=self.status_var,
        ).pack(anchor="w", pady=(5, 0))
