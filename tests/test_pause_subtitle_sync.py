"""Pause/resume timing regressions through the real playback window."""
import unittest
from unittest.mock import Mock, patch

import wx

from app import PlaybackFrame
from maoer_api import DanmakuItem, PlaybackInfo


class PauseSubtitleSyncTests(unittest.TestCase):
    def setUp(self):
        self.app = wx.GetApp() or wx.App(False)
        self.clock = 100.0
        self.enterContext(patch('app.time.monotonic', side_effect=lambda: self.clock))
        self.enterContext(patch('app.wx.CallLater'))
        self.enterContext(patch('app.ScreenReaderAnnouncer', side_effect=lambda *a, **k: Mock()))
        self.player = Mock()
        self.frame = PlaybackFrame(None, Mock(), self.player, Mock(), Mock(), read_subtitle_default=True)
        self.addCleanup(self.frame.Destroy)
        self.canvas = self.frame.danmaku_canvas
        self.canvas.timer.Stop()
        self.frame.playback = PlaybackInfo(1, '暂停同步测试', '', duration_ms=1000000)
        self.canvas.position = 10.0
        self.canvas.set_paused(False)

    def space(self, paused):
        self.player.toggle_pause.return_value = paused
        event = Mock()
        event.GetKeyCode.return_value = wx.WXK_SPACE
        event.ControlDown.return_value = False
        event.ShiftDown.return_value = False
        event.AltDown.return_value = False
        self.frame.on_char_hook(event)

    def status(self, position, paused, rate=1):
        self.player.status.side_effect = lambda callback: callback({
            'ok': True, 'position': position, 'paused': paused, 'rate': rate, 'duration': 1000,
        })
        self.frame._sync_playback_status(self.frame.load_generation)

    def spoken(self):
        return [call.args[0] for call in self.frame.screen_reader.announce.call_args_list]

    def test_pause_latency_never_becomes_persistent_subtitle_delay(self):
        for latency in (0, 0.1, 0.25, 0.7, 0.8):
            for rate in (0.5, 1, 2):
                with self.subTest(latency=latency, rate=rate):
                    self.canvas.reset()
                    self.canvas.timer.Stop()
                    self.canvas.position = 10
                    self.canvas.set_paused(False)
                    self.canvas.set_playback_rate(rate)
                    self.frame.screen_reader.reset_mock()
                    self.canvas.set_items([DanmakuItem(13, '甲：恢复后的字幕', 4)])
                    self.space(True)
                    self.clock += latency
                    stopped_at = 10 + latency * rate
                    self.status(stopped_at, True, rate)
                    self.clock += 60
                    self.space(False)
                    self.status(stopped_at, False, rate)
                    self.clock += (13 - stopped_at) / rate + 0.001
                    self.canvas.on_timer(None)
                    self.assertEqual(self.spoken(), ['甲：恢复后的字幕'])
                    self.frame.status_reader.announce.assert_not_called()

    def test_repeated_pauses_do_not_accumulate_clock_drift(self):
        position = 10
        for _ in range(5):
            self.space(True)
            self.clock += 0.15
            position += 0.15
            self.status(position, True)
            self.assertAlmostEqual(self.canvas.position, position)
            self.clock += 600
            self.space(False)
            self.status(position, False)
            self.clock += 1
            self.canvas.on_timer(None)
            position += 1
            self.assertAlmostEqual(self.canvas.position, position)

    def test_resume_from_recovery_hold_anchors_to_actual_media_progress(self):
        self.status(10, True)
        self.clock += 0.6
        self.status(10.6, False)
        self.assertAlmostEqual(self.canvas.position, 10.6)
        self.clock += 0.4
        self.assertAlmostEqual(self.canvas.current_position(), 11)

    def test_small_backward_correction_does_not_repeat_subtitles_or_comments(self):
        self.frame.read_danmaku_enabled = True
        self.canvas.set_items([DanmakuItem(10, '甲：一句字幕', 4), DanmakuItem(10, '一条弹幕', 1)])
        self.canvas.on_timer(None)
        sprites = list(self.canvas.active)
        emitted = set(self.canvas._emitted_ids)
        self.status(9.8, False)
        self.assertAlmostEqual(self.canvas.position, 9.8)
        self.assertEqual(self.canvas.active, sprites)
        self.assertEqual(self.canvas._emitted_ids, emitted)
        self.clock += 0.21
        self.canvas.on_timer(None)
        self.assertCountEqual(self.spoken(), ['甲：一句字幕', '一条弹幕'])

    def test_small_forward_correction_keeps_due_subtitle_and_user_offset(self):
        self.canvas.set_subtitle_offset(0.5)
        item = DanmakuItem(10, '乙：偏移后的字幕', 4)
        self.canvas.set_items([item])
        self.status(10.55, False)
        self.canvas.on_timer(None)
        self.assertEqual(self.spoken(), [item.text])
        self.assertEqual(item.time, 10)
        self.assertEqual(self.canvas.subtitle_offset_seconds, 0.5)

    def jump(self, seconds):
        self.player.status.side_effect = lambda callback: callback({
            'ok': True, 'position': self.canvas.position, 'duration': 1000, 'paused': self.canvas.paused,
        })
        self.player.seek_to.side_effect = lambda target, callback, **kwargs: callback({
            'ok': True, 'position': target, 'paused': False,
        })
        dialog = Mock(seconds=seconds)
        dialog.ShowModal.return_value = wx.ID_OK
        event = Mock()
        event.GetKeyCode.return_value = ord('J')
        event.ControlDown.return_value = event.ShiftDown.return_value = event.AltDown.return_value = False
        with patch('app.JumpTimeDialog', return_value=dialog), patch('app.wx.CallAfter'):
            self.frame.on_char_hook(event)
        self.assertTrue(self.player.seek_to.call_args.kwargs['resume'])

    def test_jumps_playing_or_paused_align_subtitle_clock_and_restart_playback(self):
        for paused in (False, True):
            for target in (0, 5, 20.345):
                with self.subTest(paused=paused, target=target):
                    self.frame.screen_reader.reset_mock()
                    self.canvas.seek(40 - self.canvas.position)
                    self.canvas.set_items([DanmakuItem(target + 0.5, '乙：跳转后的字幕', 4)])
                    self.canvas.set_paused(paused)
                    self.jump(target)
                    self.assertFalse(self.canvas.paused)
                    self.assertAlmostEqual(self.canvas.position, target)
                    self.clock += 0.51
                    self.canvas.on_timer(None)
                    self.assertEqual(self.spoken(), ['乙：跳转后的字幕'])

    def test_explicit_nearby_backward_jump_replays_caption_unlike_clock_correction(self):
        self.canvas.set_items([DanmakuItem(10, '甲：需要重听的字幕', 4)])
        self.canvas.on_timer(None)
        self.assertEqual(self.spoken(), ['甲：需要重听的字幕'])
        self.jump(9.5)
        self.clock += 0.51
        self.canvas.on_timer(None)
        self.assertEqual(self.spoken(), ['甲：需要重听的字幕', '甲：需要重听的字幕'])

    def test_relative_five_second_seek_preserves_pause_and_realigns_subtitles(self):
        for paused in (False, True):
            for delta in (-5, 5):
                with self.subTest(paused=paused, delta=delta):
                    self.canvas.seek(20 - self.canvas.position)
                    self.canvas.set_paused(paused)
                    self.frame._seek_relative(delta)
                    self.player.seek.assert_called_with(delta)
                    self.assertAlmostEqual(self.canvas.position, 20 + delta)
                    self.assertEqual(self.canvas.paused, paused)

    def test_jump_uses_raw_time_and_preserves_offset_in_both_directions(self):
        for offset in (-0.5, 0.5):
            with self.subTest(offset=offset):
                self.frame.screen_reader.reset_mock()
                self.canvas.seek(40 - self.canvas.position)
                self.canvas.set_subtitle_offset(offset)
                self.canvas.set_items([DanmakuItem(21, '乙：带偏移的字幕', 4)])
                self.jump(20)
                self.assertEqual(self.player.seek_to.call_args.args[0], 20)
                self.clock += 1 + offset + 0.001
                self.canvas.on_timer(None)
                self.assertEqual(self.spoken(), ['乙：带偏移的字幕'])
                self.assertEqual(self.canvas.subtitle_offset_seconds, offset)


if __name__ == '__main__':
    unittest.main()
