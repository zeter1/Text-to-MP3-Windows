"""PyInstaller runtime smoke test for the packaged Windows application.

At normal startup this hook is inert. With ``--self-test`` it runs before the
large GUI module, proving that the frozen runtime contains pywin32/COM support
and the bundled imageio-ffmpeg executable, then exits without opening Tkinter.
"""

from __future__ import annotations

import os
import subprocess
import sys
import json
from pathlib import Path


def run_packaged_self_test() -> int:
    if os.name != "nt":
        raise RuntimeError("ZeTer Voice packaged runtime is Windows-only.")

    import imageio_ffmpeg
    import pythoncom
    from win32com.client import Dispatch

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    if not ffmpeg or not os.path.isfile(ffmpeg):
        raise RuntimeError(f"Bundled FFmpeg was not found: {ffmpeg!r}")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    probe = subprocess.run(
        [ffmpeg, "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=creationflags,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            "Bundled FFmpeg failed its version probe: "
            + (probe.stderr or probe.stdout or "unknown error")[-1200:]
        )

    pythoncom.CoInitialize()
    try:
        voice = Dispatch("SAPI.SpVoice")
        voices = voice.GetVoices()
        voice_count = int(voices.Count)
        if voice_count < 1:
            raise RuntimeError("Windows SAPI is available but no speech voices were found.")
    finally:
        voices = None
        voice = None
        pythoncom.CoUninitialize()

    version_line = (probe.stdout or probe.stderr or "").splitlines()[0]
    if getattr(sys, 'frozen', False):
        from tts_app.engine_runtime import engine_python, engine_environment, program_root
        child = subprocess.run([engine_python(), '-c',
            'import sys,ssl,asyncio,pip,json;print(json.dumps({"exe":sys.executable,"prefix":sys.prefix,"path":sys.path}))'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30, env=engine_environment(), creationflags=creationflags)
        if child.returncode:
            raise RuntimeError('Private engine Python failed its runtime check.')
        identity = json.loads(child.stdout)
        if Path(identity['exe']).resolve() != Path(engine_python()).resolve():
            raise RuntimeError('Unexpected child interpreter identity.')
        private = program_root() / 'engine_runtime'
        if Path(identity['prefix']).resolve() != private.resolve():
            raise RuntimeError('Engine Python escaped its private runtime.')
        print('private engine Python: ok')
    print(f"packaged self-test: ok | voices={voice_count} | {version_line}")
    return 0


def run_engine_self_test() -> int:
    import tempfile
    import threading
    from tts_app.voice_catalog import extra_voice_names
    from tts_app.voice_process import VoiceSession, checked_wav
    from tts_app.sapi_core import find_ffmpeg
    from tts_app.generated_preview import preview_generated_audio
    from tts_app.runtime import DEFAULT_AUDIO_OUTPUT_LABEL
    results = []
    for index in (0, 5):
        voice = extra_voice_names()[index]
        with tempfile.TemporaryDirectory(prefix='zeter_voice_smoke_') as directory:
            wav = Path(directory) / 'speech.wav'
            with VoiceSession(voice, threading.Event()) as engine:
                engine.synthesize('Начало проверки. Русский голос работает. Проверка завершена.', wav, 0, 0, 80)
            details = checked_wav(wav)
            output = Path(directory) / 'speech.mp3'
            subprocess.run([find_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                            '-i', str(wav), '-codec:a', 'libmp3lame', str(output)],
                           check=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if output.stat().st_size < 1000:
                raise RuntimeError('Engine MP3 is empty.')
            import shutil
            shutil.copyfile(wav, Path(sys.executable).parent / f'voice-self-test-{index}.wav')
            shutil.copyfile(output, Path(sys.executable).parent / f'voice-self-test-{index}.mp3')
            results.append({'voice_index': index, 'frames': details['frames'], 'mp3_bytes': output.stat().st_size})
    acknowledged = []
    playback = preview_generated_audio('Проверка звука завершена.', extra_voice_names()[0],
        DEFAULT_AUDIO_OUTPUT_LABEL, 0, 0, 70, threading.Event(), threading.Event(),
        segment_done_callback=acknowledged.append)
    if len(acknowledged) != 1 or not acknowledged[0].get('playback_verified'):
        raise RuntimeError('No verified playback acknowledgement.')
    (Path(sys.executable).parent / 'voice-self-test.json').write_text(
        json.dumps({'results': results, 'playback_completed': playback['completed_chunks']}), encoding='utf-8')
    return 0


def run_gui_self_test() -> int:
    # Custom hooks precede PyInstaller's standard Tk runtime hook. Mirror its
    # private-data paths only for this explicit test; normal startup is unchanged.
    if getattr(sys, 'frozen', False):
        os.environ['TCL_LIBRARY'] = str(Path(sys._MEIPASS) / '_tcl_data')
        os.environ['TK_LIBRARY'] = str(Path(sys._MEIPASS) / '_tk_data')
    import tkinter as tk
    import time
    from tts_app.app import TTSApp
    from tts_app import app_workspace_init
    app_workspace_init.windows_autostart_is_enabled = lambda: False
    root = tk.Tk()
    app = TTSApp(root)
    try:
        root.geometry('900x680')
        root.update()
        if 'ZeTer Voice' not in root.title():
            raise RuntimeError('Unexpected product title.')
        manager = app.engine_manager
        manager.frame.master.select(manager.frame)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        if not manager.frame.winfo_viewable() or not all(button.winfo_viewable() for button in manager.buttons):
            raise RuntimeError('Engine controls are not visible.')
        report = {'title': root.title(), 'text_tabs': len(app.tabs),
                  'engine_page_visible': True, 'voice_count': len(app.voices)}
        (Path(sys.executable).parent / 'gui-self-test.json').write_text(json.dumps(report), encoding='utf-8')
    finally:
        app.on_close()
    return 0


if "--self-test-gui" in sys.argv:
    raise SystemExit(run_gui_self_test())
if "--self-test-voices" in sys.argv:
    raise SystemExit(run_engine_self_test())
if "--self-test" in sys.argv:
    raise SystemExit(run_packaged_self_test())
