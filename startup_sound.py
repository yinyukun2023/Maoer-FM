from __future__ import annotations

import os
from pathlib import Path

if os.name == "nt":
    import winsound
else:
    winsound = None


STARTUP_SOUND = Path(__file__).resolve().parent / "assets" / "startup_mia.wav"
DOWNLOAD_COMPLETED_SOUND = Path(__file__).resolve().parent / "assets" / "DownloadCompleted.wav"
DOWNLOAD_FAILED_SOUND = Path(__file__).resolve().parent / "assets" / "DownloadFailed.wav"


def play_startup_sound() -> None:
    """Start the original cat sound without blocking the main window."""
    _play_sound(STARTUP_SOUND)


def play_download_completed_sound() -> None:
    """Play the completion cue once without blocking the download dialog."""
    _play_sound(DOWNLOAD_COMPLETED_SOUND)


def play_download_failed_sound() -> None:
    """An independent failure cue; never use a screen reader or system alert."""
    _play_sound(DOWNLOAD_FAILED_SOUND)


def _play_sound(path: Path) -> None:
    if winsound is None or not path.is_file():
        return
    try:
        winsound.PlaySound(
            str(path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )
    except RuntimeError:
        # A missing audio device must not interrupt the operation being announced.
        pass
