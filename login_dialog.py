from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import tempfile
import threading

import requests
import wx

from maoer_api import LoginCaptcha, MaoerApi, USER_AGENT


PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


class CaptchaAudioPlayer:
    def __init__(self, parent: wx.Window) -> None:
        self.parent = parent
        self._audio_url = ""
        self._cached_url = ""
        self._audio_file: Path | None = None
        self._alias = f"maoer_captcha_{id(self)}"
        self._lock = threading.Lock()

    def play(self, audio_url: str) -> None:
        self._audio_url = audio_url
        threading.Thread(target=self._play_worker, args=(audio_url,), daemon=True).start()

    def replay(self) -> None:
        if not self._audio_url:
            return
        self.play(self._audio_url)

    def destroy(self) -> None:
        self._close_mci()
        self._delete_cached_file()

    def _play_worker(self, audio_url: str) -> None:
        try:
            with self._lock:
                audio_file = self._download_audio(audio_url)
                self._play_file(audio_file)
        except Exception as exc:
            wx.CallAfter(
                wx.MessageBox,
                f"验证码播放失败：{exc}",
                "播放失败",
                wx.OK | wx.ICON_ERROR,
                self.parent,
            )

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
    def __init__(self, parent: wx.Window, api: MaoerApi, phone: str, captcha: LoginCaptcha) -> None:
        super().__init__(parent, title="语音验证码", size=(420, 180))
        self.api = api
        self.phone = phone
        self.captcha = captcha
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

    def _bind_events(self) -> None:
        self.play_button.Bind(wx.EVT_BUTTON, self.on_play_captcha)
        self.confirm_button.Bind(wx.EVT_BUTTON, self.on_confirm)
        self.voice_box.Bind(wx.EVT_TEXT_ENTER, self.on_confirm)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_CLOSE, self.on_close)

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
        voice_answer = self.voice_box.GetValue().strip()
        if not voice_answer:
            wx.MessageBox("请输入语音验证码", "错误", wx.OK | wx.ICON_ERROR, self)
            self.voice_box.SetFocus()
            return

        self._set_busy(True)

        def work() -> None:
            self.api.send_login_sms_code(self.phone, self.captcha, voice_answer)

        def done(_result: object) -> None:
            self._set_busy(False)
            self.EndModal(wx.ID_OK)

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            wx.MessageBox(str(exc) or type(exc).__name__, "错误", wx.OK | wx.ICON_ERROR, self)
            self.voice_box.SetFocus()

        self._run_async(work, done, failed)

    def _set_busy(self, busy: bool) -> None:
        self.SetTitle("语音验证码 - 正在发送短信验证码..." if busy else "语音验证码")
        self.play_button.Enable(not busy)
        self.confirm_button.Enable(not busy)
        self.cancel_button.Enable(not busy)

    def _run_async(self, work, done, failed) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                wx.CallAfter(failed, exc)
            else:
                wx.CallAfter(done, result)

        threading.Thread(target=runner, daemon=True).start()

    def on_close(self, event: wx.CloseEvent) -> None:
        self._audio_player.destroy()
        event.Skip()


class LoginDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, api: MaoerApi) -> None:
        super().__init__(parent, title="账号登录", size=(440, 210))
        self.api = api
        self.cookie_header = ""
        self._countdown = 0

        self._build_ui()
        self._bind_events()
        self.phone_box.SetFocus()

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        self.phone_label = wx.StaticText(panel, label="手机号")
        self.phone_box = wx.TextCtrl(panel)
        self.phone_box.SetName("手机号")
        root.Add(self.phone_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        root.Add(self.phone_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        voice_row = wx.BoxSizer(wx.HORIZONTAL)
        self.get_captcha_button = wx.Button(panel, label="获取验证码")
        voice_row.Add(self.get_captcha_button, 0)
        root.Add(voice_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.sms_label = wx.StaticText(panel, label="短信验证码")
        self.sms_box = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.sms_box.SetName("短信验证码")
        root.Add(self.sms_label, 0, wx.LEFT | wx.RIGHT, 10)
        root.Add(self.sms_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        self.login_button = wx.Button(panel, wx.ID_OK, label="登录")
        self.cancel_button = wx.Button(panel, wx.ID_CANCEL, label="取消")
        button_row.AddStretchSpacer(1)
        button_row.Add(self.login_button, 0, wx.RIGHT, 8)
        button_row.Add(self.cancel_button, 0)
        root.Add(button_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(root)

    def _bind_events(self) -> None:
        self.get_captcha_button.Bind(wx.EVT_BUTTON, self.on_get_captcha)
        self.login_button.Bind(wx.EVT_BUTTON, self.on_login)
        self.sms_box.Bind(wx.EVT_TEXT_ENTER, self.on_login)

    def on_get_captcha(self, _event: wx.Event) -> None:
        phone = self.phone_box.GetValue().strip()
        if not PHONE_RE.fullmatch(phone):
            self._show_error("手机号格式不正确")
            self.phone_box.SetFocus()
            return

        self._set_busy(True, "正在获取语音验证码...")

        def work() -> LoginCaptcha:
            return self.api.start_login_captcha()

        self._run_async(work, self._captcha_ready, self._operation_failed)

    def _captcha_ready(self, captcha: LoginCaptcha) -> None:
        self._set_busy(False)
        phone = self.phone_box.GetValue().strip()
        if not PHONE_RE.fullmatch(phone):
            self._show_error("手机号格式不正确")
            self.phone_box.SetFocus()
            return

        dialog = VoiceCaptchaDialog(self, self.api, phone, captcha)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self._sms_sent()
        finally:
            dialog.Destroy()

    def _sms_sent(self) -> None:
        self._set_busy(False, "短信验证码已发送")
        self.sms_box.SetFocus()
        self._start_countdown(60)

    def on_login(self, event: wx.Event) -> None:
        phone = self.phone_box.GetValue().strip()
        sms_code = self.sms_box.GetValue().strip()
        if not PHONE_RE.fullmatch(phone):
            self._show_error("手机号格式不正确")
            self.phone_box.SetFocus()
            return
        if not sms_code:
            self._show_error("请输入短信验证码")
            self.sms_box.SetFocus()
            return

        self._set_busy(True, "正在登录...")

        def work() -> str:
            cookie = self.api.sms_login(phone, sms_code)
            self.api.save_cookie(cookie, "cookey")
            return cookie

        self._run_async(work, self._login_success, self._operation_failed)

    def _login_success(self, cookie: str) -> None:
        self.cookie_header = cookie
        wx.MessageBox(
            f"登录成功，Cookie 已保存到 {self.api.saved_cookie_path()}",
            "登录成功",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )
        self.EndModal(wx.ID_OK)

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self.get_captcha_button.Enable(not busy and self._countdown <= 0)
        self.login_button.Enable(not busy)
        if status:
            self.SetTitle(f"账号登录 - {status}")
        elif not busy:
            self.SetTitle("账号登录")

    def _start_countdown(self, seconds: int) -> None:
        self._countdown = seconds
        self._tick_countdown()

    def _tick_countdown(self) -> None:
        if self._countdown <= 0:
            self.get_captcha_button.SetLabel("获取验证码")
            self.get_captcha_button.Enable(True)
            return
        self.get_captcha_button.SetLabel(f"重新获取({self._countdown})")
        self.get_captcha_button.Enable(False)
        self._countdown -= 1
        wx.CallLater(1000, self._tick_countdown)

    def _run_async(self, work, done, failed) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                wx.CallAfter(failed, exc)
            else:
                wx.CallAfter(done, result)

        threading.Thread(target=runner, daemon=True).start()

    def _operation_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self._show_error(str(exc) or type(exc).__name__)

    def _show_error(self, message: str) -> None:
        wx.MessageBox(message or "操作失败", "错误", wx.OK | wx.ICON_ERROR, self)
