"""Compatibility entry point for Text → MP3.

Implementation lives in the small modules under :mod:`tts_app`.  Keep this file
small: Codex and other agents should route changes through ``docs/CODEMAP.md``.
"""

from tts_app import *  # noqa: F401,F403 - legacy public surface
from tts_app.runtime import _ATOMIC_WRITE_LOCK, _is_transient_atomic_replace_error
from tts_app.text_processing import _split_oversized_piece, _split_sapi_xml_adjustment
from tts_app.ffmpeg_tools import _parse_ffmpeg_clock


if __name__ == "__main__":
    main()
