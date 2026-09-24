from __future__ import annotations

import os
from pathlib import Path

if os.name == "nt":
    import winsound
else:
    winsound = None


STARTUP_SOUND = Path(__file__).resolve().parent / "assets" / "startup_mia.wav"


def play_startup_sound() -> None:
    """Start the original cat sound without blocking the main window."""
    if winsound is None or not STARTUP_SOUND.is_file():
        return
    try:
        winsound.PlaySound(
            str(STARTUP_SOUND),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
    except RuntimeError:
        # Audio device problems must not prevent the rest of the app from starting.
        pass
