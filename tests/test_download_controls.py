from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import wx

from download_dialog import DownloadProgressDialog, DownloadRequest
from download_queue import CreatedDownload, DownloadBatch
from downloads import DownloadCancelled, download_audio
from maoer_api import ApiError, MediaItem, PlaybackInfo


class DownloadControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = wx.GetApp() or wx.App(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.messages = self.enterContext(patch('download_dialog.message_box', return_value=wx.YES))
        self.enterContext(patch('download_dialog.play_download_completed_sound'))
        self.enterContext(patch('download_dialog.play_download_failed_sound'))
        self.enterContext(patch('requests.Session.request', side_effect=AssertionError('No network in tests')))

    def dialog(self, folder=None):
        items = [MediaItem('sound', i + 1, f'第{i + 1}集') for i in range(3)]
        dialog = DownloadProgressDialog(None, DownloadRequest('作品', items, folder or self.root / '作品', ''))
        self.addCleanup(dialog.Destroy)
        self.addCleanup(dialog.timer.Stop)
        return dialog

    def launch(self, batch, work):
        results = []
        worker = threading.Thread(target=batch.run, args=(work, lambda *args: results.append(args)), daemon=True)
        worker.start()
        def stop():
            batch.set()
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'queue did not stop')
        self.addCleanup(stop)
        return worker, results

    def pump_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            wx.Yield()
            time.sleep(0.005)
        self.assertTrue(predicate(), 'UI operation did not finish')

    def own(self, dialog, index, content=b'new'):
        dialog.request.folder.mkdir(parents=True, exist_ok=True)
        path = dialog.request.folder / f'{index}.mp3'
        path.write_bytes(content)
        dialog._created_files[index] = CreatedDownload.capture(path, dialog.request.folder)
        dialog.tasks[index].state, dialog.tasks[index].status = 'complete', '完成'
        return path

    def test_single_pause_yields_to_next_and_resume_finishes_original(self):
        batch = DownloadBatch(2)
        paused, second = threading.Event(), threading.Event()
        order = []
        def work(index, control):
            if index == 0:
                control.pause()
                paused.set()
                control.checkpoint()
            order.append(index)
            if index == 1:
                second.set()
            return index
        worker, results = self.launch(batch, work)
        self.assertTrue(paused.wait(1))
        self.assertTrue(second.wait(1))
        self.assertTrue(worker.is_alive())
        self.assertEqual(order, [1])
        batch.controls[0].resume()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(order, [1, 0])
        self.assertTrue(all(error is None for _, _, error in results))

    def test_batch_resume_keeps_individual_pause_and_stop_wakes_it(self):
        batch = DownloadBatch(2)
        batch.controls[0].pause()
        batch.pause()
        started = threading.Event()
        worker, results = self.launch(batch, lambda index, control: started.set())
        self.assertFalse(started.wait(0.05))
        batch.resume()
        self.assertTrue(started.wait(1))
        self.assertFalse(batch.controls[0].started)
        self.assertTrue(batch.controls[0].is_paused())
        batch.set()
        worker.join(1)
        self.assertIsInstance(next(row[2] for row in results if row[0] == 0), DownloadCancelled)

    def test_cancel_active_and_waiting_tasks_does_not_cancel_next(self):
        batch = DownloadBatch(3)
        started = threading.Event()
        order = []
        batch.controls[2].set()
        def work(index, control):
            order.append(index)
            if index == 0:
                started.set()
                while True:
                    control.checkpoint()
                    time.sleep(0.005)
            return 'complete'
        worker, results = self.launch(batch, work)
        self.assertTrue(started.wait(1))
        batch.controls[0].set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(order, [0, 1])
        self.assertEqual(next(row[1] for row in results if row[0] == 1), 'complete')
        self.assertTrue(all(isinstance(row[2], DownloadCancelled) for row in results if row[0] != 1))

    def test_transfer_pause_preserves_partial_bytes_while_next_finishes(self):
        batch = DownloadBatch(2)
        paused, second = threading.Event(), threading.Event()
        api = Mock()
        api.playback_info.side_effect = lambda item: PlaybackInfo(item.id, item.title, f'https://media.invalid/{item.id}.mp3')
        with patch('downloads.MaoerApi') as anonymous, patch('downloads.av.open') as media:
            session = anonymous.return_value.session.__enter__.return_value
            def response_for(*args, **kwargs):
                response = MagicMock(status_code=200, headers={'Content-Length': '6'})
                response.__enter__.return_value = response
                response.iter_content.return_value = iter([b'abc', b'def'])
                return response
            session.get.side_effect = response_for
            media.return_value.__enter__.return_value.decode.side_effect = lambda **kwargs: iter([object()])
            def work(index, control):
                def progress(stage, percent):
                    if index == 0 and stage == '正在下载' and not paused.is_set():
                        control.pause()
                        paused.set()
                path = download_audio(api, MediaItem('sound', index, str(index)), self.root,
                                      control, Mock(), progress)
                if index == 1:
                    second.set()
                return path
            worker, results = self.launch(batch, work)
            self.assertTrue(paused.wait(1))
            self.assertTrue(second.wait(1))
            self.assertFalse((self.root / '0.mp3').exists())
            self.assertEqual((self.root / '1.mp3').read_bytes(), b'abcdef')
            batch.controls[0].resume()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual((self.root / '0.mp3').read_bytes(), b'abcdef')
            self.assertEqual(session.get.call_count, 2, 'resuming must not redownload the file')
            self.assertTrue(all(error is None for _, _, error in results))
            self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['0.mp3', '1.mp3'])

    def test_context_menu_has_state_specific_actions(self):
        dialog = self.dialog()
        dialog.running = True
        for started, paused, expected in ((False, False, ['取消下载']),
                                          (True, False, ['暂停下载', '取消下载']),
                                          (True, True, ['继续下载', '取消下载'])):
            with self.subTest(started=started, paused=paused):
                control = dialog.controls[0]
                control.started = started
                control.pause() if paused else control.resume()
                def choose(menu, position):
                    self.assertEqual([item.GetItemLabelText().split('(')[0] for item in menu.GetMenuItems()], expected)
                    return menu.GetMenuItems()[0].GetId() if started else wx.ID_NONE
                with patch.object(dialog.pending_list, 'GetPopupMenuSelectionFromUser', side_effect=choose):
                    dialog._show_task_menu()
                if started:
                    self.assertEqual(control.is_paused(), not paused)

    def test_menu_action_does_not_cancel_task_that_finished_while_menu_was_open(self):
        dialog = self.dialog()
        dialog.running = True
        def choose(menu, position):
            dialog.controls[0].done = True
            return menu.GetMenuItems()[-1].GetId()
        with patch.object(dialog.pending_list, 'GetPopupMenuSelectionFromUser', side_effect=choose):
            dialog._show_task_menu()
        self.messages.assert_not_called()
        self.assertFalse(dialog.controls[0].is_set())

    def test_keyboard_context_menu_uses_current_selection(self):
        dialog = self.dialog()
        for key in (wx.WXK_MENU, wx.WXK_F10):
            event = Mock()
            event.GetKeyCode.return_value = key
            event.ShiftDown.return_value = True
            with patch.object(dialog, '_show_task_menu') as show:
                dialog._on_task_key(event)
            show.assert_called_once()
            event.Skip.assert_not_called()

    def test_failed_list_menu_and_keyboard_only_start_explicit_retries(self):
        dialog = self.dialog()
        for index, task in enumerate(dialog.tasks):
            task.state = 'failed' if index != 1 else 'skipped'
        dialog._refresh_lists()
        for key in (wx.WXK_MENU, wx.WXK_WINDOWS_MENU, wx.WXK_F10):
            event = Mock()
            event.GetEventObject.return_value = dialog.failed_list
            event.GetKeyCode.return_value = key
            event.ShiftDown.return_value = True
            with patch.object(dialog, '_show_failed_menu') as show, patch.object(dialog, '_retry_tasks') as retry:
                dialog._on_task_key(event)
            show.assert_called_once()
            retry.assert_not_called()
        for action in (0, 1):
            def choose(menu, position):
                items = menu.GetMenuItems()
                self.assertEqual([item.GetItemLabel() for item in items], ['重新开始(&R)', '全部开始(&A)'])
                self.assertTrue(all(item.IsEnabled() for item in items))
                return items[action].GetId()
            with patch.object(dialog.failed_list, 'GetPopupMenuSelectionFromUser', side_effect=choose), \
                    patch.object(dialog, '_retry_tasks') as retry:
                dialog._show_failed_menu()
            retry.assert_called_once_with([0] if action == 0 else [0, 1, 2])
        # Right-clicking another row changes the target; opening a menu starts no work.
        event = Mock()
        event.GetEventObject.return_value = dialog.failed_list
        event.GetPosition.return_value = wx.Point(10, 20)
        with patch.object(dialog.failed_list, 'ScreenToClient', return_value=wx.Point(1, 2)), \
                patch.object(dialog.failed_list, 'HitTest', return_value=(2, 0)), \
                patch.object(dialog, '_show_failed_menu') as show, patch.object(dialog, '_retry_tasks') as retry:
            dialog._on_task_context_menu(event)
        self.assertEqual(dialog.failed_list.GetItemData(dialog.failed_list.GetFirstSelected()), 2)
        show.assert_called_once_with(wx.Point(1, 2))
        retry.assert_not_called()

    def test_failed_jobs_retry_to_completion_without_touching_other_results(self):
        for other_state in ('complete', 'skipped'):
            with self.subTest(other_state=other_state):
                dialog = self.dialog(self.root / other_state)
                original = self.own(dialog, 2, b'original file')
                for index, task in enumerate(dialog.tasks):
                    task.state = 'failed' if index < 2 else other_state
                    task.reason = '测试失败' if index < 2 else ''
                    dialog.controls[index].started = dialog.controls[index].done = True
                dialog._started = True
                dialog._refresh_lists()
                attempts = []
                def download(api, item, folder, control, key, progress, **kwargs):
                    attempts.append(item.id)
                    self.assertEqual(folder, dialog.request.folder)
                    progress('正在下载', 50)
                    path = folder / (item.title + '.mp3')
                    path.write_bytes(b'retried')
                    return path
                with patch('download_dialog.MaoerApi'), patch('download_dialog.download_audio', side_effect=download), \
                        patch.object(dialog.pending_list, 'SetFocus') as focus:
                    dialog._retry_tasks([1])
                    self.assertEqual(dialog.tasks[1].reason, '')
                    self.assertEqual(dialog.pending_list.GetItemData(dialog.pending_list.GetFirstSelected()), 1)
                    focus.assert_called_once()
                    self.pump_until(lambda: not dialog.running)
                    self.assertEqual(attempts, [2])
                    self.assertEqual([task.state for task in dialog.tasks], ['failed', 'complete', other_state])
                    dialog._retry_tasks([0, 1, 2])
                    self.pump_until(lambda: not dialog.running)
                self.assertEqual(attempts, [2, 1])
                self.assertEqual([task.state for task in dialog.tasks], ['complete', 'complete', other_state])
                self.assertEqual(original.read_bytes(), b'original file')
                self.assertEqual(dialog.failed_list.GetItemCount(), int(other_state == 'skipped'))

    def test_queue_accepts_retry_before_failed_worker_releases_its_slot(self):
        batch = DownloadBatch(2)
        attempts, results = [], []
        def work(index, control):
            attempts.append(index)
            if attempts == [0]:
                raise ApiError('first attempt failed')
            return index
        def result(index, value, error):
            results.append((index, value, error))
            if error is not None:
                self.assertFalse(batch.controls[index].done)
                batch.retry([index])
        worker = threading.Thread(target=batch.run, args=(work, result), daemon=True)
        worker.start()
        worker.join(2)
        if worker.is_alive():
            batch.set()
            worker.join(2)
            self.fail('retry was lost or the queue did not finish')
        self.assertEqual(attempts, [0, 0, 1])
        self.assertEqual([value for _, value, error in results if error is None], [0, 1])

    def test_retry_while_queue_finish_callback_is_pending_is_not_lost(self):
        dialog = self.dialog()
        for task, control in zip(dialog.tasks, dialog.controls):
            task.state = 'failed'
            control.started = control.done = True
        dialog.running = dialog._started = True
        dialog._refresh_lists()
        with patch.object(dialog, '_run') as run:
            dialog._retry_tasks([0, 1, 2])
            run.assert_not_called()
            dialog._finished(False)
            self.pump_until(lambda: run.call_count == 1)
        self.assertTrue(dialog.running)
        self.assertTrue(all(task.state == 'pending' for task in dialog.tasks))
        self.messages.assert_not_called()

    def test_failed_retry_waits_for_current_download_then_runs_in_same_queue(self):
        batch = DownloadBatch(3)
        second_started, release = threading.Event(), threading.Event()
        attempts = []
        def work(index, control):
            attempts.append(index)
            if attempts == [0]:
                raise ApiError('first attempt failed')
            if index == 1:
                second_started.set()
                while not release.wait(0.01):
                    control.checkpoint()
            return index
        worker, results = self.launch(batch, work)
        self.addCleanup(release.set)
        self.assertTrue(second_started.wait(1))
        batch.retry([0])
        self.assertEqual(batch.active, 1)
        self.assertFalse(batch.controls[0].started)
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(attempts, [0, 1, 0, 2])
        self.assertEqual([value for _, value, error in results if error is None], [1, 0, 2])

    def test_single_cancel_yes_no_only_affects_selected_task(self):
        dialog = self.dialog()
        dialog.running = True
        for initially_paused in (False, True):
            control = dialog.controls[0]
            control.pause() if initially_paused else control.resume()
            self.messages.return_value = wx.NO
            dialog._cancel_task(0)
            self.assertEqual(control.is_paused(), initially_paused)
            self.assertFalse(control.is_set())
        self.messages.return_value = wx.YES
        dialog._cancel_task(0)
        self.assertTrue(dialog.controls[0].is_set())
        self.assertFalse(dialog.cancel.is_set())
        self.assertFalse(dialog.controls[1].is_set())
        self.assertEqual(dialog.tasks[0].state, 'cancelled')
        self.assertTrue(self.messages.call_args.args[2] & wx.NO_DEFAULT)
        self.assertTrue(self.messages.call_args.args[2] & wx.YES_NO)

    def test_batch_button_labels_and_stop_preserves_completed_files(self):
        dialog = self.dialog()
        saved = self.own(dialog, 0)
        dialog.running = True
        self.assertEqual(dialog.pause_button.GetLabel(), '全部暂停(&P)')
        self.assertEqual(dialog.cancel_button.GetLabel(), '停止下载(&C)')
        self.assertEqual(dialog.clear_button.GetLabel(), '全部取消(&Q)')
        dialog._toggle_pause(None)
        self.assertEqual(dialog.pause_button.GetLabel(), '全部继续(&P)')
        dialog._cancel_download(False)
        self.assertEqual(self.messages.call_args.args[0], '是否取消下载？已完成的文件会保留，未完成的临时文件会清理。')
        dialog._finished(True)
        self.assertTrue(saved.exists())
        self.assertFalse(dialog._clear_requested)

    def test_clear_only_this_batch_after_workers_finish_including_racing_output(self):
        dialog = self.dialog()
        saved = self.own(dialog, 0)
        old = dialog.request.folder / '原有文件.mp3'
        old.write_bytes(b'old')
        dialog.running = True
        dialog._cancel_all(None)
        self.assertTrue(saved.exists(), 'must wait for the writer to finish')
        self.assertFalse(dialog._clearing)
        late = self.own(dialog, 1, b'finished during cancel confirmation')
        dialog._finished(True)
        self.pump_until(lambda: not dialog._clearing)
        self.assertFalse(saved.exists())
        self.assertFalse(late.exists())
        self.assertEqual(old.read_bytes(), b'old')
        self.assertTrue(dialog.request.folder.is_dir())
        self.assertEqual(dialog.completed_list.GetItemCount(), 0)
        self.assertFalse(dialog.clear_button.IsEnabled())

    def test_clear_no_keeps_files_and_previous_pause_state(self):
        dialog = self.dialog()
        saved = self.own(dialog, 0)
        dialog.running = True
        dialog.controls[1].pause()
        self.messages.return_value = wx.NO
        dialog._cancel_all(None)
        self.assertFalse(dialog.cancel.is_paused())
        self.assertTrue(dialog.controls[1].is_paused())
        self.assertFalse(dialog.cancel.is_set())
        self.assertFalse(dialog._clear_requested)
        self.assertTrue(saved.exists())

    def test_clear_after_finished_retains_modified_or_replaced_file_and_reports_it(self):
        dialog = self.dialog()
        saved = self.own(dialog, 0)
        saved.write_bytes(b'modified by user')
        dialog._cancel_all(None)
        self.pump_until(lambda: not dialog._clearing)
        self.assertEqual(saved.read_bytes(), b'modified by user')
        self.assertEqual(self.messages.call_args.args[1], '部分文件未清理')
        self.assertEqual(dialog.tasks[0].state, 'complete')

    def test_file_ownership_cannot_escape_target_folder(self):
        folder = self.root / '作品'
        folder.mkdir()
        outside = self.root / 'other.mp3'
        outside.write_bytes(b'keep')
        with self.assertRaises(OSError):
            CreatedDownload.capture(outside, folder)
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_close_waits_until_clear_is_finished(self):
        dialog = self.dialog()
        dialog._clearing = True
        with patch.object(dialog, '_finish_window') as end:
            dialog._close(None)
            end.assert_not_called()
            dialog._clear_finished([], {})
            end.assert_called_once_with(wx.ID_CANCEL)

    def test_open_folder_is_custom_or_default_download_root(self):
        for directory in ('默认下载', '自定义目录'):
            dialog = self.dialog(self.root / directory / '作品')
            with patch('download_dialog.os.startfile') as startfile:
                dialog._open_folder(None)
            startfile.assert_called_once_with(str(self.root / directory))

    def test_preparation_requests_are_serial_and_not_mixed_after_pause_or_cancel(self):
        dialog = self.dialog()
        dialog.running = True
        dialog.player = Mock()
        infos = [PlaybackInfo(i, str(i), 'https://media.invalid/audio.mp3') for i in (1, 2)]
        first_ready, second_ready = threading.Event(), threading.Event()
        first, second = {}, {}
        dialog._request_key(infos[0], first_ready, first, dialog.controls[0])
        dialog.controls[0].pause()
        dialog._request_key(infos[1], second_ready, second, dialog.controls[1])
        self.assertIs(dialog.key_control, dialog.controls[0])
        with patch('download_dialog.wx.CallAfter', side_effect=lambda callback, *args: callback(*args)):
            dialog._key_done(key=b'first')
        self.assertEqual(first, {'key': b'first'})
        self.assertTrue(first_ready.is_set())
        self.assertFalse(second_ready.is_set())
        self.assertIs(dialog.key_control, dialog.controls[1])
        dialog.controls[1].set()
        dialog._poll_key(None)
        self.assertTrue(second_ready.is_set())
        self.assertIn('error', second)
        self.assertEqual(first, {'key': b'first'})

    def test_worker_records_output_before_completion_callback(self):
        dialog = self.dialog()
        dialog.running = True
        def download(api, item, folder, control, get_key, progress, **options):
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f'{item.id}.mp3'
            path.write_bytes(b'file')
            return path
        queued = []
        with patch('download_dialog.MaoerApi'), patch('download_dialog.download_audio', side_effect=download), \
                patch('download_dialog.wx.CallAfter', side_effect=lambda callback, *args: queued.append((callback, args))):
            dialog._run()
        self.assertEqual(len(dialog._created_files), 3)
        self.assertEqual([task.state for task in dialog.tasks], ['pending'] * 3)
        for callback, args in queued:
            callback(*args)
        self.assertEqual([task.state for task in dialog.tasks], ['complete'] * 3)
        self.assertFalse(dialog.running)


if __name__ == '__main__':
    unittest.main()
