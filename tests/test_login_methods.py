from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
import wx
import wx.html2 as html2

from login_dialog import CaptchaAudioPlayer, LOGIN_PROVIDERS, LoginDialog, VoiceCaptchaDialog
from maoer_api import AccountInfo, ApiError, BASE_URL, LoginCaptcha, MaoerApi
from web_login import (
    BrowserLoginDialog, NativePasswordLoginDialog, PASSWORD_LOGIN_SCRIPT, VOICE_LOGIN_SCRIPT,
    _remove_login_profile, is_login_return, login_url, read_webview_cookies,
)


class LoginApiTests(unittest.TestCase):
    def test_login_403_preserves_the_server_validation_message(self):
        response = requests.Response()
        response.status_code = 403
        response.url = BASE_URL + "/account/smslogin"
        response._content = json.dumps({
            "success": False, "code": 100010007,
            "info": [{"message": "验证失败", "type": 6}],
        }).encode("utf-8")
        api = MaoerApi(cookie="")
        with patch.object(api.session, "post", return_value=response):
            with self.assertRaisesRegex(ApiError, "验证失败"):
                api.sms_login("12345678", "synthetic", "HK")

    def test_sms_login_keeps_region_and_scopes_cookies(self):
        api = MaoerApi(cookie="")
        api.session.cookies.set("session", "test==", domain=".missevan.com", path="/")
        api.session.cookies.set("foreign", "excluded", domain="geetest.com", path="/")
        api.session.cookies.set("private", "excluded", domain="www.missevan.com", path="/other")
        with patch.object(api.session, "post", return_value=Mock(json=lambda: {"success": True})) as post:
            self.assertEqual(api.sms_login("12345678", "123456", "HK"), "session=test==")
            self.assertEqual(post.call_args.args, (BASE_URL + "/account/smslogin",))
            self.assertEqual(post.call_args.kwargs["data"], {
                "mobile": "12345678", "identify_code": "123456", "region": "HK", "remember_me": "1",
            })

    def test_login_refreshes_cookie_jar_and_reads_official_regions(self):
        api = MaoerApi(cookie="session=stale")
        with patch.object(api.session, "get") as get, patch.object(api, "_get") as config:
            api._prepare_login()
            self.assertNotIn("Cookie", api.session.headers)
            self.assertTrue(get.called)
            config.return_value = {"info": [{"code": "HK", "name": "中国香港", "value": 852}]}
            self.assertEqual(api.login_regions(), [("HK", "中国香港 +852")])

    def test_sms_sending_requires_successful_voice_verification(self):
        api = MaoerApi(cookie="")
        captcha = LoginCaptcha("gt", "challenge", "https://test.invalid/audio")
        response = Mock(text='callback({"status":"success","data":{"result":"success","validate":"token"}})')
        with patch.object(api.session, "get", return_value=response), patch.object(api, "_post_form_json") as post:
            token = api.verify_login_captcha(captcha, "1234")
            self.assertEqual(token, "geetest|challenge|token|token|jordan")
            post.assert_not_called()
            api.send_login_sms_code("12345678", captcha, "1234", "HK")
            self.assertEqual(post.call_args.args[1]["region"], "HK")
            self.assertEqual(post.call_args.args[1]["captcha_token"], token)
            response.text = 'callback({"status":"success","data":{"result":"fail"}})'
            with self.assertRaises(ApiError):
                api.verify_login_captcha(captcha, "bad")

    def test_provider_urls_and_return_origin_are_exact(self):
        for provider, _ in LOGIN_PROVIDERS:
            url = urlsplit(login_url(provider))
            self.assertEqual(url.netloc, "www.missevan.com")
            self.assertEqual(parse_qs(url.query), {"type": [provider], "backurl": [BASE_URL + "/"]})
        with self.assertRaises(ValueError):
            login_url("unexpected")
        self.assertTrue(is_login_return(BASE_URL + "/member/authcallback"))
        for url in ("http://www.missevan.com/", "https://www.missevan.com.evil.test/",
                    "https://www.missevan.com@evil.test/", "file:///test"):
            self.assertFalse(is_login_return(url))


class LoginDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def setUp(self):
        self.api = MaoerApi(cookie="")
        self.dialog = LoginDialog(None, self.api)
        self.addCleanup(self.dialog.Destroy)
        modal = patch.object(self.dialog, "IsModal", return_value=True)
        modal.start()
        self.addCleanup(modal.stop)
        shown = patch.object(self.dialog, "IsShown", return_value=True)
        shown.start()
        self.addCleanup(shown.stop)
        network = patch("requests.Session.request", side_effect=AssertionError("Live network is forbidden"))
        network.start()
        self.addCleanup(network.stop)

    @staticmethod
    def run_task(dialog, work, done, failed):
        try:
            result = work()
        except Exception as exc:
            failed(exc)
        else:
            done(result)

    def test_all_six_tabs_are_idle_until_explicit_login(self):
        notebook = self.dialog.notebook
        self.assertEqual([notebook.GetPageText(i) for i in range(notebook.GetPageCount())],
                         ["短信登录", "密码登录", "QQ", "微信", "微博", "哔哩哔哩"])
        with patch.object(self.dialog, "_browser_login") as browser, patch.object(self.dialog, "_run_async") as task:
            for index, (provider, _) in enumerate(LOGIN_PROVIDERS, 2):
                notebook.SetSelection(index)
                browser.assert_not_called()
                task.assert_not_called()
                self.dialog.on_login(None)
                browser.assert_called_once_with(provider)
                browser.reset_mock()
        self.assertTrue(self.dialog.password_box.GetWindowStyle() & wx.TE_PASSWORD)

    def test_password_login_uses_native_verification_without_showing_a_webpage(self):
        dialog = self.dialog
        dialog.notebook.SetSelection(1)
        dialog.login_name_box.SetValue(" 12345678 ")
        dialog.password_box.SetValue(" password with spaces ")
        dialog.password_region = "HK"
        dialog.password_region_button.SetLabel("国家/地区：中国香港 +852")
        with patch("web_login.BrowserLoginDialog") as browser, \
                patch("web_login.NativePasswordLoginDialog", create=True) as native, \
                patch.object(dialog, "EndModal"):
            native.return_value.start.side_effect = lambda callback: callback(wx.ID_CANCEL, "")
            dialog.on_login(None)
            browser.assert_not_called()
            native.assert_called_once_with(dialog, None, login_name="12345678", password=" password with spaces ",
                                           region_label="中国香港 +852")

    def test_native_password_accepts_validated_result_and_clears_password(self):
        dialog = self.dialog
        dialog.notebook.SetSelection(1)
        dialog.login_name_box.SetValue(" person@example.test ")
        dialog.password_box.SetValue(" password with spaces ")
        dialog.password_region = "HK"
        dialog.password_region_button.SetLabel("国家/地区：中国香港 +852")
        account = AccountInfo(42, "测试账号", "")
        with patch("web_login.NativePasswordLoginDialog") as browser, \
                patch.object(self.api, "save_cookie") as save, patch.object(dialog, "EndModal") as end:
            browser.return_value.start.side_effect = lambda callback: callback(wx.ID_OK, "")
            browser.return_value.account_info = account
            browser.return_value.api = MaoerApi(cookie="session=verified")
            dialog.on_login(None)
            browser.assert_called_once_with(dialog, None, login_name="person@example.test",
                                            password=" password with spaces ", region_label="中国香港 +852")
            self.assertIs(dialog.account_info, account)
            self.assertEqual(dialog.cookie_header, "session=verified")
            self.assertEqual(dialog.password_box.GetValue(), "")
            self.assertEqual((dialog.login.username, dialog.login.password),
                             ("person@example.test", " password with spaces "))
            end.assert_called_once_with(wx.ID_OK)
            save.assert_not_called()
            browser.return_value.Destroy.assert_called_once()
            browser.return_value.ShowModal.assert_not_called()

    def test_native_password_cancel_preserves_input_and_restores_focus(self):
        dialog = self.dialog
        dialog.notebook.SetSelection(1)
        dialog.login_name_box.SetValue("13800000000")
        dialog.password_box.SetValue("test password")
        with patch("web_login.NativePasswordLoginDialog") as browser, \
                patch.object(dialog.password_box, "SetFocus") as focus, patch.object(dialog, "EndModal") as end:
            browser.return_value.start.side_effect = lambda callback: callback(wx.ID_CANCEL, "")
            dialog.on_login(None)
            end.assert_not_called()
            self.assertIs(dialog.api, self.api)
            self.assertFalse(dialog._busy)
            self.assertEqual(dialog.password_box.GetValue(), "test password")
            self.assertEqual(dialog.login_name_box.GetValue(), "13800000000")
            focus.assert_called_once()
            browser.return_value.Destroy.assert_called_once()

    def test_voice_dialog_sends_sms_only_on_confirmation(self):
        with patch("login_dialog.wx.CallAfter"), patch("login_dialog.CaptchaAudioPlayer"):
            voice = VoiceCaptchaDialog(self.dialog, self.api, "12345678", LoginCaptcha("g", "c", ""), "HK")
        try:
            voice.voice_box.SetValue("1234")
            with patch("login_dialog.run_dialog_task", side_effect=self.run_task), \
                    patch.object(self.api, "send_login_sms_code") as sms, patch.object(voice, "EndModal") as end:
                sms.assert_not_called()
                voice.on_confirm(None)
                sms.assert_called_once_with("12345678", voice.captcha, "1234", "HK")
                end.assert_called_once_with(wx.ID_OK)
        finally:
            voice.Destroy()

    def test_password_transfer_rejects_other_pages_and_canceled_dialogs(self):
        dialog = SimpleNamespace(IsBeingDeleted=lambda: False, IsModal=lambda: True, IsShown=lambda: True,
                                 _password_pending=("synthetic", "secret", "中国大陆 +86"),
                                 _password_inflight=False, _password_done=Mock(), webview=Mock())
        dialog._is_active = lambda: BrowserLoginDialog._is_active(dialog)
        for url in ("http://www.missevan.com/member/login", "https://www.missevan.com.evil.test/member/login",
                    "https://www.missevan.com@evil.test/member/login", BASE_URL + "/member/forgetpw"):
            dialog.webview.GetCurrentURL.return_value = url
            BrowserLoginDialog._submit_password(dialog)
            dialog.webview.RunScriptAsync.assert_not_called()
        dialog.IsModal = lambda: False
        dialog.webview.GetCurrentURL.return_value = BASE_URL + "/member/login"
        BrowserLoginDialog._submit_password(dialog)
        dialog.webview.RunScriptAsync.assert_not_called()

    def test_native_preparation_does_not_open_a_dialog_and_ignores_result_after_cancel(self):
        dialog = self.dialog
        dialog.notebook.SetSelection(1)
        dialog.login_name_box.SetValue("person@example.test")
        dialog.password_box.SetValue("synthetic password")
        with patch("web_login.NativePasswordLoginDialog") as native, patch.object(dialog, "EndModal") as end:
            dialog.on_login(None)
            self.assertTrue(dialog._busy)
            native.return_value.ShowModal.assert_not_called()
            callback = native.return_value.start.call_args.args[0]
            dialog.IsShown.return_value = False  # EndModal is still unwinding.
            native.return_value.account_info = AccountInfo(42, "测试", "")
            callback(wx.ID_OK, "")
            end.assert_not_called()
            self.assertIsNone(dialog.account_info)
            self.assertIsNone(dialog._native_login)
            self.assertIs(dialog.api, self.api)
            native.return_value.Destroy.assert_called_once()

    def test_canceled_captcha_does_not_play_a_late_download(self):
        player = CaptchaAudioPlayer(self.dialog)
        with patch.object(player, "_download_audio", return_value=Path("synthetic.mp3")), \
                patch.object(player, "_play_file") as play, patch("login_dialog.wx.CallAfter") as later:
            player._play_worker("https://test.invalid/audio")
            callback, *args = later.call_args.args
            player.destroy()
            callback(*args)
            play.assert_not_called()

    def test_native_navigation_does_not_announce_webpage_login_instructions(self):
        for dialog_type, background in ((NativePasswordLoginDialog, True), (BrowserLoginDialog, False)):
            dialog = SimpleNamespace(_background=background, announcer=Mock())
            for url in ("about:blank", BASE_URL + "/member/login", "missevan://login"):
                with self.subTest(background=background, url=url):
                    event = Mock(GetURL=lambda: url)
                    dialog.announcer.reset_mock()
                    dialog_type._on_navigating(dialog, event)
                    if url.startswith("missevan:"):
                        event.Veto.assert_called_once()
                        if not background:
                            dialog.announcer.announce.assert_called_once_with("请继续在网页中登录或扫码登录。")
                            continue
                    else:
                        event.Veto.assert_not_called()
                    dialog.announcer.announce.assert_not_called()

    def test_browser_success_requires_verified_account_and_ignores_late_completion(self):
        dialog = SimpleNamespace(IsBeingDeleted=lambda: False, IsModal=lambda: True, IsShown=lambda: True,
                                 _last_cookie="", _failed=Mock(), announcer=Mock(), EndModal=Mock())
        dialog._is_active = lambda: BrowserLoginDialog._is_active(dialog)
        dialog._finish_login = lambda: BrowserLoginDialog._finish_login(dialog)
        cookie = json.dumps({"cookies": [
            {"name": "session", "value": "synthetic", "domain": ".missevan.com"},
            {"name": "thirdparty", "value": "excluded", "domain": ".qq.com"},
        ]})
        with patch("web_login.run_dialog_task") as task:
            BrowserLoginDialog._cookies_ready(dialog, 0, cookie, False)
            dialog.EndModal.assert_not_called()
            _, work, done, failed = task.call_args.args
            with patch.object(MaoerApi, "account_info", return_value=AccountInfo(None, "", "")):
                with self.assertRaises(ApiError):
                    work()
            failed(ApiError("未登录"))
            dialog.EndModal.assert_not_called()
            done(AccountInfo(42, "测试", ""))
            self.assertEqual(dialog.api.cookie_header, "session=synthetic")
            dialog.EndModal.assert_called_once_with(wx.ID_OK)
            task.reset_mock()
            dialog.IsModal = lambda: False
            BrowserLoginDialog._cookies_ready(dialog, 0, cookie, True)
            task.assert_not_called()


