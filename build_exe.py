from __future__ import annotations

import argparse
from datetime import date
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
SPEC_FILE = ROOT / "maoer.spec"
UPDATER_ENTRY_FILE = ROOT / "updater_entry.py"
BUILD_INFO_FILE = ROOT / "_build_info.py"
DIST_DIR = ROOT / "dist"
OUTPUT_EXE = DIST_DIR / "猫耳FM.exe"
UPDATER_EXE = DIST_DIR / "updater.exe"
FULL_PACKAGE_DIR = DIST_DIR / "完整包"
UPDATE_PACKAGE_DIR = DIST_DIR / "更新包"
UPDATE_CONFIG_NAME = "update-config.json"
UPDATE_UPLOAD_DIR_NAME = "updates"
UPDATE_PACKAGE_UPLOAD_DIR = UPDATE_PACKAGE_DIR / UPDATE_UPLOAD_DIR_NAME
UPDATE_MANIFEST_URL = f"https://update.1630.org/maoer-fm/{UPDATE_CONFIG_NAME}"
UPDATE_DOWNLOAD_BASE_URL = "https://update.1630.org/maoer-fm/updates/"
UPDATE_TEXT_NAME = "update.txt"
HOTKEYS_TEXT_NAME = "热键表.txt"
BUNDLED_TEXT_NAMES = (HOTKEYS_TEXT_NAME, UPDATE_TEXT_NAME)
SERVER_APP_FILENAME = "Maoer-FM.exe"
APP_EXE_TARGET = "{app_exe}"
RELEASE_EXE_PREFIX = "maoer-fm"
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
SERVER_MANIFEST_TIMEOUT = 10


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
    parser.add_argument(
        "--version",
        help="Write this version into the app before building.",
    )
    parser.add_argument(
        "--version-mode",
        choices=("auto", "manual"),
        help="Choose version source without the interactive prompt.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Use the current date as version and build without the version prompt.",
    )
    parser.add_argument(
        "--notes",
        default="",
        help="Release notes written into dist/更新包/update-config.json.",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Open a wxPython build window for entering release notes.",
    )
    return parser.parse_args()


def auto_version() -> str:
    today = date.today()
    return f"{today.year}.{today.month}.{today.day}"


def validate_version(version: str) -> str:
    version = version.strip()
    if not version:
        raise SystemExit("Version cannot be empty.")
    if not VERSION_RE.match(version):
        raise SystemExit("Version can only contain letters, numbers, dot, underscore, plus and hyphen.")
    return version


def choose_build_version(args: argparse.Namespace) -> str:
    if args.version:
        return validate_version(args.version)

    if args.auto:
        return auto_version()

    if args.version_mode == "auto":
        return auto_version()
    if args.version_mode == "manual":
        return read_manual_version()

    if not sys.stdin.isatty():
        version = auto_version()
        print(f"No interactive input available. Using date version: {version}")
        return version

    default_version = auto_version()
    print("请选择打包版本号写入方式：")
    print(f"1. 自动获取日期版本号（{default_version}）")
    print("2. 手动输入版本号")
    choice = input("请输入 1 或 2 [1]: ").strip() or "1"
    if choice == "1":
        return default_version
    if choice == "2":
        return read_manual_version()
    raise SystemExit("Invalid choice. Please enter 1 or 2.")


def read_manual_version() -> str:
    if not sys.stdin.isatty():
        raise SystemExit("--version-mode manual requires interactive input or --version.")
    return validate_version(input("请输入版本号，例如 2026.5.3: "))


def write_build_info(version: str) -> None:
    build_date = date.today().isoformat()
    content = (
        "from __future__ import annotations\n\n"
        f"APP_VERSION = {version!r}\n"
        f"BUILD_DATE = {build_date!r}\n"
        f"UPDATE_MANIFEST_URL = {UPDATE_MANIFEST_URL!r}\n"
    )
    BUILD_INFO_FILE.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote app version {version} to {BUILD_INFO_FILE.name}")


def release_exe_name(version: str) -> str:
    safe_version = re.sub(r"[^0-9A-Za-z._+-]+", "_", version)
    return f"{RELEASE_EXE_PREFIX}-{safe_version}.exe"


def update_file_url(relative_path: str) -> str:
    escaped = quote(relative_path.replace("\\", "/"), safe="/._+-")
    return f"{UPDATE_DOWNLOAD_BASE_URL}{escaped}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_updater_exe(pyinstaller: object) -> None:
    if not UPDATER_ENTRY_FILE.exists():
        raise SystemExit(f"Missing updater entry file: {UPDATER_ENTRY_FILE}")
    print(f"Building updater helper from {UPDATER_ENTRY_FILE.name}...")
    pyinstaller.run(
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            "updater",
            str(UPDATER_ENTRY_FILE),
        ]
    )
    if not UPDATER_EXE.exists():
        raise SystemExit(f"Updater build finished, but output was not found: {UPDATER_EXE}")


