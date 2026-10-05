from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

import wx

from download_dialog import DownloadDialog, DownloadProgressDialog, DownloadRequest, show_download_dialog
from downloads import (DownloadCancelled, DownloadControl, DownloadSelection, check_cancel,
                       download_audio, load_selection, numbered_title)
from maoer_api import ApiError, MediaItem, PlaybackInfo
from startup_sound import DOWNLOAD_FAILED_SOUND, play_download_failed_sound


class DownloadTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = wx.GetApp() or wx.App(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.items = [MediaItem('sound', i + 1, f'第{i + 1}集', subtitle='作品 / 正剧') for i in range(4)]
        self.network = patch('requests.Session.request', side_effect=AssertionError('No real network in download tests'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def selector(self, checked=(1,), width=2):
        selection = DownloadSelection('作品', '发布者', self.items, list(checked), width)
        dialog = DownloadDialog(None, selection, '', self.root)
        self.addCleanup(dialog.Destroy)
        return dialog

    def progress(self, *, width=None):
        dialog = DownloadProgressDialog(None, DownloadRequest('作品', self.items, self.root / '作品', '', width))
        self.addCleanup(dialog.Destroy)
        self.addCleanup(dialog.timer.Stop)
        return dialog

    def test_selection_defaults_and_width_use_whole_drama_before_single_episode_slice(self):
        api = Mock()
        drama = MediaItem('drama', 1000, '作品')
        api.drama_for_sound.return_value = drama
        api.publisher_name_for_item.return_value = '发布者'
        for total in (99, 100, 125):
            with self.subTest(total=total):
                episodes = [MediaItem('sound', i + 1, f'第{i + 1}集', subtitle='作品 / 正剧') for i in range(total)]
                # Official main-episode membership also counts a non-numbered finale.
                episodes[-1].title = '完结篇'
                episodes += [MediaItem('sound', 9999, '第一期花絮', subtitle='作品 / 花絮')]
                api.drama_episodes.return_value = episodes
                current = episodes[20]
                single = load_selection(api, current, False)
                batch = load_selection(api, current, True)
                from_drama = load_selection(api, drama, True)
                self.assertEqual(single.items, [current])
                self.assertEqual(single.checked, [0])
                self.assertEqual(batch.checked, [20])
                self.assertEqual(from_drama.checked, [])
                self.assertEqual(single.number_width, 3 if total >= 100 else 2)
                self.assertEqual(batch.number_width, single.number_width)

    def test_numbered_names_only_change_episode_digits(self):
        for title, width, expected in (
            ('第一期.m4a', 2, '第01期.m4a'), ('第十五集.m4a', 2, '第15集.m4a'),
            ('第一期（上）', 3, '第001期（上）'), ('第十五集·下', 3, '第015集·下'),
            ('作品 第一百零二集 完结', 3, '作品 第102集 完结'),
            ('第１２集', 2, '第12集'), ('第02期', 3, '第002期'),
            ('第 一 期', 2, '第 01 期'), ('第二〇一期', 3, '第201期'),
            ('花絮第一期', 3, '花絮第一期'), ('番外第一集', 2, '番外第一集'),
            ('没有编号的结尾', 3, '没有编号的结尾'),
        ):
            with self.subTest(title=title):
                item = MediaItem('sound', 1, title)
                self.assertEqual(numbered_title(item, width), expected)
                self.assertEqual(item.title, title)
        self.assertEqual(numbered_title(MediaItem('sound', 1, '第一期', subtitle='作品 / 音乐'), 3), '第一期')

    def test_selection_range_includes_cursor_and_does_not_touch_previous_items(self):
        dialog = self.selector()
        self.assertEqual(dialog.list.GetSelection(), 1)
        self.assertEqual(dialog.list.GetCheckedItems(), (1,))
        dialog.list.SetSelection(2)
        dialog._check_from_current(True)
        self.assertEqual(dialog.list.GetCheckedItems(), (1, 2, 3))
        dialog._check_from_current(False)
        self.assertEqual(dialog.list.GetCheckedItems(), (1,))
        self.assertEqual(dialog.list.GetSelection(), 2)
        self.assertFalse(hasattr(dialog, 'results'))
        self.assertFalse(hasattr(dialog, 'reader'))
        self.assertFalse(hasattr(dialog, 'gauge'))

    def test_selector_submits_request_without_starting_transfer(self):
        dialog = self.selector(width=3)
        dialog.numbered.SetValue(True)
        dialog.publisher.SetValue(True)
        with patch.object(dialog, 'EndModal') as end, patch('download_dialog.download_audio') as download:
            dialog._start(None)
        end.assert_called_once_with(wx.ID_OK)
        download.assert_not_called()
        self.assertEqual(dialog.request.items, [self.items[1]])
        self.assertEqual(dialog.request.number_width, 3)
        self.assertEqual(dialog.request.folder.name, '作品【发布者】')

    def test_selector_destroyed_before_task_window_is_constructed(self):
        events = []
        with patch('download_dialog.DownloadDialog') as selection_type, \
                patch('download_dialog.DownloadProgressDialog') as progress_type, \
                patch('download_dialog.wx.CallAfter'):
            selector = selection_type.return_value
            selector.ShowModal.return_value = wx.ID_OK
            selector.Destroy.side_effect = lambda: events.append('selector destroyed')
            progress = Mock()
            def create(*args):
                self.assertEqual(events, ['selector destroyed'])
                return progress
            progress_type.side_effect = create
            show_download_dialog(None, Mock(), '', self.root)
            progress.ShowModal.assert_called_once()
            progress.Destroy.assert_called_once()

    def test_task_lists_preserve_selection_and_have_no_live_announcements(self):
        dialog = self.progress()
        dialog.running = True
        self.assertFalse(dialog.failed_list.IsShown())
        dialog.pending_list.Select(0, False)
        dialog.pending_list.Select(2)
        dialog.pending_list.Focus(2)
        with patch('uia_live_region.ScreenReaderAnnouncer.announce') as announce, \
                patch('download_dialog.play_download_completed_sound') as success, \
                patch('download_dialog.play_download_failed_sound') as failed:
            dialog._task_progress(0, '正在准备音频', 0)
            self.assertEqual(dialog.pending_list.GetFirstSelected(), 2)
            self.assertEqual(dialog.pending_list.GetItemText(0, 1), '正在准备音频 0%')
            dialog._task_result(0, 'complete', '')
            self.assertEqual(dialog.pending_list.GetItemData(dialog.pending_list.GetFirstSelected()), 2)
            self.assertEqual(dialog.completed_list.GetItemText(0, 1), '完成')
            dialog._task_result(1, 'failed', '网络请求失败')
            self.assertTrue(dialog.failed_list.IsShown())
            self.assertEqual(dialog.failed_list.GetItemText(0, 1), '失败')
            self.assertEqual(dialog.failed_list.GetItemText(0, 2), '网络请求失败')
            dialog._task_result(1, 'failed', '重复通知不重复播放')
            success.assert_called_once()
            failed.assert_called_once()
            announce.assert_not_called()
        self.assertEqual(dialog.pending_list.GetName(), '待下载')
        self.assertEqual(dialog.completed_list.GetName(), '已下载')
        self.assertFalse(hasattr(dialog, 'reader'))

    def test_worker_routes_success_failure_skip_and_numbering_without_stopping_batch(self):
        dialog = self.progress(width=3)
        dialog.running = True
        outcomes = [self.root / '第001集.m4a', ApiError('测试失败'), FileExistsError(), self.root / '第004集.m4a']
        queued = []
        with patch('download_dialog.MaoerApi'), \
                patch('download_dialog.download_audio', side_effect=outcomes) as download, \
                patch('download_dialog.wx.CallAfter', side_effect=lambda f, *a, **kw: queued.append((f, a, kw))), \
                patch('download_dialog.play_download_completed_sound') as success, \
                patch('download_dialog.play_download_failed_sound') as failure, \
                patch('download_dialog.message_box') as message:
            dialog._run()
            for callback, args, kwargs in queued:
                callback(*args, **kwargs)
        self.assertEqual([task.state for task in dialog.tasks], ['complete', 'failed', 'skipped', 'complete'])
        self.assertEqual(success.call_count, 2)
        self.assertEqual(failure.call_count, 1)
        self.assertEqual(download.call_count, 4)
        self.assertTrue(all(call.kwargs == {'number_width': 3} for call in download.call_args_list))
        self.assertIn('完成 2，失败 1，跳过 1', message.call_args.args[0])

    def test_pause_blocks_checkpoints_and_cancel_wakes_waiter(self):
        for cancel_task in (False, True):
            with self.subTest(cancel=cancel_task):
                control = DownloadControl()
                control.pause()
                result = []
                entered, done = threading.Event(), threading.Event()
                def work():
                    entered.set()
                    try:
                        check_cancel(control)
                        result.append('continued')
                    except DownloadCancelled:
                        result.append('cancelled')
                    finally:
                        done.set()
                worker = threading.Thread(target=work, daemon=True)
                worker.start()
                self.assertTrue(entered.wait(1))
                self.assertFalse(done.wait(0.05))
                control.set() if cancel_task else control.resume()
                self.assertTrue(done.wait(1))
                worker.join(1)
                self.assertEqual(result, ['cancelled' if cancel_task else 'continued'])

    def test_preparation_timeout_clock_does_not_count_pause(self):
        with patch('downloads.time.monotonic', return_value=100) as clock:
            control = DownloadControl()
            self.assertEqual(control.active_time(), 100)
            clock.return_value = 105
            control.pause()
            clock.return_value = 900
            self.assertEqual(control.active_time(), 105)
            control.resume()
            clock.return_value = 910
            self.assertEqual(control.active_time(), 115)

    def test_transfer_pause_resume_numbered_filename_and_cancel_cleanup(self):
        for cancel_task in (False, True):
            with self.subTest(cancel=cancel_task), patch('downloads.MaoerApi') as anonymous, \
                    patch('downloads.av.open') as media:
                api = Mock()
                api.playback_info.return_value = PlaybackInfo(1, '第一期', 'https://media.invalid/audio.mp3')
                response = anonymous.return_value.session.__enter__.return_value.get.return_value.__enter__.return_value
                response.status_code = 200
                response.headers = {'Content-Length': '6'}
                response.iter_content.return_value = iter([b'abc', b'def'])
                media.return_value.__enter__.return_value.decode.return_value = iter([object()])
                control = DownloadControl()
                paused, done = threading.Event(), threading.Event()
                outcomes = []
                folder = self.root / str(cancel_task)

                def progress(stage, percent):
                    if stage == '正在下载' and not paused.is_set():
                        control.pause()
                        paused.set()

                def work():
                    try:
                        outcomes.append(download_audio(api, MediaItem('sound', 1, '第一期'), folder,
                                                       control, Mock(), progress, number_width=3))
                    except Exception as exc:
                        outcomes.append(exc)
                    finally:
                        done.set()

                worker = threading.Thread(target=work, daemon=True)
                worker.start()
                try:
                    self.assertTrue(paused.wait(1))
                    self.assertFalse(done.wait(0.05))
                    self.assertFalse((folder / '第001期.mp3').exists())
                    control.set() if cancel_task else control.resume()
                    self.assertTrue(done.wait(1))
                finally:
                    control.set()
                    worker.join(2)
                if cancel_task:
                    self.assertIsInstance(outcomes[0], DownloadCancelled)
                    self.assertEqual(list(folder.iterdir()), [])
                else:
                    self.assertEqual(outcomes, [folder / '第001期.mp3'])
                    self.assertEqual(outcomes[0].read_bytes(), b'abcdef')
                    self.assertEqual(list(folder.iterdir()), outcomes)

    def test_dialog_cancel_command_and_close_button_share_confirmation(self):
        dialog = self.progress()
        dialog.running = True
        for target in (dialog, dialog.close_button):
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, wx.ID_CANCEL)
            event.SetEventObject(target)
            with patch('download_dialog.message_box', return_value=wx.NO) as confirm, \
                    patch.object(dialog, 'EndModal') as close:
                target.GetEventHandler().ProcessEvent(event)
                confirm.assert_called_once()
                close.assert_not_called()
                self.assertFalse(dialog.cancel.is_set())

    def test_cancel_confirmation_preserves_previous_pause_and_defaults_to_no(self):
        dialog = self.progress()
        dialog.running = True
        for paused in (False, True):
            control = dialog.cancel = DownloadControl()
            if paused:
                control.pause()
            with patch('download_dialog.message_box', return_value=wx.NO) as confirm:
                dialog._cancel_download(False)
            self.assertTrue(confirm.call_args.args[2] & wx.NO_DEFAULT)
            self.assertEqual(control.is_paused(), paused)
            self.assertFalse(control.is_set())
        with patch('download_dialog.message_box', return_value=wx.YES):
            dialog._cancel_download(False)
        self.assertTrue(dialog.cancel.is_set())
        self.assertFalse(dialog.cancel.is_paused())
        with patch('download_dialog.message_box') as message, patch.object(dialog, 'EndModal') as close:
            dialog._finished(True)
        message.assert_not_called()
        close.assert_not_called()
        self.assertTrue(all(task.state == 'cancelled' for task in dialog.tasks))

    def test_completion_popup_then_optional_close_and_close_requires_cancel(self):
        for close_after in (False, True):
            dialog = self.progress()
            dialog.running = True
            for task in dialog.tasks:
                task.state, task.status = 'complete', '完成'
            dialog.auto_close.SetValue(close_after)
            with patch('download_dialog.message_box', return_value=wx.OK) as message, \
                    patch.object(dialog, 'EndModal') as close:
                dialog._finished(False)
                message.assert_called_once()
                self.assertIn('恭喜，下载任务已完成', message.call_args.args[0])
                self.assertEqual(close.call_count, int(close_after))
        dialog = self.progress()
        dialog.running = True
        with patch('download_dialog.message_box', return_value=wx.YES), patch.object(dialog, 'EndModal') as close:
            dialog._close(None)
            close.assert_not_called()
            dialog._finished(True)
            close.assert_called_once_with(wx.ID_CANCEL)

    def test_cancel_popup_defers_racing_completion(self):
        dialog = self.progress()
        dialog.running = True
        def confirm(*args):
            dialog._finished(False)
            self.assertTrue(dialog.running)
            return wx.YES
        with patch('download_dialog.message_box', side_effect=confirm) as message:
            dialog._cancel_download(False)
        self.assertFalse(dialog.running)
        self.assertTrue(dialog.cancel.is_set())
        self.assertEqual(message.call_count, 1)

    def test_open_download_folder_and_independent_failure_sound(self):
        dialog = self.progress()
        with patch('download_dialog.os.startfile') as open_folder:
            dialog._open_folder(None)
        open_folder.assert_called_once_with(str(dialog.request.folder.parent))
        self.assertTrue(dialog.request.folder.parent.is_dir())
        with wave.open(str(DOWNLOAD_FAILED_SOUND)) as sound:
            self.assertGreater(sound.getnframes(), 0)
        with patch('startup_sound._play_sound') as sound:
            play_download_failed_sound()
        sound.assert_called_once_with(DOWNLOAD_FAILED_SOUND)


if __name__ == '__main__':
    unittest.main()
