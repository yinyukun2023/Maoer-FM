from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time

import wx
import wx.html2 as html2

from app_paths import webview2_profile_dir
from browser_player import HiddenBrowserPlayer
from downloads import (DownloadCancelled, DownloadSelection, check_cancel, download_audio,
                       download_error, folder_name)
from maoer_api import ApiError, MaoerApi, PlaybackInfo
from startup_sound import play_download_completed_sound
from ui_dialogs import message_box
from uia_live_region import ScreenReaderAnnouncer, set_native_accessible_name
from web_login import call_webview_devtools, _remove_login_profile


class _DownloadPlayer(HiddenBrowserPlayer):
    """Reuse website playback without changing the user's player or its volume."""
    def __init__(self, parent: wx.Window, cookie: str) -> None:
        super().__init__(parent, cookie=cookie, volume=0)
        root = webview2_profile_dir().resolve()
        self.profile = Path(tempfile.mkdtemp(prefix='download-', dir=root)).resolve()
        if self.profile.parent != root:
            raise ApiError("无法创建下载组件的临时目录")

    def _prepare_environment(self) -> None:
        os.environ.setdefault('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '--autoplay-policy=no-user-gesture-required')
        os.environ['WEBVIEW2_USER_DATA_FOLDER'] = str(self.profile)

    def _ensure_webview(self):
        previous = os.environ.get('WEBVIEW2_USER_DATA_FOLDER')
        try:
            view = super()._ensure_webview()
            view.Enable(False)  # Never enter the native dialog's Tab order.
            return view
        finally:
            if previous is None:
                os.environ.pop('WEBVIEW2_USER_DATA_FOLDER', None)
            else:
                os.environ['WEBVIEW2_USER_DATA_FOLDER'] = previous

    def _queue_system_volume(self, volume: int) -> None:
        # Core Audio volume belongs to the real player, not this silent WebView.
        pass

    def _install_user_scripts(self, webview) -> None:
        super()._install_user_scripts(webview)
        webview.AddUserScript("""(() => {
          const p = HTMLMediaElement.prototype;
          for (const [name, value] of [['volume', 0], ['muted', true]]) {
            const d = Object.getOwnPropertyDescriptor(p, name);
            Object.defineProperty(p, name, {configurable: true,
              get() { return value; }, set() { d.set.call(this, value); }});
          }
          const play = p.play;
          p.play = function(...args) {
            this.muted = true; this.volume = 0;
            return play.apply(this, args);
          };
        })();""", html2.WEBVIEW_INJECT_AT_DOCUMENT_START)

    def shutdown(self) -> None:
        super().shutdown()
        threading.Thread(target=_remove_login_profile, args=(self.profile,), daemon=True).start()


class DownloadDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, selection: DownloadSelection, cookie: str, root: Path) -> None:
        super().__init__(parent, title=f"下载 — {selection.title}", size=(720, 620),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.selection = selection
        self.cookie = cookie
        self.cancel = threading.Event()
        self.running = False
        self.close_requested = False
        self.player: _DownloadPlayer | None = None
        self.key_request = None
        self.key_handler = None
        self.key_pending = False
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_key, self.timer)

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self, label='选择要下载的音频；空格切换勾选。'), 0, wx.ALL, 10)
        self.list = wx.CheckListBox(self, choices=[item.title for item in selection.items])
        set_native_accessible_name(self.list, '下载音频')
        for index in selection.checked:
            self.list.Check(index)
        self.list.SetSelection(selection.checked[0] if selection.checked else 0)
        layout.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        choices = wx.BoxSizer(wx.HORIZONTAL)
        self.all_button = wx.Button(self, label='全选(&A)')
        self.none_button = wx.Button(self, label='全不选(&U)')
        choices.Add(self.all_button, 0, wx.RIGHT, 8)
        choices.Add(self.none_button, 0)
        layout.Add(choices, 0, wx.ALL, 10)
        self.all_button.Bind(wx.EVT_BUTTON, lambda event: self._check_all(True))
        self.none_button.Bind(wx.EVT_BUTTON, lambda event: self._check_all(False))

        layout.Add(wx.StaticText(self, label='下载路径(&L)：'), 0, wx.LEFT | wx.RIGHT, 10)
        path_row = wx.BoxSizer(wx.HORIZONTAL)
        self.path = wx.TextCtrl(self, value=str(root))
        set_native_accessible_name(self.path, '下载路径')
        self.browse = wx.Button(self, label='浏览(&B)…')
        path_row.Add(self.path, 1, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)
        path_row.Add(self.browse, 0)
        layout.Add(path_row, 0, wx.EXPAND | wx.ALL, 10)
        self.browse.Bind(wx.EVT_BUTTON, self._browse)
        self.publisher = wx.CheckBox(self, label='文件夹名带发布者标签(&P)')
        self.publisher.Enable(bool(selection.publisher))
        layout.Add(self.publisher, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.destination = wx.StaticText(self, label='')
        layout.Add(self.destination, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.publisher.Bind(wx.EVT_CHECKBOX, self._update_folder)
        self._update_folder()

        self.status = wx.StaticText(self, label='勾选后按“开始下载”。同名文件会跳过，不会覆盖。')
        layout.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.reader = ScreenReaderAnnouncer(self.status, native_only=True)
        self.gauge = wx.Gauge(self, range=100)
        self.gauge.SetName('当前音频下载进度')
        layout.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.results = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80))
        set_native_accessible_name(self.results, '下载结果')
        layout.Add(self.results, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = wx.Button(self, label='开始下载(&D)')
        self.close_button = wx.Button(self, wx.ID_CANCEL, label='关闭(&C)')
        buttons.AddStretchSpacer()
        buttons.Add(self.start_button, 0, wx.RIGHT, 8)
        buttons.Add(self.close_button, 0)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.start_button.Bind(wx.EVT_BUTTON, self._start)
        self.close_button.Bind(wx.EVT_BUTTON, self._close)
        self.Bind(wx.EVT_CLOSE, self._close)
        self.SetSizer(layout)
        self.SetMinSize((540, 500))
        self.CentreOnParent()
        self.list.SetFocus()

    def _check_all(self, checked: bool) -> None:
        for index in range(self.list.GetCount()):
            self.list.Check(index, checked)
        self._status(f"已勾选 {len(self.list.GetCheckedItems())} 个音频", announce=True)

    def _update_folder(self, event=None) -> None:
        self.destination.SetLabel('剧集文件夹：' + folder_name(self.selection, self.publisher.GetValue()))

    def _browse(self, event) -> None:
        with wx.DirDialog(self, '选择下载路径', defaultPath=self.path.GetValue(),
                          style=wx.DD_DEFAULT_STYLE) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.path.SetValue(dialog.GetPath())
        self.browse.SetFocus()

    def _status(self, text: str, percent: int | None = None, *, announce: bool = False) -> None:
        if not self or self.IsBeingDeleted():
            return
        self.status.SetLabel(text)
        if percent is not None:
            self.gauge.SetValue(percent)
        if announce:
            self.reader.announce(text)

    def _start(self, event) -> None:
        if self.running:
            return
        selected = [self.selection.items[i] for i in self.list.GetCheckedItems()]
        if not selected:
            message_box('请至少勾选一个音频。', '下载', wx.OK | wx.ICON_INFORMATION, self)
            self.list.SetFocus()
            return
        root = Path(self.path.GetValue().strip()).expanduser()
        if not self.path.GetValue().strip() or not root.is_absolute():
            message_box('请选择或填写完整的下载路径。', '下载', wx.OK | wx.ICON_INFORMATION, self)
            self.path.SetFocus()
            return
        folder = root / folder_name(self.selection, self.publisher.GetValue())
        self.running = True
        self.cancel.clear()
        self.results.Clear()
        self.close_button.SetLabel('取消下载(&C)')
        self.close_button.SetFocus()
        self._enable_options(False)
        self._status(f'准备下载 {len(selected)} 个音频', 0, announce=True)

        def run() -> None:
            api = MaoerApi(cookie=self.cookie)
            completed = skipped = failed = 0
            try:
                check_cancel(self.cancel)
                folder.mkdir(parents=True, exist_ok=True)
                # ponytail: one worker downloads sequentially; parallel transfers
                # need separate website sessions and are unnecessary here.
                for number, item in enumerate(selected, 1):
                    check_cancel(self.cancel)
                    prefix = f'{number}/{len(selected)} {item.title}'
                    last_stage = ''

                    def progress(stage, percent):
                        nonlocal last_stage
                        changed = stage != last_stage
                        last_stage = stage
                        wx.CallAfter(self._status, f'{prefix}：{stage} {percent}%', percent, announce=changed)

                    try:
                        path = download_audio(api, item, folder, self.cancel, self._get_key, progress)
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        if isinstance(exc, FileExistsError):
                            skipped += 1
                        else:
                            failed += 1
                        wx.CallAfter(self.results.AppendText, f'{item.title}：{download_error(exc)}\n')
                    else:
                        completed += 1
                        wx.CallAfter(play_download_completed_sound)
                        wx.CallAfter(self.results.AppendText, f'完成：{path.name}\n')
            except DownloadCancelled:
                wx.CallAfter(self.results.AppendText, '已取消；已完成的文件保留，未完成的临时文件已清理。\n')
            except Exception as exc:
                failed = len(selected) - completed - skipped
                wx.CallAfter(self.results.AppendText, download_error(exc) + '\n')
            finally:
                api.session.close()
                wx.CallAfter(self._finished, completed, skipped, failed)

        threading.Thread(target=run, daemon=True).start()

    def _enable_options(self, enabled: bool) -> None:
        for control in (self.list, self.all_button, self.none_button, self.path, self.browse, self.start_button):
            control.Enable(enabled)
        self.publisher.Enable(enabled and bool(self.selection.publisher))

    def _get_key(self, playback: PlaybackInfo) -> bytes:
        ready = threading.Event()
        result = {}
        wx.CallAfter(self._request_key, playback, ready, result)
        while not ready.wait(0.1):
            check_cancel(self.cancel)
        check_cancel(self.cancel)
        if 'error' in result:
            raise ApiError(result['error'])
        return result['key']

    def _request_key(self, playback, ready, result) -> None:
        if self.cancel.is_set() or not self.running:
            result['error'] = '下载已取消'
            ready.set()
            return
        self.key_request = (playback, ready, result)
        self.key_started = time.monotonic()
        self.key_pending = False
        try:
            if self.player is None:
                self.player = _DownloadPlayer(self, self.cookie)
            self.player.play(playback)
            self.timer.Start(500)
        except Exception:
            self._key_done(error='无法启动音频组件，请确认 WebView2 Runtime 已安装')

    def _poll_key(self, event) -> None:
        request = self.key_request
        if request is None:
            return
        if self.cancel.is_set():
            self._key_done(error='下载已取消')
            return
        if time.monotonic() - self.key_started > 45:
            self._key_done(error='官网音频准备超时，请确认当前账号可以播放该音频后重试')
            return
        if self.key_pending or self.player is None or self.player._webview is None:
            return
        playback = request[0]
        expression = """(() => {
          if (new URL(location.href).searchParams.get('id') !== '%d') return null;
          const m = window.index?.mo?.soundDemo;
          const d = m?.currentLoader?.dashPlayer;
          if (!d?.state?.initialized || !m?.element?.mediaKeys || m.element.readyState < 1) return null;
          return d.state.protectionDataSet?.['org.w3.clearkey']?.clearkeys || null;
        })()""" % playback.sound_id

        def received(error, payload):
            if self.key_request is not request:
                return
            self.key_pending = False
            if error < 0:
                return
            try:
                keys = json.loads(payload).get('result', {}).get('value')
                if not isinstance(keys, dict):
                    return
                kid = bytes.fromhex(playback.dash_audio['bilidrm_uri'].removeprefix('uri:bili://').replace('-', ''))
                name = base64.urlsafe_b64encode(kid).decode().rstrip('=')
                encoded = keys.get(name)
                if encoded is None:
                    return
                key = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
                if len(kid) != 16 or len(key) != 16:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                self._key_done(error='官网返回的音频信息无效，请重试')
            else:
                self._key_done(key=key)

        try:
            self.key_handler = call_webview_devtools(self.player._webview, 'Runtime.evaluate',
                                                    {'expression': expression, 'returnByValue': True}, received)
            self.key_pending = True
        except Exception:
            self.key_pending = False

    def _key_done(self, *, key=None, error=None) -> None:
        self.timer.Stop()
        request, self.key_request = self.key_request, None
        if self.player is not None:
            self.player.stop()
        if request is not None:
            request[2]['error' if error else 'key'] = error or key
            request[1].set()

    def _finished(self, completed: int, skipped: int, failed: int) -> None:
        self.running = False
        self._key_done(error='下载已结束')
        if self.player is not None:
            self.player.shutdown()
            self.player = None
        self.key_handler = None
        self._enable_options(True)
        self.close_button.SetLabel('关闭(&C)')
        label = '下载已取消' if self.cancel.is_set() else '下载结束'
        self._status(f'{label}：完成 {completed}，跳过 {skipped}，失败 {failed}', announce=True)
        if self.close_requested:
            self._close(None)

    def _close(self, event) -> None:
        if self.running:
            self.close_requested = True
            self.cancel.set()
            self._key_done(error='下载已取消')
            self._status('正在取消下载，请稍候…', announce=True)
            if isinstance(event, wx.CloseEvent) and event.CanVeto():
                event.Veto()
            return
        self.timer.Stop()
        self.reader.close()
        self.EndModal(wx.ID_CANCEL)
