import ctypes
from ctypes import wintypes
import sys
import unittest
from unittest.mock import Mock, patch

import wx

from app import PlaybackFrame, SubtitleOffsetDialog


@unittest.skipUnless(sys.platform == "win32", "Windows input-method contexts")
class PlaybackImeTests(unittest.TestCase):
    def setUp(self):
        self.app = wx.GetApp() or wx.App(False)
        self.imm = ctypes.WinDLL("imm32")
        self.imm.ImmGetContext.argtypes = (wintypes.HWND,)
        self.imm.ImmGetContext.restype = wintypes.HANDLE
        self.imm.ImmReleaseContext.argtypes = (wintypes.HWND, wintypes.HANDLE)
        self.imm.ImmGetOpenStatus.argtypes = (wintypes.HANDLE,)
        self.imm.ImmGetOpenStatus.restype = wintypes.BOOL

    def context(self, window):
        handle = window.GetHandle()
        context = self.imm.ImmGetContext(handle)
        if context:
            self.imm.ImmReleaseContext(handle, context)
        return context

    def test_only_playback_controls_lose_ime_while_search_and_new_editors_keep_it(self):
        host = wx.Frame(None)
        self.addCleanup(host.Destroy)
        search = wx.TextCtrl(host)
        wx.Yield()
        original_context = self.context(search)
        if not original_context:
            self.skipTest("No input-method context installed on this test machine")
        original_open = self.imm.ImmGetOpenStatus(original_context)
        with patch("app.ScreenReaderAnnouncer", side_effect=lambda *a, **k: Mock()):
            frame = PlaybackFrame(host, Mock(), Mock(), Mock(), Mock())
        try:
            for window in (frame, frame.danmaku_canvas, frame.danmaku_canvas.bitmap_view,
                           frame.live_region, frame.status_live_region):
                with self.subTest(window=type(window).__name__):
                    self.assertIsNone(self.context(window))
            self.assertEqual(self.context(search), original_context)
            self.assertEqual(self.imm.ImmGetOpenStatus(original_context), original_open)
            dialog = SubtitleOffsetDialog(frame, 0)
            try:
                editor = next(c for c in dialog.offset.GetChildren() if isinstance(c, wx.TextCtrl))
                self.assertEqual(self.context(editor), original_context)
            finally:
                dialog.Destroy()
            rules_dialog = wx.Dialog(frame)
            try:
                rules_editor = wx.TextCtrl(rules_dialog)
                self.assertEqual(self.context(rules_editor), original_context)
                rules_editor.SetValue("中文过滤规则")
                self.assertEqual(rules_editor.GetValue(), "中文过滤规则")
            finally:
                rules_dialog.Destroy()
        finally:
            frame.Destroy()
            wx.Yield()
        self.assertEqual(self.context(search), original_context)


if __name__ == "__main__":
    unittest.main()
