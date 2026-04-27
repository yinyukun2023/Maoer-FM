from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SPEC_FILE = ROOT / "maoer.spec"
OUTPUT_EXE = ROOT / "dist" / "猫耳FM.exe"


def run_command(command: list[str]) -> None:
    print("+ " + subprocess.list2cmdline(command))
    subprocess.run(command, cwd=ROOT, check=True)


def ensure_pyinstaller(auto_install: bool) -> object:
    try:
        return importlib.import_module("PyInstaller.__main__")
    except ImportError:
        if not auto_install:
            raise SystemExit(
                "PyInstaller is not installed. Run this script without --no-install, "
                "or install it with: python -m pip install pyinstaller"
            )

    print("PyInstaller is not installed. Installing it with pip...")
    run_command([sys.executable, "-m", "pip", "install", "pyinstaller"])

    try:
        return importlib.import_module("PyInstaller.__main__")
    except ImportError as exc:
        raise SystemExit("PyInstaller installation finished, but it still cannot be imported.") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Maoer FM as a single Windows exe.")
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Do not install PyInstaller automatically when it is missing.",
    )
    return parser.parse_args()


def remove_path(path: Path) -> None:
    if not path.exists():
        return

    resolved = path.resolve()
    if resolved == ROOT or ROOT not in resolved.parents:
        raise RuntimeError(f"Refusing to delete outside project root: {resolved}")

    if resolved.is_dir():
        shutil.rmtree(resolved)
    else:
        resolved.unlink()
    print(f"Removed: {resolved}")


def cleanup_build_artifacts() -> None:
    print("Cleaning build artifacts...")
    remove_path(ROOT / "build")
    remove_path(ROOT / "__pycache__")
    if OUTPUT_EXE.parent.exists():
        for old_exe in OUTPUT_EXE.parent.glob("*.exe"):
            if old_exe.resolve() != OUTPUT_EXE.resolve():
                remove_path(old_exe)


def main() -> int:
    args = parse_args()
    if not SPEC_FILE.exists():
        raise SystemExit(f"Missing spec file: {SPEC_FILE}")

    os.chdir(ROOT)
    pyinstaller = ensure_pyinstaller(auto_install=not args.no_install)

    print(f"Building single-file exe from {SPEC_FILE.name}...")
    pyinstaller.run(["--noconfirm", "--clean", str(SPEC_FILE)])

    if not OUTPUT_EXE.exists():
        raise SystemExit(f"Build finished, but output was not found: {OUTPUT_EXE}")

    cleanup_build_artifacts()
    print(f"Build complete: {OUTPUT_EXE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
