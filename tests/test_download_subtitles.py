"""SRT conversion/download, content defaults and queue ownership; no live account."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests
import wx

from app_settings import AppSettings, DownloadSettings, load_settings, save_settings
from download_dialog import DownloadDialog, DownloadProgressDialog, DownloadRequest
from download_settings import DownloadSettingsDialog
from downloads import DownloadCancelled, DownloadControl, DownloadSelection, download_subtitle, subtitles_to_srt
from maoer_api import ApiError, MaoerApi, MediaItem, PlaybackInfo, PurchaseRequired


PAYLOAD = ('[\n {"start_time":1234,"role":"角色（OS）","content":"原文：你好！",'
           '"end_time":5678,"extra":"保留"}\n]\n').encode('utf-8')
EXPECTED_SRT = '1\n00:00:01,234 --> 00:00:05,678\n角色（OS）：原文：你好！\n\n'.encode('utf-8')


def response_for(payload=PAYLOAD, status=200, length=None):
    response = MagicMock(status_code=status, headers={
        'Content-Length': str(len(payload) if length is None else length)})
    response.__enter__.return_value = response
    response.iter_content.return_value = [payload]
    return response


class SrtConversionTests(unittest.TestCase):
    def test_preserves_roles_os_and_multiline_body_in_stable_time_order(self):
        rows = [
            {'start_time': 6500, 'end_time': 8500, 'content': '【场景说明】\n\n第二行'},
            {'start_time': 1234, 'end_time': 5678, 'role': '角色（OS）', 'content': '原文：你好！'},
        ]
        self.assertEqual(subtitles_to_srt(rows), EXPECTED_SRT.decode() +
            '2\n00:00:06,500 --> 00:00:08,500\n【场景说明】\n第二行\n\n')

    def test_missing_end_uses_next_start_with_five_second_cap_and_media_end(self):
        rows = [{'start_time': start, 'content': str(start)} for start in (1000, 3000, 20000)]
        self.assertEqual(subtitles_to_srt(rows, 22000),
            '1\n00:00:01,000 --> 00:00:03,000\n1000\n\n'
            '2\n00:00:03,000 --> 00:00:08,000\n3000\n\n'
            '3\n00:00:20,000 --> 00:00:22,000\n20000\n\n')

    def test_same_time_speakers_keep_separate_cues_and_positive_durations(self):
        rows = [{'start_time': 1000, 'role': role, 'content': '你好'} for role in ('甲', '乙')]
        rows.append({'start_time': 2500, 'content': '旁白'})
        result = subtitles_to_srt(rows)
        self.assertEqual(result.count('00:00:01,000 --> 00:00:02,500'), 2)
        self.assertIn('甲：你好', result)
        self.assertIn('乙：你好', result)
        self.assertIn('00:00:02,500 --> 00:00:07,500', result)

    def test_valid_original_end_is_not_shortened_to_next_caption(self):
        rows = [{'start_time': 0, 'end_time': 10000, 'content': '长字幕'},
                {'start_time': 2000, 'end_time': 4000, 'content': '重叠字幕'}]
        result = subtitles_to_srt(rows)
        self.assertIn('00:00:00,000 --> 00:00:10,000', result)
        self.assertIn('00:00:02,000 --> 00:00:04,000', result)

    def test_invalid_rows_are_skipped_and_invalid_end_times_get_positive_fallback(self):
        rows = [None, {}, {'start_time': True, 'content': '无效'},
                {'start_time': 'nan', 'content': '无效'}, {'start_time': -1, 'content': '无效'},
                {'start_time': 1000, 'end_time': False, 'content': '第一句'},
                {'start_time': 2000, 'end_time': 1000, 'content': '第二句'},
                {'start_time': 3000, 'end_time': 'inf', 'content': '第三句'}]
        result = subtitles_to_srt(rows, 3500)
        self.assertNotIn('无效', result)
        self.assertIn('1\n00:00:01,000 --> 00:00:02,000', result)
        self.assertIn('2\n00:00:02,000 --> 00:00:03,000', result)
        self.assertIn('3\n00:00:03,000 --> 00:00:03,500', result)

    def test_timecodes_carry_across_minutes_hours_and_round_to_milliseconds(self):
        result = subtitles_to_srt([{'start_time': 3599999.6, 'end_time': 3601000.4, 'content': '跨小时'}])
        self.assertIn('01:00:00,000 --> 01:00:01,000', result)

    def test_conversion_honours_cancellation_and_omits_rows_past_known_audio_end(self):
        rows = [{'start_time': 1000, 'content': '有内容'}, {'start_time': 5000, 'content': '超出音频'}]
        self.assertNotIn('超出音频', subtitles_to_srt(rows, 3000))
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(DownloadCancelled):
            subtitles_to_srt(rows, cancel=cancelled)


class SubtitleFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.api = MaoerApi(cookie='session=fake-test-cookie')
        self.addCleanup(self.api.session.close)
        self.item = MediaItem('sound', 1, '第一集')
        self.api.playback_info = Mock(return_value=PlaybackInfo(
            1, '不使用接口长标题', '', subtitle_url='https://cdn.invalid/1.json'))
        self.api._get_text = Mock(return_value='<i/>')
        self.http = self.enterContext(patch('requests.Session.get', autospec=True,
                                           return_value=response_for()))

    def fetch(self, **kwargs):
        return download_subtitle(self.api, self.item, self.folder, threading.Event(), Mock(), **kwargs)

    def test_utf8_srt_uses_original_text_and_names_without_forwarding_cookie(self):
        saved = self.fetch(number_width=3, filename_prefix='007 ')
        self.assertEqual(saved.name, '007 第001集.srt')
        self.assertEqual(saved.read_bytes(), EXPECTED_SRT)
        self.assertEqual(list(self.folder.iterdir()), [saved])
        self.api.playback_info.assert_called_once_with(self.item)
        session, url = self.http.call_args.args
        self.assertEqual(url, 'https://cdn.invalid/1.json')
        self.assertNotIn('Cookie', session.headers)
        self.assertNotIn('cookies', self.http.call_args.kwargs)

    def test_permission_failure_cannot_download_captions(self):
        self.api.playback_info.side_effect = PurchaseRequired('尚未购买')
        with self.assertRaises(PurchaseRequired):
            self.fetch()
        self.http.assert_not_called()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_prechecked_playback_info_does_not_request_metadata_again(self):
        playback = self.api.playback_info.return_value
        self.api.playback_info.side_effect = AssertionError('Audio already checked playback permissions')
        saved = self.fetch(playback=playback)
        self.assertEqual(saved.read_bytes(), EXPECTED_SRT)
        self.api.playback_info.assert_not_called()

    def test_missing_url_or_empty_track_returns_no_file(self):
        self.api.playback_info.return_value.subtitle_url = ''
        self.assertIsNone(self.fetch())
        self.http.assert_not_called()
        self.api.playback_info.return_value.subtitle_url = 'https://cdn.invalid/empty.json'
        self.http.return_value = response_for(b'[]')
        self.assertIsNone(self.fetch())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_bad_json_http_truncation_and_oversize_leave_no_partial_file(self):
        cases = [response_for(b'<html>failure</html>'), response_for(b'{"error":true}'),
                 response_for(status=500), response_for(length=len(PAYLOAD) + 5),
                 response_for(length=17 * 1024 * 1024), response_for(b'')]
        for response in cases:
            with self.subTest(response=response):
                self.http.return_value = response
                with self.assertRaises(ApiError):
                    self.fetch()
                self.assertEqual(list(self.folder.iterdir()), [])

    def test_existing_srt_is_never_overwritten(self):
        original = self.folder / '第一集.srt'
        original.write_bytes(b'user original')
        with self.assertRaises(FileExistsError):
            self.fetch()
        self.http.assert_not_called()
        self.assertEqual(original.read_bytes(), b'user original')

    def test_atomic_publish_preserves_file_created_during_transfer(self):
        original = self.folder / '第一集.srt'
        def progress(stage, percent):
            if stage == '正在下载字幕':
                original.write_bytes(b'created by someone else')
        with self.assertRaises(FileExistsError):
            download_subtitle(self.api, self.item, self.folder, threading.Event(), progress)
        self.assertEqual(original.read_bytes(), b'created by someone else')
        self.assertEqual(list(self.folder.iterdir()), [original])

    def test_network_failure_removes_partial_file(self):
        self.http.side_effect = requests.Timeout('signed URL must not become a filename')
        with self.assertRaises(requests.Timeout):
            self.fetch()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_pause_then_resume_or_cancel_keeps_transfer_cooperative(self):
        for cancel_transfer in (False, True):
            with self.subTest(cancel=cancel_transfer):
                control = DownloadControl()
                paused = threading.Event()
                outcome = {}
                self.http.return_value = response_for()
                self.http.return_value.iter_content.return_value = [PAYLOAD[:10], PAYLOAD[10:]]
                def progress(stage, percent):
                    if stage == '正在下载字幕' and not paused.is_set():
                        control.pause()
                        paused.set()
                def work():
                    try:
                        outcome['file'] = download_subtitle(self.api, self.item, self.folder, control, progress)
                    except Exception as error:
                        outcome['error'] = error
                worker = threading.Thread(target=work, daemon=True)
                worker.start()
                try:
                    self.assertTrue(paused.wait(2))
                    self.assertTrue(worker.is_alive())
                    self.assertFalse((self.folder / '第一集.srt').exists())
                    control.set() if cancel_transfer else control.resume()
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                    if cancel_transfer:
                        self.assertIsInstance(outcome['error'], DownloadCancelled)
                        self.assertEqual(list(self.folder.iterdir()), [])
                    else:
                        saved = outcome['file']
                        self.assertEqual(saved.read_bytes(), EXPECTED_SRT)
                        saved.unlink()
                finally:
                    control.set()
                    worker.join(3)


class SubtitleSelectionAndQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = wx.GetApp() or wx.App(False)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / '下载'
        self.folder.mkdir()
        self.owner = wx.Frame(None)
        self.addCleanup(self.owner.Destroy)
        self.items = [MediaItem('sound', 1, '第一集'), MediaItem('sound', 2, '第二集')]
        self.messages = self.enterContext(patch('download_dialog.message_box', return_value=wx.OK))
        self.enterContext(patch('download_dialog.play_download_completed_sound'))
        self.enterContext(patch('download_dialog.play_download_failed_sound'))
        self.enterContext(patch('requests.Session.request', side_effect=AssertionError('No live network')))

    def selector(self, options=None):
        window = DownloadDialog(self.owner, DownloadSelection('作品', '发布者', self.items, [1],
                                  sequence_positions={1: 4, 2: 5}), '', self.folder, options=options)
        self.addCleanup(window.Destroy)
        return window

    def progress(self, *, audio=True, subtitles=True):
        request = DownloadRequest('作品', self.items, self.folder, '', number_width=2,
                                  filename_prefixes={1: '04 ', 2: '05 '},
                                  download_audio=audio, download_subtitles=subtitles)
        window = DownloadProgressDialog(self.owner, request)
        self.addCleanup(window.Destroy)
        self.addCleanup(window.timer.Stop)
        window.running = window._started = True
        return window

    def run_queue(self, window, *, bad_subtitles=False):
        api = Mock()
        api.sound_subtitles.return_value = []
        api.playback_info.side_effect = lambda item: PlaybackInfo(
            item.id, item.title, '', subtitle_url=f'https://cdn.invalid/{item.id}.json')
        def audio(api, item, folder, control, key, progress, **kwargs):
            path = folder / (kwargs['filename_prefix'] + ('第01集' if item.id == 1 else '第02集') + '.mp3')
            if path.exists():
                raise FileExistsError()
            path.write_bytes(b'audio bytes')
            return path
        callbacks = []
        with patch('download_dialog.MaoerApi', return_value=api), \
                patch('download_dialog.download_audio', side_effect=audio) as audio_download, \
                patch('requests.Session.get', autospec=True, side_effect=lambda *a, **kw:
                      response_for(b'bad JSON' if bad_subtitles else PAYLOAD)), \
                patch('download_dialog.wx.CallAfter', side_effect=lambda f, *a, **kw: callbacks.append((f, a, kw))), \
                patch.object(window, '_get_key', side_effect=AssertionError('No media preparation for captions')):
            window._run()
        for callback, args, kwargs in callbacks:
            callback(*args, **kwargs)
        return api, audio_download

    def pump(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            wx.Yield()
            time.sleep(0.01)
        self.assertTrue(condition())

    def test_three_defaults_initialize_independent_checkboxes_and_request(self):
        for mode, expected in (('audio', (True, False)), ('subtitles', (False, True)), ('both', (True, True))):
            with self.subTest(mode=mode):
                options = DownloadSettings(content_mode=mode)
                selector = self.selector(options)
                self.assertEqual(selector.GetTitle(), '下载剧集 — 作品')
                self.assertEqual(selector.subtitles.GetLabel(), '下载剧集字幕')
                self.assertEqual(selector.audio.GetLabel(), '下载音频')
                self.assertEqual((selector.audio.GetValue(), selector.subtitles.GetValue()), expected)
                self.assertTrue(selector.audio.IsEnabled() and selector.subtitles.IsEnabled())
                selector.by_list_order.SetValue(True)
                with patch.object(selector, 'EndModal'):
                    selector._start(None)
                self.assertEqual(selector.request.items, [self.items[1]])
                self.assertEqual((selector.request.download_audio, selector.request.download_subtitles), expected)
                self.assertEqual(selector.request.filename_prefixes, {2: '05 '})
                selector.audio.SetValue(not expected[0])
                self.assertEqual(options.content_mode, mode)

    def test_neither_checkbox_selected_keeps_selector_open(self):
        selector = self.selector()
        selector.audio.SetValue(False)
        with patch.object(selector, 'EndModal') as end:
            selector._start(None)
        end.assert_not_called()
        self.assertIsNone(selector.request)
        self.assertIn('下载剧集字幕', self.messages.call_args.args[0])

    def test_default_combo_persists_all_modes_and_cancel_does_not_change_settings(self):
        profile = self.root / 'profile'
        profile.mkdir()
        with patch('app_settings.app_data_dir', return_value=profile):
            settings = AppSettings(downloads=DownloadSettings(play_sound=False))
            save_settings(settings)
            for index, mode in enumerate(('audio', 'subtitles', 'both')):
                window = DownloadSettingsDialog(self.owner, settings.downloads, self.folder,
                    lambda options: save_settings(replace(settings, downloads=options)))
                self.addCleanup(window.Destroy)
                self.assertEqual(window.content_mode.GetName(), '默认下载内容')
                self.assertEqual(list(window.content_mode.GetItems()), ['音频', '字幕', '音频加字幕'])
                window.content_mode.SetSelection(index)
                self.assertEqual(load_settings(), settings)
                with patch.object(window, 'EndModal'):
                    window._save(None)
                settings = load_settings()
                self.assertEqual(settings.downloads.content_mode, mode)
                self.assertFalse(settings.downloads.play_sound)
                selector = self.selector(settings.downloads)
                self.assertEqual(selector.subtitles.GetValue(), mode != 'audio')

    def test_legacy_or_invalid_content_mode_defaults_to_audio(self):
        with patch('app_settings.app_data_dir', return_value=self.root):
            for value in (None, [], True, 'invalid'):
                (self.root / 'settings.json').write_text(json.dumps({'downloads': {'content_mode': value}}), encoding='utf-8')
                self.assertEqual(load_settings().downloads.content_mode, 'audio')

    def test_subtitle_only_downloads_each_selected_track_without_audio_or_media(self):
        window = self.progress(audio=False)
        api, audio = self.run_queue(window)
        audio.assert_not_called()
        self.assertEqual(api.playback_info.call_count, 2)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ['04 第01集.srt', '05 第02集.srt'])
        self.assertTrue(all(path.read_bytes() == EXPECTED_SRT for path in self.folder.iterdir()))
        self.assertTrue(all(task.state == 'complete' for task in window.tasks))

    def test_audio_only_keeps_the_previous_download_path(self):
        window = self.progress(subtitles=False)
        with patch('download_dialog.download_subtitle') as subtitle:
            api, audio = self.run_queue(window)
        subtitle.assert_not_called()
        self.assertEqual(audio.call_count, 2)
        self.assertTrue(all(path.suffix == '.mp3' for path in self.folder.iterdir()))

    def test_both_files_are_owned_and_clear_preserves_existing_files(self):
        original = self.folder / '04 第01集.mp3'
        original.write_bytes(b'original user audio')
        window = self.progress()
        self.run_queue(window)
        self.assertEqual(len(window._created_files), 3)
        self.assertTrue(all(task.state == 'complete' for task in window.tasks))
        window._clear_requested = True
        window._begin_clear()
        self.pump(lambda: not window._clearing)
        self.assertEqual(list(self.folder.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b'original user audio')

    def test_subtitle_failure_retains_audio_and_retry_fetches_only_missing_files(self):
        window = self.progress()
        self.run_queue(window, bad_subtitles=True)
        self.assertTrue(all(task.state == 'failed' for task in window.tasks))
        audio_before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        self.assertEqual(len(audio_before), 2)
        for task in window.tasks:
            task.state = 'pending'
        window.cancel.retry([0, 1])
        window.running = True
        self.run_queue(window)
        self.assertTrue(all(task.state == 'complete' for task in window.tasks))
        for name, data in audio_before.items():
            self.assertEqual((self.folder / name).read_bytes(), data)
        self.assertEqual(len(list(self.folder.glob('*.srt'))), 2)
        self.assertEqual(list(self.folder.glob('*.json')), [])

    def test_existing_subtitles_are_skipped_without_claiming_ownership(self):
        for name in ('04 第01集.srt', '05 第02集.srt'):
            (self.folder / name).write_bytes(b'original user captions')
        window = self.progress(audio=False)
        self.run_queue(window)
        self.assertTrue(all(task.state == 'skipped' for task in window.tasks))
        self.assertEqual(window._created_files, {})
        self.assertEqual(window.completed_list.GetItemCount(), 2)
        self.assertEqual(window.subtitle_failed_list.GetItemCount(), 0)
        self.assertIn('同名文件已存在', window.completed_list.GetItemText(0, 1))
        self.assertTrue(all(p.read_bytes() == b'original user captions' for p in self.folder.iterdir()))

    def test_clear_retains_a_subtitle_modified_after_download(self):
        window = self.progress()
        self.run_queue(window)
        changed = self.folder / '04 第01集.srt'
        changed.write_bytes(EXPECTED_SRT + b' ')
        window._clear_requested = True
        window._begin_clear()
        self.pump(lambda: not window._clearing)
        self.assertEqual(list(self.folder.iterdir()), [changed])
        self.assertEqual(changed.read_bytes(), EXPECTED_SRT + b' ')
        self.assertIn('文件位置或内容已改变', self.messages.call_args.args[0])

    def test_missing_track_reports_subtitle_failure_even_when_audio_completed(self):
        for audio in (False, True):
            with self.subTest(audio=audio), patch('download_dialog.download_subtitle', return_value=None):
                window = self.progress(audio=audio)
                self.run_queue(window)
                self.assertTrue(all(task.state == 'failed' for task in window.tasks))
                self.assertTrue(all('暂无' in task.reason for task in window.tasks))
                self.assertEqual(window.subtitle_failed_list.GetItemCount(), 2)
                self.assertEqual(window.audio_failed_list.GetItemCount(), 0)


if __name__ == '__main__':
    unittest.main()
