"""Own one modeless download batch and coordinate safe application shutdown."""
from pathlib import Path

import wx

from app_settings import DownloadSettings
from download_dialog import show_download_dialog
from ui_dialogs import message_box
from uia_live_region import ScreenReaderAnnouncer


class _DownloadVisibilityKeys(wx.EventFilter):
    """Application-local shortcut, before playback's X speed key handles it."""
    def __init__(self, manager) -> None:
        super().__init__()
        self.manager = manager

    def FilterEvent(self, event):
        if (event.GetEventType() != wx.EVT_CHAR_HOOK.typeId
                or event.GetKeyCode() not in (ord('X'), ord('x'), wx.WXK_CONTROL_X)
                or event.GetModifiers() != (wx.MOD_CONTROL | wx.MOD_SHIFT)):
            return self.Event_Skip
        manager = self.manager
        source = event.GetEventObject()
        if not isinstance(source, wx.Window) or not source:
            return self.Event_Skip
        top = wx.GetTopLevelParent(source)
        if top not in (manager.parent, manager.window, getattr(manager.parent, 'player_frame', None)):
            return self.Event_Skip
        if not top.IsEnabled():
            return self.Event_Skip
        if not event.IsAutoRepeat():
            manager.toggle_tasks()
        return self.Event_Processed


class DownloadManager:
    def __init__(self, parent, focus_main) -> None:
        self.parent = parent
        self.focus_main = focus_main
        self.window = None
        self.exiting = False
        self._confirming_exit = False
        self._exit_ready = None
        self._region = wx.StaticText(parent, label='', pos=(-100, -100), size=(1, 1))
        self._region.SetName('下载提示')
        self._reader = ScreenReaderAnnouncer(self._region, native_only=True)
        self._keys = _DownloadVisibilityKeys(self)
        wx.EvtHandler.AddFilter(self._keys)

    def toggle_tasks(self) -> None:
        window = self.window
        if (self.exiting or window is None or window._disposed
                or window._confirming_cancel or window._completion_pending):
            return
        if window.hidden:
            window.restore()
        else:
            window._hide(None)

    def show_tasks(self) -> None:
        if self.exiting:
            return
        if self.window is not None:
            self.window.restore()
        else:
            message_box('当前没有下载任务。', '下载任务', wx.OK | wx.ICON_INFORMATION, self.parent)

    def can_start(self) -> bool:
        if self.exiting:
            return False
        if self.window is not None and (self.window._completion_pending or self.window._confirming_cancel):
            return False
        if self.window is not None and self.window.busy:
            self.window.restore()
            message_box('已有下载任务正在进行，请等待完成或停止当前任务后再开始新的下载。',
                        '下载任务', wx.OK | wx.ICON_INFORMATION, self.window)
            return False
        return True

    def select(self, selection, cookie: str, default_root: Path, options: DownloadSettings) -> None:
        if not self.can_start():
            return
        root = Path(options.directory) if options.directory else default_root
        window = show_download_dialog(self.parent, selection, cookie, root, options=options,
                                      on_hidden=self.focus_main, on_closed=self._closed,
                                      announce=self._reader.announce)
        if window is None:
            if not self.exiting:
                self.focus_main()
            return
        if self.exiting:
            window.stop_for_exit()
            return
        if self.window is not None:
            self.window._finish_window()
        self.window = window

    def _closed(self, window) -> None:
        if self.window is window:
            self.window = None
        if self.exiting and self.window is None and self._exit_ready is not None:
            callback, self._exit_ready = self._exit_ready, None
            wx.CallAfter(callback)

    def request_exit(self, when_ready) -> bool:
        """True means safe now; False means declined or awaiting worker cleanup."""
        if self._confirming_exit:
            return False
        if self.window is None:
            self.exiting = True
            return True
        if self.exiting:
            return False
        window = self.window
        if not window.busy:
            self.exiting = True
            window._finish_window()
            return True
        if window._confirming_cancel or window._completion_pending:
            return False
        self._confirming_exit = window._confirming_cancel = True
        was_paused = window.cancel.is_paused()
        window.cancel.pause()
        try:
            answer = message_box('还有下载任务尚未结束，是否停止下载并退出程序？\n'
                                 '已完成的文件会保留，未完成的临时文件会清理。',
                                 '退出程序', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self.parent)
        finally:
            self._confirming_exit = window._confirming_cancel = False
        if answer == wx.YES:
            self.exiting = True
            self._exit_ready = when_ready
            window.stop_for_exit()
        else:
            if not was_paused:
                window.cancel.resume()
            window._finish_deferred()
            if not window._disposed:
                window._refresh_controls()
        return False

    def dispose(self) -> None:
        if self._keys is not None:
            wx.EvtHandler.RemoveFilter(self._keys)
            self._keys = None
        self._reader.close()
