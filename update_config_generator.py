from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import wx


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "dist" / "更新包" / "update-config.json"
DEFAULT_UPDATE_BASE_URL = "https://update.1630.org/maoer-fm/updates/"
APP_EXE_TARGET = "{app_exe}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def update_file_url(base_url: str, server_path: str) -> str:
    base = base_url.rstrip("/") + "/"
    escaped = quote(server_path.replace("\\", "/"), safe="/._+-")
    return f"{base}{escaped}"


def file_entry(source: Path, server_path: str, target: str, base_url: str) -> dict[str, Any]:
    return {
        "path": server_path.replace("\\", "/"),
        "target": target.replace("\\", "/"),
        "url": update_file_url(base_url, server_path),
        "sha256": file_sha256(source),
        "size": source.stat().st_size,
    }


class UpdateConfigGenerator(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title="更新配置生成器", size=(760, 620))

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        form = wx.FlexGridSizer(cols=2, hgap=10, vgap=8)
        form.AddGrowableCol(1, 1)

        self.enabled = wx.CheckBox(panel, label="启用更新")
        self.enabled.SetValue(True)
        self.version = wx.TextCtrl(panel)
        self.notes = wx.TextCtrl(panel, style=wx.TE_MULTILINE)
        self.base_url = wx.TextCtrl(panel, value=DEFAULT_UPDATE_BASE_URL)
        self.output = wx.FilePickerCtrl(
            panel,
            path=str(DEFAULT_OUTPUT),
            message="选择 update-config.json 输出位置",
            wildcard="update-config.json|update-config.json|JSON files (*.json)|*.json|All files (*.*)|*.*",
            style=wx.FLP_SAVE | wx.FLP_USE_TEXTCTRL | wx.FLP_OVERWRITE_PROMPT,
        )

        self._add_row(form, panel, "状态", self.enabled)
        self._add_row(form, panel, "版本号", self.version)
        self._add_row(form, panel, "更新日志", self.notes)
        self._add_row(form, panel, "下载目录", self.base_url)
        self._add_row(form, panel, "输出文件", self.output)
        root.Add(form, 0, wx.EXPAND | wx.ALL, 12)

        root.Add(self._section(panel, "主程序"), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        main_form = wx.FlexGridSizer(cols=2, hgap=10, vgap=8)
        main_form.AddGrowableCol(1, 1)
        self.main_file = wx.FilePickerCtrl(
            panel,
            message="选择主程序文件",
            wildcard="Executable files (*.exe)|*.exe|All files (*.*)|*.*",
            style=wx.FLP_OPEN | wx.FLP_USE_TEXTCTRL | wx.FLP_FILE_MUST_EXIST,
        )
        self.main_server_path = wx.TextCtrl(panel, value="Maoer-FM.exe")
        self.main_target = wx.TextCtrl(panel, value=APP_EXE_TARGET)
        self._add_row(main_form, panel, "本地文件", self.main_file)
        self._add_row(main_form, panel, "服务器文件名", self.main_server_path)
        self._add_row(main_form, panel, "安装目标", self.main_target)
        root.Add(main_form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        root.Add(self._section(panel, "更新器"), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        updater_form = wx.FlexGridSizer(cols=2, hgap=10, vgap=8)
        updater_form.AddGrowableCol(1, 1)
        self.updater_file = wx.FilePickerCtrl(
            panel,
            message="选择 updater.exe",
            wildcard="Executable files (*.exe)|*.exe|All files (*.*)|*.*",
            style=wx.FLP_OPEN | wx.FLP_USE_TEXTCTRL | wx.FLP_FILE_MUST_EXIST,
        )
        self.updater_server_path = wx.TextCtrl(panel, value="updater.exe")
        self.updater_target = wx.TextCtrl(panel, value="updater.exe")
        self._add_row(updater_form, panel, "本地文件", self.updater_file)
        self._add_row(updater_form, panel, "服务器文件名", self.updater_server_path)
        self._add_row(updater_form, panel, "安装目标", self.updater_target)
        root.Add(updater_form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        root.Add(self._section(panel, "JSON 预览"), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.preview = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        root.Add(self.preview, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.refresh_button = wx.Button(panel, label="刷新预览")
        self.generate_button = wx.Button(panel, label="生成配置")
        buttons.AddStretchSpacer()
        buttons.Add(self.refresh_button, 0, wx.RIGHT, 8)
        buttons.Add(self.generate_button, 0)
        root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(root)
        self.Centre()

        self.refresh_button.Bind(wx.EVT_BUTTON, self._on_refresh_preview)
        self.generate_button.Bind(wx.EVT_BUTTON, self._on_generate)
        self._refresh_preview(show_errors=False)

    def _section(self, parent: wx.Window, label: str) -> wx.StaticText:
        text = wx.StaticText(parent, label=label)
        font = text.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        text.SetFont(font)
        return text

    def _add_row(
        self,
        sizer: wx.FlexGridSizer,
        parent: wx.Window,
        label: str,
        control: wx.Window,
    ) -> None:
        sizer.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(control, 1, wx.EXPAND)

    def _on_refresh_preview(self, event: wx.CommandEvent) -> None:
        self._refresh_preview(show_errors=True)

    def _on_generate(self, event: wx.CommandEvent) -> None:
        config = self._build_config(show_errors=True)
        if config is None:
            return
        output = Path(self.output.GetPath()).expanduser()
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        except OSError as exc:
            wx.MessageBox(f"写入配置失败：\n{exc}", "生成失败", wx.OK | wx.ICON_ERROR, self)
            return
        self.preview.SetValue(json.dumps(config, ensure_ascii=False, indent=2))
        wx.MessageBox(f"已生成更新配置：\n{output}", "生成完成", wx.OK | wx.ICON_INFORMATION, self)

    def _refresh_preview(self, show_errors: bool) -> None:
        config = self._build_config(show_errors=show_errors)
        if config is None:
            return
        self.preview.SetValue(json.dumps(config, ensure_ascii=False, indent=2))

    def _build_config(self, show_errors: bool) -> dict[str, Any] | None:
        version = self.version.GetValue().strip()
        if not version:
            return self._fail("请填写版本号。", show_errors)
        base_url = self.base_url.GetValue().strip()
        if not base_url:
            return self._fail("请填写下载目录。", show_errors)

        files: list[dict[str, Any]] = []
        for source_picker, server_ctrl, target_ctrl, name in (
            (self.main_file, self.main_server_path, self.main_target, "主程序"),
            (self.updater_file, self.updater_server_path, self.updater_target, "更新器"),
        ):
            source = Path(source_picker.GetPath())
            server_path = server_ctrl.GetValue().strip()
            target = target_ctrl.GetValue().strip()
            if not source.exists():
                return self._fail(f"请选择{name}本地文件。", show_errors)
            if not server_path:
                return self._fail(f"请填写{name}服务器文件名。", show_errors)
            if not target:
                return self._fail(f"请填写{name}安装目标。", show_errors)
            files.append(file_entry(source, server_path, target, base_url))

        return {
            "enabled": self.enabled.GetValue(),
            "version": version,
            "notes": self.notes.GetValue().strip(),
            "files": files,
        }

    def _fail(self, message: str, show_errors: bool) -> None:
        if show_errors:
            wx.MessageBox(message, "配置不完整", wx.OK | wx.ICON_ERROR, self)
        return None


def main() -> int:
    app = wx.App(False)
    frame = UpdateConfigGenerator()
    frame.Show()
    app.MainLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
