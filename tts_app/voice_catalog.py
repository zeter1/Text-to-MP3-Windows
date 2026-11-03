"""Fixed voice identities and the trusted local Silero model; no network/import side effects."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sys

MODEL_URL = 'https://models.silero.ai/models/tts/ru/v5_6_ru.pt'
MODEL_SHA256 = '04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e'
MODEL_BYTES = 147664410
MODEL_PATH = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
              else Path(__file__).resolve().parents[1]) / 'Движки речи' / 'silero_v5_6_ru.pt'
EDGE_RUNTIME = MODEL_PATH.parent / 'edge_tts_7_3_1'
VOICES = {
    'Silero — Ксения (без интернета)': ('silero', 'kseniya'),
    'Silero — Ксения, вариант 2 (без интернета)': ('silero', 'xenia'),
    'Silero — Бая (без интернета)': ('silero', 'baya'),
    'Silero — Айдар (без интернета)': ('silero', 'aidar'),
    'Silero — Евгений (без интернета)': ('silero', 'eugene'),
    'Microsoft Edge — Светлана (интернет)': ('edge', 'ru-RU-SvetlanaNeural'),
    'Microsoft Edge — Дмитрий (интернет)': ('edge', 'ru-RU-DmitryNeural'),
}


def extra_voice_names() -> list[str]:
    return list(VOICES)


def voice_spec(description: str) -> tuple[str, str] | None:
    return VOICES.get(description)


@contextmanager
def verified_model(path: Path = MODEL_PATH, digest: str = MODEL_SHA256):
    try:
        stream = path.open('rb')
    except FileNotFoundError:
        raise RuntimeError('Модель Silero не установлена. Выполните: python tools/setup_voices.py') from None
    with stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest:
            raise RuntimeError('Модель Silero повреждена. Повторите python tools/setup_voices.py')
        stream.seek(0)
        yield stream
