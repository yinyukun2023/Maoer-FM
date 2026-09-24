from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

from app_paths import app_data_dir


SETTINGS_FILENAME = "settings.json"


@dataclass(frozen=True)
class AppSettings:
    startup_sound: bool = True
    read_subtitle: bool = False
    read_danmaku: bool = False


def load_settings() -> AppSettings:
    path = app_data_dir(create=False) / SETTINGS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return AppSettings()
    if not isinstance(data, dict):
        return AppSettings()

    defaults = AppSettings()
    values = {
        name: value if isinstance(value, bool) else getattr(defaults, name)
        for name in asdict(defaults)
        for value in (data.get(name),)
    }
    return AppSettings(**values)


def save_settings(settings: AppSettings) -> None:
    directory = app_data_dir()
    path = directory / SETTINGS_FILENAME
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix="settings-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(asdict(settings), temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
