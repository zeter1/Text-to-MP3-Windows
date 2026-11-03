"""Install optional engines/model explicitly; never import the stateful application."""
import argparse
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tts_app'))
from voice_catalog import MODEL_PATH, MODEL_URL, MODEL_SHA256, MODEL_BYTES, EDGE_RUNTIME, verified_model


def install_model(source: Path | None = None) -> None:
    if MODEL_PATH.is_file():
        try:
            with verified_model(MODEL_PATH):
                print('Локальная модель Silero уже готова.')
                return
        except RuntimeError:
            pass
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='setup_', dir=MODEL_PATH.parent) as temp:
        candidate = Path(temp) / 'model.pt'
        if source is not None:
            if source.stat().st_size != MODEL_BYTES:
                raise RuntimeError('Неверный размер модели.')
            shutil.copyfile(source, candidate)
        else:
            # Fixed HTTPS endpoint, certificate verification, no arbitrary redirects.
            with urllib.request.urlopen(MODEL_URL, timeout=30) as response, candidate.open('xb') as output:
                if response.geturl() != MODEL_URL:
                    raise RuntimeError('Неожиданное перенаправление загрузки модели.')
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MODEL_BYTES:
                        raise RuntimeError('Неверный размер модели.')
                    output.write(block)
        if candidate.stat().st_size != MODEL_BYTES:
            raise RuntimeError('Загрузка модели не завершена.')
        with verified_model(candidate):
            pass
        if MODEL_PATH.exists():
            with MODEL_PATH.open('rb') as old_model:
                old_hash = hashlib.file_digest(old_model, 'sha256').hexdigest()
            backup = MODEL_PATH.with_name('silero_previous_' + old_hash[:16] + '.pt')
            if not backup.exists():
                shutil.copyfile(MODEL_PATH, backup)
        candidate.replace(MODEL_PATH)
    print('Silero готов: пять русских голосов, дальнейшая озвучка без интернета.')


def main() -> None:
    parser = argparse.ArgumentParser(description='Подготовка русских голосов Silero и Edge TTS')
    parser.add_argument('--install-deps', action='store_true', help='Установить только отсутствующие библиотеки')
    parser.add_argument('--model-file', type=Path, help='Установить проверенную модель из локального файла')
    args = parser.parse_args()
    for module, packages in [('torch', ['torch==2.8.0', '--index-url', 'https://download.pytorch.org/whl/cpu']),
                             ('edge_tts', ['edge-tts==7.3.1'])]:
        if importlib.util.find_spec(module) is None:
            if not args.install_deps:
                raise SystemExit('Нет библиотеки ' + module + '. Добавьте --install-deps к этой команде.')
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                            '--no-input', *packages], check=True, timeout=1200)
    if args.install_deps and not (EDGE_RUNTIME / 'edge_tts' / '__init__.py').is_file():
        # A private copy fixes the old Edge handshake without upgrading other applications.
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                        '--no-input', '--no-deps', '--target', str(EDGE_RUNTIME), 'edge-tts==7.3.1'],
                       check=True, timeout=180)
    install_model(args.model_file)


if __name__ == '__main__':
    main()
