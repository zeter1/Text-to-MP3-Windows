from __future__ import annotations

import builtins
import dis
import importlib
import inspect
import pkgutil
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PKG = ROOT / "tts_app"

# One deliberately transactional method remains large. All other implementation
# modules should stay compact enough for cheap targeted AI inspection.
SIZE_LIMIT = 24 * 1024
SIZE_ALLOWLIST = {"task_conversion_worker.py": 60 * 1024}
FACADE_LIMIT = 4 * 1024
FACADES = {
    "runtime.py", "sapi.py", "windows_tools.py", "problem_logger.py",
    "task_ui.py", "task_sources.py", "task_preview_runtime.py",
    "app_events.py", "app_workspace.py", "app_recovery_hotkeys.py",
    "task_tab.py", "app.py",
}


def main() -> int:
    failures: list[str] = []

    for path in sorted(PKG.glob("*.py")):
        limit = SIZE_ALLOWLIST.get(path.name, SIZE_LIMIT)
        size = path.stat().st_size
        if size > limit:
            failures.append(f"module too large: {path.name} = {size} bytes > {limit}")
        if path.name in FACADES and size > FACADE_LIMIT:
            failures.append(f"facade too large: {path.name} = {size} bytes > {FACADE_LIMIT}")

    # Import every source module. Most Windows-only dependencies are intentionally
    # imported lazily inside functions, so this works as a cross-platform gate.
    import tts_app
    imported_modules = []
    for info in pkgutil.iter_modules(tts_app.__path__, prefix="tts_app."):
        imported_modules.append(importlib.import_module(info.name))

    import text_to_mp3
    imported_modules.append(text_to_mp3)

    # compileall/import-only checks miss names that are resolved only when a
    # method is actually called (the exact class of regression that once broke
    # startup after splitting app_workspace.py). Inspect bytecode and make sure
    # every LOAD_GLOBAL/LOAD_NAME has a real module global or builtin.
    missing_runtime_globals: set[tuple[str, str, str]] = set()

    def inspect_code(code, globals_dict, module_name: str, qualname: str) -> None:
        for instruction in dis.get_instructions(code):
            if instruction.opname not in {"LOAD_GLOBAL", "LOAD_NAME"}:
                continue
            name = str(instruction.argval)
            if name not in globals_dict and not hasattr(builtins, name):
                missing_runtime_globals.add((module_name, qualname, name))
        for constant in code.co_consts:
            if isinstance(constant, types.CodeType):
                inspect_code(
                    constant, globals_dict, module_name, qualname + "::<nested>"
                )

    for module in imported_modules:
        module_name = module.__name__
        for obj in vars(module).values():
            if inspect.isfunction(obj) and obj.__module__ == module_name:
                inspect_code(obj.__code__, obj.__globals__, module_name, obj.__qualname__)
            elif inspect.isclass(obj) and obj.__module__ == module_name:
                for attr_name, attr_value in vars(obj).items():
                    function = (
                        attr_value.__func__
                        if isinstance(attr_value, (staticmethod, classmethod))
                        else attr_value
                    )
                    if inspect.isfunction(function) and function.__module__ == module_name:
                        inspect_code(
                            function.__code__,
                            function.__globals__,
                            module_name,
                            f"{obj.__name__}.{attr_name}",
                        )

    for module_name, qualname, name in sorted(missing_runtime_globals):
        failures.append(
            f"unresolved runtime global: {module_name}:{qualname} -> {name}"
        )
    from tts_app import runtime
    from tts_app.problem_logger import ProblemLogger
    from tts_app.windows_tools import GlobalCopyHotkeyMonitor

    required_runtime = {
        "APP_VERSION",
        "STORAGE_BASE_DIR",
        "SETTINGS_DIR",
        "LOGS_DIR",
        "RECOVERY_DIR",
        "atomic_write_json",
        "scan_recovery_jobs",
        "probe_audio_file",
        "handle_text_ctrl_shortcut",
        "_ATOMIC_WRITE_LOCK",
        "_is_transient_atomic_replace_error",
    }
    missing = sorted(name for name in required_runtime if not hasattr(runtime, name))
    if missing:
        failures.append("runtime facade missing: " + ", ".join(missing))

    for cls, names in (
        (ProblemLogger, ("event", "error", "task_started", "_write_diagnostic_summary", "close")),
        (GlobalCopyHotkeyMonitor, ("parse_hotkey", "enable", "_wait_for_clipboard_change", "_windows_keyboard_hook_loop")),
    ):
        missing = [name for name in names if not hasattr(cls, name)]
        if missing:
            failures.append(f"{cls.__name__} facade missing: {', '.join(missing)}")

    if failures:
        print("verify_structure: FAIL")
        for item in failures:
            print(" -", item)
        return 1

    print("verify_structure: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
