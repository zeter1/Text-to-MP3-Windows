"""Stage private CPython stdlib and pip from the clean build environment."""
from pathlib import Path
import importlib.util
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('voice_worker.py', 'voice_catalog.py', 'engine_installation.py', 'engine_runtime.py')


def prepare(distribution: Path) -> None:
    distribution = distribution.resolve()
    if not distribution.is_relative_to((ROOT / 'dist').resolve()) or not distribution.is_dir():
        raise RuntimeError('Only the generated distribution can receive the engine runtime.')
    target = distribution / 'engine_runtime'
    if target.exists():
        raise RuntimeError('Engine runtime destination already exists.')
    base = Path(sys.base_prefix).resolve()
    if sys.version_info[:2] != (3, 13) or not (base / 'python.exe').is_file():
        raise RuntimeError('Build with the tested CPython 3.13 runtime.')
    target.mkdir()
    files = [base / 'python.exe', *base.glob('python3*.dll'), *base.glob('vcruntime*.dll'), base / 'LICENSE.txt']
    for source in files:
        if not source.is_file():
            raise RuntimeError('Missing runtime source: ' + source.name)
        shutil.copyfile(source, target / source.name)
    ignore = shutil.ignore_patterns('site-packages', '__pycache__', '*.pyc', '*.pdb', 'test', 'tests', 'idlelib', 'turtledemo')
    shutil.copytree(base / 'DLLs', target / 'DLLs', ignore=ignore)
    shutil.copytree(base / 'Lib', target / 'Lib', ignore=ignore)
    spec = importlib.util.find_spec('pip')
    if spec is None or spec.origin is None:
        raise RuntimeError('pip must be available in the build environment.')
    shutil.copytree(Path(spec.origin).parent, target / 'Lib' / 'site-packages' / 'pip', ignore=ignore)
    for metadata in Path(spec.origin).parent.parent.glob('pip-*.dist-info'):
        shutil.copytree(metadata, target / 'Lib' / 'site-packages' / metadata.name, ignore=ignore)
    # Ignore machine registry/env/user site while finding standalone voice_catalog.
    (target / 'python313._pth').write_text('.\nDLLs\nLib\nLib/site-packages\n../tts_app\n', encoding='utf-8')
    (distribution / 'tts_app').mkdir(exist_ok=True)
    (distribution / 'tools').mkdir(exist_ok=True)
    for name in MODULES:
        shutil.copyfile(ROOT / 'tts_app' / name, distribution / 'tts_app' / name)
    shutil.copyfile(ROOT / 'tools' / 'manage_voice_engine.py', distribution / 'tools' / 'manage_voice_engine.py')


if __name__ == '__main__':
    prepare(Path(sys.argv[1]))
