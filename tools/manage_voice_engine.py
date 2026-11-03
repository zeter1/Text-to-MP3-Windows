"""Standalone management subprocess; avoid importing stateful tts_app/__init__.py."""
import importlib.util
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType('tts_app')
package.__path__ = [str(ROOT / 'tts_app')]
sys.modules['tts_app'] = package
from tts_app.engine_installation import install_engine, remove_engine

if __name__ == '__main__':
    try:
        if len(sys.argv) != 3 or sys.argv[1] not in {'install', 'remove'}:
            raise ValueError('Invalid command')
        action = install_engine if sys.argv[1] == 'install' else remove_engine
        result = {'ok': True, 'message': action(sys.argv[2])}
    except Exception:
        result = {'ok': False, 'message': 'Операция не завершена. Проверьте интернет, доступ к папке и наличие свободного места. Существующие компоненты не заменялись.'}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    raise SystemExit(0 if result['ok'] else 1)
