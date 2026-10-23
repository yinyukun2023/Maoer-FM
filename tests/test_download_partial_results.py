"""Per-content failures and subtitle-source fallback; no real account or media."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import wx
import requests

from download_dialog import DownloadProgressDialog, DownloadRequest
from downloads import download_subtitle
from maoer_api import ApiError, MaoerApi, MediaItem, PlaybackInfo
from tests.test_download_subtitles import PAYLOAD, EXPECTED_SRT, response_for


class PartialDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = wx.GetApp() or wx.App(False)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.item = MediaItem('sound', 1, '番外')
        self.enterContext(patch('download_dialog.message_box', return_value=wx.OK))
        self.enterContext(patch('download_dialog.play_download_completed_sound'))
        self.enterContext(patch('download_dialog.play_download_failed_sound'))

    def window(self):
        window = DownloadProgressDialog(None, DownloadRequest('作品', [self.item], self.folder, '',
                                        download_audio=True, download_subtitles=True))
        self.addCleanup(window.Destroy)
        self.addCleanup(window.timer.Stop)
        window.running = window._started = True
        return window

    def run_jobs(self, window, audio_error=None, subtitle_result=None, subtitle_error=None):
        def audio(*args, **kwargs):
            if audio_error:
                raise audio_error
            path = self.folder / '番外.mp3'
            path.write_bytes(b'audio')
            return path
        def subtitles(*args, **kwargs):
            if subtitle_error:
                raise subtitle_error
            return subtitle_result() if callable(subtitle_result) else subtitle_result
        callbacks = []
        with patch('download_dialog.MaoerApi'), \
                patch('download_dialog.download_audio', side_effect=audio), \
                patch('download_dialog.download_subtitle', side_effect=subtitles) as subtitle, \
                patch('download_dialog.wx.CallAfter', side_effect=lambda f, *a, **kw: callbacks.append((f, a, kw))):
            window._run()
        for callback, args, kwargs in callbacks:
            callback(*args, **kwargs)
        return subtitle

    def test_missing_subtitle_is_a_visible_subtitle_failure_after_audio_succeeds(self):
        window = self.window()
        self.run_jobs(window)
        failed = getattr(window, 'subtitle_failed_list', window.failed_list)
        self.assertEqual(failed.GetItemCount(), 1, '音频成功但无字幕时，字幕失败项被遗漏')
        self.assertIn('暂无', failed.GetItemText(0, 2))
        self.assertEqual((self.folder / '番外.mp3').read_bytes(), b'audio')

    def test_audio_failure_does_not_prevent_attempting_the_subtitle(self):
        window = self.window()
        subtitle = self.folder / '番外.srt'
        subtitle.write_text('subtitle', encoding='utf-8')
        download = self.run_jobs(window, audio_error=ApiError('音频失败'), subtitle_result=subtitle)
        download.assert_called_once()

    def test_xml_caption_available_without_independent_json_is_downloadable(self):
        api = MaoerApi(cookie='')
        self.addCleanup(api.session.close)
        api.playback_info = Mock(return_value=PlaybackInfo(1, '番外', '', duration_ms=7000))
        api._get_text = Mock(return_value='<i><d p="1.25,4,25,0">角色：你好</d>'
                                        '<d p="2,1,25,0">普通观众弹幕不应导出</d></i>')
        saved = download_subtitle(api, self.item, self.folder, threading.Event(), Mock())
        self.assertIsNotNone(saved, '官网弹幕式字幕存在，但下载误报无字幕')
        text = saved.read_text(encoding='utf-8')
        self.assertIn('角色：你好', text)
        self.assertNotIn('普通观众弹幕', text)

    def test_two_failures_put_the_same_episode_in_both_named_lists(self):
        window = self.window()
        self.run_jobs(window, audio_error=ApiError('音频失败'), subtitle_error=ApiError('字幕失败'))
        for control, title, reason in ((window.audio_failed_list, '音频下载失败', '音频失败'),
                                        (window.subtitle_failed_list, '字幕下载失败', '字幕失败')):
            self.assertTrue(control.IsShown())
            self.assertEqual(control.GetName(), title)
            self.assertEqual(control.GetItemCount(), 1)
            self.assertEqual(control.GetItemData(0), 0)
            self.assertEqual(control.GetItemText(0, 2), reason)

    def test_audio_failure_and_successful_subtitle_are_classified_independently(self):
        window = self.window()
        path = self.folder / '番外.srt'
        path.write_bytes(EXPECTED_SRT)
        self.run_jobs(window, audio_error=ApiError('音频网络失败'), subtitle_result=path)
        self.assertEqual(window.audio_failed_list.GetItemCount(), 1)
        self.assertEqual(window.subtitle_failed_list.GetItemCount(), 0)
        self.assertEqual(window.completed_list.GetItemCount(), 1)
        self.assertIn('字幕完成', window.completed_list.GetItemText(0, 1))
        self.assertIn('音频失败', window.completed_list.GetItemText(0, 1))

    def test_retry_from_either_failure_list_retries_both_requested_parts(self):
        for kind in ('audio', 'subtitle'):
            with self.subTest(kind=kind):
                window = self.window()
                self.run_jobs(window, audio_error=ApiError('失败'), subtitle_error=ApiError('失败'))
                control = window.audio_failed_list if kind == 'audio' else window.subtitle_failed_list
                def choose(menu, position):
                    self.assertTrue(menu.GetMenuItems()[0].IsEnabled())
                    return menu.GetMenuItems()[0].GetId()
                with patch.object(control, 'GetPopupMenuSelectionFromUser', side_effect=choose), \
                        patch.object(window, 'start') as start:
                    window._show_failed_menu(control=control)
                start.assert_called_once()
                self.assertEqual(window.tasks[0].state, 'pending')
                self.assertEqual(window.tasks[0].parts, {})
                def saved_subtitle():
                    path = self.folder / '番外.srt'
                    path.write_bytes(EXPECTED_SRT)
                    return path
                window.running = True
                self.run_jobs(window, subtitle_result=saved_subtitle)
                self.assertEqual(set(window.tasks[0].parts), {'audio', 'subtitle'})
                self.assertTrue(all(part.state == 'complete' for part in window.tasks[0].parts.values()))
                self.assertEqual(window.audio_failed_list.GetItemCount(), 0)
                self.assertEqual(window.subtitle_failed_list.GetItemCount(), 0)
                self.assertEqual(window.tasks[0].state, 'complete')

    def test_both_failure_lists_support_menu_key_and_shift_f10(self):
        window = self.window()
        for control in (window.audio_failed_list, window.subtitle_failed_list):
            for key in (wx.WXK_MENU, wx.WXK_F10):
                event = Mock()
                event.GetKeyCode.return_value = key
                event.GetEventObject.return_value = control
                event.ShiftDown.return_value = True
                with patch.object(window, '_show_failed_menu') as menu:
                    window._on_task_key(event)
                menu.assert_called_once()
                event.Skip.assert_not_called()

    def test_missing_or_unusable_json_falls_back_to_xml_without_mixing_sources(self):
        for response in (response_for(b'[]'), response_for(b'bad json'), response_for(status=404),
                         requests.Timeout('network')):
            with self.subTest(source=type(response).__name__):
                api = MaoerApi(cookie='')
                self.addCleanup(api.session.close)
                api.playback_info = Mock(return_value=PlaybackInfo(1, '番外', '',
                    subtitle_url='https://cdn.invalid/subtitle.json'))
                api._get_text = Mock(return_value='<i><d p="1,4,25,0">只有XML的字幕</d>'
                                                 '<d p="2,1,25,0">普通弹幕</d></i>')
                options = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch('requests.Session.get', **options):
                    saved = download_subtitle(api, self.item, self.folder, threading.Event(), Mock())
                self.assertIn('只有XML的字幕', saved.read_text(encoding='utf-8'))
                self.assertNotIn('普通弹幕', saved.read_text(encoding='utf-8'))
                api._get_text.assert_called_once_with('/sound/getdm', {'soundid': 1})
                saved.unlink()

    def test_valid_json_does_not_append_xml_captions(self):
        api = MaoerApi(cookie='')
        self.addCleanup(api.session.close)
        api.playback_info = Mock(return_value=PlaybackInfo(1, '番外', '',
                                   subtitle_url='https://cdn.invalid/subtitle.json'))
        api._get_text = Mock(side_effect=AssertionError('JSON可用时不请求XML'))
        with patch('requests.Session.get', return_value=response_for(PAYLOAD)):
            saved = download_subtitle(api, self.item, self.folder, threading.Event(), Mock())
        self.assertEqual(saved.read_bytes(), EXPECTED_SRT)
        api._get_text.assert_not_called()

    def test_xml_network_error_is_not_reported_as_no_subtitles(self):
        api = MaoerApi(cookie='')
        self.addCleanup(api.session.close)
        api.playback_info = Mock(return_value=PlaybackInfo(1, '番外', ''))
        api._get_text = Mock(side_effect=requests.Timeout('network'))
        with self.assertRaisesRegex(ApiError, '字幕获取失败'):
            download_subtitle(api, self.item, self.folder, threading.Event(), Mock())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_duration_mismatch_does_not_switch_away_from_usable_json(self):
        api = MaoerApi(cookie='')
        self.addCleanup(api.session.close)
        api.playback_info = Mock(return_value=PlaybackInfo(1, '番外', '', duration_ms=500,
                                   subtitle_url='https://cdn.invalid/subtitle.json'))
        api._get_text = Mock(side_effect=AssertionError('可用JSON不因时长不符换源'))
        with patch('requests.Session.get', return_value=response_for(PAYLOAD)):
            with self.assertRaisesRegex(ApiError, '字幕时间超出音频总时长'):
                download_subtitle(api, self.item, self.folder, threading.Event(), Mock())
        api._get_text.assert_not_called()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_cancel_after_audio_success_keeps_it_without_making_a_subtitle_failure(self):
        window = self.window()
        def cancel_subtitle():
            window.cancel.set()
            from downloads import DownloadCancelled
            raise DownloadCancelled()
        self.run_jobs(window, subtitle_result=cancel_subtitle)
        self.assertEqual(window.tasks[0].state, 'cancelled')
        self.assertEqual(window.tasks[0].parts['audio'].state, 'complete')
        self.assertEqual(window.tasks[0].parts['subtitle'].state, 'cancelled')
        self.assertEqual(window.audio_failed_list.GetItemCount(), 0)
        self.assertEqual(window.subtitle_failed_list.GetItemCount(), 0)
        self.assertEqual((self.folder / '番外.mp3').read_bytes(), b'audio')


if __name__ == '__main__':
    unittest.main()
