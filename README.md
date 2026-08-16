# Text to MP3 for Windows

Windows desktop application for reading text aloud and converting text to MP3 using Microsoft SAPI and FFmpeg.

## Features

- Text-to-speech playback through Windows SAPI.
- Export long text to MP3.
- Multiple text tabs with persistent workspace state.
- Per-tab global hotkeys for capturing selected text from other applications.
- Pause and resume reading with a saved position.
- Optional deletion of already-read sentences.
- Voice, speed, pitch, volume and playback-device controls.
- MP3 bitrate selection.
- Recovery of interrupted MP3 conversion jobs.
- Diagnostic logging designed for troubleshooting with ChatGPT/Codex.
- Optional autostart with Windows.

## Requirements

- Windows 10/11.
- Python 3.11+ recommended.
- A Microsoft SAPI voice installed in Windows.
- `pywin32`.
- `imageio-ffmpeg` (or FFmpeg available in `PATH`).

## Installation

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

## Run

```powershell
py text_to_mp3.py
```

## Main dependencies

The application primarily uses the Python standard library plus:

- `pywin32` for Windows SAPI/COM integration.
- `imageio-ffmpeg` as a fallback source for an FFmpeg executable.

## Data and logs

The program creates local runtime folders next to the script/executable when that location is writable, including settings, diagnostic logs, recovery data and MP3 text backups. These runtime folders are excluded from Git by `.gitignore`.

## Verification

The repository includes a Windows GitHub Actions workflow that compiles the main source file on every push and pull request:

```powershell
python -m py_compile text_to_mp3.py
```

This is a non-destructive syntax check; Windows SAPI, playback devices and FFmpeg behavior still require real Windows testing.

## Platform

This project is Windows-specific because speech synthesis and several system integrations use Windows SAPI, COM, the registry and Win32 APIs.

## Version

Current application version in the source: **4.2 FULL**.

## License

No open-source license is currently granted. The source code is published for portfolio and code-review purposes.
