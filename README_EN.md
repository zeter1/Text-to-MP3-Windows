**Язык / Language:** [Русский](README.md) · **English**

# Text to MP3 for Windows

**Text to MP3 for Windows** is a Python desktop application for reading text with Microsoft SAPI system voices and saving text as MP3. It supports multiple tabs, global hotkeys, recovery of interrupted jobs, and long texts.

The project is designed for everyday use: text can be listened to directly in the application, selected text can be captured from other programs, and audio versions of articles, notes, and documents can be created.

## What the project demonstrates

- Python integration with Microsoft SAPI and COM through `pywin32`;
- global Windows hotkeys and transfer of selected text between applications;
- state management for multiple independent tabs;
- long-running MP3 generation with recovery data after interruption;
- speech synthesis integrated with FFmpeg;
- local user-state persistence separated from repository source files;
- lightweight CI verification for a Windows-specific project without pretending to emulate SAPI or real audio devices.

## Features

- text-to-speech through Microsoft SAPI;
- saving text to MP3;
- multiple independent text tabs;
- workspace persistence across launches;
- global hotkeys for capturing selected text;
- pause/resume with reading-position preservation;
- optional removal of already-read sentences;
- voice, speed, pitch, and volume settings;
- playback-device selection;
- MP3 bitrate settings;
- recovery of interrupted MP3 conversion;
- diagnostic logs for troubleshooting;
- optional Windows autostart.

## Installation

1. Install Python 3.11 or newer for Windows.
2. Make sure at least one Microsoft SAPI voice is installed in Windows.
3. Download the repository using **Code → Download ZIP** or Git:

```bash
git clone https://github.com/zeter1/Text-to-MP3-Windows.git
cd Text-to-MP3-Windows
```

4. Create a virtual environment and install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

MP3 generation uses `imageio-ffmpeg` or FFmpeg available from the system `PATH`.

## Launch

```powershell
py text_to_mp3.py
```

## Usage

### Reading text aloud

1. Start the application.
2. Paste or type text into a tab.
3. Select the voice, speed, volume, and other settings.
4. Start reading.
5. Use pause and resume when necessary.

### Capturing text from another application

1. Configure a global hotkey for the required tab.
2. Select text in a browser, document, or another application.
3. Press the assigned hotkey.
4. The selected text is transferred to Text to MP3 for reading or saving.

### Creating an MP3

1. Prepare the text in a tab.
2. Select the voice and synthesis settings.
3. Configure the MP3 parameters.
4. Start saving.
5. For long jobs, recovery data is stored so a partially completed process does not always need to restart from zero after a failure.

## Architecture

The current application remains a large single-file Windows module, `text_to_mp3.py`. Several logical subsystems are separated inside it:

```text
Tkinter UI / tabs
    ↓
text state + settings
    ↓
Microsoft SAPI / COM speech
    ↓
audio playback / MP3 pipeline
    ↓
FFmpeg
    ↓
recovery + backups + diagnostics
```

Windows hotkeys, selected-text transfer, autostart, and state persistence are additional subsystems. This reflects a working legacy application; further safe module decomposition remains a separate engineering task.

## Typical uses

- listening to long texts instead of reading from a screen;
- preparing audio versions of articles and notes;
- creating MP3 files from text;
- quickly listening to selected text from another application;
- working with several texts at once.

## Main dependencies

- `pywin32` — Microsoft SAPI, COM, and Windows integration;
- `imageio-ffmpeg` — fallback FFmpeg provider.

Most other logic uses the Python standard library.

## Data and recovery

The application can store settings, tab state, recovery data, text backups for long MP3 conversions, and diagnostic logs next to the application. Runtime data is excluded from Git through `.gitignore`.

## Project verification

```powershell
python -m compileall -q text_to_mp3.py tests
python -m unittest discover -s tests -v
```

GitHub Actions runs compile checks and offline repository-contract regression tests. These validate syntax, required runtime-data exclusions, and key dependency contracts without launching SAPI/COM.

## Limitations and verification level

- the application is intended for Windows 10/11 only;
- real SAPI, COM, global-hotkey, and audio-device behavior requires Windows runtime verification;
- the MP3 pipeline depends on an available FFmpeg binary;
- voice quality and available voices depend on installed SAPI voices;
- CI does not claim full verification of hardware- and Windows-dependent scenarios.

## Platform

The application targets Windows 10/11 because it uses Microsoft SAPI, COM, Win32 APIs, and Windows system functionality.

## Version

Current source version: **4.2 FULL**.

## Documentation and support

- [Security / privacy](SECURITY.md)
- [Support and diagnostics](SUPPORT.md)
- the bug-report form is under `.github/ISSUE_TEMPLATE/bug_report.yml`.

## License

No open-source license is currently granted. The source code is published for portfolio review and implementation study.
