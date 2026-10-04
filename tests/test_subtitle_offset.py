import ctypes
import sys
import unittest
from unittest.mock import Mock, patch

import wx

from app import PlaybackFrame, SubtitleJumpDialog, SubtitleOffsetDialog
from app_settings import SubtitleFilterRules
from maoer_api import DanmakuItem, PlaybackInfo


class SubtitleOffsetTests(unittest.TestCase):
    def setUp(self):
        self.app = wx.GetApp() or wx.App(False)
        self.player = Mock()
        with patch("app.ScreenReaderAnnouncer", side_effect=lambda *a, **k: Mock()):
            self.frame = PlaybackFrame(None, Mock(), self.player, Mock(), Mock(), read_subtitle_default=True)
        self.addCleanup(self.frame.Destroy)
        self.canvas = self.frame.danmaku_canvas
        self.canvas.timer.Stop()

    def output_at(self, seconds):
        self.canvas.position = seconds
        self.canvas._spawn_due_items()

    def spoken(self):
        return [c.args[0] for c in self.frame.screen_reader.announce.call_args_list]

    def edit_offset(self, seconds, result=wx.ID_OK):
        dialog = Mock(offset_seconds=seconds)
        dialog.ShowModal.return_value = result
        with patch("app.SubtitleOffsetDialog", return_value=dialog), patch("app.save_settings") as save:
            self.frame._edit_subtitle_offset()
        save.assert_not_called()
        return dialog

    def test_advance_and_delay_affect_display_and_speech_but_not_comments_or_source_times(self):
        for offset in (-0.5, 0, 0.5):
            with self.subTest(offset=offset):
                self.canvas.reset()
                self.canvas.timer.Stop()
                self.frame.screen_reader.reset_mock()
                self.frame.read_danmaku_enabled = True
                self.canvas.set_subtitle_offset(offset)
                subtitle = DanmakuItem(10, "角色：你好", 4)
                comment = DanmakuItem(10, "普通弹幕", 1)
                self.canvas.set_items([subtitle, comment])
                events = []
                with patch.object(self.canvas, "_spawn_item", side_effect=lambda item: events.append((item, self.canvas.position))):
                    for seconds in sorted({9.4, 10 + offset, 10, 10.6}):
                        self.output_at(seconds)
                self.assertEqual(next(t for item, t in events if item is subtitle), 10 + offset)
                self.assertEqual(next(t for item, t in events if item is comment), 10)
                self.assertEqual(len(self.spoken()), 2)
                self.assertCountEqual(self.spoken(), [subtitle.text, comment.text])
                self.assertEqual(subtitle.time, 10)
                self.assertIs(self.canvas.items[0], subtitle)

    def test_changing_offset_does_not_replay_subtitles_already_emitted(self):
        self.canvas.set_items([DanmakuItem(1, "甲：第一句", 4), DanmakuItem(3, "乙：第二句", 4)])
        self.output_at(1)
        self.canvas.set_subtitle_offset(2)
        self.output_at(3)
        self.assertEqual(self.spoken(), ["甲：第一句"])
        self.output_at(5)
        self.assertEqual(self.spoken(), ["甲：第一句", "乙：第二句"])
        self.canvas.seek(-2)
        self.output_at(3)
        self.assertEqual(self.spoken()[-1], "甲：第一句")

    def test_changing_offset_does_not_replay_or_remove_ordinary_comments(self):
        self.frame.read_danmaku_enabled = True
        self.canvas.set_items([DanmakuItem(1, "普通弹幕", 1), DanmakuItem(4, "字幕", 4)])
        self.output_at(1)
        sprites = list(self.canvas.active)
        self.canvas.set_subtitle_offset(2)
        self.assertEqual(self.canvas.active, sprites)
        self.output_at(1)
        self.assertEqual(self.spoken(), ["普通弹幕"])

    def test_offset_menu_is_only_present_with_subtitle_reading_enabled(self):
        for enabled in (False, True):
            self.frame.read_subtitle_enabled = enabled
            labels = []
            def popup(menu, point):
                labels.extend(i.GetItemLabelText() for i in menu.GetMenuItems() if not i.IsSeparator())
                return wx.ID_NONE
            with patch.object(self.frame, "GetPopupMenuSelectionFromUser", side_effect=popup):
                self.frame._show_playback_menu(wx.DefaultPosition)
            self.assertEqual("字幕时间偏移…" in labels, enabled)
        self.frame.read_subtitle_enabled = False
        with patch("app.SubtitleOffsetDialog") as dialog:
            self.frame._edit_subtitle_offset()
        dialog.assert_not_called()

    def offset_shortcut_event(self, key=ord("J")):
        event = Mock()
        event.GetKeyCode.return_value = key
        event.ControlDown.return_value = True
        event.ShiftDown.return_value = False
        event.AltDown.return_value = False
        return event

    def test_ctrl_j_opens_local_offset_without_toggling_reading_or_filter(self):
        for key in (ord("J"), ord("j"), wx.WXK_CONTROL_J):
            with self.subTest(key=key):
                self.frame.subtitle_filter_enabled = True
                dialog = Mock(offset_seconds=-0.5)
                dialog.ShowModal.return_value = wx.ID_OK
                event = self.offset_shortcut_event(key)
                previous_offset = self.frame.subtitle_offset_seconds
                with patch("app.SubtitleOffsetDialog", return_value=dialog) as create_dialog, \
                        patch("app.wx.Window.FindFocus", return_value=self.canvas), \
                        patch("app.save_settings") as save:
                    self.frame.on_char_hook(event)
                create_dialog.assert_called_once_with(self.frame, previous_offset)
                dialog.ShowModal.assert_called_once()
                dialog.Destroy.assert_called_once()
                self.assertEqual(self.frame.subtitle_offset_seconds, -0.5)
                self.assertEqual(self.canvas.subtitle_offset_seconds, -0.5)
                self.assertTrue(self.frame.read_subtitle_enabled)
                self.assertTrue(self.frame.subtitle_filter_enabled)
                self.frame.status_reader.announce.assert_not_called()
                self.frame.screen_reader.announce.assert_not_called()
                save.assert_not_called()
                event.Skip.assert_not_called()

    def test_ctrl_j_is_silent_and_inactive_with_subtitle_reading_off(self):
        self.frame.read_subtitle_enabled = False
        for key in (ord("J"), wx.WXK_CONTROL_J):
            event = self.offset_shortcut_event(key)
            with patch("app.SubtitleOffsetDialog") as dialog, \
                    patch("app.wx.Window.FindFocus", return_value=self.canvas):
                self.frame.on_char_hook(event)
            dialog.assert_not_called()
            self.assertFalse(self.frame.read_subtitle_enabled)
            self.assertFalse(self.frame.subtitle_filter_enabled)
            self.frame.status_reader.announce.assert_not_called()
            self.frame.screen_reader.announce.assert_not_called()
            event.Skip.assert_not_called()

    def test_ctrl_j_does_not_open_offset_from_child_dialog(self):
        child_dialog = wx.Dialog(self.frame)
        self.addCleanup(child_dialog.Destroy)
        editor = wx.TextCtrl(child_dialog)
        event = self.offset_shortcut_event()
        with patch("app.SubtitleOffsetDialog") as dialog, \
                patch("app.wx.Window.FindFocus", return_value=editor):
            self.frame.on_char_hook(event)
        dialog.assert_not_called()
        event.Skip.assert_called_once()
        self.assertTrue(self.frame.read_subtitle_enabled)
        self.assertFalse(self.frame.subtitle_filter_enabled)

    def test_existing_f_and_ctrl_f_shortcuts_still_toggle_their_original_features(self):
        with patch("app.SubtitleOffsetDialog") as dialog:
            event = self.offset_shortcut_event(ord("F"))
            event.ShiftDown.return_value = False
            self.frame.on_char_hook(event)
            self.assertTrue(self.frame.subtitle_filter_enabled)
            self.assertTrue(self.frame.read_subtitle_enabled)
            event.ControlDown.return_value = False
            self.frame.on_char_hook(event)
            self.assertFalse(self.frame.read_subtitle_enabled)
            self.assertFalse(self.frame.subtitle_filter_enabled)
        dialog.assert_not_called()

    def test_old_ctrl_shift_f_does_not_open_offset_or_change_f_toggles(self):
        for key in (ord("F"), wx.WXK_CONTROL_F):
            event = self.offset_shortcut_event(key)
            event.ShiftDown.return_value = True
            with patch("app.SubtitleOffsetDialog") as dialog:
                self.frame.on_char_hook(event)
            dialog.assert_not_called()
            self.assertTrue(self.frame.read_subtitle_enabled)
            self.assertFalse(self.frame.subtitle_filter_enabled)
            self.frame.status_reader.announce.assert_not_called()
            event.Skip.assert_called_once()

    def test_plain_j_still_jumps_instead_of_opening_offset(self):
        event = self.offset_shortcut_event()
        event.ControlDown.return_value = False
        with patch.object(self.frame, "_prompt_jump_to_time") as jump, \
                patch("app.SubtitleOffsetDialog") as dialog:
            self.frame.on_char_hook(event)
        jump.assert_called_once()
        dialog.assert_not_called()

    def test_local_offset_survives_tracks_but_not_a_new_window(self):
        self.edit_offset(-0.5)
        self.assertEqual(self.frame.subtitle_offset_seconds, -0.5)
        with patch.object(self.frame, "_load_danmaku"), patch("app.wx.CallLater"), patch("app.wx.CallAfter"):
            for sound_id in (1, 2, 1, 1):
                self.frame.play(PlaybackInfo(sound_id, "测试", "fixture"))
                self.assertEqual(self.frame.subtitle_offset_seconds, -0.5)
                self.assertEqual(self.canvas.subtitle_offset_seconds, -0.5)
                self.canvas.timer.Stop()
        with patch("app.ScreenReaderAnnouncer", side_effect=lambda *a, **k: Mock()):
            fresh = PlaybackFrame(None, Mock(), Mock(), Mock(), Mock(), subtitle_offset_seconds=0.7)
        try:
            self.assertEqual(fresh.subtitle_offset_seconds, 0.7)
            self.assertEqual(fresh.danmaku_canvas.subtitle_offset_seconds, 0.7)
        finally:
            fresh.Destroy()

    def test_cancel_and_f_toggle_do_not_discard_local_offset(self):
        self.edit_offset(0.5)
        self.edit_offset(-0.5, wx.ID_CANCEL)
        self.frame._toggle_subtitle_reader()
        self.frame._toggle_subtitle_reader()
        self.assertEqual(self.frame.subtitle_offset_seconds, 0.5)
        self.assertEqual(self.canvas.subtitle_offset_seconds, 0.5)

    def test_editing_offset_does_not_seek_or_unpause_audio(self):
        self.canvas.position = 100
        self.canvas.paused = True
        self.edit_offset(-0.5)
        self.assertEqual(self.canvas.position, 100)
        self.assertTrue(self.canvas.paused)
        self.player.seek.assert_not_called()
        self.player.seek_to.assert_not_called()
        self.player.toggle_pause.assert_not_called()

    def test_jump_list_keeps_original_timestamp_and_fraction(self):
        self.canvas.set_subtitle_offset(5)
        row = DanmakuItem(63.65, "甲：你好", 4)
        self.canvas.set_items([row])
        dialog = SubtitleJumpDialog(self.frame, self.canvas.items, 60)
        try:
            self.assertEqual(dialog.selected_seconds(), 63.65)
            self.assertIn("1分03.65秒", dialog.subtitle_list.GetString(0))
        finally:
            dialog.Destroy()

    def test_offset_keeps_role_and_os_filter_identity(self):
        self.frame.subtitle_filter_enabled = True
        self.frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="role", os_body=True)
        self.canvas.set_subtitle_offset(0.5)
        rows = [DanmakuItem(i + 1, text, 4) for i, text in enumerate(
            ["甲：第一句", "甲：第二句", "甲：(OS)第一段", "甲：(OS)第二段", "乙：第三句"])]
        self.frame._set_danmaku_items(self.frame.load_generation, rows)
        for i in range(1, 6):
            self.output_at(i + 0.5)
        self.assertEqual(self.spoken(), ["甲", "甲：(OS)", "乙"])

    def test_late_loading_uses_shifted_time_without_outputting_future_subtitle(self):
        self.canvas.set_subtitle_offset(5)
        self.canvas.position = 8
        self.canvas.set_items([DanmakuItem(5, "还没到时间", 4)])
        self.output_at(8)
        self.assertEqual(self.spoken(), [])
        self.output_at(10)
        self.assertEqual(self.spoken(), ["还没到时间"])
        self.canvas.set_subtitle_offset(-5)
        self.canvas.position = 8
        self.canvas.set_items([DanmakuItem(10, "已到时间", 4)])
        self.output_at(8)
        self.output_at(8.1)
        self.assertEqual(self.spoken(), ["还没到时间", "已到时间"])

    def test_advance_before_zero_outputs_at_start_and_seek_replays_with_offset(self):
        self.canvas.set_subtitle_offset(-2)
        self.canvas.set_items([DanmakuItem(1, "开场", 4), DanmakuItem(5, "下一句", 4)])
        self.output_at(0)
        self.output_at(3)
        self.assertEqual(self.spoken(), ["开场", "下一句"])
        self.canvas.sync_position(0, True)
        self.output_at(0)
        self.assertEqual(self.spoken()[-1], "开场")

    def test_dialog_accepts_signed_seconds_with_only_the_requested_short_description(self):
        for is_default in (False, True):
            dialog = SubtitleOffsetDialog(self.frame, 0.0, is_default=is_default)
            try:
                self.assertEqual(dialog.offset.GetName(), "字幕偏移（秒），负数提前，正数延后")
                self.assertEqual(dialog.offset.GetValue(), 0)
                dialog.offset.SetValue(-0.5)
                with patch.object(dialog, "EndModal") as end:
                    dialog._accept(Mock())
                end.assert_called_once_with(wx.ID_OK)
                self.assertEqual(dialog.offset_seconds, -0.5)
                labels = [child.GetLabel() for panel in dialog.GetChildren() for child in panel.GetChildren()
                          if isinstance(child, wx.StaticText)]
                self.assertEqual(labels, ["以秒为单位，负数提前，正数延后"])
            finally:
                dialog.Destroy()

    def test_dialog_rejects_invalid_input_without_closing(self):
        dialog = SubtitleOffsetDialog(self.frame, 0)
        try:
            for value in ("", "abc", "nan", "inf", "-3601", "3601", "1:20", "--0.5", "+-0.5"):
                with patch.object(dialog.offset, "GetTextValue", return_value=value), \
                        patch.object(dialog, "EndModal") as end, patch("app.wx.MessageBox") as message:
                    dialog._accept(Mock())
                end.assert_not_called()
                message.assert_called_once()
        finally:
            dialog.Destroy()

    @unittest.skipUnless(sys.platform == "win32", "Windows screen-reader accessibility")
    def test_native_edit_exposes_name_role_and_value_in_both_dialogs(self):
        import comtypes
        from comtypes.client import GetModule
        from ctypes import wintypes

        accessible_type = GetModule("oleacc.dll").IAccessible
        get_accessible = ctypes.oledll.oleacc.AccessibleObjectFromWindow
        get_accessible.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.POINTER(comtypes.GUID),
                                  ctypes.POINTER(ctypes.POINTER(accessible_type)))
        get_accessible.restype = ctypes.HRESULT
        for is_default in (False, True):
            with self.subTest(is_default=is_default):
                dialog = SubtitleOffsetDialog(self.frame, -0.5, is_default=is_default)
                try:
                    editors = [child for child in dialog.offset.GetChildren() if isinstance(child, wx.TextCtrl)]
                    self.assertEqual(len(editors), 1)
                    accessible = ctypes.POINTER(accessible_type)()
                    get_accessible(editors[0].GetHandle(), 0xFFFFFFFC,
                                   ctypes.byref(accessible_type._iid_), ctypes.byref(accessible))
                    self.assertEqual(accessible.accName[0], "字幕偏移（秒），负数提前，正数延后")
                    self.assertEqual(accessible.accRole[0], 42)  # ROLE_SYSTEM_TEXT
                    self.assertEqual(accessible.accValue[0], "-0.50")
                    dialog.offset.SetValue(1.5)
                    self.assertEqual(accessible.accValue[0], "1.50")
                    self.assertEqual(accessible.accName[0], dialog.offset.GetName())
                finally:
                    dialog.Destroy()

    def test_offset_uses_decimal_seconds_not_j_time_units(self):
        dialog = SubtitleOffsetDialog(self.frame, 0)
        try:
            for text, expected in (("-1", -1), ("1", 1), ("-0.5", -0.5),
                                   ("-0.05", -0.05), ("-0.50", -0.5), ("1.5", 1.5),
                                   ("3.45", 3.45), ("3.70", 3.7), ("+60", 60)):
                with patch.object(dialog.offset, "GetTextValue", return_value=text), \
                        patch.object(dialog, "EndModal") as end:
                    dialog._accept(Mock())
                end.assert_called_once_with(wx.ID_OK)
                self.assertEqual(dialog.offset_seconds, expected)
            self.assertEqual(PlaybackFrame._parse_jump_time("1.5"), 65)
            self.assertEqual(PlaybackFrame._parse_jump_time("3.45"), 225)
        finally:
            dialog.Destroy()

    def test_saved_decimal_seconds_are_preserved_on_reopen(self):
        for seconds in (0, -0.5, 1.5, -3.45, 61, -3600):
            dialog = SubtitleOffsetDialog(self.frame, seconds)
            try:
                self.assertEqual(dialog.offset.GetValue(), seconds)
                with patch.object(dialog, "EndModal"):
                    dialog._accept(Mock())
                self.assertEqual(dialog.offset_seconds, seconds)
            finally:
                dialog.Destroy()
