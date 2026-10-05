from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
import threading

import wx
import wx.html2 as html2

from app_paths import webview2_profile_dir
from browser_player import HiddenBrowserPlayer
from download_queue import CreatedDownload, DownloadBatch
from downloads import (DownloadCancelled, DownloadControl, DownloadSelection, check_cancel, download_audio,
                       download_error, folder_name)
from maoer_api import ApiError, MaoerApi, MediaItem, PlaybackInfo
from startup_sound import play_download_completed_sound, play_download_failed_sound
from ui_dialogs import message_box
from uia_live_region import set_native_accessible_name
from web_login import call_webview_devtools, _remove_login_profile


DOWNLOAD_NO_HISTORY_SCRIPT = r"""(() => {
  if (window.__maoerDownloadNoHistory) return;
  window.__maoerDownloadNoHistory = true;
  const ignored = value => {
    try {
      const url = new URL(value && typeof value.url === 'string' ? value.url : value, location.href);
      if (url.protocol !== 'https:' && url.protocol !== 'http:') return false;
      return ((url.hostname === 'www.missevan.com' || url.hostname === 'missevan.com') &&
               url.pathname === '/sound/addplaytimes') ||
             (url.hostname === 'data.missevan.com' && url.pathname === '/statistics/playlog-web');
    } catch (_) { return false; }
  };
  const body = JSON.stringify({success: true, code: 0, info: null, data: null});
  // Only download playback reports are acknowledged locally. Authentication,
  // permission checks, media, and the normal playback window stay untouched.
  if (typeof window.fetch === 'function') {
    const fetch = window.fetch;
    window.fetch = function(input) {
      if (ignored(input)) return Promise.resolve(new Response(body, {
        status: 200, headers: {'Content-Type': 'application/json'}
      }));
      return fetch.apply(this, arguments);
    };
  }
  if (typeof XMLHttpRequest !== 'undefined') {
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    const local = new WeakSet();
    XMLHttpRequest.prototype.open = function(method, url) {
      if (!ignored(url)) {
        local.delete(this);
        return open.apply(this, arguments);
      }
      local.add(this);
      // Keep native readyState/load/error/abort behavior, including jQuery's
      // XHR-based fetch fallback, without sending a request to the website.
      return open.call(this, 'GET', 'data:application/json,' + encodeURIComponent(body),
                       arguments.length < 3 || arguments[2] !== false);
    };
    XMLHttpRequest.prototype.send = function(value) {
      return send.call(this, local.has(this) ? null : value);
    };
  }
  if (typeof navigator.sendBeacon === 'function') {
    const beacon = navigator.sendBeacon;
    navigator.sendBeacon = function(url) {
      return ignored(url) || beacon.apply(this, arguments);
    };
  }
})();"""


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
        webview.AddUserScript(DOWNLOAD_NO_HISTORY_SCRIPT, html2.WEBVIEW_INJECT_AT_DOCUMENT_START)
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


@dataclass
class DownloadRequest:
    title: str
    items: list[MediaItem]
    folder: Path
    cookie: str = field(repr=False)
    number_width: int | None = None
    download_root: Path | None = None


@dataclass
class DownloadTask:
    item: MediaItem
    state: str = 'pending'
    status: str = '等待下载'
    reason: str = ''


