"""Play generated WAV through the chosen SAPI endpoint; acknowledge only actual playback."""
from pathlib import Path
import tempfile
import threading
import time
from .runtime_core import SVS_FLAGS_ASYNC, SVSFPURGE_BEFORE_SPEAK
from .sapi_core import set_audio_output_by_description
from .sapi_preview_diagnostics import (
    preview_sapi_snapshot, verify_preview_stream, release_preview_exception_frames,
)
from .text_processing import build_preview_segments
from .voice_process import VoiceSession, checked_wav


def preview_generated_audio(
    text: str, voice_description: str, audio_output_description: str,
    rate: int, pitch: int, volume: int,
    stop_event: threading.Event, pause_event: threading.Event,
    progress_callback=None, state_callback=None, heartbeat_callback=None,
    checkpoint_callback=None, segment_done_callback=None, diagnostic_callback=None,
) -> dict:
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    voice = None
    stream = None
    started = time.monotonic()
    segments = build_preview_segments(text, max_chars=900)
    completed = 0
    actual_output = ''
    try:
        voice = win32com.client.Dispatch('SAPI.SpVoice')
        actual_output = set_audio_output_by_description(voice, audio_output_description)
        voice.Rate = 0
        voice.Volume = 100
        with tempfile.TemporaryDirectory(prefix='tts_preview_') as temp:
            with VoiceSession(voice_description, stop_event) as engine:
                for index, segment in enumerate(segments, start=1):
                    if stop_event.is_set():
                        break
                    info = {'index': index, 'total': len(segments), **segment}
                    checkpoint = {'segment': index, 'segments_total': len(segments),
                                  'start': segment['start'], 'end': segment['end'],
                                  'word_position': 0, 'word_length': 0, 'sapi_position_raw': None}
                    if progress_callback:
                        progress_callback(info)
                    if checkpoint_callback:
                        checkpoint_callback({**checkpoint, 'reason': 'segment_start'})
                    wav = Path(temp) / 'segment.wav'
                    try:
                        engine.synthesize(segment['text'], wav, rate, pitch, volume)
                    except InterruptedError:
                        if stop_event.is_set():
                            break
                        raise
                    duration = checked_wav(wav)['duration']
                    while pause_event.is_set() and not stop_event.is_set():
                        if state_callback:
                            state_callback('paused')
                        if checkpoint_callback:
                            checkpoint_callback({**checkpoint, 'reason': 'paused'})
                        stop_event.wait(0.04)
                    if stop_event.is_set():
                        break
                    stream = win32com.client.Dispatch('SAPI.SpFileStream')
                    opened = False
                    verified = False
                    paused = False
                    segment_error = None
                    try:
                        stream.Open(str(wav), 0, False)
                        opened = True
                        before = preview_sapi_snapshot(voice)
                        stream_id = int(voice.SpeakStream(stream, SVS_FLAGS_ASYNC))
                        if state_callback:
                            state_callback('running')
                        active_elapsed = 0.0
                        last_tick = time.monotonic()
                        last_heartbeat = last_tick
                        while not stop_event.is_set():
                            tick = time.monotonic()
                            if not paused:
                                active_elapsed += tick - last_tick
                            last_tick = tick
                            if pause_event.is_set() and not paused:
                                voice.Pause()
                                paused = True
                                if state_callback:
                                    state_callback('paused')
                                if checkpoint_callback:
                                    checkpoint_callback({**checkpoint, 'reason': 'paused'})
                            elif not pause_event.is_set() and paused:
                                voice.Resume()
                                paused = False
                                if state_callback:
                                    state_callback('running')
                            if not paused and voice.WaitUntilDone(40):
                                pythoncom.PumpWaitingMessages()
                                if stop_event.is_set():
                                    break
                                after = preview_sapi_snapshot(voice)
                                verify_preview_stream(stream_id, before, after)
                                verified = True
                                break
                            if active_elapsed > duration + 30:
                                raise TimeoutError('Windows не завершил воспроизведение звука.')
                            if heartbeat_callback and tick - last_heartbeat >= 15:
                                heartbeat_callback({'segment': index, 'segments_total': len(segments),
                                                    'elapsed_sec': tick - started, 'paused': paused})
                                last_heartbeat = tick
                            pythoncom.PumpWaitingMessages()
                            if paused:
                                stop_event.wait(0.03)
                    except BaseException as exc:
                        segment_error = exc
                        release_preview_exception_frames(exc)
                        raise
                    finally:
                        # A stream must stay open until SAPI releases it, including on errors/Stop.
                        cleanup_error = None
                        try:
                            if not verified or stop_event.is_set():
                                if paused:
                                    voice.Resume()
                                voice.Speak('', SVSFPURGE_BEFORE_SPEAK | SVS_FLAGS_ASYNC)
                                if not voice.WaitUntilDone(1000):
                                    raise TimeoutError('Windows не подтвердил остановку звука.')
                        except Exception as exc:
                            cleanup_error = exc
                            release_preview_exception_frames(exc)
                            voice = None
                        finally:
                            try:
                                if opened:
                                    stream.Close()
                            except Exception as exc:
                                cleanup_error = cleanup_error or exc
                                release_preview_exception_frames(exc)
                        stream = None
                        if cleanup_error is not None:
                            if segment_error is None:
                                raise cleanup_error
                            segment_error.add_note('Дополнительно не удалось штатно освободить аудиопоток.')
                    if not verified or stop_event.is_set():
                        break
                    completed += 1
                    if segment_done_callback:
                        segment_done_callback({**info, 'playback_verified': True})
        return {'requested_audio_output': audio_output_description,
                'actual_audio_output': actual_output, 'chunks': len(segments),
                'completed_chunks': completed, 'sapi_stream_blocks': len(segments),
                'stopped': stop_event.is_set(), 'paused_at_finish': pause_event.is_set(),
                'duration_sec': round(time.monotonic() - started, 3)}
    except BaseException as exc:
        release_preview_exception_frames(exc)
        raise
    finally:
        stream = None
        voice = None
        pythoncom.CoUninitialize()
