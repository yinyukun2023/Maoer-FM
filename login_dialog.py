from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import TYPE_CHECKING

import requests
import wx

from account_store import LoginCredentials, SavedAccount, normalize_cookie, unprotect_login
from maoer_api import AccountInfo, ApiError, LoginCaptcha, MaoerApi, USER_AGENT
from uia_live_region import ScreenReaderAnnouncer, set_native_accessible_name

if TYPE_CHECKING:
    from app import MaoerFrame


PHONE_RE = re.compile(r"^1[3-9][0-9]{9}$")
LOGIN_PROVIDERS = (("qq", "QQ"), ("wechat", "微信"), ("weibo", "微博"), ("bilibili", "哔哩哔哩"))


def valid_phone(phone: str, region: str) -> bool:
    return bool(PHONE_RE.fullmatch(phone) if region == "CN" else re.fullmatch(r"[0-9]{5,15}", phone))


def validated_account(api: MaoerApi) -> AccountInfo:
    account = api.account_info()
    if account.user_id is None or account.user_id <= 0:
        raise ApiError("无法确认登录账号，请更新 Cookie 或重新登录")
    return account


def run_dialog_task(dialog, work, done, failed, *, active=None) -> None:
    def deliver(callback, result) -> None:
        if dialog and not dialog.IsBeingDeleted() and (active() if active else dialog.IsModal() and dialog.IsShown()):
            callback(result)

    def runner() -> None:
        try:
            result = work()
        except Exception as exc:
            wx.CallAfter(deliver, failed, exc)
        else:
            wx.CallAfter(deliver, done, result)

    threading.Thread(target=runner, daemon=True).start()


class CaptchaAudioPlayer:
    def __init__(self, parent: wx.Window) -> None:
        self.parent = parent
        self._audio_url = ""
        self._cached_url = ""
        self._audio_file: Path | None = None
        self._alias = f"maoer_captcha_{id(self)}"
        self._lock = threading.Lock()
        self._closed = False

    def play(self, audio_url: str) -> None:
        if self._closed:
            return
        self._audio_url = audio_url
        threading.Thread(target=self._play_worker, args=(audio_url,), daemon=True).start()

    def replay(self) -> None:
        if not self._audio_url:
            return
        self.play(self._audio_url)

    def destroy(self) -> None:
        self._closed = True
        self._close_mci()
        self._delete_cached_file()

    def _play_worker(self, audio_url: str) -> None:
        try:
            with self._lock:
                if self._closed:
                    return
                audio_file = self._download_audio(audio_url)
                if self._closed:
                    self._delete_cached_file()
                    return
                wx.CallAfter(self._play_downloaded, audio_file)
        except Exception as exc:
            wx.CallAfter(self._report_error, exc)

    def _play_downloaded(self, audio_file: Path) -> None:
        if self._closed:
            return
        try:
            self._play_file(audio_file)
        except Exception as exc:
            self._report_error(exc)

    def _report_error(self, exc: Exception) -> None:
        if not self._closed and self.parent and not self.parent.IsBeingDeleted():
            wx.MessageBox(f"验证码播放失败：{exc}", "播放失败", wx.OK | wx.ICON_ERROR, self.parent)

    def _download_audio(self, audio_url: str) -> Path:
        if self._cached_url == audio_url and self._audio_file and self._audio_file.exists():
            return self._audio_file

        response = requests.get(
            audio_url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://www.missevan.com/",
            },
            timeout=15,
        )
        response.raise_for_status()

        self._delete_cached_file()
        handle, path = tempfile.mkstemp(prefix="maoer_captcha_", suffix=".mp3")
        os.close(handle)
        audio_file = Path(path)
        audio_file.write_bytes(response.content)
        self._cached_url = audio_url
        self._audio_file = audio_file
        return audio_file

    def _play_file(self, audio_file: Path) -> None:
        if os.name != "nt":
            raise RuntimeError("当前验证码播放方式仅支持 Windows")
        self._close_mci()
        path = str(audio_file)
        try:
            self._mci(f'open "{path}" type mpegvideo alias {self._alias}')
        except RuntimeError:
            self._mci(f'open "{path}" alias {self._alias}')
        self._mci(f"play {self._alias} from 0")

    def _close_mci(self) -> None:
        if os.name == "nt":
            try:
                self._mci(f"close {self._alias}")
            except RuntimeError:
                pass

    def _delete_cached_file(self) -> None:
        if self._audio_file is not None:
            try:
                self._audio_file.unlink(missing_ok=True)
            except OSError:
                pass
        self._audio_file = None
        self._cached_url = ""

    @staticmethod
    def _mci(command: str) -> None:
        winmm = ctypes.WinDLL("winmm")
        winmm.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
        winmm.mciSendStringW.restype = ctypes.c_uint
        winmm.mciGetErrorStringW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
        winmm.mciGetErrorStringW.restype = ctypes.c_int
        buffer = ctypes.create_unicode_buffer(512)
        error = winmm.mciSendStringW(command, buffer, len(buffer), 0)
        if error:
            message = ctypes.create_unicode_buffer(512)
            winmm.mciGetErrorStringW(error, message, len(message))
            raise RuntimeError(message.value or f"MCI 错误 {error}")


