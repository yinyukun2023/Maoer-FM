"""Default download options; saving never changes an already running request."""
from pathlib import Path
from typing import Callable

import wx

from app_settings import DownloadSettings
from ui_dialogs import message_box
from uia_live_region import set_native_accessible_name


class DownloadSettingsDialog(wx.Dialog):
    def __init__(self, parent, settings: DownloadSettings, default_root: Path,
                 on_save: Callable[[DownloadSettings], None]) -> None:
        super().__init__(parent, title='下载设置', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.on_save = on_save
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self, label='默认下载路径(&L)：'), 0, wx.ALL, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.path = wx.TextCtrl(self, value=settings.directory or str(default_root), size=(460, -1))
        set_native_accessible_name(self.path, '默认下载路径')
        self.browse = wx.Button(self, label='浏览(&B)…')
        self.browse.Bind(wx.EVT_BUTTON, self._browse)
        row.Add(self.path, 1, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)
        row.Add(self.browse)
        layout.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.options = {}
        for name, label in (
            ('create_drama_folder', '下载时创建剧集文件夹(&F)'),
            ('announce_hidden_complete', '每个音频下载成功或失败时通过活动区域朗读(&R)'),
            ('play_sound', '播放下载提示音(&S)'),
            ('auto_close_hidden', '窗口隐藏时，下载完成后关闭下载任务窗口(&H)'),
            ('escape_hides', '按 Esc 隐藏下载任务窗口(&X)'),
        ):
            control = wx.CheckBox(self, label=label)
            control.SetValue(getattr(settings, name))
            self.options[name] = control
            layout.Add(control, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.save_button = wx.Button(self, wx.ID_OK, label='确定')
        cancel = wx.Button(self, wx.ID_CANCEL, label='取消')
        buttons.AddStretchSpacer()
        buttons.Add(self.save_button, 0, wx.RIGHT, 8)
        buttons.Add(cancel)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.save_button.SetDefault()
        self.save_button.Bind(wx.EVT_BUTTON, self._save)
        self.SetSizerAndFit(layout)
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()
        self.path.SetFocus()

    def _browse(self, event) -> None:
        with wx.DirDialog(self, '选择默认下载路径', defaultPath=self.path.GetValue(),
                          style=wx.DD_DEFAULT_STYLE) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.path.SetValue(dialog.GetPath())
        self.browse.SetFocus()

    def _save(self, event) -> None:
        text = self.path.GetValue().strip()
        path = Path(text).expanduser()
        if not text or '\0' in text or not path.is_absolute():
            message_box('请选择或填写完整的下载路径。', '下载设置', wx.OK | wx.ICON_INFORMATION, self)
            self.path.SetFocus()
            return
        settings = DownloadSettings(directory=str(path), **{
            name: control.GetValue() for name, control in self.options.items()
        })
        try:
            self.on_save(settings)
        except OSError as exc:
            message_box(f'保存下载设置失败：{exc}', '下载设置', wx.OK | wx.ICON_ERROR, self)
            return
        self.EndModal(wx.ID_OK)
