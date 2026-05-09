from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import wx

from _build_info import APP_VERSION, UPDATE_MANIFEST_URL
from app_paths import app_data_dir


UPDATE_ARGUMENT = "--apply-update"
SKIP_UPDATE_ARGUMENT = "--skip-update-check"
UPDATE_DIR_NAME = "updates"
REQUEST_TIMEOUT = (5, 25)
DOWNLOAD_CHUNK_SIZE = 1024 * 256
APP_NAME = "猫耳FM"


@dataclass(frozen=True)
class UpdateFile:
    path: str
    target: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    notes: str
    files: list[UpdateFile]


class UpdateCancelled(RuntimeError):
    """Raised when the user confirms cancelling a forced update download."""


def _is_wx_main_thread() -> bool:
    is_main_thread = getattr(wx, "IsMainThread", None)
    if callable(is_main_thread):
        return bool(is_main_thread())
    return threading.current_thread() is threading.main_thread()


class UpdateDownloadDialog(wx.Dialog):
    def __init__(self, parent: wx.Window | None, update: UpdateInfo) -> None:
        style = wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP
        close_box = getattr(wx, "CLOSE_BOX", 0)
        if close_box:
            style &= ~close_box
        super().__init__(parent, title="正在更新", style=style)
        self._cancel_requested = False
        self._allow_close = False

        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        self.status_label = wx.StaticText(panel, label="正在下载更新文件...")
        self.progress = wx.Gauge(panel, range=100)
        self.progress_label = wx.StaticText(panel, label="总进度：0%")
        self.cancel_button = wx.Button(panel, wx.ID_CANCEL, label="取消")

        main_sizer.Add(self.status_label, 0, wx.EXPAND | wx.ALL, 12)
        main_sizer.Add(self.progress, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        main_sizer.Add(self.progress_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        button_sizer.AddStretchSpacer()
        button_sizer.Add(self.cancel_button, 0)
        main_sizer.Add(button_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(main_sizer)
        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(frame_sizer)
        self.SetMinSize((380, -1))
        self.Fit()
        self.CentreOnParent()

        self.cancel_button.Bind(wx.EVT_BUTTON, self._on_cancel)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def mark_preparing(self) -> None:
        if not _is_wx_main_thread():
            wx.CallAfter(self.mark_preparing)
            return
        self.status_label.SetLabel("正在准备更新...")
        self.progress.Pulse()
        self.progress_label.SetLabel("总进度：准备中")
        self.Layout()

    def mark_downloading(self) -> None:
        if not _is_wx_main_thread():
            wx.CallAfter(self.mark_downloading)
            return
        self.status_label.SetLabel("正在下载更新文件...")
        self.cancel_button.Enable()
        self.Layout()

    def update_progress(self, percent: int | None) -> None:
        if not _is_wx_main_thread():
            wx.CallAfter(self.update_progress, percent)
            return
        if percent is None:
            self.progress.Pulse()
            self.progress_label.SetLabel("总进度：正在下载")
        else:
            value = max(0, min(100, percent))
            self.progress.SetValue(value)
            self.progress_label.SetLabel(f"总进度：{value}%")
        self.Layout()
        wx.YieldIfNeeded()

    def bring_to_front(self) -> None:
        if not _is_wx_main_thread():
            wx.CallAfter(self.bring_to_front)
            return
        self.Show()
        self.Raise()
        self.SetFocus()
        self.RequestUserAttention(wx.USER_ATTENTION_INFO)
        if os.name == "nt":
            handle = int(self.GetHandle())
            if handle:
                ctypes.windll.user32.ShowWindow(handle, 9)
                ctypes.windll.user32.SetForegroundWindow(handle)
        wx.YieldIfNeeded()

    def mark_complete(self) -> None:
        if not _is_wx_main_thread():
            wx.CallAfter(self.mark_complete)
            return
        self.progress.SetValue(100)
        self.progress_label.SetLabel("总进度：100%")
        self.status_label.SetLabel("更新完成。")
        self.cancel_button.Disable()
        self._allow_close = True
        self.Layout()
        wx.YieldIfNeeded()

    def mark_installing(self) -> None:
        if not _is_wx_main_thread():
            wx.CallAfter(self.mark_installing)
            return
        self.status_label.SetLabel("正在安装更新...")
        self.progress.Pulse()
        self.progress_label.SetLabel("总进度：正在安装")
        self.cancel_button.Enable()
        self.Layout()
        wx.YieldIfNeeded()

    def _on_cancel(self, event: wx.CommandEvent) -> None:
        self._confirm_cancel()

    def _on_close(self, event: wx.CloseEvent) -> None:
        if self._allow_close:
            event.Skip()
            return
        self._confirm_cancel()
        if event.CanVeto():
            event.Veto()

    def _confirm_cancel(self) -> None:
        if self._cancel_requested:
            return
        dialog = wx.MessageDialog(
            self,
            "更新正在进行，确定要终止本次更新吗？",
            "取消更新",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        try:
            result = dialog.ShowModal()
        finally:
            dialog.Destroy()
        if result == wx.ID_YES:
            self._cancel_requested = True
            self.status_label.SetLabel("正在取消本次更新...")
            self.cancel_button.Disable()
            self.Layout()


def handle_update_cli(argv: list[str]) -> int | None:
    if len(argv) < 3 or argv[1] != UPDATE_ARGUMENT:
        return None
    return apply_update_from_state(Path(argv[2]))


def run_startup_update_check() -> bool:
    if SKIP_UPDATE_ARGUMENT in sys.argv:
        return True
    if os.environ.get("MAOER_SKIP_UPDATE_CHECK"):
        return True
    if not _running_from_frozen_app() and not os.environ.get("MAOER_ENABLE_SOURCE_UPDATE"):
        return True

    try:
        update = check_for_update()
    except Exception as exc:
        _debug_log(f"update check skipped: {type(exc).__name__}: {exc}")
        return True

    if update is None:
        return True

    try:
        state_path = prepare_update_state(update)
        launch_update_helper(state_path)
    except UpdateCancelled:
        return False
    except Exception as exc:
        wx.MessageBox(
            f"更新失败，程序将退出。请重新打开后重试。\n\n{exc}",
            "更新失败",
            wx.OK | wx.ICON_ERROR,
        )
        return False

    return False


def check_for_update() -> UpdateInfo | None:
    response = requests.get(
        UPDATE_MANIFEST_URL,
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    manifest = response.json()
    if not isinstance(manifest, dict):
        raise ValueError("更新配置不是 JSON 对象")
    if manifest.get("enabled") is False:
        return None

    latest_version = str(manifest.get("version") or "").strip()
    if not latest_version:
        raise ValueError("更新配置缺少 version")

    files = _select_update_files(manifest)
    if not files:
        raise ValueError("更新配置缺少 files 文件清单")

    notes = str(manifest.get("notes") or manifest.get("message") or "").strip()
    changed_files = _changed_update_files(files)
    if not _is_version_newer(latest_version, APP_VERSION) and not changed_files:
        return None
    if not changed_files:
        return None
    return UpdateInfo(latest_version, notes, changed_files)


def download_update_files(update: UpdateInfo, progress: UpdateDownloadDialog | None = None) -> list[dict[str, str]]:
    update_dir = _updates_dir(update.version)
    update_dir.mkdir(parents=True, exist_ok=True)
    download_dir = update_dir / "files"
    if download_dir.exists():
        shutil.rmtree(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    total_size = sum(file.size for file in update.files)
    use_byte_progress = total_size > 0 and all(file.size > 0 for file in update.files)
    downloaded_size = 0
    completed_files = 0
    downloaded_files: list[dict[str, str]] = []

    owns_progress = progress is None
    if progress is None:
        progress = UpdateDownloadDialog(None, update)
    if owns_progress:
        progress.Show()
        progress.bring_to_front()
    progress.mark_downloading()
    progress.update_progress(0)
    last_percent = -1
    try:
        for update_file in update.files:
            if progress.cancel_requested:
                raise UpdateCancelled("用户取消更新")
            relative_path = update_file.path or Path(urlparse(update_file.url).path).name
            download_path = _safe_child(download_dir, relative_path)
            download_path.parent.mkdir(parents=True, exist_ok=True)

            with requests.get(update_file.url, stream=True, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                with download_path.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if progress.cancel_requested:
                            raise UpdateCancelled("用户取消更新")
                        if not chunk:
                            continue
                        file.write(chunk)
                        if use_byte_progress:
                            downloaded_size += len(chunk)
                            percent = min(99, int(downloaded_size * 100 / total_size))
                            if percent != last_percent:
                                last_percent = percent
                                progress.update_progress(percent)

            _verify_sha256(download_path, update_file.sha256)
            completed_files += 1
            if not use_byte_progress:
                progress.update_progress(int(completed_files * 100 / len(update.files)))
            downloaded_files.append(
                {
                    "path": str(download_path),
                    "target": update_file.target,
                    "sha256": update_file.sha256,
                }
            )
        if owns_progress:
            progress.mark_complete()
    except UpdateCancelled:
        shutil.rmtree(download_dir, ignore_errors=True)
        raise
    finally:
        if owns_progress:
            progress.Destroy()

    return downloaded_files


def prepare_update_state(update: UpdateInfo) -> Path:
    target_exe = _target_executable()
    install_dir = target_exe.parent
    update_dir = _updates_dir(update.version)
    update_dir.mkdir(parents=True, exist_ok=True)
    bundled_helper = target_exe.with_name("updater.exe")
    helper_path = str(bundled_helper) if bundled_helper.exists() else ""
    state = {
        "version": update.version,
        "current_version": APP_VERSION,
        "target_exe": str(target_exe),
        "install_dir": str(install_dir),
        "download_files": [
            {
                "path": file.path,
                "target": file.target,
                "url": file.url,
                "sha256": file.sha256,
                "size": file.size,
            }
            for file in update.files
        ],
        "files": [],
        "helper_path": helper_path,
        "backup_dir": str(update_dir / f"backup-{int(time.time())}"),
        "notes": update.notes,
        "parent_pid": os.getpid(),
        "launch_args": _normal_launch_args(sys.argv[1:]),
    }
    state_path = update_dir / "update-state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state_path


def _download_update_info_from_state(state: dict[str, Any]) -> UpdateInfo:
    version = str(state.get("version") or "").strip()
    if not version:
        raise ValueError("更新状态缺少 version")
    raw_files = state.get("download_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("更新状态缺少下载文件清单")

    files: list[UpdateFile] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("更新状态包含无效下载文件项")
        path = str(item.get("path") or "").strip().replace("\\", "/")
        target = str(item.get("target") or path).strip().replace("\\", "/")
        url = str(item.get("url") or "").strip()
        if not path or not target or not url:
            raise ValueError("更新状态下载文件信息不完整")
        files.append(
            UpdateFile(
                path=path,
                target=target,
                url=url,
                sha256=str(item.get("sha256") or "").strip(),
                size=_positive_int(item.get("size")),
            )
        )
    return UpdateInfo(version, str(state.get("notes") or "").strip(), files)


def _state_has_downloaded_files(state: dict[str, Any]) -> bool:
    files = state.get("files")
    return isinstance(files, list) and bool(files)


def launch_update_helper(state_path: Path) -> None:
    command: list[str]
    if _running_from_frozen_app():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        bundled_helper = Path(sys.executable).with_name("updater.exe")
        helper_source = bundled_helper
        if not helper_source.exists():
            helper_source = Path(str(state.get("helper_path") or ""))
        if not helper_source.exists():
            raise FileNotFoundError(f"缺少更新器: {bundled_helper}")
        helper_path = state_path.parent / f"updater-{os.getpid()}.exe"
        shutil.copy2(helper_source, helper_path)
        command = [str(helper_path), UPDATE_ARGUMENT, str(state_path)]
    else:
        command = [sys.executable, str(Path(__file__).with_name("updater_entry.py")), UPDATE_ARGUMENT, str(state_path)]

    if os.name == "nt":
        _run_elevated(command, state_path.parent)
        return

    creationflags = 0
    subprocess.Popen(command, cwd=str(state_path.parent), close_fds=True, creationflags=creationflags)


def _run_elevated(command: list[str], cwd: Path) -> None:
    executable = command[0]
    parameters = subprocess.list2cmdline(command[1:])
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        parameters,
        str(cwd),
        1,
    )
    if result <= 32:
        raise OSError(f"管理员权限申请失败，错误码: {result}")


def apply_update_from_state(state_path: Path) -> int:
    app = wx.GetApp() or wx.App(False)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        update = _download_update_info_from_state(state)
    except Exception as exc:
        _message_box(f"更新安装失败：\n{exc}", "更新失败", icon="error")
        return 1

    progress = UpdateDownloadDialog(None, update)
    progress.Show()
    progress.bring_to_front()
    progress.mark_preparing()

    result_code = 1

    def exit_main_loop() -> None:
        try:
            app.ExitMainLoop()
        except RuntimeError:
            pass

    def finish_success(final_state: dict[str, Any]) -> None:
        nonlocal result_code
        try:
            progress.mark_complete()
            progress.Destroy()
            notes = str(final_state.get("notes") or "").strip()
            if notes:
                show_update_notes_dialog(notes)
            _restart_application(final_state)
        except Exception as exc:
            result_code = 1
            _message_box(f"更新安装失败：\n{exc}", "更新失败", icon="error")
        else:
            result_code = 0
        finally:
            exit_main_loop()

    def finish_cancelled() -> None:
        progress.Destroy()
        exit_main_loop()

    def finish_error(message: str) -> None:
        progress.Destroy()
        _message_box(message, "更新失败", icon="error")
        exit_main_loop()

    def worker() -> None:
        try:
            _wait_for_parent(int(state.get("parent_pid") or 0))
            if not _state_has_downloaded_files(state):
                downloaded_files = download_update_files(update, progress)
                state["files"] = downloaded_files
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            progress.mark_installing()
            _apply_update_state(state, progress)
        except UpdateCancelled:
            wx.CallAfter(finish_cancelled)
        except Exception as exc:
            wx.CallAfter(finish_error, f"更新安装失败：\n{exc}")
        else:
            wx.CallAfter(finish_success, state)

    thread = threading.Thread(target=worker, name="MaoerUpdateWorker", daemon=True)
    thread.start()
    app.MainLoop()
    return result_code


def _apply_update_state(state: dict[str, Any], progress: UpdateDownloadDialog | None = None) -> None:
    target_exe = Path(str(state["target_exe"])).resolve()
    install_dir = Path(str(state.get("install_dir") or target_exe.parent)).resolve()
    files = state.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("更新状态缺少文件列表")

    backup_dir = Path(str(state.get("backup_dir") or (install_dir / f"backup-{int(time.time())}"))).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        for item in files:
            _raise_if_update_cancelled(progress)
            if not isinstance(item, dict):
                raise ValueError("更新状态包含无效文件项")
            source = Path(str(item.get("path") or "")).resolve()
            if not source.exists():
                raise FileNotFoundError(source)
            _verify_sha256(source, str(item.get("sha256") or ""))
            target = _resolve_update_target(str(item.get("target") or ""), install_dir, target_exe)
            backup = _safe_child(backup_dir, _backup_relative_path(target, install_dir, target_exe))
            _replace_file(source, target, backup)
            _raise_if_update_cancelled(progress)
    except Exception:
        _restore_backup(backup_dir, install_dir)
        raise
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)


def _raise_if_update_cancelled(progress: UpdateDownloadDialog | None) -> None:
    if progress is None:
        return
    if _is_wx_main_thread():
        wx.YieldIfNeeded()
    if progress.cancel_requested:
        raise UpdateCancelled("用户取消更新")


def _resolve_update_target(target: str, install_dir: Path, target_exe: Path) -> Path:
    target = target.strip().replace("\\", "/")
    if target == "{app_exe}":
        return target_exe
    if not target:
        raise ValueError("更新文件缺少目标路径")
    return _safe_child(install_dir, target)


def _backup_relative_path(target: Path, install_dir: Path, target_exe: Path) -> str:
    target = target.resolve()
    if target == target_exe.resolve():
        return target_exe.name
    try:
        return target.relative_to(install_dir).as_posix()
    except ValueError as exc:
        raise ValueError(f"非法更新目标路径: {target}") from exc


def _replace_file(source: Path, target: Path, backup: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if backup.exists():
            _remove_any(backup)
        shutil.move(str(target), str(backup))
    shutil.copy2(source, target)


def _backup_then_delete(install_dir: Path, backup_dir: Path, relative_path: str) -> None:
    target = _safe_child(install_dir, relative_path)
    if not target.exists():
        return
    backup = _safe_child(backup_dir, relative_path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        _remove_any(backup)
    shutil.move(str(target), str(backup))


def _restore_backup(backup_dir: Path, install_dir: Path) -> None:
    if not backup_dir.exists():
        return
    for source in sorted(backup_dir.rglob("*"), key=lambda path: len(path.parts)):
        if not source.exists() or source.is_dir():
            continue
        relative = source.relative_to(backup_dir)
        target = _safe_child(install_dir, relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _remove_any(target)
        shutil.move(str(source), str(target))


def _restart_application(state: dict[str, Any]) -> None:
    target_exe = Path(str(state["target_exe"]))
    launch_args = _normal_launch_args([str(item) for item in state.get("launch_args") or []])
    if os.name == "nt":
        _restart_application_unelevated(target_exe, launch_args)
        return
    subprocess.Popen([str(target_exe), *launch_args], cwd=str(target_exe.parent), close_fds=True)


def _restart_application_unelevated(target_exe: Path, launch_args: list[str]) -> None:
    command = ["explorer.exe", str(target_exe), *launch_args]
    subprocess.Popen(command, cwd=str(target_exe.parent), close_fds=True)


def show_update_notes_dialog(notes: str) -> None:
    app = wx.GetApp() or wx.App(False)
    style = wx.DEFAULT_DIALOG_STYLE
    close_box = getattr(wx, "CLOSE_BOX", 0)
    if close_box:
        style &= ~close_box
    dialog = wx.Dialog(None, title="更新成功", style=style)
    panel = wx.Panel(dialog)
    root = wx.BoxSizer(wx.VERTICAL)

    label = wx.StaticText(panel, label="内容")
    content = wx.TextCtrl(
        panel,
        value=notes.strip() or "暂无更新日志。",
        style=wx.TE_MULTILINE | wx.TE_READONLY,
    )
    ok_button = wx.Button(panel, wx.ID_OK, label="确定")
    ok_button.SetDefault()

    root.Add(label, 0, wx.EXPAND | wx.ALL, 12)
    root.Add(content, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

    buttons = wx.BoxSizer(wx.HORIZONTAL)
    buttons.AddStretchSpacer()
    buttons.Add(ok_button, 0)
    root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

    panel.SetSizer(root)
    frame_sizer = wx.BoxSizer(wx.VERTICAL)
    frame_sizer.Add(panel, 1, wx.EXPAND)
    dialog.SetSizer(frame_sizer)
    dialog.SetMinSize((460, 320))
    dialog.Fit()
    dialog.CentreOnParent()
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def _select_update_files(manifest: dict[str, Any]) -> list[UpdateFile]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        return []

    files: list[UpdateFile] = []
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("url") or "").strip()
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path and raw_url:
            path = Path(urlparse(raw_url).path).name
        if not raw_url or not path:
            continue
        target = str(item.get("target") or path).strip().replace("\\", "/")
        files.append(
            UpdateFile(
                path=path,
                target=target,
                url=_absolute_update_url(raw_url),
                sha256=str(item.get("sha256") or item.get("hash") or "").strip(),
                size=_positive_int(item.get("size")),
            )
        )
    return files


def _changed_update_files(files: list[UpdateFile]) -> list[UpdateFile]:
    target_exe = _target_executable()
    install_dir = target_exe.parent
    changed: list[UpdateFile] = []
    for update_file in files:
        target = _resolve_update_target(update_file.target, install_dir, target_exe)
        if not target.exists():
            changed.append(update_file)
            continue
        expected = _normalize_sha256(update_file.sha256)
        if expected:
            if _file_sha256(target) != expected:
                changed.append(update_file)
            continue
        if update_file.size > 0 and target.stat().st_size != update_file.size:
            changed.append(update_file)
    return changed


def _absolute_update_url(raw_url: str) -> str:
    if urlparse(raw_url).scheme:
        return raw_url
    base = _update_download_base_url()
    return urljoin(base, raw_url)


def _update_download_base_url() -> str:
    parsed = urlparse(UPDATE_MANIFEST_URL)
    if not parsed.scheme or not parsed.netloc:
        return UPDATE_MANIFEST_URL if UPDATE_MANIFEST_URL.endswith("/") else f"{UPDATE_MANIFEST_URL}/"
    path = parsed.path
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/"
    return parsed._replace(path=path, params="", query="", fragment="").geturl()


def _verify_sha256(path: Path, expected: str) -> None:
    expected = _normalize_sha256(expected)
    if not expected:
        return
    actual = _file_sha256(path)
    if actual != expected:
        path.unlink(missing_ok=True)
        raise ValueError(f"更新文件校验失败，期望 {expected}，实际 {actual}")


def _normalize_sha256(value: str) -> str:
    return value.lower().replace("sha256:", "").replace(" ", "").strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _updates_dir(version: str) -> Path:
    safe_version = re.sub(r"[^0-9A-Za-z._-]+", "_", version)
    return app_data_dir() / UPDATE_DIR_NAME / safe_version


def _target_executable() -> Path:
    if _running_from_frozen_app():
        return Path(sys.executable).resolve()
    return Path(__file__).with_name("app.py").resolve()


def _running_from_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def _normal_launch_args(args: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == UPDATE_ARGUMENT:
            skip_next = True
            continue
        if arg == SKIP_UPDATE_ARGUMENT:
            continue
        cleaned.append(arg)
    return cleaned


def _wait_for_parent(pid: int) -> None:
    if pid <= 0:
        time.sleep(1.0)
        return
    if os.name != "nt":
        time.sleep(1.0)
        return

    synchronize = 0x00100000
    wait_timeout = 30_000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        time.sleep(1.0)
        return
    try:
        kernel32.WaitForSingleObject(handle, wait_timeout)
    finally:
        kernel32.CloseHandle(handle)


def _safe_child(root: Path, relative_path: str) -> Path:
    _validate_relative_path(relative_path)
    root = root.resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"非法更新路径: {relative_path}")
    return path


def _validate_relative_path(relative_path: str) -> None:
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    if not parts or parts[0] == "/" or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"非法更新路径: {relative_path}")
    if re.match(r"^[A-Za-z]:", relative_path):
        raise ValueError(f"非法更新路径: {relative_path}")


def _remove_any(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _positive_int(value: Any) -> int:
    if not value:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _is_version_newer(remote: str, current: str) -> bool:
    return _compare_versions(remote, current) > 0


def _compare_versions(left: str, right: str) -> int:
    left_parts = _version_parts(left)
    right_parts = _version_parts(right)
    max_length = max(len(left_parts), len(right_parts))
    for index in range(max_length):
        left_value = left_parts[index] if index < len(left_parts) else 0
        right_value = right_parts[index] if index < len(right_parts) else 0
        if left_value == right_value:
            continue
        if isinstance(left_value, int) and isinstance(right_value, int):
            return 1 if left_value > right_value else -1
        return 1 if str(left_value) > str(right_value) else -1
    return 0


def _version_parts(version: str) -> list[int | str]:
    parts: list[int | str] = []
    for token in re.findall(r"\d+|[A-Za-z]+", version):
        if token.isdigit():
            parts.append(int(token))
        else:
            parts.append(token.lower())
    return parts or [0]


def _message_box(message: str, title: str, icon: str = "info") -> None:
    if os.name == "nt":
        flags = 0x40 if icon == "info" else 0x10
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        return
    print(f"{title}: {message}", file=sys.stderr)


def _debug_log(message: str) -> None:
    if os.environ.get("MAOER_DEBUG"):
        print(f"[updater] {message}", flush=True)
