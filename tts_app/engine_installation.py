"""Additive engine management. Standalone imports; never reads application state."""
from pathlib import Path
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from .voice_catalog import MODEL_PATH, MODEL_URL, MODEL_BYTES, EDGE_RUNTIME, verified_model

ENGINE_ROOT = MODEL_PATH.parent
OPTIONAL = ENGINE_ROOT / 'optional_dependencies'
BACKUPS = ENGINE_ROOT.parent / '_maintenance_backups' / 'removed_engines'


def engine_path(engine: str) -> Path:
    if engine not in {'silero', 'edge'}:
        raise ValueError('Неизвестный движок.')
    target = MODEL_PATH if engine == 'silero' else EDGE_RUNTIME
    if (ENGINE_ROOT.is_symlink() or ENGINE_ROOT.is_junction() or target.is_symlink()
            or target.is_junction() or target.resolve().parent != ENGINE_ROOT.resolve()):
        raise RuntimeError('Небезопасный путь движка.')
    return target


def engine_status(engine: str) -> str:
    target = engine_path(engine)
    if not target.exists():
        return 'Не установлен'
    if engine == 'silero':
        try:
            with verified_model(target):
                pass
        except (RuntimeError, OSError):
            return 'Файл требует проверки; автоматически не заменяется'
        if importlib.util.find_spec('torch') is None and not (OPTIONAL / 'torch').is_dir():
            return 'Модель установлена; нет библиотеки PyTorch'
    elif not (target / 'edge_tts' / '__init__.py').is_file():
        return 'Папка существует; автоматически не заменяется'
    return 'Установлен'


def remove_engine(engine: str) -> str:
    target = engine_path(engine)
    if not target.exists():
        return 'Уже отсутствует'
    if BACKUPS.is_symlink() or not BACKUPS.resolve().is_relative_to(ENGINE_ROOT.parent.resolve()):
        raise RuntimeError('Небезопасный путь резервной копии.')
    destination = BACKUPS / (engine + '_' + uuid.uuid4().hex)
    # Exact one-source/one-target map: no overwrite, preserve original bytes for recovery.
    if destination.exists():
        raise RuntimeError('Резервная копия уже существует.')
    BACKUPS.mkdir(parents=True, exist_ok=True)
    target.rename(destination)
    if target.exists() or not destination.exists():
        raise RuntimeError('Не удалось подтвердить удаление движка.')
    return 'Удалён из программы. Резервная копия: ' + str(destination)


def install_engine(engine: str) -> str:
    target = engine_path(engine)
    if target.exists():
        return 'Уже существует — оставлен без изменений'
    ENGINE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='install_', dir=ENGINE_ROOT) as temporary:
        stage = Path(temporary)
        if engine == 'silero':
            if importlib.util.find_spec('torch') is None and not (OPTIONAL / 'torch').is_dir():
                if OPTIONAL.exists():
                    raise RuntimeError('Папка библиотек уже существует; оставлена без изменений.')
                deps = stage / 'dependencies'
                pip_install(['torch==2.8.0', '--index-url', 'https://download.pytorch.org/whl/cpu'], deps)
                deps.rename(OPTIONAL)
            candidate = stage / 'model.pt'
            with urllib.request.urlopen(MODEL_URL, timeout=30) as response, candidate.open('xb') as output:
                if response.geturl() != MODEL_URL:
                    raise RuntimeError('Неожиданное перенаправление загрузки.')
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MODEL_BYTES:
                        raise RuntimeError('Неверный размер модели.')
                    output.write(block)
            if size != MODEL_BYTES:
                raise RuntimeError('Неполная загрузка модели.')
            with verified_model(candidate):
                pass
        else:
            candidate = stage / 'edge'
            pip_install(['edge-tts==7.3.1'], candidate)
            if not (candidate / 'edge_tts' / '__init__.py').is_file():
                raise RuntimeError('Установка Edge не завершена.')
        # rename (not replace) refuses an existing Windows destination.
        if target.exists():
            raise RuntimeError('Движок появился во время установки; сохранён без изменений.')
        candidate.rename(target)
    return 'Установка завершена'


def pip_install(packages: list[str], destination: Path) -> None:
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-input', '--only-binary=:all:', '--no-compile',
                    '--disable-pip-version-check', '--target', str(destination), *packages],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=1200,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