def update_sources() -> list[dict[str, str | Path]]:
    sources: list[dict[str, str | Path]] = [
        {
            "source": OUTPUT_EXE,
            "server_path": SERVER_APP_FILENAME,
            "target": APP_EXE_TARGET,
        },
        {
            "source": UPDATER_EXE,
            "server_path": UPDATER_EXE.name,
            "target": UPDATER_EXE.name,
        },
    ]
    for filename in BUNDLED_TEXT_NAMES:
        sources.append(
            {
                "source": ROOT / filename,
                "server_path": filename,
                "target": filename,
            }
        )
    return sources


def file_entry(source: Path, server_path: str, target: str) -> dict[str, object]:
    return {
        "path": server_path.replace("\\", "/"),
        "target": target.replace("\\", "/"),
        "url": update_file_url(server_path),
        "sha256": file_sha256(source),
        "size": source.stat().st_size,
    }


def build_update_config(version: str, notes: str) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for item in update_sources():
        source = Path(item["source"])
        if not source.exists():
            raise SystemExit(f"Missing update file: {source}")
        files.append(file_entry(source, str(item["server_path"]), str(item["target"])))
    return {
        "enabled": True,
        "version": version,
        "notes": notes,
        "files": files,
    }


def fetch_server_config() -> dict[str, object] | None:
    request = Request(
        UPDATE_MANIFEST_URL,
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urlopen(request, timeout=SERVER_MANIFEST_TIMEOUT) as response:
            data = response.read().decode("utf-8")
    except (OSError, URLError, TimeoutError) as exc:
        print(f"Cannot read server update config, generating full update package: {exc}")
        return None
    try:
        manifest = json.loads(data)
    except json.JSONDecodeError as exc:
        print(f"Server update config is invalid JSON, generating full update package: {exc}")
        return None
    if not isinstance(manifest, dict):
        print("Server update config is not an object, generating full update package.")
        return None
    return manifest


def server_file_hashes(manifest: dict[str, object] | None) -> dict[str, str]:
    if manifest is None:
        return {}
    hashes: dict[str, str] = {}
    files = manifest.get("files")
    if not isinstance(files, list):
        return hashes
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("target") or "").replace("\\", "/").strip()
        sha256 = str(item.get("sha256") or item.get("hash") or "").lower().strip()
        if path and sha256:
            hashes[path] = sha256
    return hashes


def copy_package_file(source: Path, package_dir: Path, server_path: str) -> None:
    target = package_dir / server_path.replace("\\", "/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_full_package_files() -> int:
    if FULL_PACKAGE_DIR.exists():
        remove_path(FULL_PACKAGE_DIR)
    FULL_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)

    copied = 0
    for item in update_sources():
        copy_package_file(Path(item["source"]), FULL_PACKAGE_DIR, str(item["server_path"]))
        copied += 1
    print(f"完整包文件夹已生成 {copied} 个文件。")
    return copied


def copy_update_package_files(update_config: dict[str, object], server_manifest: dict[str, object] | None) -> int:
    if UPDATE_PACKAGE_DIR.exists():
        remove_path(UPDATE_PACKAGE_DIR)
    UPDATE_PACKAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    remote_hashes = server_file_hashes(server_manifest)
    copied = 0
    for item in update_sources():
        source = Path(item["source"])
        server_path = str(item["server_path"]).replace("\\", "/")
        entry = next(
            file_item
            for file_item in update_config["files"]
            if isinstance(file_item, dict) and file_item["path"] == server_path
        )
        if remote_hashes and remote_hashes.get(server_path) == str(entry["sha256"]).lower():
            continue
        copy_package_file(source, UPDATE_PACKAGE_UPLOAD_DIR, server_path)
        copied += 1
    if not remote_hashes:
        print("更新包文件夹已生成全部文件。")
    elif copied:
        print(f"更新包文件夹已生成 {copied} 个差异文件。")
    else:
        print("服务器文件已是最新，更新包文件夹为空。")
    return copied


