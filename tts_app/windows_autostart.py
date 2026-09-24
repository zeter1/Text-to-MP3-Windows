from __future__ import annotations

from .runtime import *

def windows_autostart_command() -> str:
    """
    Команда, которую Windows запускает при входе текущего пользователя.

    EXE запускается напрямую. Для .py/.pyw используется pythonw.exe, если он
    установлен рядом с текущим Python, чтобы при автозапуске не появлялось
    консольное окно.
    """
    if os.name != "nt":
        return ""

    if getattr(sys, "frozen", False):
        arguments = [str(Path(sys.executable).resolve())]
    else:
        try:
            script_path = app_code_dir() / "text_to_mp3.py"
        except NameError:
            script_path = Path(sys.argv[0]).resolve()

        python_executable = Path(sys.executable).resolve()
        if python_executable.name.casefold() in {
            "python.exe",
            "python_d.exe",
        }:
            pythonw = python_executable.with_name("pythonw.exe")
            if pythonw.exists():
                python_executable = pythonw

        arguments = [
            str(python_executable),
            str(script_path),
        ]

    return subprocess.list2cmdline(arguments)

def windows_autostart_is_enabled() -> bool:
    """Проверить фактическое наличие записи программы в HKCU\\...\\Run."""
    if os.name != "nt":
        return False

    try:
        import winreg
    except Exception:
        return False

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_REG_PATH,
            0,
            winreg.KEY_READ,
        ) as key:
            value, _value_type = winreg.QueryValueEx(
                key,
                AUTOSTART_REG_VALUE,
            )
        return bool(str(value or "").strip())
    except FileNotFoundError:
        return False
    except OSError:
        return False

def set_windows_autostart(enabled: bool) -> str:
    """
    Включить/выключить автозапуск для текущего пользователя.

    Используется HKCU, поэтому права администратора не требуются.
    Возвращает записанную команду при включении и пустую строку при выключении.
    """
    if os.name != "nt":
        raise RuntimeError(
            "Автозапуск этой программы поддерживается только в Windows."
        )

    import winreg

    if enabled:
        command = windows_autostart_command()
        if not command:
            raise RuntimeError(
                "Не удалось определить команду запуска программы."
            )

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key,
                AUTOSTART_REG_VALUE,
                0,
                winreg.REG_SZ,
                command,
            )
        return command

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(
                    key,
                    AUTOSTART_REG_VALUE,
                )
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass

    return ""

def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
