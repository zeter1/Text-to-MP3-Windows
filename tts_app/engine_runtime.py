"""Locate the private Python used by optional engines in source and portable builds."""
from pathlib import Path
import os
import sys


def program_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


def engine_python() -> str:
    if not getattr(sys, 'frozen', False):
        return sys.executable
    executable = program_root() / 'engine_runtime' / 'python.exe'
    if not executable.is_file():
        raise RuntimeError('Не найден встроенный Python движков. Распакуйте архив программы целиком.')
    return str(executable)


def engine_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop('PYTHONHOME', None)
    environment.pop('PYTHONPATH', None)
    environment['PYTHONNOUSERSITE'] = '1'
    environment['PYTHONUTF8'] = '1'
    return environment
