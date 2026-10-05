from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit

from comtypes import COMObject, COMMETHOD, GUID, HRESULT, IUnknown
import wx
import wx.html2 as html2

from ui_dialogs import message_box

from account_store import normalize_cookie
from app_paths import webview2_profile_dir
from login_dialog import CaptchaAudioPlayer, LOGIN_PROVIDERS, LoginDialog, run_dialog_task, validated_account
from maoer_api import BASE_URL, AccountInfo, ApiError, MaoerApi
from uia_live_region import HIDDEN_WEBVIEW_SCRIPT, ScreenReaderAnnouncer


# Only operate the official form; its own scripts handle login and verification.
PASSWORD_LOGIN_SCRIPT = r"""(function(loginName, password, regionLabel) {
    if (location.origin !== 'https://www.missevan.com' || location.pathname !== '/member/login')
        return 'manual';
    const form = document.querySelector('form[class*="legacy-password-login"]');
    if (!form) {
        const tab = Array.from(document.querySelectorAll('.tab-list .tab'))
            .find(item => item.textContent.trim() === '密码登录');
        if (tab && !tab.classList.contains('active')) tab.click();
        return 'waiting';
    }
    const email = loginName.includes('@');
    if (Boolean(form.querySelector('.email-input')) !== email) {
        const switcher = form.querySelector('.login-switch');
        if (!switcher) return 'manual';
        switcher.click();
        return 'waiting';
    }
    if (!email) {
        const normalize = text => text.replace(/[()\s]/g, '');
        const selected = form.querySelector('.region-select-item');
        if (!selected) return 'manual';
        if (normalize(selected.textContent) !== normalize(regionLabel)) {
            const option = Array.from(form.querySelectorAll('.region-dropdown-item'))
                .find(item => normalize(item.textContent) === normalize(regionLabel));
            if (option) option.click();
            return 'waiting';
        }
    }
    const nameInput = form.querySelector(email ? '.email-input input' : '.mobile-region input');
    const passwordInput = form.querySelector('input[type="password"]');
    const submit = form.querySelector('button[type="submit"]');
    if (!nameInput || !passwordInput || !submit || submit.disabled) return 'manual';
    // Use the native setter so React observes each input event.
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    for (const [input, value] of [[nameInput, loginName], [passwordInput, password]]) {
        setValue.call(input, value);
        input.dispatchEvent(new Event('input', {bubbles: true}));
    }
    submit.focus();
    submit.click();
    return 'submitted';
})"""


# The user hears and answers the challenge in native controls. Only forward
# that answer to the official widget; never construct verification tokens here.
VOICE_LOGIN_SCRIPT = r"""(function(action, answer) {
    if (location.origin !== 'https://www.missevan.com')
        return JSON.stringify({kind: 'error', message: '登录组件离开了官网，请重试。'});
    if (location.pathname !== '/member/login') return JSON.stringify({kind: 'account'});
    const error = Array.from(document.querySelectorAll(
        'form[class*="legacy-password-login"] .form-error-msg, .prelude-toast-text.show'
    )).find(element => element.textContent.trim());
    if (error) return JSON.stringify({kind: 'error', message: error.textContent.trim()});
    if (!window.__maoerNativeVoice) {
        window.__maoerNativeVoice = true;
        document.addEventListener('play', event => {
            if (event.target.matches('audio.geetest_music')) {
                event.target.muted = true;
                event.target.pause();
            }
        }, true);
    }
    const voice = document.querySelector('.geetest_voice_wrap');
    const audio = voice && voice.querySelector('audio.geetest_music');
    const input = voice && voice.querySelector('input.geetest_input');
    const button = voice && voice.querySelector('.geetest_btn');
    if (audio && audio.src && input && button) {
        audio.pause();
        audio.muted = true;
        window.__maoerVoiceRequested = false;
        if (action === 'answer') {
            input.focus();
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, answer);
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            button.click();
            return JSON.stringify({kind: 'submitted'});
        }
        const tip = voice.querySelector('.geetest_result_tip');
        if (tip && tip.__maoerRevision === undefined) {
            tip.__maoerRevision = 0;
            new MutationObserver(() => tip.__maoerRevision++).observe(tip,
                {attributes: true, childList: true, subtree: true, characterData: true});
        }
        const text = tip ? tip.textContent.trim() : '';
        return JSON.stringify({kind: 'voice', audio: audio.src, length: input.maxLength,
            error_revision: tip ? tip.__maoerRevision : 0,
            error: /错误|不正确|失败|重试|过期/.test(text) ? text : ''});
    }
    const switcher = document.querySelector('.geetest_voice');
    if (switcher && !window.__maoerVoiceRequested) {
        window.__maoerVoiceRequested = true;
        switcher.click();
    }
    return JSON.stringify({kind: 'loading'});
})"""