class DownloadDialog(wx.Dialog):
    """Selection only: no worker, playback component or live announcements."""
    def __init__(self, parent: wx.Window, selection: DownloadSelection, cookie: str, root: Path) -> None:
        super().__init__(parent, title=f"选择下载音频 — {selection.title}", size=(760, 520),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.selection = selection
        self.cookie = cookie
        self.request: DownloadRequest | None = None
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(self, label='选择要下载的音频；空格切换勾选。'), 0, wx.ALL, 10)
        self.list = wx.CheckListBox(self, choices=[item.title for item in selection.items])
        set_native_accessible_name(self.list, '下载音频')
        for index in selection.checked:
            self.list.Check(index)
        if selection.items:
            index = selection.checked[0] if selection.checked else 0
            self.list.SetSelection(index)
            self.list.SetFirstItem(index)
        layout.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        choices = wx.BoxSizer(wx.HORIZONTAL)
        self.all_button = wx.Button(self, label='全选(&A)')
        self.none_button = wx.Button(self, label='全不选(&U)')
        self.from_button = wx.Button(self, label='选择当前集至列表末尾(&S)')
        self.uncheck_from_button = wx.Button(self, label='取消勾选当前集至列表末尾(&E)')
        for button in (self.all_button, self.none_button, self.from_button, self.uncheck_from_button):
            choices.Add(button, 0, wx.RIGHT, 8)
        layout.Add(choices, 0, wx.ALL, 10)
        self.all_button.Bind(wx.EVT_BUTTON, lambda event: self._check_all(True))
        self.none_button.Bind(wx.EVT_BUTTON, lambda event: self._check_all(False))
        self.from_button.Bind(wx.EVT_BUTTON, lambda event: self._check_from_current(True))
        self.uncheck_from_button.Bind(wx.EVT_BUTTON, lambda event: self._check_from_current(False))

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
        self.numbered = wx.CheckBox(self, label='正剧集数按数字编号命名(&N)')
        layout.Add(self.numbered, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.destination = wx.StaticText(self, label='')
        layout.Add(self.destination, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.publisher.Bind(wx.EVT_CHECKBOX, self._update_folder)
        self._update_folder()
        layout.Add(wx.StaticText(self, label='勾选后按“开始下载”。同名文件会跳过，不会覆盖。'),
                   0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = wx.Button(self, label='开始下载(&D)')
        self.close_button = wx.Button(self, wx.ID_CANCEL, label='关闭(&C)')
        buttons.AddStretchSpacer()
        buttons.Add(self.start_button, 0, wx.RIGHT, 8)
        buttons.Add(self.close_button)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.start_button.Bind(wx.EVT_BUTTON, self._start)
        self.SetSizer(layout)
        self.SetMinSize((740, 480))
        self.CentreOnParent()
        self.list.SetFocus()

    def _check_all(self, checked: bool) -> None:
        for index in range(self.list.GetCount()):
            self.list.Check(index, checked)
        self.list.SetFocus()

    def _check_from_current(self, checked: bool) -> None:
        current = self.list.GetSelection()
        if current != wx.NOT_FOUND:
            for index in range(current, self.list.GetCount()):
                self.list.Check(index, checked)
        self.list.SetFocus()

    def _update_folder(self, event=None) -> None:
        self.destination.SetLabel('剧集文件夹：' + folder_name(self.selection, self.publisher.GetValue()))

    def _browse(self, event) -> None:
        with wx.DirDialog(self, '选择下载路径', defaultPath=self.path.GetValue(),
                          style=wx.DD_DEFAULT_STYLE) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.path.SetValue(dialog.GetPath())
        self.browse.SetFocus()

    def _start(self, event) -> None:
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
        self.request = DownloadRequest(
            self.selection.title, selected, root / folder_name(self.selection, self.publisher.GetValue()),
            self.cookie, self.selection.number_width if self.numbered.GetValue() else None,
            download_root=root)
        self.EndModal(wx.ID_OK)


def show_download_dialog(parent, selection, cookie, root) -> None:
    selector = DownloadDialog(parent, selection, cookie, root)
    request = None
    try:
        if selector.ShowModal() == wx.ID_OK:
            request = selector.request
    finally:
        selector.Destroy()
    if request is None:
        return
    progress = DownloadProgressDialog(parent, request)
    try:
        wx.CallAfter(progress.start)
        progress.ShowModal()
    finally:
        progress.Destroy()


class DownloadProgressDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, request: DownloadRequest) -> None:
        super().__init__(parent, title=f"下载任务 — {request.title}", size=(820, 660),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.request = request
        self.cookie = request.cookie
        self.tasks = [DownloadTask(item) for item in request.items]
        self.cancel = DownloadBatch(len(self.tasks))
        self.controls = self.cancel.controls
        self.running = False
        self._started = False
        self._retry_requested = False
        self.close_requested = False
        self._confirming_cancel = False
        self._deferred_finished = None
        self._clear_requested = self._clearing = False
        self._created_files = {}
        self._ownership_errors = {}
        self._output_lock = threading.Lock()
        self._key_queue = []
        self.key_control = None
        self.player: _DownloadPlayer | None = None
        self.key_request = self.key_handler = None
        self.key_pending = False
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_key, self.timer)
        layout = wx.BoxSizer(wx.VERTICAL)

        def make_list(name, failed=False):
            label = wx.StaticText(self, label=name)
            control = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
            set_native_accessible_name(control, name)
            control.InsertColumn(0, '音频', width=330)
            control.InsertColumn(1, '状态', width=100 if failed else 410)
            if failed:
                control.InsertColumn(2, '原因', width=330)
            layout.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
            layout.Add(control, 1, wx.EXPAND | wx.ALL, 10)
            return label, control

        _label, self.pending_list = make_list('待下载')
        self.pending_list.Bind(wx.EVT_CONTEXT_MENU, self._on_task_context_menu)
        self.pending_list.Bind(wx.EVT_CHAR_HOOK, self._on_task_key)
        _label, self.completed_list = make_list('已下载')
        self.failed_label, self.failed_list = make_list('下载失败', True)
        self.failed_list.Bind(wx.EVT_CONTEXT_MENU, self._on_task_context_menu)
        self.failed_list.Bind(wx.EVT_CHAR_HOOK, self._on_task_key)
        self.failed_label.Hide()
        self.failed_list.Hide()
        self.auto_close = wx.CheckBox(self, label='下载完成后关闭窗口(&A)')
        layout.Add(self.auto_close, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.open_folder_button = wx.Button(self, label='打开下载文件夹(&O)')
        self.pause_button = wx.Button(self, label='全部暂停(&P)')
        self.cancel_button = wx.Button(self, label='停止下载(&C)')
        self.clear_button = wx.Button(self, label='全部取消(&Q)')
        self.close_button = wx.Button(self, wx.ID_CANCEL, label='关闭(&X)')
        for button in (self.open_folder_button, self.pause_button, self.cancel_button, self.clear_button, self.close_button):
            buttons.Add(button, 0, wx.RIGHT, 8)
        layout.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.open_folder_button.Bind(wx.EVT_BUTTON, self._open_folder)
        self.pause_button.Bind(wx.EVT_BUTTON, self._toggle_pause)
        self.cancel_button.Bind(wx.EVT_BUTTON, lambda event: self._cancel_download(False))
        self.clear_button.Bind(wx.EVT_BUTTON, self._cancel_all)
        # Dialog-generated Cancel events (including Escape) must confirm too.
        self.Bind(wx.EVT_BUTTON, self._close, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self._close)
        self.SetSizer(layout)
        self.SetMinSize((760, 580))
        self._refresh_lists()
        self.CentreOnParent()
        self.pending_list.SetFocus()

    def _refresh_lists(self) -> None:
        if not self or self.IsBeingDeleted():
            return
        groups = (
            (self.pending_list, [i for i, task in enumerate(self.tasks)
                                 if task.state in {'pending', 'downloading', 'cancelling', 'cancelled'}]),
            (self.completed_list, [i for i, task in enumerate(self.tasks) if task.state == 'complete']),
            (self.failed_list, [i for i, task in enumerate(self.tasks) if task.state in {'failed', 'skipped'}]),
        )
        show_failed = bool(groups[2][1])
        if self.failed_list.IsShown() != show_failed:
            self.failed_label.Show(show_failed)
            self.failed_list.Show(show_failed)
            self.Layout()
        for control, ids in groups:
            previous = [control.GetItemData(row) for row in range(control.GetItemCount())]
            selected = control.GetFirstSelected()
            selected_id = previous[selected] if selected >= 0 else None
            control.Freeze()
            try:
                if previous != ids:
                    control.DeleteAllItems()
                    for row, task_id in enumerate(ids):
                        control.InsertItem(row, self.tasks[task_id].item.title)
                        control.SetItemData(row, task_id)
                    if ids:
                        row = ids.index(selected_id) if selected_id in ids else min(max(selected, 0), len(ids) - 1)
                        control.Select(row)
                        control.Focus(row)
                for row, task_id in enumerate(ids):
                    task = self.tasks[task_id]
                    status = task.status
                    if task.state in {'pending', 'downloading'}:
                        if self.cancel.is_set() or self.controls[task_id].is_set():
                            status = '正在取消'
                        elif self.cancel.is_paused() or self.controls[task_id].is_paused():
                            status = '已暂停：' + status
                        elif (self.controls[task_id].started and not self.controls[task_id].done
                              and self.cancel.active != task_id):
                            status = '等待继续：' + status
                    values = [task.item.title, status]
                    if control is self.failed_list:
                        values.append(task.reason)
                    for column, text in enumerate(values):
                        if control.GetItemText(row, column) != text:
                            control.SetItem(row, column, text)
            finally:
                control.Thaw()

    def start(self) -> None:
        if not self or self.IsBeingDeleted() or self._started:
            return
        self._started = self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        def work(index, control):
            api = MaoerApi(cookie=self.cookie)
            try:
                return download_audio(api, self.tasks[index].item, self.request.folder, control,
                                      lambda playback: self._get_key(playback, control),
                                      lambda stage, percent: wx.CallAfter(self._task_progress, index, stage, percent),
                                      number_width=self.request.number_width)
            finally:
                api.session.close()

        def result(index, path, error):
            if error is None:
                # The worker records ownership before any queued UI callback;
                # a stop/clear racing the final rename cannot miss this file.
                try:
                    created = CreatedDownload.capture(path, self.request.folder)
                except OSError:
                    with self._output_lock:
                        self._ownership_errors[index] = '无法核实文件位置或身份，未自动删除'
                else:
                    with self._output_lock:
                        self._created_files[index] = created
                state, reason = 'complete', ''
            elif isinstance(error, DownloadCancelled) or self.controls[index].is_set():
                state, reason = 'cancelled', ''
            else:
                state = 'skipped' if isinstance(error, FileExistsError) else 'failed'
                reason = download_error(error)
            wx.CallAfter(self._task_result, index, state, reason)

        try:
            self.cancel.run(work, result)
        finally:
            wx.CallAfter(self._finished, self.cancel.is_set())

    def _task_progress(self, index: int, stage: str, percent: int) -> None:
        if not self or self.IsBeingDeleted() or not self.running:
            return
        task = self.tasks[index]
        if task.state not in {'pending', 'downloading'} or self.controls[index].is_set():
            return
        task.state = 'downloading'
        task.status = f'{stage} {max(0, min(100, percent))}%'
        self._refresh_lists()

    def _task_result(self, index: int, state: str, reason: str) -> None:
        if not self or self.IsBeingDeleted() or not self.running:
            return
        task = self.tasks[index]
        if task.state in {'complete', 'failed', 'skipped', 'cleared'}:
            return
        task.state = state
        task.status = {'complete': '完成', 'failed': '失败', 'skipped': '已跳过', 'cancelled': '已取消'}[state]
        task.reason = reason
        self._refresh_lists()
        if state == 'complete':
            play_download_completed_sound()
        elif state == 'failed':
            play_download_failed_sound()

    def _get_key(self, playback: PlaybackInfo, control: DownloadControl) -> bytes:
        ready = threading.Event()
        result = {}
        wx.CallAfter(self._request_key, playback, ready, result, control)
        while not ready.wait(0.1):
            check_cancel(control)
        check_cancel(control)
        if 'error' in result:
            raise ApiError(result['error'])
        return result['key']

    def _request_key(self, playback, ready, result, control) -> None:
        if control.is_set() or not self.running:
            result['error'] = '下载已取消'
            ready.set()
            return
        self._key_queue.append((playback, ready, result, control))
        self._start_next_key()

    def _start_next_key(self) -> None:
        if self.key_request is not None or not self.running:
            return
        while self._key_queue and self._key_queue[0][3].is_set():
            _playback, ready, result, _control = self._key_queue.pop(0)
            result['error'] = '下载已取消'
            ready.set()
        if not self._key_queue:
            self.timer.Stop()
            return
        if self.cancel.is_paused():
            self.timer.Start(500)
            return
        playback, ready, result, self.key_control = self._key_queue.pop(0)
        self.key_request = (playback, ready, result)
        self.key_started = self.cancel.active_time()
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
            self._start_next_key()
            return
        if self.key_control.is_set():
            self._key_done(error='下载已取消')
            return
        if self.cancel.is_paused():
            return
        if self.cancel.active_time() - self.key_started > 45:
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
        self.key_control = None
        if self.player is not None:
            self.player.stop()
        if request is not None:
            request[2]['error' if error else 'key'] = error or key
            request[1].set()
        if self.running and self._key_queue:
            wx.CallAfter(self._start_next_key)

    def _cancel_keys(self) -> None:
        queued, self._key_queue = self._key_queue, []
        for _playback, ready, result, _control in queued:
            result['error'] = '下载已取消'
            ready.set()
        self._key_done(error='下载已取消')

    def _on_task_key(self, event) -> None:
        if event.GetKeyCode() in (wx.WXK_MENU, wx.WXK_WINDOWS_MENU) or (event.GetKeyCode() == wx.WXK_F10 and event.ShiftDown()):
            if event.GetEventObject() is self.failed_list:
                self._show_failed_menu()
            else:
                self._show_task_menu()
            return
        event.Skip()

    def _on_task_context_menu(self, event) -> None:
        control = self.failed_list if event.GetEventObject() is self.failed_list else self.pending_list
        show = self._show_failed_menu if control is self.failed_list else self._show_task_menu
        position = event.GetPosition()
        if position == wx.DefaultPosition:
            show()
            return
        position = control.ScreenToClient(position)
        row, _flags = control.HitTest(position)
        if row == wx.NOT_FOUND:
            return
        previous = control.GetFirstSelected()
        if previous >= 0:
            control.Select(previous, False)
        control.Select(row)
        control.Focus(row)
        show(position)

    def _show_failed_menu(self, position=wx.DefaultPosition) -> None:
        row = self.failed_list.GetFirstSelected()
        if row < 0 or self._clearing or self._clear_requested or self.close_requested or self._confirming_cancel:
            return
        if self.running and self.cancel.is_set():
            return
        index = self.failed_list.GetItemData(row)
        menu = wx.Menu()
        restart = menu.Append(wx.ID_ANY, '重新开始(&R)')
        restart_all = menu.Append(wx.ID_ANY, '全部开始(&A)')
        restart.Enable(self.tasks[index].state == 'failed')
        restart_all.Enable(any(task.state == 'failed' for task in self.tasks))
        restart_id, all_id = restart.GetId(), restart_all.GetId()
        try:
            choice = self.failed_list.GetPopupMenuSelectionFromUser(menu, position)
        finally:
            menu.Destroy()
        if choice == restart_id:
            self._retry_tasks([index])
        elif choice == all_id:
            self._retry_tasks(list(range(len(self.tasks))))

    def _retry_tasks(self, indices: list[int]) -> None:
        if (not self or self.IsBeingDeleted() or self._clearing or self._clear_requested
                or self.close_requested or self._confirming_cancel or (self.running and self.cancel.is_set())):
            return
        indices = [index for index in indices if self.tasks[index].state == 'failed']
        if not indices:
            return
        if not self.running:
            self.cancel.clear()
            self.cancel.resume()
        for index in indices:
            task = self.tasks[index]
            task.state, task.status, task.reason = 'pending', '等待重试', ''
        self.cancel.retry(indices)
        self._retry_requested = True
        if not self.running:
            self._started = False
            self.start()
        self._refresh_controls()
        # Explicit retry moves focus with the task; background progress never does.
        for row in range(self.pending_list.GetItemCount()):
            self.pending_list.Select(row, self.pending_list.GetItemData(row) == indices[0])
        row = self.pending_list.GetFirstSelected()
        if row >= 0:
            self.pending_list.Focus(row)
            self.pending_list.EnsureVisible(row)
            self.pending_list.SetFocus()

    def _show_task_menu(self, position=wx.DefaultPosition) -> None:
        row = self.pending_list.GetFirstSelected()
        if row < 0 or not self.running or self.cancel.is_set() or self._clearing:
            return
        index = self.pending_list.GetItemData(row)
        control = self.controls[index]
        if control.is_set() or control.done or self.tasks[index].state not in {'pending', 'downloading'}:
            return
        menu = wx.Menu()
        pause_id = wx.NewIdRef()
        cancel_id = wx.NewIdRef()
        if control.started:
            menu.Append(int(pause_id), '继续下载(&P)' if control.is_paused() else '暂停下载(&P)')
        menu.Append(int(cancel_id), '取消下载(&C)')
        try:
            choice = self.pending_list.GetPopupMenuSelectionFromUser(menu, position)
        finally:
            menu.Destroy()
        # The task may have finished while the native menu was open.
        if control.done or control.is_set() or self.tasks[index].state not in {'pending', 'downloading'}:
            return
        if choice == int(pause_id) and control.started:
            control.resume() if control.is_paused() else control.pause()
            self._refresh_controls()
        elif choice == int(cancel_id):
            self._cancel_task(index)

    def _cancel_task(self, index: int) -> None:
        control = self.controls[index]
        if not self.running or control.is_set() or control.done or self._confirming_cancel:
            return
        was_paused = control.is_paused()
        control.pause()
        self._refresh_lists()
        self._confirming_cancel = True
        try:
            answer = message_box(f'是否取消下载“{self.tasks[index].item.title}”？',
                                 '取消下载', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self)
            if answer == wx.YES and not control.done:
                control.set()
                task = self.tasks[index]
                if task.state in {'pending', 'downloading'}:
                    task.state = 'cancelling' if control.started else 'cancelled'
                    task.status = '正在取消' if control.started else '已取消'
                if self.key_control is control:
                    self._key_done(error='下载已取消')
            elif not was_paused:
                control.resume()
        finally:
            self._confirming_cancel = False
        self._refresh_controls()
        self._finish_deferred()

    def _cancel_all(self, event) -> None:
        if self._clearing or self._clear_requested or self._confirming_cancel:
            return
        was_paused = self.cancel.is_paused()
        if self.running:
            self.cancel.pause()
        self._refresh_controls()
        self._confirming_cancel = True
        try:
            answer = message_box(
                '是否全部取消？这会停止所有下载，并清空本次任务已经下载的音频和临时文件。\n'
                '只删除本次任务生成的文件，原来已有的文件不会删除。删除后无法恢复。',
                '全部取消', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self)
            if answer == wx.YES:
                self._clear_requested = True
                self.cancel.set()
                self._cancel_keys()
            elif not was_paused:
                self.cancel.resume()
        finally:
            self._confirming_cancel = False
        self._refresh_controls()
        self._finish_deferred()
        if self._clear_requested and not self.running and not self._clearing:
            self._begin_clear()

    def _begin_clear(self) -> None:
        # Only after all file handles and worker threads have finished.
        if self.running or self._clearing:
            return
        self._clearing = True
        self._refresh_controls()
        with self._output_lock:
            files = dict(self._created_files)
            errors = dict(self._ownership_errors)

        def clean():
            removed = []
            for index, created in files.items():
                try:
                    created.remove()
                except OSError as exc:
                    errors[index] = str(exc) if str(exc) == '文件位置或内容已改变，为避免误删已保留' else '文件被占用、已改变或无法删除，请手动检查'
                else:
                    removed.append(index)
            wx.CallAfter(self._clear_finished, removed, errors)

        threading.Thread(target=clean, daemon=True).start()

    def _clear_finished(self, removed, errors) -> None:
        self._clearing = self._clear_requested = False
        with self._output_lock:
            for index in removed:
                self._created_files.pop(index, None)
                self._ownership_errors.pop(index, None)
                self.tasks[index].state, self.tasks[index].status = 'cleared', '已清理'
        self._refresh_controls()
        if errors:
            details = '\n'.join(f'{self.tasks[index].item.title}：{reason}' for index, reason in errors.items())
            message_box(f'已清理本次任务的 {len(removed)} 个文件。以下文件已保留：\n{details}',
                        '部分文件未清理', wx.OK | wx.ICON_WARNING, self)
        else:
            message_box(f'本次任务已全部取消，已清理本次下载的 {len(removed)} 个文件。原来已有的文件未改动。',
                        '全部取消', wx.OK | wx.ICON_INFORMATION, self)
        if self.close_requested:
            self.EndModal(wx.ID_CANCEL)
        else:
            self.close_button.SetFocus()

    def _toggle_pause(self, event) -> None:
        if not self.running or self.cancel.is_set():
            return
        if self.cancel.is_paused():
            self.cancel.resume()
        else:
            self.cancel.pause()
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        active = self.running and not self.cancel.is_set() and not self._clearing
        self.pause_button.Enable(active)
        self.cancel_button.Enable(active)
        self.pause_button.SetLabel('全部继续(&P)' if self.cancel.is_paused() else '全部暂停(&P)')
        self.clear_button.Enable(not self._clear_requested and not self._clearing and
                                 (self.running or bool(self._created_files)))
        self._refresh_lists()

    def _open_folder(self, event) -> None:
        root = self.request.download_root if self.request.download_root is not None else self.request.folder.parent
        try:
            root.mkdir(parents=True, exist_ok=True)
            if os.name == 'nt':
                os.startfile(str(root))
            elif not wx.LaunchDefaultApplication(str(root)):
                raise OSError()
        except OSError:
            message_box('无法打开下载文件夹，请检查路径和权限。', '打开文件夹', wx.OK | wx.ICON_ERROR, self)

    def _cancel_download(self, close_after: bool) -> None:
        if not self.running or self._confirming_cancel:
            return
        if self.cancel.is_set():
            self.close_requested |= close_after
            return
        was_paused = self.cancel.is_paused()
        self.cancel.pause()
        self._refresh_controls()
        self._confirming_cancel = True
        try:
            result = message_box('是否取消下载？已完成的文件会保留，未完成的临时文件会清理。',
                                 '取消下载', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self)
        finally:
            self._confirming_cancel = False
        if result == wx.YES:
            self.close_requested = close_after
            self.cancel.set()
            self._cancel_keys()
        elif not was_paused:
            self.cancel.resume()
        self._refresh_controls()
        self._finish_deferred()

    def _finish_deferred(self) -> None:
        if self._deferred_finished is not None:
            cancelled, self._deferred_finished = self._deferred_finished, None
            self._finished(cancelled)

    def _finished(self, cancelled: bool) -> None:
        if not self or self.IsBeingDeleted() or not self.running:
            return
        if self._confirming_cancel:
            self._deferred_finished = cancelled
            return
        if (self._retry_requested and not cancelled and not self.cancel.is_set()
                and any(not control.done for control in self.controls)):
            # A retry can arrive after the old queue exits but before this UI callback.
            threading.Thread(target=self._run, daemon=True).start()
            return
        self._retry_requested = False
        self.running = False
        cancelled = cancelled or self.cancel.is_set()
        self._cancel_keys()
        if self.player is not None:
            self.player.shutdown()
            self.player = None
        self.key_handler = None
        if cancelled:
            for task in self.tasks:
                if task.state in {'pending', 'downloading', 'cancelling'}:
                    task.state, task.status = 'cancelled', '已取消'
        self._refresh_controls()
        if self._clear_requested:
            self._begin_clear()
            return
        if self.close_requested:
            self.EndModal(wx.ID_CANCEL)
            return
        if not cancelled:
            completed = sum(task.state == 'complete' for task in self.tasks)
            failed = sum(task.state == 'failed' for task in self.tasks)
            skipped = sum(task.state == 'skipped' for task in self.tasks)
            removed = sum(task.state == 'cancelled' for task in self.tasks)
            message_box(f'恭喜，下载任务已完成。\n完成 {completed}，失败 {failed}，跳过 {skipped}，已取消 {removed}。',
                        '下载完成', wx.OK | wx.ICON_INFORMATION, self)
            if self.auto_close.GetValue():
                self.EndModal(wx.ID_OK)
                return
        if self.failed_list.GetItemCount():
            self.failed_list.SetFocus()
        elif self.completed_list.GetItemCount():
            self.completed_list.SetFocus()
        else:
            self.close_button.SetFocus()

    def _close(self, event) -> None:
        if self._clearing:
            self.close_requested = True
            if isinstance(event, wx.CloseEvent) and event.CanVeto():
                event.Veto()
            return
        if self.running:
            self._cancel_download(True)
            if isinstance(event, wx.CloseEvent) and event.CanVeto():
                event.Veto()
            return
        self.timer.Stop()
        self.EndModal(wx.ID_CANCEL)
