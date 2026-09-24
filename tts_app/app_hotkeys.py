from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *

class AppHotkeysMixin:
    def _normalized_tab_hotkey(
        self,
        value: str,
    ) -> str:
        raw = str(value or "").strip()
        if (
            not raw
            or raw.casefold()
            == TAB_HOTKEY_NONE_LABEL.casefold()
        ):
            return TAB_HOTKEY_NONE_LABEL

        parsed = GlobalCopyHotkeyMonitor.parse_hotkey(
            raw
        )
        return str(parsed["text"])

    def _task_hotkey_owner(
        self,
        normalized_hotkey: str,
        *,
        exclude_workspace_id: str | None = None,
    ) -> TaskTab | None:
        wanted = str(normalized_hotkey or "").strip()
        if (
            not wanted
            or wanted == TAB_HOTKEY_NONE_LABEL
        ):
            return None

        for task in self.tabs.values():
            if (
                exclude_workspace_id
                and task.workspace_id
                == exclude_workspace_id
            ):
                continue
            if task.applied_copy_hotkey == wanted:
                return task
        return None

    def _stop_tab_hotkey_monitor(
        self,
        workspace_id: str,
    ) -> None:
        monitor = self.tab_hotkey_monitors.pop(
            workspace_id,
            None,
        )
        if monitor is not None:
            try:
                monitor.stop()
            except Exception:
                pass

    def apply_tab_copy_hotkey(
        self,
        task: TaskTab,
        *,
        silent: bool = False,
        save_workspace: bool = True,
    ) -> bool:
        """
        Bind one unique global hotkey to one exact tab.

        Routing never depends on which tab is currently visible.  "Нет" removes
        the binding. Aliases such as Й+Ц and Q+W normalize to the same physical
        keys, so they are also treated as duplicates.
        """
        previous = (
            task.applied_copy_hotkey
            or TAB_HOTKEY_NONE_LABEL
        )

        try:
            normalized = self._normalized_tab_hotkey(
                task.copy_hotkey_var.get()
            )
        except Exception as exc:
            task.copy_hotkey_var.set(previous)
            task.copy_hotkey_status_var.set(
                f"Ошибка: {exc}"
            )
            self.logger.event(
                "tab_copy_hotkey_invalid",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=task.copy_hotkey_var.get(),
                error=f"{type(exc).__name__}: {exc}",
            )
            if not silent:
                messagebox.showerror(
                    "Горячая клавиша",
                    str(exc),
                )
            return False

        if normalized == TAB_HOTKEY_NONE_LABEL:
            self._stop_tab_hotkey_monitor(
                task.workspace_id
            )
            task.applied_copy_hotkey = (
                TAB_HOTKEY_NONE_LABEL
            )
            task.copy_hotkey_var.set(
                TAB_HOTKEY_NONE_LABEL
            )
            task.copy_hotkey_status_var.set(
                "Горячая клавиша не назначена."
            )
            self.logger.event(
                "tab_copy_hotkey_cleared",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                tab_title=task.tab_title(),
            )
            if save_workspace:
                self.schedule_workspace_save()
            return True

        owner = self._task_hotkey_owner(
            normalized,
            exclude_workspace_id=task.workspace_id,
        )
        if owner is not None:
            task.copy_hotkey_var.set(previous)
            message = (
                f'Горячая клавиша {normalized} уже назначена '
                f'вкладке «{owner.tab_title()}». '
                "Выберите другую клавишу."
            )
            task.copy_hotkey_status_var.set(
                f"{normalized} уже занята другой вкладкой."
            )
            self.logger.event(
                "tab_copy_hotkey_conflict",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=normalized,
                owner_task_id=owner.task_id,
                owner_tab_id=owner.workspace_id,
                owner_tab_title=owner.tab_title(),
            )
            if not silent:
                messagebox.showwarning(
                    "Горячая клавиша уже используется",
                    message,
                )
            return False

        # Nothing to rebuild when the same binding is simply re-applied.
        existing_monitor = self.tab_hotkey_monitors.get(
            task.workspace_id
        )
        if (
            previous == normalized
            and existing_monitor is not None
            and existing_monitor.enabled
        ):
            task.copy_hotkey_var.set(normalized)
            task.copy_hotkey_status_var.set(
                f"{normalized} → только эта вкладка"
            )
            return True

        # Validate first, then replace the old monitor.
        old_monitor = existing_monitor
        self._stop_tab_hotkey_monitor(
            task.workspace_id
        )

        try:
            monitor = GlobalCopyHotkeyMonitor(
                self.root,
                lambda capture_info, workspace_id=task.workspace_id:
                    self._on_tab_copy_hotkey(
                        workspace_id,
                        capture_info,
                    ),
                hotkey=normalized,
            )
            monitor.enable()
            self.tab_hotkey_monitors[
                task.workspace_id
            ] = monitor

        except Exception as exc:
            # Best effort: restore the previous valid assignment.
            if (
                old_monitor is not None
                and previous
                != TAB_HOTKEY_NONE_LABEL
            ):
                try:
                    restored_monitor = (
                        GlobalCopyHotkeyMonitor(
                            self.root,
                            lambda capture_info, workspace_id=task.workspace_id:
                                self._on_tab_copy_hotkey(
                                    workspace_id,
                                    capture_info,
                                ),
                            hotkey=previous,
                        )
                    )
                    restored_monitor.enable()
                    self.tab_hotkey_monitors[
                        task.workspace_id
                    ] = restored_monitor
                except Exception:
                    previous = TAB_HOTKEY_NONE_LABEL

            task.applied_copy_hotkey = previous
            task.copy_hotkey_var.set(previous)
            task.copy_hotkey_status_var.set(
                f"Не удалось включить {normalized}."
            )
            self.logger.event(
                "tab_copy_hotkey_enable_failed",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=normalized,
                error=f"{type(exc).__name__}: {exc}",
            )
            if not silent:
                messagebox.showerror(
                    "Горячая клавиша",
                    "Не удалось включить горячую клавишу.\n\n"
                    f"{exc}",
                )
            return False

        task.applied_copy_hotkey = normalized
        task.copy_hotkey_var.set(normalized)
        task.copy_hotkey_status_var.set(
            f"{normalized} → только эта вкладка"
        )

        self.logger.event(
            "tab_copy_hotkey_applied",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            tab_title=task.tab_title(),
            hotkey=normalized,
        )

        if save_workspace:
            self.schedule_workspace_save()
        return True

    def _activate_restored_tab_hotkeys(self) -> None:
        """
        Restore persisted per-tab bindings in visible tab order.

        If an old/corrupt workspace somehow contains duplicate bindings, the
        first tab keeps the key and later conflicting tabs are reset to "Нет".
        """
        for widget_name in self.notebook.tabs():
            task = None
            for candidate in self.tabs.values():
                if str(candidate.frame) == widget_name:
                    task = candidate
                    break
            if task is None:
                continue

            requested = str(
                task.copy_hotkey_var.get()
                or TAB_HOTKEY_NONE_LABEL
            )
            if not self.apply_tab_copy_hotkey(
                task,
                silent=True,
                save_workspace=False,
            ):
                task.applied_copy_hotkey = (
                    TAB_HOTKEY_NONE_LABEL
                )
                task.copy_hotkey_var.set(
                    TAB_HOTKEY_NONE_LABEL
                )
                task.copy_hotkey_status_var.set(
                    "Горячая клавиша не назначена."
                )
                self.logger.event(
                    "restored_tab_hotkey_reset",
                    task_id=task.task_id,
                    tab_id=task.workspace_id,
                    requested_hotkey=requested,
                    reason="invalid_or_duplicate",
                )

        self.schedule_workspace_save()

    def _on_tab_copy_hotkey(
        self,
        workspace_id: str,
        capture_info: dict | None = None,
    ) -> None:
        """
        A hotkey is permanently routed to its owning tab, regardless of which
        tab is currently selected on screen.
        """
        if self.shutdown_in_progress:
            return

        task = self._task_by_workspace_id(
            workspace_id
        )
        monitor = self.tab_hotkey_monitors.get(
            workspace_id
        )
        if (
            task is None
            or monitor is None
            or not monitor.enabled
        ):
            return

        hotkey = monitor.get_hotkey()
        info = dict(capture_info or {})
        source_process = str(
            info.get("source_process") or ""
        )
        source_pid = info.get("source_pid")
        copy_latency_ms = float(
            info.get("copy_latency_ms") or 0
        )

        if info.get("clipboard_changed") is False:
            self.logger.event(
                "global_copy_clipboard_timeout",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                hotkey=hotkey,
                source_process=source_process,
                source_pid=source_pid,
                copy_latency_ms=copy_latency_ms,
                timeout_sec=CLIPBOARD_COPY_TIMEOUT_SEC,
            )
            task.status_var.set(
                "Не удалось дождаться нового текста в буфере обмена."
            )
            return

        try:
            captured = self.root.clipboard_get()
        except tk.TclError as exc:
            self.logger.event(
                "global_copy_clipboard_read_failed",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                hotkey=hotkey,
                source_process=source_process,
                source_pid=source_pid,
                copy_latency_ms=copy_latency_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        if not isinstance(captured, str):
            return

        raw_captured = captured.strip()
        if not raw_captured:
            return

        captured = sanitize_hotkey_capture_text(
            raw_captured
        )
        if not captured:
            self.logger.event(
                "global_copy_filtered_empty",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                hotkey=hotkey,
                raw_chars=len(raw_captured),
                source_process=source_process,
                source_pid=source_pid,
                copy_latency_ms=copy_latency_ms,
            )
            task.status_var.set(
                "В выделении нет русских букв, цифр, точек или запятых."
            )
            return

        removed_chars = max(
            0,
            len(raw_captured) - len(captured),
        )

        captured_hash = hashlib.sha256(
            captured.encode("utf-8")
        ).hexdigest()[:16]

        if monitor.remember_text_and_check_duplicate(
            captured
        ):
            self.logger.event(
                "global_copy_duplicate_skipped",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                hotkey=hotkey,
                chars=len(captured),
                raw_chars=len(raw_captured),
                filtered_chars=len(captured),
                removed_chars=removed_chars,
                captured_sha256_16=captured_hash,
                source_process=source_process,
                source_pid=source_pid,
                copy_latency_ms=copy_latency_ms,
            )
            return

        self.logger.event(
            "global_copy_text_captured",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            tab_title=task.tab_title(),
            hotkey=hotkey,
            routing_mode="fixed_tab",
            chars=len(captured),
            raw_chars=len(raw_captured),
            filtered_chars=len(captured),
            removed_chars=removed_chars,
            captured_sha256_16=captured_hash,
            source_process=source_process,
            source_pid=source_pid,
            copy_latency_ms=copy_latency_ms,
            clipboard_changed=info.get(
                "clipboard_changed"
            ),
            preview_running=bool(
                task.preview_thread
                and task.preview_thread.is_alive()
            ),
            paused=bool(task.preview_is_paused),
            auto_read_hotkey_text=bool(
                task.auto_read_hotkey_var.get()
            ),
        )

        # Critical routing rule: DO NOT use current_tab() here.
        task.append_external_selected_text(
            captured
        )