def login_url(provider: str | None = None) -> str:
    if provider is None:
        return BASE_URL + "/member/login?" + urlencode({"backurl": BASE_URL + "/"})
    if provider not in dict(LOGIN_PROVIDERS):
        raise ValueError("不支持的登录方式")
    return BASE_URL + "/member/authlogin?" + urlencode({"type": provider, "backurl": BASE_URL + "/"})


def is_login_return(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc in {"www.missevan.com", "missevan.com"}


class _DevToolsCompleted(IUnknown):
    _iid_ = GUID("{5C4889F0-5EF6-4C5A-952C-D8F1B92D0574}")
    _methods_ = [COMMETHOD([], HRESULT, "Invoke",
                          (["in"], HRESULT, "errorCode"), (["in"], ctypes.c_wchar_p, "result"))]


class _CookieResult(COMObject):
    _com_interfaces_ = [_DevToolsCompleted]

    def __init__(self, callback) -> None:
        self.callback = callback

    def Invoke(self, this, errorCode, result):
        # Dispatch outside the COM callback; the dialog may close on success.
        wx.CallAfter(self.callback, errorCode, result or "")
        return 0


def read_webview_cookies(webview: html2.WebView, url: str, callback):
    """Read URL-scoped cookies, including HttpOnly, from this WebView only."""
    backend = webview.GetNativeBackend()
    if not backend:
        raise ApiError("登录网页尚未准备好，请稍后重试")
    pointer = ctypes.c_void_p(int(backend))
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    # ICoreWebView2::CallDevToolsProtocolMethod is slot 36 in WebView2.h.
    # wx exposes ICoreWebView2 via GetNativeBackend, but has no cookie API.
    invoke = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p,
                               ctypes.c_wchar_p, ctypes.c_void_p)(vtable[36])
    handler = _CookieResult(callback)
    result = invoke(pointer, "Network.getCookies", json.dumps({"urls": [url]}),
                    handler.QueryInterface(_DevToolsCompleted))
    if result < 0:
        raise ApiError("无法读取网页登录结果，请重试")
    return handler


def _remove_login_profile(path: Path) -> None:
    # WebView2 releases its file locks shortly after the last window closes.
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.25)
    # Any remaining files are covered by the existing app-exit profile cleanup.