def write_release_artifacts(version: str, notes: str = "") -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if not UPDATER_EXE.exists():
        raise SystemExit(f"Missing updater executable: {UPDATER_EXE}")

    update_config = build_update_config(version, notes)
    server_manifest = fetch_server_config()
    full_count = copy_full_package_files()
    copied = copy_update_package_files(update_config, server_manifest)

    config_path = UPDATE_PACKAGE_DIR / UPDATE_CONFIG_NAME
    config_path.write_text(json.dumps(update_config, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    update_text_path = copy_update_text_file()

    print(f"Wrote update config: {config_path}")
    if update_text_path is None:
        print(f"No {UPDATE_TEXT_NAME} found, skipped.")
    print(f"Wrote full package folder: {FULL_PACKAGE_DIR} ({full_count} files)")
    print(f"Wrote update package folder: {UPDATE_PACKAGE_DIR} ({copied} update files)")


def cleanup_dist_surface_outputs() -> None:
    for path in (
        OUTPUT_EXE,
        UPDATER_EXE,
        DIST_DIR / SERVER_APP_FILENAME,
        DIST_DIR / UPDATE_CONFIG_NAME,
    ):
        if path.exists():
            remove_path(path)


def copy_update_text_file() -> Path | None:
    source = ROOT / UPDATE_TEXT_NAME
    if not source.exists():
        return None
    target = UPDATE_PACKAGE_DIR / UPDATE_TEXT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    print(f"Wrote update text: {target}")
    return target


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
    if DIST_DIR.exists():
        for old_exe in DIST_DIR.glob(f"{RELEASE_EXE_PREFIX}-*.exe"):
            remove_path(old_exe)
        for old_zip in DIST_DIR.glob(f"{RELEASE_EXE_PREFIX}-*.zip"):
            remove_path(old_zip)
        old_update_dir = DIST_DIR / "update"
        if old_update_dir.exists():
            remove_path(old_update_dir)
        for package_dir in (FULL_PACKAGE_DIR, UPDATE_PACKAGE_DIR):
            if package_dir.exists():
                remove_path(package_dir)
        old_config = DIST_DIR / UPDATE_CONFIG_NAME
        if old_config.exists():
            remove_path(old_config)


def launch_build_ui() -> int:
    try:
        import wx
    except ImportError as exc:
        raise SystemExit("wxPython is not installed. Install it with: python -m pip install wxPython") from exc

    class NamedTextAccessible(wx.Accessible):
        def __init__(self, window: wx.Window, name: str) -> None:
            super().__init__(window)
            self._name = name

        def GetName(self, child_id: int) -> tuple[int, str]:
            return wx.ACC_OK, self._name

        def GetDescription(self, child_id: int) -> tuple[int, str]:
            return wx.ACC_OK, self._name

        def GetHelpText(self, child_id: int) -> tuple[int, str]:
            return wx.ACC_OK, self._name

        def GetRole(self, child_id: int) -> tuple[int, int]:
            return wx.ACC_OK, wx.ROLE_SYSTEM_TEXT

        def GetValue(self, child_id: int) -> tuple[int, str]:
            window = self.GetWindow()
            if hasattr(window, "GetValue"):
                return wx.ACC_OK, str(window.GetValue())
            return wx.ACC_NOT_IMPLEMENTED, ""

    class BuildFrame(wx.Frame):
        def __init__(self) -> None:
            super().__init__(None, title="猫耳FM 打包工具", size=(560, 360))
            self._running = False
            self._accessible_objects: list[wx.Accessible] = []

            panel = wx.Panel(self)
            root = wx.BoxSizer(wx.VERTICAL)

            form = wx.FlexGridSizer(cols=2, hgap=10, vgap=8)
            form.AddGrowableCol(1, 1)

            self.version = wx.TextCtrl(panel, value=auto_version())
            self.notes = wx.TextCtrl(panel, style=wx.TE_MULTILINE)
            self.status_label = wx.StaticText(panel, label="等待生成")
            self._set_accessible_label(self.version, "版本号")
            self._set_accessible_label(self.notes, "更新内容")

            self._add_row(form, panel, "版本号", self.version)
            self._add_row(form, panel, "更新内容", self.notes)
            self._add_row(form, panel, "状态", self.status_label)
            root.Add(form, 1, wx.EXPAND | wx.ALL, 12)

            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.build_button = wx.Button(panel, label="确定")
            buttons.AddStretchSpacer()
            buttons.Add(self.build_button, 0)
            root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

            panel.SetSizer(root)
            self.Centre()

            self.build_button.Bind(wx.EVT_BUTTON, self._on_build)
            self.Bind(wx.EVT_CLOSE, self._on_close)

        def _add_row(
            self,
            sizer: wx.FlexGridSizer,
            parent: wx.Window,
            label: str,
            control: wx.Window,
        ) -> None:
            sizer.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_TOP | wx.TOP, 3)
            sizer.Add(control, 1, wx.EXPAND)

        def _set_accessible_label(self, control: wx.Window, label: str) -> None:
            control.SetName(label)
            control.SetToolTip(label)
            control.SetHelpText(label)
            accessible = NamedTextAccessible(control, label)
            control.SetAccessible(accessible)
            self._accessible_objects.append(accessible)

        def _on_build(self, event: wx.CommandEvent) -> None:
            if self._running:
                return
            try:
                version = validate_version(self.version.GetValue())
            except SystemExit as exc:
                wx.MessageBox(str(exc), "版本号无效", wx.OK | wx.ICON_ERROR, self)
                return

            self._running = True
            self.build_button.Disable()
            self.version.Disable()
            self.notes.Disable()
            self.status_label.SetLabel("正在生成文件，请稍候...")
            self.Layout()

            notes = self.notes.GetValue().strip()
            thread = threading.Thread(target=self._run_build, args=(version, notes), daemon=True)
            thread.start()

        def _run_build(self, version: str, notes: str) -> None:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--version",
                version,
                "--notes",
                notes,
            ]
            try:
                creation_flags = 0
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creation_flags = subprocess.CREATE_NO_WINDOW
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation_flags,
                )
            except OSError as exc:
                wx.CallAfter(self._build_failed, f"启动打包失败：\n{exc}")
                return

            if completed.returncode != 0:
                output = (completed.stdout or "").strip()
                tail = "\n".join(output.splitlines()[-20:])
                message = f"生成失败，退出码：{completed.returncode}"
                if tail:
                    message = f"{message}\n\n{tail}"
                wx.CallAfter(self._build_failed, message)
                return

            wx.CallAfter(self._build_succeeded)

        def _build_succeeded(self) -> None:
            self._running = False
            self.build_button.Enable()
            self.version.Enable()
            self.notes.Enable()
            self.status_label.SetLabel("生成成功")
            self.Layout()
            wx.MessageBox(
                (
                    "文件生成成功。\n\n"
                    f"更新配置：{UPDATE_PACKAGE_DIR / UPDATE_CONFIG_NAME}\n"
                    f"更新文件：{UPDATE_PACKAGE_UPLOAD_DIR}\n"
                    f"完整包：{FULL_PACKAGE_DIR}"
                ),
                "生成成功",
                wx.OK | wx.ICON_INFORMATION,
                self,
            )

        def _build_failed(self, message: str) -> None:
            self._running = False
            self.build_button.Enable()
            self.version.Enable()
            self.notes.Enable()
            self.status_label.SetLabel("生成失败")
            self.Layout()
            wx.MessageBox(message, "生成失败", wx.OK | wx.ICON_ERROR, self)

        def _on_close(self, event: wx.CloseEvent) -> None:
            if self._running:
                wx.MessageBox("正在生成文件，请稍候。", "正在生成", wx.OK | wx.ICON_INFORMATION, self)
                if event.CanVeto():
                    event.Veto()
                return
            event.Skip()

    app = wx.App(False)
    frame = BuildFrame()
    frame.Show()
    app.MainLoop()
    return 0


