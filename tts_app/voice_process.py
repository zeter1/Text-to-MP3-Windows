"""Cancellable engine sessions. Only validated PCM WAV is published to a job."""
from pathlib import Path
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
from .voice_catalog import voice_spec
from .engine_runtime import engine_python, engine_environment, program_root


def stop_process(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    else:
        process.wait()


def checked_wav(path: Path) -> dict:
    with wave.open(str(path), 'rb') as audio:
        frames = audio.getnframes()
        if (audio.getparams()[:3] != (1, 2, 24000) or frames <= 0
                or len(audio.readframes(frames)) != frames * 2):
            raise RuntimeError('Движок создал неполный звуковой файл. Текст сохранён.')
        return {'frames': frames, 'duration': frames / 24000}


def run_audio_process(arguments: list[str], stop: threading.Event, timeout: float = 120) -> None:
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if stop.wait(0.04):
                raise InterruptedError('Озвучивание остановлено.')
            if time.monotonic() >= deadline:
                raise TimeoutError('Превышено время обработки звука.')
        if stop.is_set():
            raise InterruptedError('Озвучивание остановлено.')
        if process.returncode != 0:
            raise RuntimeError('FFmpeg не смог обработать звук нового движка.')
    finally:
        stop_process(process)


def audio_filters(rate: int, pitch: int, volume: int) -> str:
    speed = 2 ** (max(-20, min(20, int(rate))) / 10)
    tone = 2 ** (max(-24, min(24, int(pitch))) / 12)
    tempo = speed / tone
    filters = [f'asetrate={24000 * tone:.6f}', 'aresample=24000']
    while tempo > 2:
        filters.append('atempo=2')
        tempo /= 2
    while tempo < 0.5:
        filters.append('atempo=0.5')
        tempo /= 0.5
    filters.extend([f'atempo={tempo:.6f}', f'volume={max(0, min(100, int(volume))) / 100:.6f}'])
    return ','.join(filters)


class VoiceSession:
    def __init__(self, voice: str, stop_event: threading.Event, *, worker_script: Path | None = None):
        self.voice = voice
        self.stop = stop_event
        self.script = worker_script or program_root() / 'tts_app' / 'voice_worker.py'
        if worker_script is None and voice_spec(voice) is None:
            raise ValueError('Неизвестный движок речи.')
        self.process = None
        self.reader = None
        self.writer = None
        self.write_failed = threading.Event()
        self.responses = queue.Queue(maxsize=2)
        self.sequence = 0

    def __enter__(self):
        if self.stop.is_set():
            raise InterruptedError('Озвучивание остановлено.')
        self.process = subprocess.Popen([engine_python(), '-u', str(self.script), self.voice],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8', bufsize=1, env=engine_environment(),
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.reader = threading.Thread(target=self._read_responses, name='voice-protocol', daemon=True)
        self.reader.start()
        return self

    def _read_responses(self) -> None:
        try:
            while line := self.process.stdout.readline(2049):
                if len(line) > 2048:
                    break
                self.responses.put_nowait(json.loads(line))
        except (ValueError, OSError, queue.Full):
            pass

    def request(self, payload: dict, *, timeout: float = 180) -> None:
        self.sequence += 1
        message = json.dumps({**payload, 'id': self.sequence}, ensure_ascii=True) + '\n'
        if len(message) > 262144:
            raise ValueError('Слишком большой фрагмент для движка.')
        self.write_failed.clear()
        def write_request():
            try:
                self.process.stdin.write(message)
                self.process.stdin.flush()
            except (OSError, ValueError):
                self.write_failed.set()
        # A worker can hang before reading its pipe; writes must be cancellable too.
        self.writer = threading.Thread(target=write_request, name='voice-request', daemon=True)
        self.writer.start()
        deadline = time.monotonic() + timeout
        while True:
            if self.stop.is_set():
                self.__exit__(None, None, None)
                raise InterruptedError('Озвучивание остановлено.')
            if time.monotonic() >= deadline:
                self.__exit__(None, None, None)
                raise TimeoutError('Движок речи не ответил вовремя. Попробуйте меньший фрагмент.')
            try:
                response = self.responses.get(timeout=0.04)
            except queue.Empty:
                if self.write_failed.is_set() or self.process.poll() is not None:
                    raise RuntimeError('Процесс движка речи завершился.')
                continue
            if not isinstance(response, dict) or response.get('id') != self.sequence:
                raise RuntimeError('Нарушен протокол движка речи.')
            if response.get('ok') is not True:
                # Only fixed messages from our worker, never service exception strings.
                from .voice_worker import ERRORS
                raise RuntimeError(ERRORS.get(response.get('code'), ERRORS['protocol']))
            self.writer.join(timeout=1)
            return

    def synthesize(self, text: str, wav_path: Path, rate: int, pitch: int, volume: int) -> None:
        from .sapi_core import find_ffmpeg
        from .text_processing import split_text
        ffmpeg = find_ffmpeg()
        pieces = split_text(text, 900)
        if not pieces:
            raise ValueError('Нет текста для озвучивания.')
        wav_path = Path(wav_path)
        with tempfile.TemporaryDirectory(prefix='voice_', dir=wav_path.parent) as temp:
            work = Path(temp)
            combined = work / 'combined.wav'
            with wave.open(str(combined), 'wb') as output:
                output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                for index, piece in enumerate(pieces):
                    raw = work / f'raw_{index}.dat'
                    self.request({'text': piece, 'path': str(raw)})
                    pcm = work / f'pcm_{index}.wav'
                    run_audio_process([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                                       '-i', str(raw), '-vn', '-ac', '1', '-ar', '24000',
                                       '-c:a', 'pcm_s16le', str(pcm)], self.stop)
                    checked_wav(pcm)
                    with wave.open(str(pcm), 'rb') as source:
                        output.writeframes(source.readframes(source.getnframes()))
            candidate = work / 'candidate.wav'
            run_audio_process([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                               '-i', str(combined), '-af', audio_filters(rate, pitch, volume),
                               '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', str(candidate)], self.stop)
            checked_wav(candidate)
            if self.stop.is_set():
                raise InterruptedError('Озвучивание остановлено.')
            candidate.replace(wav_path)

    def __exit__(self, *_exc):
        if self.process is not None:
            stop_process(self.process)
            if self.writer is not None:
                self.writer.join(timeout=2)
            self.process.stdin.close()
            self.reader.join(timeout=2)
            self.process.stdout.close()
