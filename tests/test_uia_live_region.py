import unittest
from unittest.mock import Mock, patch

from uia_live_region import ScreenReaderAnnouncer, set_native_accessible_name


class ScreenReaderAnnouncerTests(unittest.TestCase):
    def make_announcer(self, tolk_success: bool):
        self.tolk = Mock()
        self.tolk.speak.return_value = tolk_success
        with patch("uia_live_region.TolkBridge", return_value=self.tolk):
            announcer = ScreenReaderAnnouncer(Mock())
        return announcer

    def test_tolk_output_does_not_duplicate_to_uia(self):
        announcer = self.make_announcer(True)
        with patch.object(announcer, "_handle") as handle:
            self.assertTrue(announcer.announce("  第一行\n第二行  "))
        self.tolk.speak.assert_called_once_with("第一行 第二行")
        handle.assert_not_called()

    def test_uia_remains_available_when_tolk_has_no_reader(self):
        announcer = self.make_announcer(False)
        announcer._live_region_ready = True
        objects = {"user32": Mock()}
        with patch.object(announcer, "_handle", return_value=123), \
             patch.object(announcer, "_set_accessible_text") as set_text, \
             patch("uia_live_region._automation_objects", return_value=objects):
            self.assertTrue(announcer.announce("字幕内容"))
        set_text.assert_called_once_with("字幕内容")
        objects["user32"].NotifyWinEvent.assert_called_once()

    def test_tolk_can_still_work_after_uia_failure(self):
        announcer = self.make_announcer(True)
        announcer._failed = True
        self.assertTrue(announcer.announce("弹幕内容"))
        self.tolk.speak.assert_called_once_with("弹幕内容")

    def test_close_stops_tolk_bridge(self):
        announcer = self.make_announcer(False)
        announcer.close()
        self.tolk.close.assert_called_once_with()

    def test_native_only_notifies_reader_without_creating_or_calling_tolk(self):
        with patch("uia_live_region.TolkBridge") as bridge:
            announcer = ScreenReaderAnnouncer(Mock(), native_only=True)
            announcer._live_region_ready = True
            objects = {"user32": Mock()}
            with patch.object(announcer, "_handle", return_value=123), \
                 patch("uia_live_region._automation_objects", return_value=objects):
                self.assertTrue(announcer.announce("输入超出范围"))
            objects["user32"].NotifyWinEvent.assert_called_once()
            announcer.close()
            bridge.assert_not_called()

    def test_native_failure_never_falls_back_to_subtitle_speech(self):
        with patch("uia_live_region.TolkBridge") as bridge:
            announcer = ScreenReaderAnnouncer(Mock(), native_only=True)
            with patch.object(announcer, "_handle", side_effect=RuntimeError("unavailable")):
                self.assertFalse(announcer.announce("输入超出范围"))
            bridge.assert_not_called()


class NativeAccessibleNameTests(unittest.TestCase):
    def test_labels_actual_handle_and_cleans_up_without_speaking(self):
        import ctypes
        import wx

        window = Mock()
        window.GetHandle.return_value = 123
        objects = {key: Mock() for key in ("comtypes", "CLSID_AccPropServices", "IAccPropServices",
                                          "CLSCTX_INPROC_SERVER")}
        objects["Name_Property_GUID"] = ctypes.c_int(0)
        service = objects["comtypes"].CoCreateInstance.return_value
        with patch("uia_live_region._automation_objects", return_value=objects), \
                patch("uia_live_region.TolkBridge") as bridge:
            self.assertTrue(set_native_accessible_name(window, "字幕偏移（秒）"))
            window.SetName.assert_called_once_with("字幕偏移（秒）")
            service.SetHwndPropStr.assert_called_once_with(123, 0xFFFFFFFC, 0,
                                                         objects["Name_Property_GUID"], "字幕偏移（秒）")
            event_type, handler = window.Bind.call_args.args
            self.assertIs(event_type, wx.EVT_WINDOW_DESTROY)
            event = Mock()
            event.GetEventObject.return_value = window
            handler(event)
            service.ClearHwndProps.assert_called_once()
            self.assertEqual(service.ClearHwndProps.call_args.args[:3], (123, 0xFFFFFFFC, 0))
            event.Skip.assert_called_once()
            bridge.assert_not_called()

    def test_annotation_failure_does_not_break_the_dialog_or_invoke_speech(self):
        window = Mock()
        window.GetHandle.return_value = 123
        with patch("uia_live_region._automation_objects", side_effect=RuntimeError("unavailable")), \
                patch("uia_live_region.TolkBridge") as bridge:
            self.assertFalse(set_native_accessible_name(window, "字幕偏移（秒）"))
            window.SetName.assert_called_once_with("字幕偏移（秒）")
            window.Bind.assert_not_called()
            bridge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