class VoiceCaptchaDialog(wx.Dialog):
    def __init__(
        self, parent: wx.Window, api: MaoerApi, phone: str, captcha: LoginCaptcha, region: str = "CN",
    ) -> None:
        super().__init__(parent, title="语音验证码", size=(420, 180))
        self.api = api
        self.phone = phone
        self.region = region
        self.captcha = captcha
        self._busy = False
        self._audio_player = CaptchaAudioPlayer(self)

        self._build_ui()
        self._bind_events()
        self.voice_box.SetFocus()
        wx.CallAfter(self._play_current_captcha)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        self.voice_label = wx.StaticText(panel, label="语音验证码")
        self.voice_box = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.voice_box.SetName("语音验证码")
        root.Add(self.voice_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        root.Add(self.voice_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        self.play_button = wx.Button(panel, label="播放验证码")
        self.confirm_button = wx.Button(panel, wx.ID_OK, label="确认验证码并发送短信")
        self.cancel_button = wx.Button(panel, wx.ID_CANCEL, label="取消")
        button_row.Add(self.play_button, 0, wx.RIGHT, 8)
        button_row.AddStretchSpacer(1)
        button_row.Add(self.confirm_button, 0, wx.RIGHT, 8)
        button_row.Add(self.cancel_button, 0)
        root.Add(button_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.CentreOnParent()

    def _bind_events(self) -> None:
        self.play_button.Bind(wx.EVT_BUTTON, self.on_play_captcha)
        self.confirm_button.Bind(wx.EVT_BUTTON, self.on_confirm)
        self.voice_box.Bind(wx.EVT_TEXT_ENTER, self.on_confirm)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

    def on_char_hook(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_CONTROL and self.FindFocus() is self.voice_box:
            self._play_current_captcha()
            return
        event.Skip()

    def on_play_captcha(self, _event: wx.Event) -> None:
        self._play_current_captcha()

    def _play_current_captcha(self) -> None:
        self._audio_player.play(self.captcha.voice_url)

    def on_confirm(self, _event: wx.Event) -> None:
        if self._busy:
            return
        voice_answer = self.voice_box.GetValue().strip()
        if not voice_answer:
            wx.MessageBox("请输入语音验证码", "错误", wx.OK | wx.ICON_ERROR, self)
            self.voice_box.SetFocus()
            return

        self._set_busy(True)

        def work() -> None:
            self.api.send_login_sms_code(self.phone, self.captcha, voice_answer, self.region)

        def done(_result) -> None:
            self._set_busy(False)
            self.EndModal(wx.ID_OK)

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            wx.MessageBox(str(exc) or type(exc).__name__, "错误", wx.OK | wx.ICON_ERROR, self)
            self.voice_box.SetFocus()

        self._run_async(work, done, failed)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.SetTitle("语音验证码 - 正在验证..." if busy else "语音验证码")
        self.voice_box.Enable(not busy)
        self.play_button.Enable(not busy)
        self.confirm_button.Enable(not busy)

    def _run_async(self, work, done, failed) -> None:
        run_dialog_task(self, work, done, failed)

    def Destroy(self) -> bool:
        self._audio_player.destroy()
        return super().Destroy()


class LoginDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, api: MaoerApi, *, saved: SavedAccount | None = None) -> None:
        super().__init__(parent, title="编辑账号" if saved else "账号登录",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.api = api
        self.saved = saved
        self.login: LoginCredentials | None = None
        self.note = saved.note if saved else ""
        self._saved_login = None
        message = ""
        if saved:
            try:
                self._saved_login = unprotect_login(saved.credentials)
            except ValueError as exc:
                message = str(exc)
            if not saved.credentials:
                message = "此账号尚未保存账号密码，请填写后保存。"
        self.cookie_header = ""
        self.account_info: AccountInfo | None = None
        self._busy = False
        self._countdown = 0
        self._countdown_timer: wx.CallLater | None = None
        self._native_login = None
        self._regions: list[tuple[str, str]] = []
        self.sms_region = self.password_region = "CN"
        self._build_ui()
        if saved:
            self.notebook.SetSelection(1)
            if self._saved_login:
                login = self._saved_login
                self.login_name_box.ChangeValue(login.username)
                self.password_box.ChangeValue(login.password)
                if "@" not in login.username:
                    self.phone_box.ChangeValue(login.username)
                self.sms_region = self.password_region = login.region
                for button in (self.sms_region_button, self.password_region_button):
                    button.SetLabel(f"国家/地区：{login.region_label}")
            self.status.SetLabel(message)
            self.login_name_box.SetFocus()
        else:
            self.notebook.SetFocus()

    @staticmethod
    def _text_field(page, sizer, label: str, style: int = 0) -> wx.TextCtrl:
        sizer.Add(wx.StaticText(page, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        field = wx.TextCtrl(page, style=style)
        set_native_accessible_name(field, label)
        sizer.Add(field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        return field

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(panel)
        self.notebook.SetName("登录方式")
        sms_page = wx.Panel(self.notebook)
        sms_sizer = wx.BoxSizer(wx.VERTICAL)
        self.sms_region_button = wx.Button(sms_page, label="国家/地区：中国大陆 +86")
        sms_sizer.Add(self.sms_region_button, 0, wx.ALL, 10)
        self.phone_box = self._text_field(sms_page, sms_sizer, "手机号")
        self.get_captcha_button = wx.Button(sms_page, label="获取验证码")
        sms_sizer.Add(self.get_captcha_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.sms_box = self._text_field(sms_page, sms_sizer, "短信验证码", wx.TE_PROCESS_ENTER)
        sms_page.SetSizer(sms_sizer)
        self.notebook.AddPage(sms_page, "短信登录")

        password_page = wx.Panel(self.notebook)
        password_sizer = wx.BoxSizer(wx.VERTICAL)
        self.password_region_button = wx.Button(password_page, label="国家/地区：中国大陆 +86")
        password_sizer.Add(self.password_region_button, 0, wx.ALL, 10)
        self.login_name_box = self._text_field(password_page, password_sizer, "手机号或邮箱")
        self.password_box = self._text_field(password_page, password_sizer, "密码", wx.TE_PASSWORD | wx.TE_PROCESS_ENTER)
        self.web_login_button = wx.Button(password_page, label="使用官网网页登录")
        password_sizer.Add(self.web_login_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        password_page.SetSizer(password_sizer)
        self.notebook.AddPage(password_page, "密码登录")

        for provider, label in LOGIN_PROVIDERS:
            page = wx.Panel(self.notebook)
            sizer = wx.BoxSizer(wx.VERTICAL)
            description = "使用微信扫描官方二维码完成登录。" if provider == "wechat" else f"在{label}官方授权页完成登录。"
            sizer.Add(wx.StaticText(page, label=description), 0, wx.ALL, 10)
            sizer.Add(wx.StaticText(page, label="登录成功后自动加入账号管理。"), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
            page.SetSizer(sizer)
            self.notebook.AddPage(page, label)
        root.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 10)
        self.note_box = None
        if self.saved:
            self.note_box = self._text_field(panel, root, "备注")
            self.note_box.ChangeValue(self.note)
        self.status = wx.StaticText(panel)
        self.announcer = ScreenReaderAnnouncer(self.status, native_only=True)
        root.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.login_button = wx.Button(panel, wx.ID_OK, label="保存" if self.saved else "登录")
        self.cancel_button = wx.Button(panel, wx.ID_CANCEL, label="取消")
        buttons.AddStretchSpacer()
        buttons.Add(self.login_button, 0, wx.RIGHT, 8)
        buttons.Add(self.cancel_button)
        root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetMinSize((600, self.GetSize().height))
        self.SetSize(self.GetMinSize())
        self.CentreOnParent()
        self.login_button.SetDefault()
        self.get_captcha_button.Bind(wx.EVT_BUTTON, self.on_get_captcha)
        self.login_button.Bind(wx.EVT_BUTTON, self.on_login)
        self.sms_box.Bind(wx.EVT_TEXT_ENTER, self.on_login)
        self.password_box.Bind(wx.EVT_TEXT_ENTER, self.on_login)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_method_changed)
        self.sms_region_button.Bind(wx.EVT_BUTTON, lambda event: self.on_choose_region(False))
        self.password_region_button.Bind(wx.EVT_BUTTON, lambda event: self.on_choose_region(True))
        self.web_login_button.Bind(wx.EVT_BUTTON, lambda event: self._browser_login())

    def on_method_changed(self, event: wx.Event) -> None:
        index = self.notebook.GetSelection()
        self.login_button.SetLabel(("保存" if self.saved else "登录") if index < 2
                                   else f"打开{LOGIN_PROVIDERS[index - 2][1]}登录")
        event.Skip()

    def on_choose_region(self, password: bool) -> None:
        if self._busy:
            return
        def ready(regions):
            self._regions = regions
            self._set_busy(False)
            current = self.password_region if password else self.sms_region
            dialog = wx.SingleChoiceDialog(self, "选择国家/地区", "国家/地区", [label for _, label in regions])
            try:
                dialog.SetSelection(next((i for i, (code, _) in enumerate(regions) if code == current), 0))
                if dialog.ShowModal() == wx.ID_OK:
                    code, label = regions[dialog.GetSelection()]
                    if password:
                        self.password_region = code
                    else:
                        self.sms_region = code
                    button.SetLabel(f"国家/地区：{label}")
            finally:
                dialog.Destroy()
                button.SetFocus()
        button = self.password_region_button if password else self.sms_region_button
        if self._regions:
            ready(self._regions)
        else:
            self._set_busy(True, "正在获取国家/地区…")
            self._run_async(self.api.login_regions, ready, self._operation_failed)

    def on_get_captcha(self, _event: wx.Event) -> None:
        if self._busy or self._countdown > 0:
            return
        if not valid_phone(self.phone_box.GetValue().strip(), self.sms_region):
            self._show_error("手机号格式不正确")
            self.phone_box.SetFocus()
            return
        self._set_busy(True, "正在获取语音验证码…")
        self._run_async(self.api.start_login_captcha, self._captcha_ready, self._operation_failed)

    def _captcha_ready(self, captcha: LoginCaptcha) -> None:
        dialog = VoiceCaptchaDialog(self, self.api, self.phone_box.GetValue().strip(), captcha, self.sms_region)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self._sms_sent()
            else:
                self._set_busy(False)
                self.get_captcha_button.SetFocus()
        finally:
            dialog.Destroy()

    def _sms_sent(self) -> None:
        self._set_busy(False, "短信验证码已发送")
        self.sms_box.SetFocus()
        self._start_countdown(60)

    def on_login(self, _event: wx.Event) -> None:
        if self._busy:
            return
        index = self.notebook.GetSelection()
        if index >= 2:
            self._browser_login(LOGIN_PROVIDERS[index - 2][0])
            return
        if index == 1:
            self._password_login()
            return
        phone = self.phone_box.GetValue().strip()
        sms_code = self.sms_box.GetValue().strip()
        region = self.sms_region
        if not valid_phone(phone, region):
            self._show_error("手机号格式不正确")
            self.phone_box.SetFocus()
            return
        if not sms_code:
            self._show_error("请输入短信验证码")
            self.sms_box.SetFocus()
            return
        self._set_busy(True, "正在登录…")
        def work() -> AccountInfo:
            self.api.sms_login(phone, sms_code, region)
            return validated_account(self.api)
        login = LoginCredentials(phone, region=region,
                                 region_label=self.sms_region_button.GetLabel().removeprefix("国家/地区："))
        self._run_async(work, lambda account: self._login_success(account, login), self._operation_failed)

    def _password_login(self) -> None:
        name = self.login_name_box.GetValue().strip()
        password = self.password_box.GetValue()
        region = self.password_region
        login = LoginCredentials(name, password, region,
                                 self.password_region_button.GetLabel().removeprefix("国家/地区："))
        if (self.saved and self.note_box.GetValue().strip() != self.saved.note
                and login == (self._saved_login or LoginCredentials(""))):
            self.api.set_cookie(self.saved.cookie)
            self._login_success(AccountInfo(self.saved.user_id, self.saved.nickname, ""))
            return
        if not (valid_phone(name, region) or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", name)):
            self._show_error("请输入手机号或邮箱，不能使用昵称登录")
            self.login_name_box.SetFocus()
            return
        if not password:
            self._show_error("请输入密码")
            self.password_box.SetFocus()
            return
        self._browser_login(native_password=True, login_name=name, password=password,
                            region_label=self.password_region_button.GetLabel().removeprefix("国家/地区："))

    def _browser_login(
        self, provider: str | None = None, *, native_password: bool = False, login_name: str = "", password: str = "",
        region_label: str = "中国大陆 +86",
    ) -> None:
        if self._busy:
            return
        from web_login import BrowserLoginDialog, NativePasswordLoginDialog
        try:
            dialog_type = NativePasswordLoginDialog if native_password else BrowserLoginDialog
            dialog = dialog_type(self, provider, login_name=login_name, password=password, region_label=region_label)
        except Exception:
            self._show_error("无法启动登录组件，请检查是否已安装 Microsoft Edge WebView2 Runtime")
            return
        def finished(result, error=""):
            if native_password:
                if not self or self.IsBeingDeleted() or self._native_login is not dialog:
                    return
                self._native_login = None
                if not self.IsModal() or not self.IsShown():
                    dialog.Destroy()
                    return
                self._set_busy(False)
            try:
                if error:
                    self._show_error(error)
                elif result == wx.ID_OK and dialog.account_info is not None:
                    self.api = dialog.api
                    login = LoginCredentials(login_name, password, self.password_region, region_label) if login_name else None
                    self._login_success(dialog.account_info, login)
                    return
                (self.password_box if login_name else self.login_button).SetFocus()
            finally:
                dialog.Destroy()
        if native_password:
            self._native_login = dialog
            self._set_busy(True, "正在登录，请稍候…")
            self.cancel_button.SetFocus()
            dialog.start(finished)
        else:
            finished(dialog.ShowModal())

    def _login_success(self, account: AccountInfo, login: LoginCredentials | None = None) -> None:
        if self.saved and account.user_id != self.saved.user_id:
            self.api.set_cookie("")
            self._operation_failed(ValueError("登录的是其他账号，请使用“新增”保存该账号"))
            return
        self.login = login
        self.note = self.note_box.GetValue().strip() if self.note_box else ""
        self.password_box.ChangeValue("")
        self.cookie_header = self.api.cookie_header
        self.account_info = account
        self.EndModal(wx.ID_OK)

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        self.notebook.Enable(not busy)
        self.get_captcha_button.Enable(not busy and self._countdown <= 0)
        self.login_button.Enable(not busy)
        self.status.SetLabel(status)
        self.announcer.announce(status)

    def _start_countdown(self, seconds: int) -> None:
        self._countdown = seconds
        self._tick_countdown()

    def _tick_countdown(self) -> None:
        if not self or self.IsBeingDeleted():
            return
        if self._countdown <= 0:
            self.get_captcha_button.SetLabel("获取验证码")
            self.get_captcha_button.Enable(not self._busy)
            return
        self.get_captcha_button.SetLabel(f"重新获取({self._countdown})")
        self.get_captcha_button.Enable(False)
        self._countdown -= 1
        self._countdown_timer = wx.CallLater(1000, self._tick_countdown)

    def _run_async(self, work, done, failed) -> None:
        run_dialog_task(self, work, done, failed)

    def Destroy(self) -> bool:
        if self._native_login is not None:
            dialog, self._native_login = self._native_login, None
            dialog.Destroy()
        self.login = None
        self._saved_login = None
        self.password_box.ChangeValue("")
        self.announcer.close()
        if self._countdown_timer is not None:
            self._countdown_timer.Stop()
            self._countdown_timer = None
        return super().Destroy()

    def _operation_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self._show_error(str(exc) or type(exc).__name__)
        self.login_button.SetFocus()

    def _show_error(self, message: str) -> None:
        wx.MessageBox(message or "操作失败", "登录失败", wx.OK | wx.ICON_ERROR, self)


class CookieLoginDialog(wx.Dialog):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, title="Cookie 登录",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.login = None
        self.api = MaoerApi(cookie="")
        self.account_info: AccountInfo | None = None
        self.cookie_header = ""
        self._busy = False
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        self.cookie_box = wx.TextCtrl(panel, size=(500, 180), style=wx.TE_MULTILINE | wx.HSCROLL)
        set_native_accessible_name(self.cookie_box, "Cookie")
        root.Add(self.cookie_box, 1, wx.EXPAND | wx.ALL, 10)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.ok_button = wx.Button(panel, wx.ID_OK, "确定")
        self.cancel_button = wx.Button(panel, wx.ID_CANCEL, "取消")
        buttons.AddStretchSpacer()
        buttons.Add(self.ok_button, 0, wx.RIGHT, 8)
        buttons.Add(self.cancel_button)
        root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetMinSize(self.GetSize())
        self.ok_button.SetDefault()
        self.ok_button.Bind(wx.EVT_BUTTON, self.on_confirm)
        self.cookie_box.SetFocus()
        self.CentreOnParent()

    def on_confirm(self, _event: wx.Event) -> None:
        if self._busy:
            return
        try:
            cookie = normalize_cookie(self.cookie_box.GetValue())
        except ValueError as exc:
            self._failed(exc)
            return
        self.api.set_cookie(cookie)
        self._busy = True
        self.ok_button.Disable()
        self.cookie_box.Disable()
        self.SetTitle("正在验证 Cookie…")
        run_dialog_task(self, lambda: validated_account(self.api), self._ready, self._failed)

    def _ready(self, account: AccountInfo) -> None:
        self.account_info = account
        self.cookie_header = self.api.cookie_header
        self.EndModal(wx.ID_OK)

    def _failed(self, exc: Exception) -> None:
        self._busy = False
        self.ok_button.Enable()
        self.cookie_box.Enable()
        self.SetTitle("Cookie 登录")
        wx.MessageBox(str(exc) or "Cookie 验证失败", "登录失败", wx.OK | wx.ICON_ERROR, self)
        self.cookie_box.SetFocus()


class AccountManagerDialog(wx.Dialog):
    def __init__(self, parent: MaoerFrame) -> None:
        super().__init__(parent, title="账号管理", size=(680, 380),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.owner = parent
        self._busy = False
        self._rows: tuple[SavedAccount, ...] = ()
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list.SetName("已保存账号")
        self.list.InsertColumn(0, "账号", width=240)
        self.list.InsertColumn(1, "备注", width=220)
        self.list.InsertColumn(2, "用户 ID", width=120)
        root.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)
        self.status = wx.StaticText(panel)
        self.announcer = ScreenReaderAnnouncer(self.status, native_only=True)
        root.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.add_button = wx.Button(panel, label="新增(&N)")
        self.edit_button = wx.Button(panel, label="编辑(&E)")
        self.delete_button = wx.Button(panel, label="删除(&D)")
        self.login_button = wx.Button(panel, label="登录所选账号")
        self.logout_button = wx.Button(panel, label="退出当前账号(&O)")
        self.copy_cookie_button = wx.Button(panel, label="复制 Cookie(&C)")
        self.close_button = wx.Button(panel, wx.ID_CANCEL, "关闭(&X)")
        for button, handler in (
            (self.add_button, self.on_add), (self.edit_button, self.on_edit),
            (self.delete_button, self.on_delete), (self.login_button, self.on_login),
            (self.logout_button, self.on_logout),
            (self.copy_cookie_button, self.on_copy_cookie),
        ):
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.RIGHT | wx.BOTTOM, 6)
        buttons.Add(self.close_button, 0, wx.BOTTOM, 6)
        root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetMinSize((540, 320))
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_login)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda event: self._update_buttons())
        self.list.Bind(wx.EVT_LIST_ITEM_DESELECTED, lambda event: self._update_buttons())
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.refresh(parent.account_state.active_user_id)
        self.list.SetFocus()
        self.CentreOnParent()

    def selected(self) -> SavedAccount | None:
        index = self.list.GetFirstSelected()
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def refresh(self, selected_id: int | None = None, message: str = "") -> None:
        previous = self.selected()
        selected_id = selected_id if selected_id is not None else (previous.user_id if previous else None)
        self._rows = self.owner.account_state.accounts
        active_id = self.owner.account_state.active_user_id if self.owner.account_logged_in else None
        self.list.DeleteAllItems()
        for index, account in enumerate(self._rows):
            label = account.nickname + ("（当前登录）" if account.user_id == active_id else "")
            self.list.InsertItem(index, label)
            self.list.SetItem(index, 1, account.note)
            self.list.SetItem(index, 2, str(account.user_id))
        if self._rows:
            index = next((i for i, account in enumerate(self._rows) if account.user_id == selected_id), 0)
            self.list.Select(index)
            self.list.Focus(index)
            self.list.EnsureVisible(index)
        status = message or ("当前未登录" if active_id is None else "已标记当前登录账号")
        self.status.SetLabel(status)
        if message:
            self.announcer.announce(message)
        self._update_buttons()

    def _update_buttons(self) -> None:
        account = self.selected()
        self.add_button.Enable(not self._busy)
        self.edit_button.Enable(not self._busy and account is not None)
        self.delete_button.Enable(not self._busy and account is not None)
        self.login_button.Enable(not self._busy and account is not None
                                 and account.user_id != self.owner.account_state.active_user_id)
        self.logout_button.Enable(not self._busy and self.owner.account_logged_in)
        self.copy_cookie_button.Enable(not self._busy and account is not None)

    def on_add(self, _event: wx.Event) -> None:
        choices = wx.SingleChoiceDialog(self, "选择登录方式", "新增账号", ["账号登录", "Cookie 登录"])
        try:
            if choices.ShowModal() == wx.ID_OK:
                if self.owner._show_account_login(self, cookie_login=choices.GetSelection() == 1):
                    self.EndModal(wx.ID_OK)
                    return
                self.refresh(self.owner.account_state.active_user_id)
        finally:
            choices.Destroy()
        self.list.SetFocus()

    def on_edit(self, _event: wx.Event) -> None:
        account = self.selected()
        if account is not None:
            updated = self.owner._edit_saved_account(self, account)
            self.refresh(account.user_id, "账号已更新" if updated else "")
            self.list.SetFocus()

    def on_delete(self, _event: wx.Event) -> None:
        account = self.selected()
        if account is None:
            return
        message = f"删除已保存账号“{account.nickname}”？此操作只移除本机保存的登录信息。"
        if account.user_id == self.owner.account_state.active_user_id:
            message += "\n当前账号将同时退出登录。"
        if wx.MessageBox(message, "删除账号", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES:
            if self.owner._remove_saved_account(account, self):
                self.refresh(message="账号已删除")
        self.list.SetFocus()

    def on_login(self, _event: wx.Event) -> None:
        account = self.selected()
        if self._busy or account is None:
            return
        if self.owner.account_logged_in and account.user_id == self.owner.account_state.active_user_id:
            self.EndModal(wx.ID_OK)
            return
        self._busy = True
        self._update_buttons()
        self.announcer.announce("正在登录所选账号")
        api = MaoerApi(cookie=account.cookie)

        def ready(info: AccountInfo) -> None:
            self._busy = False
            if info.user_id != account.user_id:
                failed(ValueError("保存的登录状态与账号不符，请编辑账号并重新登录"))
                return
            if self.owner._save_account(api, info, parent=self):
                self.announcer.announce(f"已登录：{info.nickname}")
                self.EndModal(wx.ID_OK)
                return
            self._update_buttons()

        def failed(exc: Exception) -> None:
            self._busy = False
            self._update_buttons()
            self.status.SetLabel("登录失败，可使用“编辑”重新填写账号密码并登录")
            wx.MessageBox(str(exc) or "登录失败", "登录失败", wx.OK | wx.ICON_ERROR, self)
            self.list.SetFocus()

        run_dialog_task(self, lambda: validated_account(api), ready, failed)

    def on_copy_cookie(self, _event: wx.Event) -> None:
        account = self.selected()
        if self._busy or account is None:
            return
        if not wx.TheClipboard.Open():
            wx.MessageBox("剪贴板暂时不可用，请重试。", "复制失败", wx.OK | wx.ICON_ERROR, self)
            return
        try:
            copied = wx.TheClipboard.SetData(wx.TextDataObject(account.cookie))
            if copied:
                wx.TheClipboard.Flush()
        finally:
            wx.TheClipboard.Close()
        if copied:
            self.status.SetLabel("Cookie 已复制")
            self.announcer.announce("Cookie 已复制")
        else:
            wx.MessageBox("无法复制 Cookie，请重试。", "复制失败", wx.OK | wx.ICON_ERROR, self)

    def on_logout(self, _event: wx.Event) -> None:
        if self.owner.on_account_logout(_event):
            self.refresh(message="已退出当前账号，保存的账号仍可使用")
            self.list.SetFocus()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        if event.GetEventObject() is self:
            self.announcer.close()
        event.Skip()