def main() -> int:
    args = parse_args()
    if args.ui or len(sys.argv) == 1:
        return launch_build_ui()

    if not SPEC_FILE.exists():
        raise SystemExit(f"Missing spec file: {SPEC_FILE}")

    os.chdir(ROOT)
    build_version = choose_build_version(args)
    write_build_info(build_version)
    pyinstaller = ensure_pyinstaller(auto_install=not args.no_install)

    print(f"Building single-file exe from {SPEC_FILE.name}...")
    pyinstaller.run(["--noconfirm", "--clean", str(SPEC_FILE)])

    if not OUTPUT_EXE.exists():
        raise SystemExit(f"Build finished, but output was not found: {OUTPUT_EXE}")

    build_updater_exe(pyinstaller)
    cleanup_build_artifacts()
    write_release_artifacts(build_version, notes=args.notes)
    cleanup_dist_surface_outputs()
    print(f"Build complete: {UPDATE_PACKAGE_DIR}")
    print("Upload the contents of this folder:")
    print(f"- {UPDATE_PACKAGE_DIR}")
    print(f"- {UPDATE_PACKAGE_DIR / UPDATE_CONFIG_NAME} -> {UPDATE_MANIFEST_URL}")
    print(f"- {UPDATE_PACKAGE_UPLOAD_DIR}\\* -> {UPDATE_DOWNLOAD_BASE_URL}")
    print(f"Full package: {FULL_PACKAGE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