class WebViewCookieTests(unittest.TestCase):
    def _run_in_child(self):
        # Other wx tests replace scheduling and leave unpumped window events.
        # Run the real WebView message loop in its own application process.
        if os.environ.get("MAOER_WEBVIEW_TEST_CHILD") != "1":
            result = subprocess.run(
                [sys.executable, "-c", "import sys,unittest; sys.path.insert(0,'tests'); "
                 f"unittest.main(module='test_login_methods',defaultTest='WebViewCookieTests.{self._testMethodName}')"],
                cwd=Path(__file__).resolve().parent.parent,
                env={**os.environ, "MAOER_WEBVIEW_TEST_CHILD": "1"},
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return True
        return False

    def test_native_webview_reads_httponly_cookie_in_isolated_profile(self):
        if self._run_in_child():
            return
        app = wx.GetApp() or wx.App(False)
        if not html2.WebView.IsBackendAvailable(html2.WebViewBackendEdge):
            self.skipTest("WebView2 Runtime is not installed")
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Set-Cookie", "test_session=synthetic; HttpOnly; SameSite=Lax")
                self.end_headers()
                self.wfile.write(b"<p>Local login test</p>")

        server = HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        profile = Path(tempfile.mkdtemp(prefix="maoer-login-test-"))
        result, handlers, popups, page_visibility = [], [], [], []
        url = f"http://127.0.0.1:{server.server_port}/"
        previous = os.environ.get("WEBVIEW2_USER_DATA_FOLDER")
        with patch("web_login.webview2_profile_dir", return_value=profile), \
                patch("web_login.login_url", return_value=url), \
                patch.object(BrowserLoginDialog, "_on_navigating", lambda self, event: None):
            dialog = BrowserLoginDialog(None, "qq")
        view = dialog.webview
        self.assertEqual(os.environ.get("WEBVIEW2_USER_DATA_FOLDER"), previous)
        def completed(error, data):
            result.append((error, json.loads(data)))
            if hasattr(html2, "wxEVT_WEBVIEW_NEWWINDOW_FEATURES"):
                view.RunScriptAsync("window.open('about:blank', 'local-auth-test')")
            else:
                dialog.EndModal(wx.ID_OK)
        def features(event):
            dialog._on_window_features(event)
            popups.append(dialog._popups[-1].IsEnabled())
            wx.CallLater(300, dialog.EndModal, wx.ID_OK)
        def loaded(event):
            if event.GetURL().startswith(url):
                view.RunScriptAsync("document.documentElement.getAttribute('aria-hidden') === 'true' || "
                                    "document.body.getAttribute('aria-hidden') === 'true'")
        def inspected(event):
            if page_visibility:
                event.Skip()
                return
            page_visibility.append(event.GetString())
            handlers.append(read_webview_cookies(view, url, completed))
        view.Bind(html2.EVT_WEBVIEW_LOADED, loaded)
        view.Bind(html2.EVT_WEBVIEW_SCRIPT_RESULT, inspected)
        if hasattr(html2, "wxEVT_WEBVIEW_NEWWINDOW_FEATURES"):
            view.Bind(wx.PyEventBinder(html2.wxEVT_WEBVIEW_NEWWINDOW_FEATURES, 1), features)
        timer = wx.CallLater(15000, dialog.EndModal, wx.ID_CANCEL)
        try:
            self.assertEqual(dialog.ShowModal(), wx.ID_OK)
            self.assertEqual(page_visibility, ["false"], "Explicit web login must remain accessible")
            self.assertEqual(result[0][0], 0)
            self.assertTrue(any(c["name"] == "test_session" and c["httpOnly"] for c in result[0][1]["cookies"]))
            self.assertTrue(any(profile.iterdir()))
            if hasattr(html2, "wxEVT_WEBVIEW_NEWWINDOW_FEATURES"):
                self.assertEqual(popups, [True])
        finally:
            timer.Stop()
            dialog.Destroy()
            app.Yield()
            server.shutdown()
            server.server_close()
            _remove_login_profile(profile)

    def test_native_password_form_submission_and_origin_guard(self):
        if self._run_in_child():
            return
        app = wx.GetApp() or wx.App(False)
        if not html2.WebView.IsBackendAvailable(html2.WebViewBackendEdge):
            self.skipTest("WebView2 Runtime is not installed")
        # Same public form structure as the official page, with local handlers
        # recording input events and submissions. No request contains credentials.
        html = """<!doctype html><meta charset="utf-8">
            <ul class="tab-list"><li class="tab" onclick="showPassword()">密码登录</li></ul>
            <main></main><script>
            window.submissions = [];
            window.inputs = [];
            function showPassword() {
                document.querySelector('main').innerHTML = `
                    <form class="test-legacy-password-login">
                        <div class="mobile-region">
                            <span class="region-select-item">中国大陆 (+86)</span>
                            <ul><li class="region-dropdown-item">中国大陆 (+86)</li>
                                <li class="region-dropdown-item">中国香港特别行政区 (+852)</li></ul>
                            <input type="text">
                        </div>
                        <div class="password-input"><input type="password"></div>
                        <button type="submit">登录</button><span class="login-switch">邮箱账号登录</span>
                    </form>`;
                document.querySelector('.tab').classList.add('active');
                for (const item of document.querySelectorAll('.region-dropdown-item'))
                    item.onclick = () => document.querySelector('.region-select-item').textContent = item.textContent;
                document.querySelector('.login-switch').onclick = () => {
                    const box = document.querySelector('.mobile-region');
                    box.className = 'email-input';
                    box.innerHTML = '<input type="text">';
                };
                document.querySelector('form').onsubmit = event => {
                    event.preventDefault();
                    window.submissions.push({
                        name: document.querySelector('input[type=text]').value,
                        password: document.querySelector('input[type=password]').value,
                        region: document.querySelector('.region-select-item')?.textContent || 'email',
                        inputs: window.inputs.slice(),
                    });
                    window.inputs = [];
                };
            }
            document.addEventListener('input', event => window.inputs.push(event.target.type));
            </script>""".encode("utf-8")
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html)

        server = HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        profile = Path(tempfile.mkdtemp(prefix="maoer-password-test-"))
        origin = f"http://127.0.0.1:{server.server_port}"
        url = origin + "/member/login"
        cases = [("synthetic", "blocked", "中国大陆 +86"),
                 ("13800000000", ' spaces " \\ 中文 ', "中国大陆 +86"),
                 ("12345678", "other password", "中国香港特别行政区 +852"),
                 ("person@example.test", "email password", "中国大陆 +86")]
        results = []
        with patch("web_login.webview2_profile_dir", return_value=profile), \
                patch("web_login.login_url", return_value=url), \
                patch.object(BrowserLoginDialog, "_on_navigating", lambda self, event: None):
            dialog = BrowserLoginDialog(None, background=True)
        view = dialog.webview
        script_patch = None

        def begin_case():
            nonlocal script_patch
            if len(results) == 1:
                # The first case keeps the production JS origin guard, proving
                # a navigation race cannot transfer credentials to this server.
                script_patch = patch("web_login.PASSWORD_LOGIN_SCRIPT", PASSWORD_LOGIN_SCRIPT.replace(BASE_URL, origin))
                script_patch.start()
            dialog._password_pending = cases[len(results)]
            dialog._password_attempts = 0
            dialog._submit_password()

        def script_finished(event):
            if dialog._password_inflight:
                dialog._on_password_script(event)
                if dialog._password_pending is None:
                    dialog._submit_password()  # Late callbacks must not resubmit.
                    view.RunScriptAsync("JSON.stringify(window.submissions)")
            else:
                result = json.loads(event.GetString())
                if isinstance(result, str):
                    result = json.loads(result)
                results.append(result)
                if len(results) == len(cases):
                    dialog.EndModal(wx.ID_OK)
                else:
                    begin_case()

        view.Bind(html2.EVT_WEBVIEW_LOADED, lambda event: begin_case() if event.GetURL() == url else None)
        view.Bind(html2.EVT_WEBVIEW_SCRIPT_RESULT, script_finished)
        timer = wx.CallLater(15000, dialog.EndModal, wx.ID_CANCEL)
        try:
            with patch.object(view, "GetCurrentURL", return_value=BASE_URL + "/member/login"):
                self.assertEqual(dialog.ShowModal(), wx.ID_OK)
            self.assertEqual([len(result) for result in results], [0, 1, 2, 3])
            for submission, (name, password, _) in zip(results[-1], cases[1:]):
                self.assertEqual(submission["name"], name)
                self.assertEqual(submission["password"], password)
                self.assertEqual(submission["inputs"], ["text", "password"])
            self.assertEqual([item["region"] for item in results[-1]],
                             ["中国大陆 (+86)", "中国香港特别行政区 (+852)", "email"])
            self.assertIsNone(dialog._password_pending)
        finally:
            timer.Stop()
            if script_patch is not None:
                script_patch.stop()
            dialog.Destroy()
            app.Yield()
            server.shutdown()
            server.server_close()
            _remove_login_profile(profile)

    def test_native_preparation_handles_direct_login_timeout_and_cancel_without_a_popup(self):
        if self._run_in_child():
            return
        app = wx.GetApp() or wx.App(False)
        if not html2.WebView.IsBackendAvailable(html2.WebViewBackendEdge):
            self.skipTest("WebView2 Runtime is not installed")
        profile = Path(tempfile.mkdtemp(prefix="maoer-native-preparation-test-"))
        cookie = json.dumps({"cookies": [{"name": "session", "value": "synthetic", "domain": ".missevan.com"}]})
        later = wx.CallLater
        def read_cookies(view, url, callback):
            wx.CallAfter(callback, 0, cookie)
        with patch("web_login.webview2_profile_dir", return_value=profile), \
                patch("web_login.login_url", return_value="about:blank"), \
                patch("web_login.CaptchaAudioPlayer"), \
                patch("web_login.read_webview_cookies", side_effect=read_cookies), \
                patch("web_login.validated_account", return_value=AccountInfo(42, "测试", "")), \
                patch("web_login.wx.CallLater", side_effect=lambda delay, *args: later(500 if delay == 30000 else delay, *args)):
            try:
                for outcome in ("success", "timeout", "cancel"):
                    parent = LoginDialog(None, MaoerApi(cookie=""))
                    dialog = NativePasswordLoginDialog(parent)
                    shown, results = [], []
                    started = False
                    def on_show(event):
                        if event.GetEventObject() is dialog and event.IsShown():
                            shown.append(True)
                        event.Skip()
                    dialog.Bind(wx.EVT_SHOW, on_show)
                    def finished(result, error):
                        results.append((result, error))
                        parent.EndModal(result)
                    def begin():
                        nonlocal started
                        if started:
                            return
                        started = True
                        dialog.start(finished)
                        if outcome == "success":
                            dialog.check_login()
                        elif outcome == "cancel":
                            dialog._voice_url = "https://static.geetest.com/synthetic.mp3"
                            dialog._enable_voice(True)
                            wx.CallAfter(dialog._show_voice)
                            parent.EndModal(wx.ID_CANCEL)
                    dialog.webview.Bind(html2.EVT_WEBVIEW_LOADED, lambda event: begin())
                    watchdog = later(5000, parent.EndModal, wx.ID_CANCEL)
                    try:
                        result = parent.ShowModal()
                        self.assertEqual(result, wx.ID_OK if outcome == "success" else wx.ID_CANCEL)
                        if outcome == "success":
                            self.assertEqual(dialog.account_info.user_id, 42)
                            self.assertEqual(results, [(wx.ID_OK, "")])
                        elif outcome == "timeout":
                            self.assertEqual(results, [(wx.ID_CANCEL, "登录验证等待超时，请重试。")])
                    finally:
                        watchdog.Stop()
                        dialog.Destroy()
                        parent.Destroy()
                        app.Yield()
                    self.assertEqual(shown, [])
                    if outcome == "cancel":
                        self.assertEqual(results, [])
            finally:
                _remove_login_profile(profile)

    def test_native_voice_controls_forward_only_the_users_answer(self):
        if self._run_in_child():
            return
        app = wx.GetApp() or wx.App(False)
        if not html2.WebView.IsBackendAvailable(html2.WebViewBackendEdge):
            self.skipTest("WebView2 Runtime is not installed")
        html = rb'''<!doctype html><meta charset="utf-8">
            <script>window.hiddenAtStart = document.documentElement.getAttribute('aria-hidden');</script>
            <div class="geetest_voice_wrap"><audio class="geetest_music"></audio>
                <input class="geetest_input" maxlength="6"><button class="geetest_btn">Confirm</button>
                <span class="geetest_result_tip"></span></div><script>
            document.documentElement.removeAttribute('aria-hidden');
            document.body.removeAttribute('aria-hidden');
            Object.defineProperty(document.querySelector('audio'), 'src',
                {value:'https://static.geetest.com/synthetic.mp3'});
            document.querySelector('button').onclick = () => {
                const answer = document.querySelector('input').value;
                if (answer === '123456') history.replaceState(null, '', '/logged-in');
                else document.querySelector('.geetest_result_tip').textContent = '\u9a8c\u8bc1\u7801\u9519\u8bef';
            };</script>'''
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(html)

        server = HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        profile = Path(tempfile.mkdtemp(prefix="maoer-native-voice-test-"))
        origin = f"http://127.0.0.1:{server.server_port}"
        url = origin + "/member/login"
        received_errors = []
        forwarded = []
        browser_focus = []
        visible_readiness = []
        premature_focus = []
        page_visibility = []
        new_webview = html2.WebView.New
        def create_webview(*args, **kwargs):
            view = new_webview(*args, **kwargs)
            def focused(event):
                browser_focus.append(wx.GetTopLevelParent(view).GetTitle())
                event.Skip()
            view.Bind(wx.EVT_SET_FOCUS, focused)
            window = wx.GetTopLevelParent(view)
            def child_focused(event):
                if not window.IsShown():
                    premature_focus.append(True)
                event.Skip()
            window.Bind(wx.EVT_CHILD_FOCUS, child_focused)
            return view
        with patch("web_login.webview2_profile_dir", return_value=profile), \
                patch("web_login.login_url", return_value=url), \
                patch("web_login.VOICE_LOGIN_SCRIPT", VOICE_LOGIN_SCRIPT.replace(BASE_URL, origin)), \
                patch("web_login.CaptchaAudioPlayer") as audio, \
                patch.object(html2.WebView, "New", side_effect=create_webview), \
                patch.object(BrowserLoginDialog, "_on_navigating", lambda self, event: None):
            parent = LoginDialog(None, MaoerApi(cookie=""))
            parent.notebook.SetSelection(1)
            dialog = NativePasswordLoginDialog(parent)
            view = dialog.webview
            def shown(event):
                if event.GetEventObject() is dialog and event.IsShown():
                    visible_readiness.append(dialog.voice_box.IsEnabled())
                event.Skip()
            dialog.Bind(wx.EVT_SHOW, shown)
            self.assertFalse(view.IsShown())
            self.assertFalse(dialog.check_button.IsShown())
            def enter_answer(answer):
                self.assertIs(wx.Window.FindFocus(), dialog.voice_box)
                self.assertTrue(dialog.cancel_button.IsEnabled())
                forwarded.append(answer)
                dialog.voice_box.SetValue(answer)
                dialog.on_confirm(None)
            def announce(message):
                if "Ctrl" in message:
                    wx.CallAfter(enter_answer, "000000")
                elif "验证码错误" in message:
                    received_errors.append(message)
                    wx.CallAfter(enter_answer, "000000" if len(received_errors) == 1 else "123456")
            dialog.announcer.announce = announce
            def loaded(event):
                if event.GetURL() == url:
                    view.RunScriptAsync("document.querySelector('input').focus(); "
                                        "JSON.stringify({atStart: window.hiddenAtStart, "
                                        "root: document.documentElement.getAttribute('aria-hidden'), "
                                        "body: document.body.getAttribute('aria-hidden'), "
                                        "focused: document.activeElement === document.querySelector('input')})")
            def inspected(event):
                if page_visibility:
                    event.Skip()
                    return
                result = json.loads(event.GetString())
                page_visibility.append(json.loads(result) if isinstance(result, str) else result)
                dialog._password_done(True)
            view.Bind(html2.EVT_WEBVIEW_LOADED, loaded)
            view.Bind(html2.EVT_WEBVIEW_SCRIPT_RESULT, inspected)
            timer = wx.CallLater(10000, dialog._native_failed, "Test timed out")
            try:
                with patch.object(view, "GetCurrentURL", return_value=BASE_URL + "/member/login"), \
                        patch.object(dialog, "check_login", side_effect=dialog._finish_login):
                    wx.CallAfter(dialog.start, lambda result, error: parent.EndModal(result))
                    self.assertEqual(parent.ShowModal(), wx.ID_OK)
                self.assertEqual(forwarded, ["000000", "000000", "123456"])
                self.assertEqual(received_errors, ["验证码错误", "验证码错误"])
                audio.return_value.play.assert_called_once_with("https://static.geetest.com/synthetic.mp3")
                self.assertEqual(dialog._pending_answer, "")
                self.assertEqual(visible_readiness, [True], "Show verification only when input is ready")
                self.assertEqual(premature_focus, [], "Preparing verification must leave focus in the login form")
                self.assertEqual(browser_focus, [], "Native login must never focus its background browser")
                self.assertEqual(page_visibility, [{"atStart": "true", "root": "true", "body": "true", "focused": False}],
                                 "Hide the background page from load through later DOM changes")
                self.assertEqual(tuple(view.GetRect()), (-32000, -32000, 1, 1))
            finally:
                timer.Stop()
                dialog.Destroy()
                parent.Destroy()
                app.Yield()
                audio.return_value.destroy.assert_called_once()
                server.shutdown()
                server.server_close()
                _remove_login_profile(profile)


if __name__ == "__main__":
    unittest.main()