class BrowserLoginDialog(wx.Dialog):
    def __init__(
        self, parent: wx.Window, provider: str | None = None, *, login_name: str = "", password: str = "",
        region_label: str = "中国大陆 +86", background: bool = False,
    ) -> None:
        if not html2.WebView.IsBackendAvailable(html2.WebViewBackendEdge):
            raise ApiError("请先安装 Microsoft Edge WebView2 Runtime")
        url = login_url(provider)
        title = dict(LOGIN_PROVIDERS).get(provider, "官网网页") + "登录"
        super().__init__(parent, title=title, size=(900, 720),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.api = MaoerApi(cookie="")
        self.account_info: AccountInfo | None = None
        self.cookie_header = ""
        self._checking = False
        self._background = background
        self._last_cookie = ""
        self._cookie_request = None
        self._password_pending = (
            (login_name, password, region_label) if provider is None and login_name and password else None
        )
        self._password_attempts = 0
        self._password_inflight = False
        self._password_timer: wx.CallLater | None = None
        self._popups: list[wx.Dialog] = []
        self._profile = Path(tempfile.mkdtemp(prefix="login-", dir=webview2_profile_dir()))
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.StaticText(panel, label="请在官方网页完成登录，完成后自动返回。")
        self.announcer = ScreenReaderAnnouncer(self.status, native_only=True)
        root.Add(self.status, 0, wx.EXPAND | wx.ALL, 10)
        # The player also sets this environment variable. Each authorization
        # attempt must create its own profile without altering the player's.
        previous = os.environ.get("WEBVIEW2_USER_DATA_FOLDER")
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(self._profile)
        try:
            # Match the player's offscreen creation; hiding after SetFocus still
            # exposes the browser briefly to screen readers during native login.
            geometry = {"pos": (-32000, -32000), "size": (1, 1)} if background else {}
            self.webview = html2.WebView.New(
                panel, url="about:blank", backend=html2.WebViewBackendEdge, **geometry)
            if background:
                self.webview.Hide()
                self.webview.AddUserScript(HIDDEN_WEBVIEW_SCRIPT, html2.WEBVIEW_INJECT_AT_DOCUMENT_START)
        except Exception:
            self.Destroy()
            raise
        finally:
            if previous is None:
                os.environ.pop("WEBVIEW2_USER_DATA_FOLDER", None)
            else:
                os.environ["WEBVIEW2_USER_DATA_FOLDER"] = previous
        self.webview.SetName("官方登录网页")
        self._bind_webview(self.webview)
        self.webview.Bind(html2.EVT_WEBVIEW_SCRIPT_RESULT, self._on_password_script)
        if not background:
            root.Add(self.webview, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.check_button = wx.Button(panel, label="完成登录")
        self.cancel_button = wx.Button(panel, wx.ID_CANCEL, label="取消")
        buttons.AddStretchSpacer()
        buttons.Add(self.check_button, 0, wx.RIGHT, 8)
        buttons.Add(self.cancel_button)
        root.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetMinSize((640, 480))
        self.check_button.Bind(wx.EVT_BUTTON, lambda event: self.check_login(manual=True))
        self.CentreOnParent()
        self.webview.LoadURL(url)
        if not background:
            self.webview.SetFocus()

    def _bind_webview(self, webview) -> None:
        webview.Bind(html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating)
        webview.Bind(html2.EVT_WEBVIEW_LOADED, self._on_loaded)
        webview.Bind(html2.EVT_WEBVIEW_NEWWINDOW, self._on_new_window)
        webview.Bind(html2.EVT_WEBVIEW_ERROR, self._on_error)
        if hasattr(html2, "wxEVT_WEBVIEW_NEWWINDOW_FEATURES"):
            # Phoenix exposes the event types without their EVT_* binders.
            webview.Bind(wx.PyEventBinder(html2.wxEVT_WEBVIEW_NEWWINDOW_FEATURES, 1), self._on_window_features)
            webview.Bind(wx.PyEventBinder(html2.wxEVT_WEBVIEW_WINDOW_CLOSE_REQUESTED, 1), self._on_window_close)

    def _is_active(self) -> bool:
        # EndModal hides the window before it clears the modal flag.
        return bool(self) and not self.IsBeingDeleted() and self.IsModal() and self.IsShown()

    def _finish_login(self) -> None:
        self.EndModal(wx.ID_OK)

    def _on_navigating(self, event) -> None:
        if event.GetURL() != "about:blank" and urlsplit(event.GetURL()).scheme != "https":
            event.Veto()
            if not self._background:
                self.announcer.announce("请继续在网页中登录或扫码登录。")

    def _on_loaded(self, event) -> None:
        if self._password_pending and urlsplit(event.GetURL()).path == "/member/login":
            wx.CallAfter(self._submit_password)
            return
        if is_login_return(event.GetURL()):
            wx.CallAfter(self.check_login)

    def _submit_password(self) -> None:
        if not self._is_active() or not self._password_pending or self._password_inflight:
            return
        url = urlsplit(self.webview.GetCurrentURL())
        if url.scheme != "https" or url.netloc != "www.missevan.com" or url.path != "/member/login":
            self._password_done(False)
            return
        self._password_attempts += 1
        self._password_inflight = True
        script = PASSWORD_LOGIN_SCRIPT + "(" + ",".join(json.dumps(value) for value in self._password_pending) + ")"
        try:
            self.webview.RunScriptAsync(script)
        except Exception:
            self._password_done(False)

    def _on_password_script(self, event) -> None:
        if not self._is_active() or not self._password_inflight:
            return
        self._password_inflight = False
        # Never include script results or errors in messages: they may contain credentials.
        result = event.GetString().strip('"') if not event.IsError() else "manual"
        # ponytail: allow 10 s for the current form; update selectors if the site
        # changes, while keeping manual website login available in the meantime.
        if result == "waiting" and self._password_attempts < 40:
            self._password_timer = wx.CallLater(250, self._submit_password)
        else:
            self._password_done(result == "submitted")

    def _password_done(self, submitted: bool) -> None:
        self._password_pending = None
        self._password_inflight = False
        if self._password_timer is not None:
            self._password_timer.Stop()
            self._password_timer = None
        message = "请在官网页面完成验证，成功后自动返回。" if submitted else "未能自动填写，请在官网页面手动登录。"
        self.status.SetLabel(message)
        self.announcer.announce(message)
        self.webview.SetFocus()

    def _on_error(self, event) -> None:
        self.announcer.announce("网页未能加载，请检查网络后关闭并重试。")

    def _on_new_window(self, event) -> None:
        url = event.GetURL()
        if url != "about:blank" and urlsplit(url).scheme != "https":
            event.Veto()
        elif not hasattr(html2, "wxEVT_WEBVIEW_NEWWINDOW_FEATURES"):
            # ponytail: wx 4.2 follows links here; wx 4.3+ preserves popup/opener state.
            event.Veto()
            self.webview.LoadURL(url)

    def _on_window_features(self, event) -> None:
        popup = wx.Dialog(self, title="官方登录授权", size=(800, 650),
                          style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        child = event.GetTargetWindowFeatures().GetChildWebView()
        child.Create(popup)
        self._bind_webview(child)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(child, 1, wx.EXPAND)
        complete = wx.Button(popup, label="完成登录")
        complete.Bind(wx.EVT_BUTTON, lambda event: self.check_login(manual=True))
        root.Add(complete, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        popup.SetSizer(root)
        popup.Bind(wx.EVT_CLOSE, lambda event: self._close_popup(popup))
        self._popups.append(popup)
        popup.CentreOnParent()
        popup.Show()
        child.SetFocus()

    def _close_popup(self, popup) -> None:
        popup.Destroy()
        wx.CallAfter(self.check_login)
        self.webview.SetFocus()

    def _on_window_close(self, event) -> None:
        window = wx.FindWindowById(event.GetId())
        popup = wx.GetTopLevelParent(window) if window else None
        if popup in self._popups:
            self._close_popup(popup)
        else:
            wx.CallAfter(self.check_login)

    def check_login(self, manual: bool = False) -> None:
        if not self._is_active() or self._checking:
            return
        self._checking = True
        self.check_button.Disable()
        try:
            self._cookie_request = read_webview_cookies(
                self.webview, BASE_URL + "/", lambda error, result: self._cookies_ready(error, result, manual))
        except Exception:
            self._failed(ApiError("无法读取登录结果，请等待网页加载后重试"), manual)

    def _cookies_ready(self, error: int, result: str, manual: bool) -> None:
        if not self._is_active():
            return
        self._cookie_request = None
        if error < 0:
            self._failed(ApiError("无法读取登录结果，请重试"), manual)
            return
        try:
            cookie = normalize_cookie(result)
        except (ValueError, TypeError):
            self._failed(ApiError("尚未完成猫耳登录，请先在网页中完成授权"), manual)
            return
        if cookie == self._last_cookie and not manual:
            self._failed(None, False)
            return
        self._last_cookie = cookie
        api = MaoerApi(cookie=cookie)
        self.announcer.announce("正在确认登录账号…")
        def done(account):
            self.api = api
            self.cookie_header = api.cookie_header
            self.account_info = account
            self._finish_login()
        run_dialog_task(self, lambda: validated_account(api), done, lambda exc: self._failed(exc, manual),
                        active=self._is_active)

    def _failed(self, exc, manual: bool) -> None:
        self._checking = False
        self.check_button.Enable()
        self.announcer.announce("请在官方网页完成登录，再按“完成登录”。")
        if manual and exc:
            message_box("尚未确认登录成功，请完成官网验证或检查网络后重试。",
                          "登录未完成", wx.OK | wx.ICON_INFORMATION, self)
            self.webview.SetFocus()

    def Destroy(self) -> bool:
        self._password_pending = None
        if self._password_timer is not None:
            self._password_timer.Stop()
            self._password_timer = None
        self.announcer.close()
        for popup in self._popups:
            if popup:
                popup.Destroy()
        result = super().Destroy()
        threading.Thread(target=_remove_login_profile, args=(self._profile,), daemon=True).start()
        return result


class NativePasswordLoginDialog(BrowserLoginDialog):
    """Native voice verification backed by the unmodified official login flow."""

    def __init__(self, parent, provider=None, **credentials) -> None:
        self._on_complete = None
        self._prepare_timer = None
        self._error = ""
        self._voice_timer = None
        self._voice_inflight = False
        self._pending_answer = ""
        self._verifying = False
        self._voice_url = ""
        self._voice_error = None
        self._answer_length = 6
        self._audio_player = None
        self._deadline = time.monotonic() + 30
        super().__init__(parent, provider, background=True, **credentials)
        self.SetTitle("语音验证码")
        self.check_button.Hide()
        self.status.SetLabel("正在准备语音验证码…")
        panel = self.status.GetParent()
        root = panel.GetSizer()
        fields = wx.BoxSizer(wx.VERTICAL)
        self.voice_box = LoginDialog._text_field(panel, fields, "语音验证码", wx.TE_PROCESS_ENTER)
        root.Insert(1, fields, 0, wx.EXPAND)
        buttons = root.GetItem(root.GetItemCount() - 1).GetSizer()
        self.play_button = wx.Button(panel, label="播放验证码")
        self.confirm_button = wx.Button(panel, label="确认验证码")
        buttons.Insert(0, self.play_button, 0, wx.RIGHT, 8)
        buttons.Insert(buttons.GetItemCount() - 1, self.confirm_button, 0, wx.RIGHT, 8)
        self.voice_box.MoveBeforeInTabOrder(self.cancel_button)
        self.play_button.MoveAfterInTabOrder(self.voice_box)
        self.confirm_button.MoveAfterInTabOrder(self.play_button)
        self.confirm_button.SetDefault()
        self.play_button.Bind(wx.EVT_BUTTON, lambda event: self._audio_player.replay())
        self.confirm_button.Bind(wx.EVT_BUTTON, self.on_confirm)
        self.voice_box.Bind(wx.EVT_TEXT_ENTER, self.on_confirm)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self._audio_player = CaptchaAudioPlayer(self)
        self._enable_voice(False)
        self.SetMinSize((440, 200))
        self.SetSize((440, 220))
        self.Layout()
        self.CentreOnParent()

    def _is_active(self) -> bool:
        if not self or self.IsBeingDeleted():
            return False
        if self.IsModal():
            return self.IsShown()
        parent = self.GetParent()
        return self._on_complete is not None and bool(parent) and parent.IsModal() and parent.IsShown()

    def start(self, callback) -> None:
        self._on_complete = callback
        self._prepare_timer = wx.CallLater(30000, self._native_failed, "登录验证等待超时，请重试。")

    def _show_voice(self) -> None:
        if not self._is_active() or self.IsModal():
            return
        if self._prepare_timer is not None:
            self._prepare_timer.Stop()
        wx.CallAfter(self._focus_voice)
        self._complete(self.ShowModal())

    def _focus_voice(self) -> None:
        if self._is_active() and self.IsModal():
            self.voice_box.SetFocus()
            self.announcer.announce(self.status.GetLabel())
            self._audio_player.play(self._voice_url)

    def _finish_login(self) -> None:
        if self.IsModal():
            self.EndModal(wx.ID_OK)
        else:
            self._complete(wx.ID_OK)

    def _complete(self, result: int) -> None:
        callback, self._on_complete = self._on_complete, None
        self._password_pending = None
        self._pending_answer = ""
        for timer in (self._prepare_timer, self._voice_timer, self._password_timer):
            if timer is not None:
                timer.Stop()
        if callback:
            wx.CallAfter(callback, result, self._error)

    def _enable_voice(self, enabled: bool) -> None:
        self.voice_box.Enable(enabled)
        self.play_button.Enable(enabled)
        self.confirm_button.Enable(enabled)

    def on_char_hook(self, event) -> None:
        if event.GetKeyCode() == wx.WXK_CONTROL and self.FindFocus() is self.voice_box:
            self._audio_player.replay()
        else:
            event.Skip()

    def _password_done(self, submitted: bool) -> None:
        self._password_pending = None
        self._password_inflight = False
        if self._password_timer is not None:
            self._password_timer.Stop()
            self._password_timer = None
        if submitted:
            self._poll_voice()
        else:
            self._native_failed("无法准备密码登录，请重试或手动选择“使用官网网页登录”。")

    def _poll_voice(self) -> None:
        if not self._is_active() or self._voice_inflight:
            return
        if self._deadline is not None and time.monotonic() > self._deadline:
            self._native_failed("登录验证等待超时，请重试。")
            return
        url = urlsplit(self.webview.GetCurrentURL())
        if self._pending_answer and (url.scheme != "https" or url.netloc != "www.missevan.com" or url.path != "/member/login"):
            self._native_failed("验证页面已变化，请重新登录。")
            return
        answer, self._pending_answer = self._pending_answer, ""
        self._voice_inflight = True
        script = VOICE_LOGIN_SCRIPT + f"({json.dumps('answer' if answer else 'inspect')}, {json.dumps(answer)})"
        try:
            self.webview.RunScriptAsync(script)
        except Exception:
            self._native_failed("无法读取登录验证状态，请重试。")

    def _on_password_script(self, event) -> None:
        if self._password_inflight:
            super()._on_password_script(event)
            return
        if not self._is_active() or not self._voice_inflight:
            return
        self._voice_inflight = False
        try:
            if event.IsError():
                raise ValueError
            state = json.loads(event.GetString())
            if isinstance(state, str):
                state = json.loads(state)
            if not isinstance(state, dict):
                raise ValueError
        except (ValueError, TypeError):
            self._native_failed("无法读取登录验证状态，请重试。")
            return
        kind = state.get("kind")
        if kind == "error":
            self._native_failed(state.get("message") or "登录失败，请重试。")
            return
        if kind == "voice":
            url = str(state.get("audio") or "")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".geetest.com"):
                self._native_failed("无法读取官方语音验证码，请重试。")
                return
            error = str(state.get("error") or "")
            error_key = (error, state.get("error_revision"))
            changed = url != self._voice_url
            if changed or (error and error_key != self._voice_error):
                self._voice_url = url
                self._verifying = False
                self._deadline = None
                length = state.get("length")
                self._answer_length = length if isinstance(length, int) and 1 <= length <= 12 else 6
                self.voice_box.ChangeValue("")
                self._enable_voice(True)
                message = error or "请输入语音验证码，在编辑框按 Ctrl 可以重播。"
                self.status.SetLabel(message)
                if self.IsModal():
                    self.announcer.announce(message)
                    self.voice_box.SetFocus()
                    if changed:
                        self._audio_player.play(url)
                else:
                    wx.CallAfter(self._show_voice)
            self._voice_error = error_key
        elif kind == "account":
            self.check_login()
        if self._is_active():
            if self._voice_timer is not None:
                self._voice_timer.Stop()
            self._voice_timer = wx.CallLater(250, self._poll_voice)

    def on_confirm(self, _event) -> None:
        if self._verifying or not self._voice_url:
            return
        answer = self.voice_box.GetValue().strip()
        if len(answer) != self._answer_length or not answer.isascii() or not answer.isdigit():
            message_box(f"请输入 {self._answer_length} 位语音验证码", "验证码", wx.OK | wx.ICON_INFORMATION, self)
            self.voice_box.SetFocus()
            return
        self._pending_answer = answer
        self._verifying = True
        self._deadline = time.monotonic() + 30
        self._enable_voice(False)
        self.status.SetLabel("正在验证并登录…")
        self.announcer.announce("正在验证并登录…")
        self._poll_voice()

    def _failed(self, exc, manual: bool) -> None:
        self._checking = False
        if exc and urlsplit(self.webview.GetCurrentURL()).path != "/member/login":
            self._native_failed("无法确认登录账号，请重试。")

    def _on_error(self, event) -> None:
        self._native_failed("登录组件加载失败，请检查网络或取消后重试。")

    def _on_new_window(self, event) -> None:
        event.Veto()
        self._native_failed("官网要求额外操作，请手动选择“使用官网网页登录”。")

    def _native_failed(self, message: str) -> None:
        if not self._is_active():
            return
        self._error = message
        self._pending_answer = ""
        self._password_pending = None
        if self._voice_timer is not None:
            self._voice_timer.Stop()
        if self._password_timer is not None:
            self._password_timer.Stop()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self._complete(wx.ID_CANCEL)

    def Destroy(self) -> bool:
        self._on_complete = None
        if self._prepare_timer is not None:
            self._prepare_timer.Stop()
        self._pending_answer = ""
        if self._voice_timer is not None:
            self._voice_timer.Stop()
        if self._audio_player is not None:
            self._audio_player.destroy()
        return super().Destroy()
