"""Synthetic regression checks; no real network, model or audio device."""
from pathlib import Path
import hashlib
import importlib
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
MIRROR = tempfile.TemporaryDirectory(prefix='tts voices тест ', dir=SOURCE)
ROOT = Path(MIRROR.name)
(ROOT / 'tts_app').mkdir()
for item in (SOURCE / 'tts_app').glob('*.py'):
    shutil.copyfile(item, ROOT / 'tts_app' / item.name)
sys.path.insert(0, str(ROOT))


class VoiceTests(unittest.TestCase):
    def catalog(self):
        try:
            return importlib.import_module('tts_app.voice_catalog')
        except ModuleNotFoundError:
            self.fail('New voice catalog is missing')

    def test_offline_first_explicit_network_labels(self):
        catalog = self.catalog()
        voices = catalog.extra_voice_names()
        self.assertEqual(len(voices), 7)
        self.assertEqual([catalog.voice_spec(v)[0] for v in voices], ['silero'] * 5 + ['edge'] * 2)
        self.assertTrue(all('без интернета' in v for v in voices[:5]))
        self.assertTrue(all('интернет' in v for v in voices[5:]))
        self.assertIsNone(catalog.voice_spec('Microsoft Irina Desktop'))
        self.assertIsNone(catalog.voice_spec('Silero — неизвестный'))

    def test_model_is_checked_without_importing_torch(self):
        catalog = self.catalog()
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            model = Path(tmp) / 'модель.pt'
            model.write_bytes(b'good fixture')
            digest = hashlib.sha256(model.read_bytes()).hexdigest()
            with catalog.verified_model(model, digest) as stream:
                self.assertEqual(stream.read(), b'good fixture')
            model.write_bytes(b'broken fixture')
            with self.assertRaisesRegex(RuntimeError, 'повреждена'):
                with catalog.verified_model(model, digest):
                    self.fail('Untrusted model accepted')

    def test_extra_voices_remain_available_if_sapi_enumeration_fails(self):
        catalog = self.catalog()
        audio = importlib.import_module('tts_app.app_audio_settings')
        events = importlib.import_module('queue').Queue()
        app = types.SimpleNamespace(events=events, current_tab=lambda: None)
        with patch.object(audio, 'get_sapi_voices', side_effect=RuntimeError('synthetic SAPI unavailable')):
            audio.AppAudioSettingsMixin._load_voices_async(app)
            first = events.get(timeout=2)
            second = events.get(timeout=2)
        self.assertEqual(first, ('', 'voices_loaded', catalog.extra_voice_names()))
        self.assertEqual(second[1], 'voices_error')

    def test_missing_model_never_downloads(self):
        catalog = self.catalog()
        with self.assertRaisesRegex(RuntimeError, 'установлена'):
            with catalog.verified_model(ROOT / 'missing.pt'):
                self.fail('Missing model accepted')

    def test_owned_worker_cancel_is_reaped(self):
        self.catalog()
        processes = importlib.import_module('tts_app.voice_process')
        worker = ROOT / 'sleep_worker.py'
        worker.write_text('import sys,time\nfor line in sys.stdin:\n time.sleep(60)\n', encoding='utf-8')
        stop = threading.Event()
        with processes.VoiceSession('fixture', stop, worker_script=worker) as session:
            timer = threading.Timer(0.15, stop.set)
            timer.start()
            started = time.monotonic()
            with self.assertRaises(InterruptedError):
                session.request({'op': 'test'}, timeout=5)
            timer.join()
            self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(session.process.poll())
        self.assertFalse(session.reader.is_alive())

    def test_worker_timeout_is_not_cancel(self):
        self.catalog()
        processes = importlib.import_module('tts_app.voice_process')
        worker = ROOT / 'timeout_worker.py'
        worker.write_text('import sys,time\nfor line in sys.stdin:\n time.sleep(60)\n', encoding='utf-8')
        with processes.VoiceSession('fixture', threading.Event(), worker_script=worker) as session:
            with self.assertRaises(TimeoutError):
                session.request({'op': 'test'}, timeout=0.15)
        self.assertIsNotNone(session.process.poll())

    def test_service_error_cannot_leak_into_parent_exception(self):
        self.catalog()
        processes = importlib.import_module('tts_app.voice_process')
        worker = ROOT / 'error_worker.py'
        worker.write_text('import sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n'
                          ' print(json.dumps({"id":r["id"],"ok":False,"code":"synthesis",'
                          '"message":"PRIVATE_REMOTE_RESPONSE"}),flush=True)\n', encoding='utf-8')
        with processes.VoiceSession('fixture', threading.Event(), worker_script=worker) as session:
            with self.assertRaises(RuntimeError) as caught:
                session.request({'op': 'test'})
        self.assertNotIn('PRIVATE_REMOTE_RESPONSE', str(caught.exception))

    def test_cancel_reaches_worker_that_never_reads_stdin(self):
        self.catalog()
        processes = importlib.import_module('tts_app.voice_process')
        worker = ROOT / 'unread_worker.py'
        worker.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
        stop = threading.Event()
        errors = []
        with processes.VoiceSession('fixture', stop, worker_script=worker) as session:
            def request():
                try:
                    session.request({'text': '\U0001f600' * 900}, timeout=3)
                except Exception as exc:
                    errors.append(type(exc))
            thread = threading.Thread(target=request, daemon=True)
            thread.start()
            stop.wait(0.15)
            stop.set()
            thread.join(timeout=1)
            reached_cancel = not thread.is_alive()
            if not reached_cancel:
                session.process.kill()
                session.process.wait(timeout=2)
                thread.join(timeout=2)
        self.assertTrue(reached_cancel, 'stdin write blocked cancellation')
        self.assertEqual(errors, [InterruptedError])

    def test_invalid_audio_keeps_previous_wav_and_cleans_owned_temp(self):
        self.catalog()
        processes = importlib.import_module('tts_app.voice_process')
        target = ROOT / 'previous.wav'
        target.write_bytes(b'previous valid sentinel')
        session = processes.VoiceSession('fixture', threading.Event(), worker_script=ROOT / 'unused.py')
        def partial(payload, **_kwargs):
            Path(payload['path']).write_bytes(b'not an audio file')
        with patch.object(session, 'request', side_effect=partial):
            with self.assertRaises(RuntimeError):
                session.synthesize('Проверка.', target, 0, 0, 100)
        self.assertEqual(target.read_bytes(), b'previous valid sentinel')
        self.assertEqual(list(ROOT.glob('voice_*')), [])

    def preview(self, mode):
        self.catalog()
        preview = importlib.import_module('tts_app.generated_preview')
        stop = threading.Event()
        streams = []
        done = []
        class Engine:
            def __init__(self, *_args): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def synthesize(self, _text, path, *_args):
                with wave.open(str(path), 'wb') as output:
                    output.setparams((1, 2, 24000, 0, 'NONE', ''))
                    output.writeframes(b'\x01\x00' * 240)
        class Stream:
            closed = False
            def Open(self, *_args): pass
            def Close(self): self.closed = True
        class Voice:
            position = 0
            current = 0
            purging = False
            def SpeakStream(self, *_args):
                self.current += 1
                return self.current
            def Speak(self, *_args): self.purging = True
            def Resume(self): pass
            def Pause(self): pass
            def WaitUntilDone(self, _timeout):
                if self.purging:
                    if mode == 'cleanup_error': raise RuntimeError('secondary cleanup error')
                    return True
                if mode == 'stopped': stop.set()
                if mode not in ('failed', 'cleanup_error'): self.position += 240
                return True
        voice = Voice()
        def dispatch(name):
            if name == 'SAPI.SpVoice': return voice
            stream = Stream()
            streams.append(stream)
            return stream
        def snapshot(current):
            return {'CurrentDevicePosition': current.position, 'CurrentStreamNumber': current.current,
                    'LastHResult': 0}
        with patch.object(preview, 'VoiceSession', Engine), \
             patch.object(preview, 'set_audio_output_by_description', return_value='chosen endpoint'), \
             patch.object(preview, 'preview_sapi_snapshot', side_effect=snapshot), \
             patch('win32com.client.Dispatch', side_effect=dispatch):
            try:
                result = preview.preview_generated_audio('Первое. Второе.', 'fixture', 'chosen endpoint',
                                                         0, 0, 100, stop, threading.Event(),
                                                         segment_done_callback=done.append)
                return result, done, streams
            finally:
                self.assertTrue(all(stream.closed for stream in streams))

    def test_preview_acknowledges_only_played_sentences(self):
        result, done, _streams = self.preview('healthy')
        self.assertEqual(result['completed_chunks'], 2)
        self.assertEqual(len(done), 2)
        self.assertTrue(all(item['playback_verified'] for item in done))

    def test_preview_stop_never_acknowledges_synthesized_sentence(self):
        result, done, _streams = self.preview('stopped')
        self.assertEqual(result['completed_chunks'], 0)
        self.assertEqual(done, [])

    def test_preview_failed_output_keeps_primary_error_during_cleanup_failure(self):
        self.catalog()
        diagnostics = importlib.import_module('tts_app.sapi_preview_diagnostics')
        with self.assertRaises(diagnostics.PreviewPlaybackError):
            self.preview('cleanup_error')


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        MIRROR.cleanup()
