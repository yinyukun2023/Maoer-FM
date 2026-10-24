from __future__ import annotations

import os
import io
import threading
import wave
from pathlib import Path

if os.name == "nt":
    import winsound
else:
    winsound = None


STARTUP_SOUND = Path(__file__).resolve().parent / "assets" / "startup_mia.wav"
DOWNLOAD_COMPLETED_SOUND = Path(__file__).resolve().parent / "assets" / "DownloadCompleted.wav"
DOWNLOAD_FAILED_SOUND = Path(__file__).resolve().parent / "assets" / "DownloadFailed.wav"


def _mixed_download_wave() -> bytes:
    """Mix both shipped PCM cues at their common sample rate, without clipping."""
    tracks = []
    rate = None
    for path in (DOWNLOAD_COMPLETED_SOUND, DOWNLOAD_FAILED_SOUND):
        with wave.open(str(path), 'rb') as source:
            if rate is not None and source.getframerate() != rate:
                raise ValueError('Download cues must have the same sample rate')
            rate = source.getframerate()
            width, channels = source.getsampwidth(), source.getnchannels()
            if width not in (2, 3) or channels not in (1, 2):
                raise ValueError('Unsupported download cue format')
            data = source.readframes(source.getnframes())
            samples = [int.from_bytes(data[i:i + width], 'little', signed=True) >> (8 * (width - 2))
                       for i in range(0, len(data), width)]
            if channels == 1:
                samples = [sample for sample in samples for _ in range(2)]
            tracks.append(samples)
    pcm = bytearray()
    for index in range(max(map(len, tracks))):
        value = sum(track[index] if index < len(track) else 0 for track in tracks) // 2
        pcm.extend(value.to_bytes(2, 'little', signed=True))
    output = io.BytesIO()
    with wave.open(output, 'wb') as mixed:
        mixed.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
        mixed.writeframes(pcm)
    return output.getvalue()


def play_download_mixed_sound() -> None:
    """Play both cues together; separate async PlaySound calls cancel each other."""
    if winsound is None:
        return
    def play() -> None:
        try:
            winsound.PlaySound(_mixed_download_wave(), winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except (OSError, ValueError, RuntimeError, wave.Error):
            pass
    threading.Thread(target=play, daemon=True, name='download-result-sound').start()


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
