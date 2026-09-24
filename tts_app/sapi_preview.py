from __future__ import annotations

from .runtime import *
from .text_processing import *

from .sapi_core import *

def preview_sapi_text(
    text: str,
    voice_description: str,
    audio_output_description: str,
    rate: int,
    pitch: int,
    volume: int,
    stop_event: threading.Event,
    pause_event: threading.Event,
    progress_callback=None,
    state_callback=None,
    heartbeat_callback=None,
    checkpoint_callback=None,
    segment_done_callback=None,
) -> dict:
    """
    Read text through one persistent SpVoice.

    Version 3.8 called Speak() once PER SENTENCE.  On some hardware endpoints
    (especially Bluetooth) that can sound like the loudness envelope is being
    restarted over and over.  Version 3.9 groups consecutive sentences into
    larger continuous SAPI streams and polls SpVoice.Status to preserve:
      - current-sentence highlighting;
      - pause/bookmark position;
      - delete-after-read callbacks;
      - Stop/Resume behavior.
    """
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    pythoncom.CoInitialize()
    started = time.perf_counter()
    stopped = False
    completed_chunks = 0
    actual_audio_output = ""
    sapi_paused = False
    last_heartbeat = started

    segments = build_preview_segments(text)

    def build_blocks() -> list[dict]:
        blocks: list[dict] = []
        current_parts: list[str] = []
        current_segments: list[dict] = []
        current_chars = 0

        def flush() -> None:
            nonlocal current_parts, current_segments, current_chars
            if not current_segments:
                return
            blocks.append(
                {
                    "text": "".join(current_parts),
                    "segments": current_segments,
                }
            )
            current_parts = []
            current_segments = []
            current_chars = 0

        for global_index, segment in enumerate(segments, start=1):
            piece = str(segment.get("text") or "")
            separator = "" if not current_segments else " "

            if (
                current_segments
                and current_chars + len(separator) + len(piece)
                > PREVIEW_SAPI_BLOCK_MAX_CHARS
            ):
                flush()
                separator = ""

            spoken_start = current_chars + len(separator)
            current_parts.append(separator)
            current_parts.append(piece)
            current_chars = spoken_start + len(piece)

            current_segments.append(
                {
                    "global_index": global_index,
                    "spoken_start": spoken_start,
                    "spoken_end": current_chars,
                    "segment": segment,
                }
            )

        flush()
        return blocks

    blocks = build_blocks()

    def make_position_mapper(
        plain_text: str,
        speak_text: str,
        flags: int,
    ):
        if not (flags & SVSF_IS_XML):
            def direct(raw_position) -> int:
                try:
                    value = int(raw_position)
                except Exception:
                    return 0
                return max(0, min(len(plain_text), value))
            return direct

        escaped = html.escape(plain_text, quote=False)
        xml_text_start = speak_text.find(escaped)
        if xml_text_start < 0:
            def fallback(raw_position) -> int:
                try:
                    value = int(raw_position)
                except Exception:
                    return 0
                return max(0, min(len(plain_text), value))
            return fallback

        # escaped_boundaries[i] == offset in escaped XML text after i source chars.
        escaped_boundaries = [0]
        escaped_offset = 0
        for char in plain_text:
            escaped_offset += len(html.escape(char, quote=False))
            escaped_boundaries.append(escaped_offset)

        def mapped(raw_position) -> int:
            try:
                raw = int(raw_position)
            except Exception:
                return 0

            relative = raw - xml_text_start
            if relative <= 0:
                return 0
            if relative >= escaped_boundaries[-1]:
                return len(plain_text)

            source_offset = (
                bisect.bisect_right(
                    escaped_boundaries,
                    relative,
                )
                - 1
            )
            return max(
                0,
                min(len(plain_text), source_offset),
            )

        return mapped

    def locate_segment(block: dict, plain_position: int) -> dict:
        metas = block["segments"]
        if not metas:
            raise RuntimeError("Пустой preview-блок.")

        position = max(0, int(plain_position))
        for meta in metas:
            if position < int(meta["spoken_end"]):
                return meta
        return metas[-1]

    def maybe_heartbeat(
        segment_index: int,
        segments_total: int,
    ) -> None:
        nonlocal last_heartbeat
        if heartbeat_callback is None:
            return
        now_perf = time.perf_counter()
        if (
            now_perf - last_heartbeat
            < PREVIEW_HEARTBEAT_INTERVAL_SEC
        ):
            return
        heartbeat_callback(
            {
                "segment": segment_index,
                "segments_total": segments_total,
                "elapsed_sec": round(
                    now_perf - started,
                    3,
                ),
                "paused": bool(
                    pause_event.is_set()
                    or sapi_paused
                ),
            }
        )
        last_heartbeat = now_perf

    def emit_checkpoint(
        meta: dict,
        *,
        reason: str,
        word_position: int = 0,
        word_length: int = 0,
        sapi_position_raw: int | None = None,
    ) -> None:
        if checkpoint_callback is None:
            return

        segment = meta["segment"]
        checkpoint_callback(
            {
                "segment": int(meta["global_index"]),
                "segments_total": len(segments),
                "start": int(segment.get("start") or 0),
                "end": int(segment.get("end") or 0),
                "word_position": max(0, int(word_position)),
                "word_length": max(0, int(word_length)),
                "sapi_position_raw": sapi_position_raw,
                "reason": reason,
            }
        )

    def emit_progress(meta: dict) -> None:
        if progress_callback is None:
            return
        segment = meta["segment"]
        progress_callback(
            {
                "index": int(meta["global_index"]),
                "total": len(segments),
                "start": int(segment["start"]),
                "end": int(segment["end"]),
                "text": str(segment["text"]),
            }
        )

    def emit_segment_done(meta: dict) -> None:
        nonlocal completed_chunks
        completed_chunks += 1
        if segment_done_callback is None:
            return
        segment = meta["segment"]
        segment_done_callback(
            {
                "index": int(meta["global_index"]),
                "total": len(segments),
                "start": int(segment["start"]),
                "end": int(segment["end"]),
                "text": str(segment["text"]),
            }
        )

    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        set_voice_by_description(voice, voice_description)
        actual_audio_output = set_audio_output_by_description(
            voice,
            audio_output_description,
        )

        native_rate, rate_overflow = split_extended_sapi_rate(rate)
        voice.Rate = int(native_rate)
        voice.Volume = int(volume)

        if not segments:
            return {
                "requested_audio_output": audio_output_description,
                "actual_audio_output": actual_audio_output,
                "chunks": 0,
                "completed_chunks": 0,
                "sapi_stream_blocks": 0,
                "stopped": False,
                "paused_at_finish": bool(pause_event.is_set()),
                "duration_sec": round(
                    time.perf_counter() - started,
                    3,
                ),
            }

        for block in blocks:
            if stop_event.is_set():
                stopped = True
                break

            first_meta = block["segments"][0]

            # Pause exactly between large SAPI streams.
            if pause_event.is_set() and not stop_event.is_set():
                emit_checkpoint(
                    first_meta,
                    reason="pause_between_stream_blocks",
                )
                if state_callback is not None:
                    state_callback("paused")
                while (
                    pause_event.is_set()
                    and not stop_event.is_set()
                ):
                    maybe_heartbeat(
                        int(first_meta["global_index"]),
                        len(segments),
                    )
                    time.sleep(0.05)

            if stop_event.is_set():
                stopped = True
                break

            if state_callback is not None:
                state_callback("running")

            block_text = str(block["text"])
            speak_text, flags = build_sapi_text(
                block_text,
                pitch,
                rate_overflow,
            )
            map_position = make_position_mapper(
                block_text,
                speak_text,
                flags,
            )

            # Reassert the requested level before every large stream.  The voice
            # object itself remains the same for the whole reading session.
            voice.Volume = int(volume)

            last_active_global = None
            done_local_count = 0
            active_meta = first_meta
            emit_progress(active_meta)
            last_active_global = int(active_meta["global_index"])

            voice.Speak(
                speak_text,
                flags | SVS_FLAGS_ASYNC,
            )

            while True:
                raw_word_position = None
                raw_sentence_position = None
                word_length = 0

                try:
                    status = voice.Status
                    raw_word_position = int(
                        getattr(
                            status,
                            "InputWordPosition",
                            0,
                        )
                        or 0
                    )
                    raw_sentence_position = int(
                        getattr(
                            status,
                            "InputSentencePosition",
                            raw_word_position,
                        )
                        or raw_word_position
                    )
                    word_length = max(
                        0,
                        int(
                            getattr(
                                status,
                                "InputWordLength",
                                0,
                            )
                            or 0
                        ),
                    )

                    plain_word_position = map_position(
                        raw_word_position
                    )
                    plain_sentence_position = map_position(
                        raw_sentence_position
                    )

                    # Word position normally gives the best live location.
                    # Sentence position is a safe fallback at boundaries.
                    live_plain_position = plain_word_position
                    if (
                        live_plain_position <= 0
                        and plain_sentence_position > 0
                    ):
                        live_plain_position = (
                            plain_sentence_position
                        )

                    active_meta = locate_segment(
                        block,
                        live_plain_position,
                    )
                    active_global = int(
                        active_meta["global_index"]
                    )

                    # Entering sentence N means every earlier sentence in this
                    # stream has finished and can be deleted/logged safely.
                    active_local_index = block["segments"].index(
                        active_meta
                    )
                    while done_local_count < active_local_index:
                        emit_segment_done(
                            block["segments"][
                                done_local_count
                            ]
                        )
                        done_local_count += 1

                    if active_global != last_active_global:
                        emit_progress(active_meta)
                        last_active_global = active_global

                except Exception:
                    # Status polling is diagnostic/UI assistance.  A temporary
                    # status failure must never stop actual speech.
                    pass

                if stop_event.is_set():
                    stopped = True
                    try:
                        if sapi_paused:
                            voice.Resume()
                            sapi_paused = False
                        voice.Speak(
                            "",
                            SVSFPURGE_BEFORE_SPEAK
                            | SVS_FLAGS_ASYNC,
                        )
                    except Exception:
                        pass
                    break

                if pause_event.is_set() and not sapi_paused:
                    local_word_position = 0

                    try:
                        # Refresh once immediately before Pause so the persisted
                        # bookmark is as close as possible to the audible word.
                        status = voice.Status
                        raw_word_position = int(
                            getattr(
                                status,
                                "InputWordPosition",
                                0,
                            )
                            or 0
                        )
                        word_length = max(
                            0,
                            int(
                                getattr(
                                    status,
                                    "InputWordLength",
                                    0,
                                )
                                or 0
                            ),
                        )
                        plain_word_position = map_position(
                            raw_word_position
                        )
                        active_meta = locate_segment(
                            block,
                            plain_word_position,
                        )
                        local_word_position = max(
                            0,
                            plain_word_position
                            - int(
                                active_meta[
                                    "spoken_start"
                                ]
                            ),
                        )

                        segment_text = str(
                            active_meta["segment"].get(
                                "text"
                            )
                            or ""
                        )
                        local_word_position = min(
                            len(segment_text),
                            local_word_position,
                        )
                        if word_length > len(segment_text):
                            word_length = 0
                    except Exception:
                        raw_word_position = None
                        word_length = 0
                        local_word_position = 0

                    voice.Pause()
                    sapi_paused = True

                    emit_checkpoint(
                        active_meta,
                        reason="pause_inside_stream_block",
                        word_position=local_word_position,
                        word_length=word_length,
                        sapi_position_raw=raw_word_position,
                    )
                    if state_callback is not None:
                        state_callback("paused")

                elif not pause_event.is_set() and sapi_paused:
                    voice.Resume()
                    sapi_paused = False
                    if state_callback is not None:
                        state_callback("running")

                if not sapi_paused and bool(
                    voice.WaitUntilDone(40)
                ):
                    # Anything not observed by status polling is definitely done
                    # once this entire stream reports completion.
                    while done_local_count < len(
                        block["segments"]
                    ):
                        emit_segment_done(
                            block["segments"][
                                done_local_count
                            ]
                        )
                        done_local_count += 1
                    break

                maybe_heartbeat(
                    int(active_meta["global_index"]),
                    len(segments),
                )
                pythoncom.PumpWaitingMessages()
                if sapi_paused:
                    time.sleep(0.03)

            if stopped:
                break

        return {
            "requested_audio_output": audio_output_description,
            "actual_audio_output": actual_audio_output,
            "chunks": len(segments),
            "completed_chunks": completed_chunks,
            "sapi_stream_blocks": len(blocks),
            "stopped": stopped,
            "paused_at_finish": bool(pause_event.is_set()),
            "duration_sec": round(
                time.perf_counter() - started,
                3,
            ),
        }
    finally:
        pythoncom.CoUninitialize()
