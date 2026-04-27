from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


APP_DIR_NAME = "Maoer-FM"


def app_data_dir(create: bool = True) -> Path:
    base = os.environ.get("APPDATA")
    if base:
        root = Path(base)
    elif os.name == "nt":
        root = Path.home() / "AppData" / "Roaming"
    else:
        root = Path.home() / ".config"

    path = root / APP_DIR_NAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def cookie_path(filename: str = "cookey", create_parent: bool = True) -> Path:
    return app_data_dir(create_parent) / filename


def webview2_profile_dir(create: bool = True) -> Path:
    path = app_data_dir(create) / "webview2_profile"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def clear_webview2_profile() -> None:
    path = webview2_profile_dir(create=False)
    if not path.exists():
        return

    for attempt in range(4):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 3:
                shutil.rmtree(path, ignore_errors=True)
                return
            time.sleep(0.2)
