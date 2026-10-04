from __future__ import annotations

import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import wx

from account_store import (
    AccountState, LoginCredentials, SavedAccount, load_accounts, normalize_cookie,
    protect_login, save_accounts, unprotect_login,
)
from app import MaoerFrame
from app_paths import webview2_profile_dir
from login_dialog import AccountManagerDialog, CookieLoginDialog, LoginDialog, run_dialog_task
from maoer_api import AccountInfo, ApiError, MaoerApi


class AccountStorageTests(unittest.TestCase):
    def test_saved_login_is_encrypted_and_old_accounts_remain_readable(self):
        login = LoginCredentials("person@example.test", " test 密码 with spaces ", "HK", "中国香港特别行政区 +852")
        with tempfile.TemporaryDirectory() as directory, patch("account_store.app_data_dir", return_value=Path(directory)):
            saved = SavedAccount(1, "甲", "token=first", credentials=protect_login(login))
            save_accounts(AccountState((saved,)))
            contents = (Path(directory) / "accounts.json").read_text(encoding="utf-8")
            self.assertNotIn(login.username, contents)
            self.assertNotIn(login.password, contents)
            ciphertext = base64.b64decode(saved.credentials)
            self.assertNotIn(login.username.encode("utf-8"), ciphertext)
            self.assertNotIn(login.password.encode("utf-8"), ciphertext)
            self.assertEqual(unprotect_login(load_accounts().get(1).credentials), login)
            with self.assertRaises(ValueError):
                unprotect_login("bm90IGVuY3J5cHRlZA==")
            data = json.loads(contents)
            del data["accounts"][0]["credentials"]
            (Path(directory) / "accounts.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(load_accounts().get(1).credentials, "")

    def test_cookie_import_preserves_tokens_and_discards_header_metadata(self):
        expected = "uid=12; token=abc%3D=="
        for text in (
            "uid=12; token=abc%3D==; Path=/; HttpOnly; SameSite=Lax; priority=High",
            "GET / HTTP/1.1\nHost: www.missevan.com\nCookie: uid=12; token=abc%3D==\nUser-Agent: example",
            json.dumps({"cookies": [
                {"domain": ".missevan.com", "name": "uid", "value": "12", "httpOnly": True},
                {"domain": "www.missevan.com", "name": "token", "value": "abc%3D=="},
                {"domain": "missevan.com.evil.test", "name": "foreign", "value": "excluded"},
            ]}),
        ):
            with self.subTest(text=text):
                self.assertEqual(normalize_cookie(text), expected)
                self.assertEqual(normalize_cookie(expected), expected)
        for text in ("", "not a cookie", "Path=/; Secure", "sid=a; sid=b",
                     '[{"name":"sid","value":"x\\r\\nInjected: y"}]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                normalize_cookie(text)

    def test_store_logout_delete_deduplicate_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch("account_store.app_data_dir", return_value=Path(directory)):
            self.assertIsNone(load_accounts())
            first = SavedAccount(1, "甲", "token=first", "工作")
            second = SavedAccount(2, "乙", "token=second")
            state = AccountState().updated(first, activate=True).updated(second)
            save_accounts(state)
            self.assertEqual(load_accounts(), state)
            state = state.updated(replace(first, cookie="token=renewed"), activate=True)
            self.assertEqual(len(state.accounts), 2)
            with patch("account_store.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                save_accounts(state)
            self.assertEqual(load_accounts().get(1).cookie, "token=first")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            state = replace(state, active_user_id=None)
            save_accounts(state)
            self.assertIsNone(load_accounts().active_user_id)
            self.assertEqual(len(load_accounts().accounts), 2)
            save_accounts(state.removed(1).removed(2))
            self.assertEqual(load_accounts(), AccountState())
            (Path(directory) / "accounts.json").write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_accounts()

    def test_logout_clears_request_jar_and_browser_profiles_are_isolated(self):
        api = MaoerApi(cookie="token=old")
        api.session.cookies.set("old_session", "old", domain=".missevan.com", path="/")
        api.set_cookie("")
        request = api.session.prepare_request(requests.Request("GET", "https://www.missevan.com/"))
        self.assertNotIn("Cookie", request.headers)
        with tempfile.TemporaryDirectory() as directory, patch("app_paths.app_data_dir", return_value=Path(directory)):
            paths = {webview2_profile_dir(create=False, cookie=value) for value in ("", "token=a", "token=b")}
            self.assertEqual(len(paths), 3)
            self.assertTrue(all(path.parent == Path(directory) / "webview2_profile" for path in paths))

    def test_closed_dialog_discards_late_result(self):
        dialog = Mock()
        dialog.IsBeingDeleted.return_value = False
        dialog.IsModal.return_value = False
        done, failed = Mock(), Mock()
        with patch("login_dialog.threading.Thread") as thread, \
                patch("login_dialog.wx.CallAfter", side_effect=lambda callback, *args: callback(*args)):
            run_dialog_task(dialog, lambda: "late cookie", done, failed)
            thread.call_args.kwargs["target"]()
        done.assert_not_called()
        failed.assert_not_called()


class AccountInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        for target, options in (
            ("account_store.app_data_dir", {"return_value": self.path}),
            ("app_settings.app_data_dir", {"return_value": self.path}),
            ("maoer_api.MaoerApi._load_cookie", {"return_value": ""}),
            ("maoer_api.MaoerApi.clear_saved_cookie", {}),
            ("requests.Session.request", {"side_effect": AssertionError("Live requests are forbidden")}),
            ("app.HiddenBrowserPlayer", {}),
            ("app.wx.CallAfter", {}),
            ("app.MaoerFrame._refresh_account_title", {}),
            ("app.MaoerFrame.load_homepage", {}),
        ):
            mock_patch = patch(target, **options)
            mock_patch.start()
            self.addCleanup(mock_patch.stop)
        self.frame = MaoerFrame()
        self.addCleanup(self.frame.audio_output_router.close)
        self.addCleanup(self.frame.Destroy)

    def save(self, user_id, *, cookie=None, note=None, login=None):
        api = MaoerApi(cookie=cookie or f"token=account{user_id}")
        info = AccountInfo(user_id, f"账号{user_id}", "")
        self.assertTrue(self.frame._save_account(api, info, note=note, login=login))
        return api

    def test_menu_and_account_lifecycle_survive_logout_and_restart(self):
        frame = self.frame
        menu = frame.GetMenuBar()
        self.assertIsNotNone(menu.FindItemById(frame.account_login_menu_id))
        self.assertIsNotNone(menu.FindItemById(frame.account_cookie_login_menu_id))
        self.save(1, note="常用")
        self.save(2)
        self.save(1, cookie="token=renewed")
        self.assertEqual(len(frame.account_state.accounts), 2)
        self.assertEqual(frame.account_state.get(1).note, "常用")
        self.assertIsNotNone(frame.GetMenuBar().FindItemById(frame.account_manage_menu_id))
        self.assertTrue(frame.on_account_logout(None))
        self.assertFalse(frame.account_logged_in)
        self.assertEqual(frame.api.cookie_header, "")
        self.assertEqual(len(load_accounts().accounts), 2)
        self.assertIsNotNone(frame.GetMenuBar().FindItemById(frame.account_manage_menu_id))
        # Even an old environment/file cookie cannot reactivate a logged-out account.
        with patch("maoer_api.MaoerApi._load_cookie", return_value="token=legacy") as legacy:
            restarted = MaoerFrame()
            try:
                self.assertEqual(restarted.api.cookie_header, "")
                self.assertEqual(len(restarted.account_state.accounts), 2)
                legacy.assert_not_called()
            finally:
                restarted.audio_output_router.close()
                restarted.Destroy()
        for account in tuple(frame.account_state.accounts):
            self.assertTrue(frame._remove_saved_account(account, frame))
        self.assertIsNotNone(frame.GetMenuBar().FindItemById(frame.account_cookie_login_menu_id))
        self.assertEqual(load_accounts(), AccountState())
        self.save(3)
        self.assertTrue(frame._remove_saved_account(frame.account_state.get(3), frame))
        self.assertFalse(frame.account_logged_in)
        self.assertEqual(frame.api.cookie_header, "")
        self.assertEqual(load_accounts(), AccountState())

    def test_save_failure_keeps_current_session_and_saved_accounts(self):
        first = self.save(1, login=LoginCredentials("person@example.test", "old password"))
        old_state = self.frame.account_state
        with patch("app.save_accounts", side_effect=OSError("disk full")), patch("app.wx.MessageBox"):
            self.assertFalse(self.frame._save_account(MaoerApi(cookie="token=second"), AccountInfo(2, "乙", "")))
            self.assertFalse(self.frame.on_account_logout(None))
        self.assertIs(self.frame.api, first)
        self.assertEqual(self.frame.account_state, old_state)
        self.assertEqual(load_accounts(), old_state)
        with patch("app.protect_login", side_effect=ValueError("Encryption failed")), patch("app.wx.MessageBox"):
            self.assertFalse(self.frame._save_account(MaoerApi(cookie="token=new"), AccountInfo(1, "甲", ""),
                                                     login=LoginCredentials("person@example.test", "new password")))
        self.assertIs(self.frame.api, first)
        self.assertEqual(load_accounts(), old_state)

    def test_edit_opens_account_login_instead_of_cookie_editor(self):
        self.save(1)
        saved = self.frame.account_state.get(1)
        current = self.save(2)
        with patch("app.LoginDialog") as login, patch("app.CookieLoginDialog") as cookie:
            login.return_value.ShowModal.return_value = wx.ID_CANCEL
            self.frame._edit_saved_account(self.frame, saved)
            cookie.assert_not_called()
            self.assertIs(login.call_args.kwargs["saved"], saved)
            self.assertEqual(login.call_args.args[1].cookie_header, "")
            login.return_value.Destroy.assert_called_once()
            login.return_value.ShowModal.return_value = wx.ID_OK
            login.return_value.api = MaoerApi(cookie="token=updated")
            login.return_value.account_info = AccountInfo(1, "一", "")
            login.return_value.note = "新备注"
            login.return_value.login = LoginCredentials("person@example.test", "new password")
            self.assertTrue(self.frame._edit_saved_account(self.frame, saved))
        updated = load_accounts().get(1)
        self.assertEqual(updated.cookie, "token=updated")
        self.assertEqual(updated.note, "新备注")
        self.assertEqual(unprotect_login(updated.credentials).password, "new password")
        self.assertIs(self.frame.api, current)
        self.assertEqual(self.frame.account_state.active_user_id, 2)

    def test_account_edit_prefills_credentials_and_rejects_other_account(self):
        remembered = LoginCredentials("12345678", " password with spaces ", "HK", "中国香港特别行政区 +852")
        self.save(1, note="备注", login=remembered)
        old_state = self.frame.account_state
        dialog = LoginDialog(self.frame, MaoerApi(cookie=""), saved=old_state.get(1))
        self.addCleanup(dialog.Destroy)
        self.assertEqual(dialog.notebook.GetSelection(), 1)
        self.assertEqual(dialog.login_name_box.GetValue(), remembered.username)
        self.assertEqual(dialog.password_box.GetValue(), remembered.password)
        self.assertEqual(dialog.password_region, "HK")
        self.assertTrue(dialog.password_box.GetWindowStyle() & wx.TE_PASSWORD)
        self.assertEqual(dialog.note_box.GetValue(), "备注")
        dialog.login_name_box.SetValue("person@example.test")
        dialog.password_box.SetValue(" changed password ")
        with patch("web_login.NativePasswordLoginDialog") as native, \
                patch.object(dialog, "EndModal") as end, patch.object(dialog, "IsModal", return_value=True), \
                patch.object(dialog, "IsShown", return_value=True), \
                patch("login_dialog.wx.MessageBox"):
            native.return_value.start.side_effect = lambda callback: callback(wx.ID_OK, "")
            native.return_value.account_info = AccountInfo(2, "其他账号", "")
            native.return_value.api = MaoerApi(cookie="token=other")
            dialog.on_login(None)
            end.assert_not_called()
            self.assertIsNone(dialog.login)
            self.assertIsNone(dialog.account_info)
            self.assertEqual(dialog.password_box.GetValue(), " changed password ")
            self.assertEqual(load_accounts(), old_state)
            native.return_value.account_info = AccountInfo(1, "账号1", "")
            native.return_value.api = MaoerApi(cookie="token=verified")
            dialog.on_login(None)
            end.assert_called_once_with(wx.ID_OK)
            self.assertEqual(dialog.login, LoginCredentials("person@example.test", " changed password ",
                                                           "HK", remembered.region_label))
            self.assertEqual(dialog.password_box.GetValue(), "")

    def test_saved_credentials_survive_cookie_refresh_and_sms_login(self):
        login = LoginCredentials("person@example.test", "remembered password")
        self.save(1, login=login)
        self.save(1, cookie="token=refreshed")
        self.assertEqual(unprotect_login(load_accounts().get(1).credentials), login)
        self.save(1, login=LoginCredentials("13800000000"))
        self.assertEqual(unprotect_login(load_accounts().get(1).credentials),
                         LoginCredentials("13800000000", login.password))

    def test_manager_selection_is_idle_and_explicit_login_checks_identity(self):
        self.save(1)
        current = self.save(2)
        dialog = AccountManagerDialog(self.frame)
        self.addCleanup(dialog.Destroy)
        self.assertIn("当前登录", dialog.list.GetItemText(1))
        with patch("login_dialog.run_dialog_task") as task, patch.object(dialog, "EndModal") as end:
            dialog.list.Select(0)
            self.assertEqual(self.frame.account_state.active_user_id, 2)
            task.assert_not_called()
            dialog.on_login(None)
            _, work, done, failed = task.call_args.args
            with patch("login_dialog.wx.MessageBox"):
                failed(ApiError("需要登录"))
            end.assert_not_called()
            self.assertIs(self.frame.api, current)
            dialog.on_login(None)
            _, work, done, failed = task.call_args.args
            done(AccountInfo(1, "账号1", ""))
            end.assert_called_once_with(wx.ID_OK)
        self.assertEqual(self.frame.account_state.active_user_id, 1)

    def test_copy_cookie_uses_selected_account_and_never_logs_in(self):
        self.save(1, cookie="token=selected==; uid=1")
        self.save(2, cookie="token=current; uid=2")
        dialog = AccountManagerDialog(self.frame)
        self.addCleanup(dialog.Destroy)
        with patch("login_dialog.wx.TheClipboard") as clipboard, patch("login_dialog.run_dialog_task") as task, \
                patch.object(dialog.announcer, "announce") as announce:
            dialog.list.Select(0)
            clipboard.SetData.assert_not_called()
            self.assertEqual(dialog.copy_cookie_button.GetLabel(), "复制 Cookie(&C)")
            dialog.on_copy_cookie(None)
            self.assertEqual(clipboard.SetData.call_args.args[0].GetText(), "token=selected==; uid=1")
            clipboard.Close.assert_called_once()
            clipboard.Flush.assert_called_once()
            task.assert_not_called()
            announce.assert_called_once_with("Cookie 已复制")
            self.assertEqual(self.frame.account_state.active_user_id, 2)
            clipboard.reset_mock()
            clipboard.Open.return_value = False
            with patch("login_dialog.wx.MessageBox") as error:
                dialog.on_copy_cookie(None)
                error.assert_called_once()
            clipboard.SetData.assert_not_called()
            clipboard.Close.assert_not_called()

    def test_adding_an_account_closes_manager_only_after_success(self):
        dialog = AccountManagerDialog(self.frame)
        self.addCleanup(dialog.Destroy)
        for selection in (0, 1):
            for chosen, logged_in in ((False, False), (True, False), (True, True)):
                with self.subTest(selection=selection, chosen=chosen, logged_in=logged_in), \
                        patch("login_dialog.wx.SingleChoiceDialog") as choice, \
                        patch.object(self.frame, "_show_account_login", return_value=logged_in) as login, \
                        patch.object(dialog, "EndModal") as end, \
                        patch.object(dialog.list, "SetFocus") as focus:
                    choice.return_value.ShowModal.return_value = wx.ID_OK if chosen else wx.ID_CANCEL
                    choice.return_value.GetSelection.return_value = selection
                    dialog.on_add(None)
                    if chosen:
                        login.assert_called_once_with(dialog, cookie_login=selection == 1)
                    else:
                        login.assert_not_called()
                    if logged_in:
                        end.assert_called_once_with(wx.ID_OK)
                        focus.assert_not_called()
                    else:
                        end.assert_not_called()
                        focus.assert_called_once()
                    choice.return_value.Destroy.assert_called_once()

    def test_cookie_dialog_validates_before_success(self):
        dialog = CookieLoginDialog(self.frame)
        self.addCleanup(dialog.Destroy)
        self.assertEqual(dialog.cookie_box.GetName(), "Cookie")
        dialog.cookie_box.SetValue("Cookie: token=abc==; Path=/; HttpOnly")
        with patch("login_dialog.run_dialog_task") as task, patch.object(dialog, "EndModal") as end:
            dialog.on_confirm(None)
            self.assertEqual(dialog.api.cookie_header, "token=abc==")
            end.assert_not_called()
            task.call_args.args[2](AccountInfo(4, "四", ""))
            end.assert_called_once_with(wx.ID_OK)
        self.assertEqual(self.frame.account_state.accounts, ())

    def test_legacy_account_can_edit_notes_without_reauthenticating(self):
        saved = SavedAccount(4, "四", "token=old", "备注")
        edit = LoginDialog(self.frame, MaoerApi(cookie=""), saved=saved)
        self.addCleanup(edit.Destroy)
        self.assertEqual(edit.login_name_box.GetValue(), "")
        self.assertEqual(edit.password_box.GetValue(), "")
        with patch.object(edit, "EndModal") as end, patch("login_dialog.run_dialog_task") as task:
            edit.note_box.SetValue("新备注")
            edit.on_login(None)
            task.assert_not_called()
            end.assert_called_once_with(wx.ID_OK)
            self.assertEqual(edit.note, "新备注")
            self.assertEqual(edit.cookie_header, saved.cookie)
            self.assertIsNone(edit.login)

    def test_old_background_result_and_auth_failure_cannot_change_new_account(self):
        self.save(1)
        original = self.frame.api
        queued = []
        done = Mock()
        with patch("app.threading.Thread") as thread, \
                patch("app.wx.CallAfter", side_effect=lambda callback, *args: queued.append((callback, args))):
            self.frame._run_background("加载", lambda: "old result", done)
            thread.call_args.kwargs["target"]()
        self.save(2)
        for callback, args in queued:
            callback(*args)
        self.frame._mark_account_logged_out("old error", original)
        done.assert_not_called()
        self.assertEqual(self.frame.account_state.active_user_id, 2)

    def test_sms_login_no_longer_persists_before_acceptance_and_timer_stops_on_destroy(self):
        api = MaoerApi(cookie="")
        dialog = LoginDialog(self.frame, api)
        timer = Mock()
        dialog._countdown_timer = timer
        with patch.object(api, "save_cookie") as save, patch.object(dialog, "EndModal"):
            dialog._login_success(AccountInfo(1, "一", ""))
            save.assert_not_called()
        dialog.Destroy()
        self.app.Yield()
        timer.Stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
