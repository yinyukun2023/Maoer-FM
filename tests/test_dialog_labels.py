from __future__ import annotations

import ctypes
from ctypes import wintypes
import ast
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import wx


class DialogLabelTests(unittest.TestCase):
    def run_in_child(self):
        if os.environ.get("MAOER_DIALOG_LABEL_TEST_CHILD") == "1":
            return False
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-m", "unittest",
             f"tests.test_dialog_labels.DialogLabelTests.{self._testMethodName}", "-q"],
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, "MAOER_DIALOG_LABEL_TEST_CHILD": "1", "PYTHONIOENCODING": "utf-8"},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return True

    def inspect_native_message(self, title, action, *, click_label=None):
        """Read and dismiss only this test thread's own native dialog."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumThreadWindows.argtypes = [wintypes.DWORD, callback_type, wintypes.LPARAM]
        user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        class GuiThreadInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GuiThreadInfo)]
        owner_thread = threading.get_native_id()
        labels = []
        handles = {}
        focused = []
        stop = threading.Event()

        def caption(handle):
            buffer = ctypes.create_unicode_buffer(2048)
            user32.GetWindowTextW(handle, buffer, len(buffer))
            return buffer.value

        @callback_type
        def collect(handle, _parameter):
            buffer = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(handle, buffer, len(buffer))
            if buffer.value.lower() == "button":
                label = caption(handle).replace("&", "")
                labels.append(label)
                handles[label] = handle
            return True

        @callback_type
        def find(handle, _parameter):
            if caption(handle) == title:
                user32.EnumChildWindows(handle, collect, 0)
                if labels:
                    info = GuiThreadInfo(cbSize=ctypes.sizeof(GuiThreadInfo))
                    if user32.GetGUIThreadInfo(owner_thread, ctypes.byref(info)):
                        focused.append(caption(info.hwndFocus).replace("&", ""))
                    if click_label in handles:
                        user32.PostMessageW(handles[click_label], 0x00F5, 0, 0)  # BM_CLICK
                    else:
                        user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE
                    stop.set()
            return not stop.is_set()

        def watch():
            deadline = time.monotonic() + 5
            while not stop.is_set() and time.monotonic() < deadline:
                user32.EnumThreadWindows(owner_thread, find, 0)
                stop.wait(0.02)

        worker = threading.Thread(target=watch, daemon=True)
        worker.start()
        try:
            result = action()
        finally:
            stop.set()
            worker.join(timeout=2)
        self.assertTrue(labels, "未找到测试弹窗的原生按钮")
        return labels, focused, result

    def test_cookie_error_native_button_is_chinese(self):
        if self.run_in_child():
            return
        from login_dialog import CookieLoginDialog

        application = wx.GetApp() or wx.App(False)
        with patch("requests.Session.request", side_effect=AssertionError("No network in dialog test")):
            dialog = CookieLoginDialog(None)
            try:
                dialog.cookie_box.SetValue("not a cookie")
                labels, _focus, _result = self.inspect_native_message("登录失败", lambda: dialog.on_confirm(None))
                self.assertEqual(labels, ["确定"])
            finally:
                dialog.Destroy()

    def test_jump_dialog_cancel_buttons_are_chinese(self):
        if self.run_in_child():
            return
        from app import JumpTimeDialog, SubtitleJumpDialog

        application = wx.GetApp() or wx.App(False)
        dialogs = [SubtitleJumpDialog(None, [], 0), JumpTimeDialog(None, "跳转", 90, 0, lambda: [])]
        try:
            for dialog in dialogs:
                with self.subTest(dialog=type(dialog).__name__):
                    self.assertEqual(dialog.FindWindow(wx.ID_OK).GetLabelText(), "跳转")
                    self.assertEqual(dialog.FindWindow(wx.ID_CANCEL).GetLabelText(), "取消")
        finally:
            for dialog in dialogs:
                dialog.Destroy()

    def test_region_choice_buttons_are_chinese(self):
        if self.run_in_child():
            return
        from login_dialog import LoginDialog
        from maoer_api import MaoerApi

        application = wx.GetApp() or wx.App(False)
        dialog = LoginDialog(None, MaoerApi(cookie=""))
        dialog._regions = [("CN", "中国大陆 +86")]
        real_choice = wx.SingleChoiceDialog
        observed = []

        def create_choice(*args, **kwargs):
            choice = real_choice(*args, **kwargs)

            def show():
                observed.append([choice.FindWindow(i).GetLabelText() for i in (wx.ID_OK, wx.ID_CANCEL)])
                return wx.ID_CANCEL

            choice.ShowModal = show
            return choice

        try:
            with patch("login_dialog.wx.SingleChoiceDialog", side_effect=create_choice), \
                    patch("requests.Session.request", side_effect=AssertionError("No network")):
                dialog.on_choose_region(False)
                dialog.on_choose_region(True)
            self.assertEqual(observed, [["确定", "取消"], ["确定", "取消"]])
        finally:
            dialog.Destroy()

    def test_drama_index_buttons_are_chinese(self):
        if self.run_in_child():
            return
        from app import MaoerFrame

        application = wx.GetApp() or wx.App(False)
        parent = wx.Frame(None)
        real_dialog = wx.Dialog
        observed = []

        def create_dialog(*args, **kwargs):
            dialog = real_dialog(*args, **kwargs)

            def show():
                observed.extend(dialog.FindWindow(i).GetLabelText() for i in (wx.ID_OK, wx.ID_CANCEL))
                return wx.ID_CANCEL

            dialog.ShowModal = show
            return dialog

        try:
            with patch("app.wx.Dialog", side_effect=create_dialog):
                MaoerFrame._show_drama_index_dialog(parent, [("类型", [(0, "全部")])])
            self.assertEqual(observed, ["确定", "取消"])
        finally:
            parent.Destroy()

    def test_native_messages_preserve_results_and_safe_default_focus(self):
        if self.run_in_child():
            return
        from ui_dialogs import message_box

        application = wx.GetApp() or wx.App(False)
        for style, labels, click, expected, focus in (
            (wx.OK, ["确定"], "确定", wx.OK, "确定"),
            (wx.OK | wx.CANCEL | wx.CANCEL_DEFAULT, ["确定", "取消"], "取消", wx.CANCEL, "取消"),
            (wx.YES_NO | wx.NO_DEFAULT, ["是(Y)", "否(N)"], "否(N)", wx.NO, "否(N)"),
            (wx.YES_NO | wx.NO_DEFAULT, ["是(Y)", "否(N)"], "是(Y)", wx.YES, "否(N)"),
            (wx.YES_NO | wx.CANCEL | wx.CANCEL_DEFAULT, ["是(Y)", "否(N)", "取消"], "取消", wx.CANCEL, "取消"),
        ):
            with self.subTest(style=style, click=click):
                actual, focused, result = self.inspect_native_message(
                    "中文按钮测试", lambda: message_box("测试内容", "中文按钮测试", style), click_label=click)
                self.assertEqual(actual, labels)
                self.assertEqual(focused, [focus])
                self.assertEqual(result, expected)

    def test_updater_failure_and_cancel_confirmation_are_chinese(self):
        if self.run_in_child():
            return
        from updater import UpdateDownloadDialog, UpdateInfo, _message_box

        application = wx.GetApp() or wx.App(False)
        labels, _focused, _result = self.inspect_native_message(
            "更新失败", lambda: _message_box("测试错误", "更新失败", "error"))
        self.assertEqual(labels, ["确定"])
        dialog = UpdateDownloadDialog(None, UpdateInfo("test", "", []))
        try:
            labels, focused, _result = self.inspect_native_message(
                "取消更新", dialog._confirm_cancel, click_label="否(N)")
            self.assertEqual(labels, ["是(Y)", "否(N)"])
            self.assertEqual(focused, ["否(N)"])
            self.assertFalse(dialog.cancel_requested)
        finally:
            dialog.Destroy()


class DialogFactoryTests(unittest.TestCase):
    def test_message_box_disposes_dialog_and_never_treats_unknown_result_as_yes(self):
        from ui_dialogs import message_box

        with patch("ui_dialogs.message_dialog") as create:
            dialog = create.return_value
            dialog.ShowModal.return_value = wx.ID_NONE
            self.assertEqual(message_box("测试"), wx.CANCEL)
            dialog.Destroy.assert_called_once()
            dialog.Destroy.reset_mock()
            dialog.ShowModal.side_effect = RuntimeError("test error")
            with self.assertRaises(RuntimeError):
                message_box("测试")
            dialog.Destroy.assert_called_once()

    def test_application_message_calls_use_chinese_factory(self):
        repo = Path(__file__).resolve().parent.parent
        for path in repo.glob("*.py"):
            if path.name == "ui_dialogs.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    function = node.func
                    is_raw_wx = (isinstance(function.value, ast.Name) and function.value.id == "wx"
                                 and function.attr in ("MessageDialog", "MessageBox"))
                    self.assertFalse(is_raw_wx, f"{path.name}:{node.lineno} 未使用中文弹窗入口")
                    self.assertNotIn(function.attr, ("MessageBoxA", "MessageBoxW"),
                                     f"{path.name}:{node.lineno} 使用了依赖系统语言的提示框")


if __name__ == "__main__":
    unittest.main()
