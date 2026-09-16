"""PyInstaller runtime smoke test for the packaged Windows application.

At normal startup this hook is inert. With ``--self-test`` it runs before the
large GUI module, proving that the frozen runtime contains pywin32/COM support
and the bundled imageio-ffmpeg executable, then exits without opening Tkinter.
"""

from __future__ import annotations

import os
import subprocess
import sys


def run_packaged_self_test() -> int:
    if os.name != "nt":
        raise RuntimeError("Text-to-MP3 packaged runtime is Windows-only.")

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
        pythoncom.CoUninitialize()

    version_line = (probe.stdout or probe.stderr or "").splitlines()[0]
    print(f"packaged self-test: ok | voices={voice_count} | {version_line}")
    return 0


if "--self-test" in sys.argv:
    raise SystemExit(run_packaged_self_test())
