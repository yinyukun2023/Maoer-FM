from __future__ import annotations

import base64
import ctypes
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
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

    def test_account_order_roundtrips_without_changing_account_data_or_login(self):
        accounts = tuple(SavedAccount(i, f"账号{i}", f"token=value{i}", f"备注{i}", f"encrypted{i}")
                         for i in (1, 2, 3))
        for active_id in (None, 2):
            for position, expected in ((0, [2, 1, 3]), (2, [1, 3, 2])):
                with self.subTest(active_id=active_id, position=position):
                    original = AccountState(accounts, active_id)
                    moved = original.moved(2, position)
                    self.assertEqual([account.user_id for account in moved.accounts], expected)
                    self.assertEqual(moved.active_user_id, active_id)
                    self.assertEqual(original.accounts, accounts)
                    for account in accounts:
                        self.assertIs(moved.get(account.user_id), account)
                    with tempfile.TemporaryDirectory() as directory, \
                            patch("account_store.app_data_dir", return_value=Path(directory)):
                        save_accounts(moved)
                        self.assertEqual(load_accounts(), moved)
                    renewed = moved.updated(replace(accounts[1], cookie="token=renewed"), activate=True)
                    self.assertEqual([account.user_id for account in renewed.accounts], expected)

    def test_account_order_boundaries_empty_and_missing_account_are_noops(self):
        empty = AccountState()
        self.assertIs(empty.moved(1, 0), empty)
        first, last = SavedAccount(1, "甲", "token=a"), SavedAccount(2, "乙", "token=b")
        state = AccountState((first, last), 1)
        for user_id, position in ((1, -1), (1, 0), (2, 99), (99, 0)):
            self.assertIs(state.moved(user_id, position), state)
        single = AccountState((first,), 1)
        self.assertIs(single.moved(1, 99), single)

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

    def _run_native_in_child(self):
        # Match the WebView test strategy: real modal loops must not pump the
        # deferred window events left by unrelated tests with mocked scheduling.
        if os.environ.get("MAOER_ACCOUNT_GUI_TEST_CHILD") == "1":
            return False
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-m", "unittest",
             f"tests.test_accounts.AccountInterfaceTests.{self._testMethodName}", "-q"],
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, "MAOER_ACCOUNT_GUI_TEST_CHILD": "1", "PYTHONIOENCODING": "utf-8"},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return True

    def test_personal_menu_entries_follow_login_state(self):
        frame = self.frame

        def check_entries(logged_in):
            menu_bar = frame.GetMenuBar()
            self.assertEqual(menu_bar.FindItemById(frame.account_logout_menu_id) is not None, logged_in)
            for login_id in (frame.account_login_menu_id, frame.account_cookie_login_menu_id):
                self.assertEqual(menu_bar.FindItemById(login_id) is not None, not logged_in)
            for item_id, label in (
                (frame.account_history_menu_id, "我的播放历史"),
                (frame.account_following_menu_id, "我的关注"),
            ):
                with self.subTest(logged_in=logged_in, label=label):
                    item = frame.GetMenuBar().FindItemById(item_id)
                    if logged_in:
                        self.assertIsNotNone(item)
                        self.assertEqual(item.GetItemLabelText(), label)
                    else:
                        self.assertIsNone(item)
            items = list(frame.GetMenuBar().GetMenu(0).GetMenuItems())
            self.assertFalse(any(left.IsSeparator() and right.IsSeparator()
                                 for left, right in zip(items, items[1:])))

        check_entries(False)
        self.save(1)
        check_entries(True)
        self.assertTrue(frame.on_account_logout(None))
        self.assertEqual(len(frame.account_state.accounts), 1)
        check_entries(False)
        self.save(1)
        check_entries(True)
        frame._mark_account_logged_out("登录状态失效", frame.api)
        check_entries(False)

    def test_login_method_choice_buttons_are_chinese(self):
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        real_choice = wx.SingleChoiceDialog
        labels = []

        def create_choice(*args, **kwargs):
            choice = real_choice(*args, **kwargs)

            def show_modal():
                labels.extend(choice.FindWindow(item_id).GetLabelText()
                              for item_id in (wx.ID_OK, wx.ID_CANCEL))
                return wx.ID_CANCEL

            choice.ShowModal = show_modal
            return choice

        with patch("login_dialog.wx.SingleChoiceDialog", side_effect=create_choice), \
                patch.object(self.frame, "_show_account_login") as login:
            manager.on_add(None)
        self.assertEqual(labels, ["确定", "取消"])
        login.assert_not_called()

    def test_login_menu_is_available_when_signed_out_with_or_without_saved_accounts(self):
        frame = self.frame
        for has_saved_accounts in (False, True):
            with self.subTest(has_saved_accounts=has_saved_accounts):
                if has_saved_accounts:
                    self.save(1)
                    self.assertTrue(frame.on_account_logout(None))
                menu = frame.GetMenuBar().GetMenu(0)
                login_entry = next((item for item in menu.GetMenuItems()
                                    if item.GetItemLabelText() == "登录账号"), None)
                self.assertIsNotNone(login_entry)
                self.assertIsNotNone(login_entry.GetSubMenu())
                self.assertIsNone(frame.GetMenuBar().FindItemById(frame.account_logout_menu_id))
                if has_saved_accounts:
                    self.assertIsNotNone(frame.GetMenuBar().FindItemById(frame.account_manage_menu_id))
                for item_id, cookie_login in ((frame.account_login_menu_id, False),
                                              (frame.account_cookie_login_menu_id, True)):
                    self.assertIsNotNone(login_entry.GetSubMenu().FindItemById(item_id))
                    with patch.object(frame, "_show_account_login") as login:
                        event = wx.CommandEvent(wx.EVT_MENU.typeId, int(item_id))
                        self.assertTrue(frame.GetEventHandler().ProcessEvent(event))
                        if cookie_login:
                            login.assert_called_once_with(frame, cookie_login=True)
                        else:
                            login.assert_called_once_with(frame)

    def test_account_manager_logout_button_only_for_selected_active_account(self):
        self.save(1)
        self.save(2)
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        self.assertEqual(manager.list.GetItemText(0), "账号1")
        self.assertEqual(manager.list.GetItemText(1), "账号2（当前登录账号）")
        self.assertTrue(manager.logout_button.IsShown())
        self.assertEqual(manager.logout_button.GetLabel(), "退出当前登录账号(&O)")
        manager.list.Select(0)
        manager._update_buttons()
        self.assertFalse(manager.logout_button.IsShown())
        with patch.object(self.frame, "on_account_logout") as logout:
            manager.on_logout(None)
            logout.assert_not_called()
        manager.list.Select(1)
        manager._update_buttons()
        self.assertTrue(manager.logout_button.IsShown())
        manager._busy = True
        manager._update_buttons()
        self.assertFalse(manager.logout_button.IsEnabled())
        with patch.object(self.frame, "on_account_logout") as logout:
            manager.on_logout(None)
            logout.assert_not_called()
        manager._busy = False
        with patch.object(manager.announcer, "announce"):
            manager.on_logout(None)
        self.assertFalse(self.frame.account_logged_in)
        self.assertFalse(manager.logout_button.IsShown())
        self.assertEqual(manager.list.GetItemText(1), "账号2")
        self.assertEqual(manager.selected().user_id, 2)
        self.assertEqual(len(load_accounts().accounts), 2)

    def test_logout_menu_names_current_login_and_preserves_saved_accounts(self):
        self.save(1)
        self.save(2)
        frame = self.frame
        self.assertEqual(self.frame.account_state.active_user_id, 2)
        logout = frame.GetMenuBar().GetMenu(0).FindItemById(frame.account_logout_menu_id)
        self.assertIsNotNone(logout)
        self.assertEqual(logout.GetItemLabel(), "退出当前登录账号(&O)")
        self.assertIsNone(frame.GetMenuBar().FindItemById(frame.account_login_menu_id))
        self.assertIsNone(frame.GetMenuBar().FindItemById(frame.account_cookie_login_menu_id))
        event = wx.CommandEvent(wx.EVT_MENU.typeId, int(frame.account_logout_menu_id))
        self.assertTrue(frame.GetEventHandler().ProcessEvent(event))
        self.assertFalse(self.frame.account_logged_in)
        self.assertIsNone(self.frame.account_state.active_user_id)
        self.assertEqual([account.user_id for account in self.frame.account_state.accounts], [1, 2])
        self.assertIsNone(frame.GetMenuBar().FindItemById(frame.account_logout_menu_id))
        self.assertIsNotNone(frame.GetMenuBar().FindItemById(frame.account_login_menu_id))
        self.assertIsNotNone(frame.GetMenuBar().FindItemById(frame.account_cookie_login_menu_id))

    @staticmethod
    def press_list_key(manager, *, key=wx.WXK_DELETE, shift=False, control=False, alt=False):
        event = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
        event.SetKeyCode(key)
        event.SetShiftDown(shift)
        event.SetControlDown(control)
        event.SetAltDown(alt)
        event.SetEventObject(manager.list)
        manager.list.GetEventHandler().ProcessEvent(event)
        return event

    @staticmethod
    def click_button(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.GetEventHandler().ProcessEvent(event)

    def test_move_buttons_mnemonics_boundaries_and_busy_state(self):
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        buttons = (manager.move_up_button, manager.move_down_button,
                   manager.move_first_button, manager.move_last_button)
        self.assertEqual([button.GetLabel() for button in buttons],
                         ["上移(&U)", "下移(&J)", "移至最前(&T)", "移至末尾(&G)"])
        self.assertFalse(any(button.IsEnabled() for button in buttons))
        self.save(1)
        manager.refresh(1)
        self.assertFalse(any(button.IsEnabled() for button in buttons))
        self.save(2)
        self.save(3)
        for selected, expected in ((1, [False, True, False, True]), (2, [True] * 4),
                                   (3, [True, False, True, False])):
            manager.refresh(selected)
            self.assertEqual([button.IsEnabled() for button in buttons], expected)
        manager.refresh(2)
        manager._busy = True
        manager._update_buttons()
        self.assertFalse(any(button.IsEnabled() for button in buttons))
        with patch.object(self.frame, "_move_saved_account") as move:
            manager._move_selected("up")
            move.assert_not_called()
        manager._busy = False
        manager.list.Select(manager.list.GetFirstSelected(), False)
        manager._update_buttons()
        self.assertFalse(any(button.IsEnabled() for button in buttons))
        self.assertFalse(manager.logout_button.IsShown())

    def test_moving_follows_selected_account_saves_order_and_does_not_change_playback(self):
        for user_id in (1, 2, 3):
            self.save(user_id, note=f"备注{user_id}")
        original_api = self.frame.api
        original_accounts = self.frame.account_state.accounts
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        manager.refresh(2)
        cases = ((manager.move_up_button, [2, 1, 3], 0),
                 (manager.move_down_button, [1, 2, 3], 1),
                 (manager.move_last_button, [1, 3, 2], 2),
                 (manager.move_first_button, [2, 1, 3], 0))
        with patch.object(self.frame, "_change_account") as change, \
                patch.object(self.frame, "_stop") as stop, \
                patch.object(manager.list, "SetFocus") as focus, \
                patch.object(manager.announcer, "announce") as announce:
            for button, expected, index in cases:
                self.click_button(button)
                self.assertEqual([a.user_id for a in self.frame.account_state.accounts], expected)
                self.assertEqual(load_accounts(), self.frame.account_state)
                self.assertEqual([int(manager.list.GetItemText(i, 2)) for i in range(3)], expected)
                self.assertEqual(manager.selected().user_id, 2)
                self.assertEqual(manager.list.GetFocusedItem(), index)
                self.assertEqual(self.frame.account_state.active_user_id, 3)
                self.assertIs(self.frame.api, original_api)
                self.assertFalse(manager.logout_button.IsShown())
                focus.assert_called()
                self.assertIn(f"第 {index + 1} 项", announce.call_args.args[0])
                for account in original_accounts:
                    self.assertEqual(self.frame.account_state.get(account.user_id), account)
            change.assert_not_called()
            stop.assert_not_called()
            manager.refresh(3)
            self.click_button(manager.move_first_button)
            self.assertEqual(manager.selected().user_id, 3)
            self.assertTrue(manager.logout_button.IsShown())
            self.assertIn("当前登录账号", manager.list.GetItemText(0))
        restarted = MaoerFrame()
        try:
            self.assertEqual([a.user_id for a in restarted.account_state.accounts], [3, 2, 1])
            self.assertEqual(restarted.account_state.active_user_id, 3)
        finally:
            restarted.audio_output_router.close()
            restarted.Destroy()

    def test_move_failure_retains_original_order_and_selection(self):
        self.save(1)
        self.save(2)
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        before = self.frame.account_state
        with patch("app.save_accounts", side_effect=OSError("disk full")), \
                patch("app.message_box") as error, patch.object(manager.announcer, "announce") as announce:
            self.click_button(manager.move_first_button)
        error.assert_called_once()
        announce.assert_not_called()
        self.assertIs(self.frame.account_state, before)
        self.assertEqual(load_accounts(), before)
        self.assertEqual(manager.selected().user_id, 2)
        self.assertEqual([int(manager.list.GetItemText(i, 2)) for i in range(2)], [1, 2])
        with patch.object(self.frame, "_persist_accounts") as save:
            manager._move_selected("last")
            save.assert_not_called()

    def test_list_navigation_keys_move_save_and_follow_same_account(self):
        for user_id in (1, 2, 3):
            self.save(user_id)
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        original_api = self.frame.api
        key_sets = (
            (wx.WXK_PAGEUP, wx.WXK_PAGEDOWN, wx.WXK_END, wx.WXK_HOME),
            (wx.WXK_NUMPAD_PAGEUP, wx.WXK_NUMPAD_PAGEDOWN, wx.WXK_NUMPAD_END, wx.WXK_NUMPAD_HOME),
        )
        with patch.object(manager.announcer, "announce") as announce, \
                patch.object(manager.list, "SetFocus") as focus, \
                patch.object(self.frame, "_change_account") as change, \
                patch.object(self.frame, "_stop") as stop:
            for up, down, last, first in key_sets:
                manager.refresh(2)
                for key, control, expected, index in (
                    (up, False, [2, 1, 3], 0),
                    (down, False, [1, 2, 3], 1),
                    (last, True, [1, 3, 2], 2),
                    (first, True, [2, 1, 3], 0),
                ):
                    with self.subTest(key=key):
                        focus.reset_mock()
                        event = self.press_list_key(manager, key=key, control=control)
                        self.assertFalse(event.GetSkipped())
                        self.assertEqual([a.user_id for a in self.frame.account_state.accounts], expected)
                        self.assertEqual(load_accounts(), self.frame.account_state)
                        self.assertEqual(manager.selected().user_id, 2)
                        self.assertEqual(manager.list.GetFocusedItem(), index)
                        focus.assert_called_once()
                        self.assertIn(f"第 {index + 1} 项", announce.call_args.args[0])
                        self.assertEqual(self.frame.account_state.active_user_id, 3)
                        self.assertIs(self.frame.api, original_api)
                manager._move_selected("down")  # Restore [1, 2, 3] for the next key set.
            change.assert_not_called()
            stop.assert_not_called()

    def test_list_navigation_keys_do_not_move_at_boundaries_or_while_busy(self):
        self.save(1)
        self.save(2)
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        original_state = self.frame.account_state
        keys = ((wx.WXK_PAGEUP, False), (wx.WXK_PAGEDOWN, False),
                (wx.WXK_HOME, True), (wx.WXK_END, True))
        with patch.object(self.frame, "_persist_accounts") as persist, \
                patch.object(manager.announcer, "announce") as announce:
            for selected, pairs in ((1, (keys[0], keys[2])), (2, (keys[1], keys[3]))):
                manager.refresh(selected)
                for key, control in pairs:
                    event = self.press_list_key(manager, key=key, control=control)
                    self.assertFalse(event.GetSkipped())
                    self.assertEqual(manager.selected().user_id, selected)
            manager._busy = True
            for key, control in keys:
                self.press_list_key(manager, key=key, control=control)
                self.assertEqual(manager.selected().user_id, 2)
            manager._busy = False
            manager.list.Select(manager.list.GetFirstSelected(), False)
            for key, control in keys:
                self.press_list_key(manager, key=key, control=control)
                self.assertIsNone(manager.selected())
            persist.assert_not_called()
            announce.assert_not_called()
        self.assertIs(self.frame.account_state, original_state)

    def test_list_navigation_keys_pass_through_unrelated_combinations(self):
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        with patch.object(manager, "_move_selected") as move:
            for key, modifiers in (
                (wx.WXK_HOME, 0), (wx.WXK_END, 0),
                (wx.WXK_PAGEUP, wx.MOD_CONTROL), (wx.WXK_PAGEDOWN, wx.MOD_CONTROL),
                (wx.WXK_PAGEUP, wx.MOD_SHIFT), (wx.WXK_PAGEDOWN, wx.MOD_ALT),
                (wx.WXK_HOME, wx.MOD_CONTROL | wx.MOD_SHIFT),
                (wx.WXK_END, wx.MOD_CONTROL | wx.MOD_ALT),
                (ord("U"), wx.MOD_ALT), (ord("J"), wx.MOD_ALT),
                (ord("T"), wx.MOD_ALT), (ord("G"), wx.MOD_ALT),
            ):
                with self.subTest(key=key, modifiers=modifiers):
                    event = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
                    event.SetKeyCode(key)
                    event.SetControlDown(bool(modifiers & wx.MOD_CONTROL))
                    event.SetShiftDown(bool(modifiers & wx.MOD_SHIFT))
                    event.SetAltDown(bool(modifiers & wx.MOD_ALT))
                    manager.on_list_key_down(event)
                    self.assertTrue(event.GetSkipped())
            move.assert_not_called()

    def test_list_navigation_key_save_failure_retains_order_and_selection(self):
        self.save(1)
        self.save(2)
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        before = self.frame.account_state
        with patch("app.save_accounts", side_effect=OSError("disk full")), \
                patch("app.message_box") as error, patch.object(manager.announcer, "announce") as announce:
            self.press_list_key(manager, key=wx.WXK_HOME, control=True)
        error.assert_called_once()
        announce.assert_not_called()
        self.assertIs(self.frame.account_state, before)
        self.assertEqual(load_accounts(), before)
        self.assertEqual(manager.selected().user_id, 2)
        self.assertEqual(manager.list.GetFocusedItem(), 1)

    def test_delete_key_shares_button_confirmation_and_removes_only_selected_account(self):
        self.save(1)
        self.save(2)
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        manager.refresh(1)
        before = self.frame.account_state
        with patch("login_dialog.message_box", return_value=wx.NO) as question:
            self.press_list_key(manager)
        question.assert_called_once()
        self.assertIn("账号1", question.call_args.args[0])
        self.assertTrue(question.call_args.args[2] & wx.NO_DEFAULT)
        self.assertEqual(self.frame.account_state, before)
        with patch("login_dialog.message_box", return_value=wx.YES), \
                patch.object(manager.announcer, "announce"):
            self.press_list_key(manager, key=wx.WXK_NUMPAD_DELETE)
        self.assertEqual([a.user_id for a in load_accounts().accounts], [2])
        self.assertTrue(self.frame.account_logged_in)
        with patch("login_dialog.message_box", return_value=wx.YES) as question, \
                patch.object(manager.announcer, "announce"):
            self.click_button(manager.delete_button)
        self.assertIn("同时退出登录", question.call_args.args[0])
        self.assertFalse(self.frame.account_logged_in)
        self.assertEqual(load_accounts(), AccountState())
        self.assertFalse(manager.logout_button.IsShown())

    def test_delete_key_does_not_delete_during_login_without_selection_or_with_modifiers(self):
        self.save(1)
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        with patch("login_dialog.message_box") as question:
            manager._busy = True
            self.press_list_key(manager)
            manager._busy = False
            for modifiers in ({"shift": True}, {"control": True}, {"alt": True}):
                self.press_list_key(manager, **modifiers)
            manager.list.Select(0, False)
            self.press_list_key(manager)
        question.assert_not_called()
        self.assertEqual(len(load_accounts().accounts), 1)

    def test_native_account_hotkeys_and_conditional_logout_tab_order(self):
        if self._run_native_in_child():
            return
        for user_id in (1, 2, 3):
            self.save(user_id)
        self.save(2)
        manager = AccountManagerDialog(self.frame)
        self.addCleanup(manager.Destroy)
        steps = [("U", [2, 1, 3]), ("J", [1, 2, 3]), ("G", [1, 3, 2]), ("T", [2, 1, 3])]
        observed, errors, timers = [], [], []

        def later(callback):
            timers.append(wx.CallLater(30, callback))

        def fail(exc):
            errors.append(str(exc))
            if manager.IsModal():
                manager.EndModal(wx.ID_CANCEL)

        def check_tab_order():
            try:
                manager.refresh(1)
                self.assertFalse(manager.logout_button.IsShown())
                manager.move_last_button.SetFocus()
                manager.move_last_button.Navigate()
                self.assertIs(wx.Window.FindFocus(), manager.close_button)
                manager.refresh(2)
                self.assertTrue(manager.logout_button.IsShown())
                manager.move_last_button.SetFocus()
                manager.move_last_button.Navigate()
                self.assertIs(wx.Window.FindFocus(), manager.logout_button)
                manager.EndModal(wx.ID_CANCEL)
            except Exception as exc:
                fail(exc)

        def verify_step():
            try:
                _key, expected = steps[len(observed)]
                order = [account.user_id for account in self.frame.account_state.accounts]
                self.assertEqual(order, expected)
                self.assertIs(wx.Window.FindFocus(), manager.list)
                self.assertEqual(manager.selected().user_id, 2)
                observed.append(order)
                later(send_step if len(observed) < len(steps) else check_tab_order)
            except Exception as exc:
                fail(exc)

        def send_step():
            try:
                manager.list.SetFocus()
                # Queue the native Alt mnemonic only to this test list. Do not inject
                # global keyboard input or steal focus from the user's application.
                user32 = ctypes.windll.user32
                user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
                user32.PostMessageW.restype = ctypes.c_int
                self.assertTrue(user32.PostMessageW(int(manager.list.GetHandle()), 0x0106,
                                                    ord(steps[len(observed)][0].lower()), 1 << 29))
                later(verify_step)
            except Exception as exc:
                fail(exc)

        with patch.object(manager.announcer, "announce"):
            later(send_step)
            timers.append(wx.CallLater(3000, lambda: fail("Native keyboard test timed out")))
            try:
                manager.ShowModal()
            finally:
                for timer in timers:
                    timer.Stop()
        self.assertEqual(errors, [])
        self.assertEqual(len(observed), 4)

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
        with patch("app.save_accounts", side_effect=OSError("disk full")), patch("app.message_box"):
            self.assertFalse(self.frame._save_account(MaoerApi(cookie="token=second"), AccountInfo(2, "乙", "")))
            self.assertFalse(self.frame.on_account_logout(None))
        self.assertIs(self.frame.api, first)
        self.assertEqual(self.frame.account_state, old_state)
        self.assertEqual(load_accounts(), old_state)
        with patch("app.protect_login", side_effect=ValueError("Encryption failed")), patch("app.message_box"):
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
                patch("login_dialog.message_box"):
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
            with patch("login_dialog.message_box"), \
                    patch.object(self.frame, '_show_account_login', return_value=False):
                failed(ApiError("需要登录"))
            end.assert_not_called()
            self.assertIs(self.frame.api, current)
            dialog.on_login(None)
            _, work, done, failed = task.call_args.args
            done(AccountInfo(1, "账号1", ""))
            end.assert_called_once_with(wx.ID_OK)
        self.assertEqual(self.frame.account_state.active_user_id, 1)

    def test_expired_saved_login_recovers_from_enter_without_editing(self):
        credentials = LoginCredentials('person@example.test', 'remembered password')
        self.save(1, note='备注', login=credentials)
        self.save(2)
        saved = self.frame.account_state.get(1)
        dialog = AccountManagerDialog(self.frame)
        self.addCleanup(dialog.Destroy)
        dialog.list.Select(0)
        with patch('login_dialog.run_dialog_task') as task, \
                patch('app.LoginDialog') as recover, \
                patch.object(dialog, 'EndModal') as end, patch('login_dialog.message_box') as error:
            recover.return_value.ShowModal.return_value = wx.ID_OK
            recover.return_value.api = MaoerApi(cookie='token=renewed')
            recover.return_value.account_info = AccountInfo(1, '账号1', '')
            recover.return_value.login = credentials
            recover.return_value.note = saved.note
            dialog.on_login(None)
            task.call_args.args[3](ApiError('需要登录'))
            self.assertEqual(recover.call_args.kwargs, {'saved': saved, 'relogin': True})
            end.assert_called_once_with(wx.ID_OK)
            error.assert_not_called()
            recover.return_value.Destroy.assert_called_once()
        self.assertEqual(self.frame.account_state.active_user_id, 1)
        self.assertEqual(self.frame.api.cookie_header, 'token=renewed')
        refreshed = load_accounts().get(1)
        self.assertEqual(refreshed.note, saved.note)
        self.assertEqual(unprotect_login(refreshed.credentials), credentials)

    def test_current_account_enter_rechecks_server_instead_of_trusting_old_login_flag(self):
        self.save(1)
        dialog = AccountManagerDialog(self.frame)
        self.addCleanup(dialog.Destroy)
        with patch('login_dialog.run_dialog_task') as task, patch.object(dialog, 'EndModal') as end, \
                patch('login_dialog.validated_account', return_value=AccountInfo(1, '账号1', '')) as validate:
            dialog.on_login(None)
            task.assert_called_once()
            end.assert_not_called()
            self.assertEqual(task.call_args.args[1]().user_id, 1)
            validate.assert_called_once()

    def test_relogin_submits_saved_password_through_native_flow_even_when_note_changes(self):
        credentials = LoginCredentials('person@example.test', 'remembered password')
        self.save(1, note='旧备注', login=credentials)
        saved = self.frame.account_state.get(1)
        with patch('login_dialog.wx.CallAfter') as later:
            dialog = LoginDialog(self.frame, MaoerApi(cookie=''), saved=saved, relogin=True)
        self.addCleanup(dialog.Destroy)
        self.assertEqual(dialog.GetTitle(), '重新登录账号')
        self.assertEqual(dialog.login_button.GetLabel(), '登录')
        self.assertEqual(dialog.password_box.GetValue(), credentials.password)
        later.assert_called_once_with(dialog._start_saved_login)
        dialog.note_box.SetValue('新备注')
        with patch('web_login.NativePasswordLoginDialog') as native, \
                patch.object(dialog, 'IsModal', return_value=True), \
                patch.object(dialog, 'IsShown', return_value=True), patch.object(dialog, 'EndModal') as end:
            dialog._start_saved_login()
            native.assert_called_once_with(dialog, None, login_name=credentials.username,
                                           password=credentials.password, region_label=credentials.region_label)
            end.assert_not_called()
            native.return_value.api = MaoerApi(cookie='token=renewed')
            native.return_value.account_info = AccountInfo(1, '账号1', '')
            native.return_value.start.call_args.args[0](wx.ID_OK, '')
            end.assert_called_once_with(wx.ID_OK)
            self.assertEqual(dialog.cookie_header, 'token=renewed')
            self.assertEqual(dialog.login, credentials)
            self.assertEqual(dialog.note, '新备注')

    def test_relogin_cancel_keeps_saved_accounts_and_network_errors_do_not_reauthenticate(self):
        for active in (False, True):
            with self.subTest(active=active):
                self.save(1, login=LoginCredentials('person@example.test', 'remembered password'))
                current = self.frame.api if active else self.save(2)
                original = self.frame.account_state
                dialog = AccountManagerDialog(self.frame)
                try:
                    dialog.list.Select(0)
                    with patch('login_dialog.run_dialog_task') as task, \
                            patch.object(self.frame, '_show_account_login', return_value=False) as recover, \
                            patch.object(dialog, 'EndModal') as end, patch('login_dialog.message_box'):
                        dialog.on_login(None)
                        task.call_args.args[3](requests.Timeout())
                        recover.assert_not_called()
                        self.assertEqual(self.frame.account_state, original)
                        dialog.on_login(None)
                        task.call_args.args[3](ApiError('需要登录'))
                        recover.assert_called_once_with(dialog, saved=original.get(1))
                        end.assert_not_called()
                        self.assertFalse(dialog._busy)
                        self.assertEqual(self.frame.account_state.accounts, original.accounts)
                        self.assertEqual(self.frame.account_state.active_user_id, None if active else 2)
                        if not active:
                            self.assertIs(self.frame.api, current)
                finally:
                    dialog.Destroy()

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
            with patch("login_dialog.message_box") as error:
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

    def test_cookie_login_waiting_focus_says_logging_in_not_cancel(self):
        if self._run_native_in_child():
            return
        dialog = CookieLoginDialog(self.frame)
        self.addCleanup(dialog.Destroy)
        dialog.cookie_box.SetValue("token=example")
        observed, errors = [], []

        def inspect():
            try:
                focused = wx.Window.FindFocus()
                observed.append((focused is dialog.ok_button,
                                 focused.GetLabelText() if isinstance(focused, wx.Button) else ""))
            except Exception as exc:
                errors.append(exc)
            finally:
                dialog.EndModal(wx.ID_CANCEL)

        def begin():
            try:
                dialog.ok_button.SetFocus()
                dialog.on_confirm(None)
                wx.CallLater(20, inspect)
            except Exception as exc:
                errors.append(exc)
                dialog.EndModal(wx.ID_CANCEL)

        with patch("login_dialog.run_dialog_task") as task:
            timer = wx.CallLater(20, begin)
            try:
                dialog.ShowModal()
            finally:
                timer.Stop()
            task.assert_called_once()
        self.assertEqual(errors, [])
        self.assertEqual(observed, [(True, "正在登录")])
        self.assertTrue(dialog.cancel_button.IsEnabled())

    def test_cookie_login_blocks_duplicate_submit_and_restores_inputs_after_failure(self):
        dialog = CookieLoginDialog(self.frame)
        self.addCleanup(dialog.Destroy)
        dialog.cookie_box.SetValue("token=example")
        with patch("login_dialog.run_dialog_task") as task, patch("login_dialog.message_box"):
            dialog.on_confirm(None)
            dialog.on_confirm(None)
            task.assert_called_once()
            self.assertTrue(dialog._busy)
            self.assertEqual(dialog.ok_button.GetLabel(), "正在登录")
            self.assertFalse(dialog.cookie_box.IsEnabled())
            self.assertTrue(dialog.cancel_button.IsEnabled())
            task.call_args.args[3](ApiError("需要登录"))
            self.assertFalse(dialog._busy)
            self.assertEqual(dialog.ok_button.GetLabel(), "确定")
            self.assertTrue(dialog.cookie_box.IsEnabled())
            dialog.on_confirm(None)
            self.assertEqual(task.call_count, 2)

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
