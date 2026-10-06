from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import wx

from app_settings import AppSettings, DownloadSettings, load_settings, save_settings
from download_dialog import DownloadDialog, DownloadProgressDialog, DownloadRequest, _DownloadPlayer
from download_manager import DownloadManager
from download_settings import DownloadSettingsDialog
from downloads import DownloadSelection
from maoer_api import MediaItem, PlaybackInfo


class DownloadDefaultStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch('app_settings.app_data_dir', return_value=self.root))

    def test_defaults_preserve_current_behavior(self):
        self.assertEqual(load_settings().downloads, DownloadSettings())
        (self.root / 'settings.json').write_text('{"read_subtitle": true}', encoding='utf-8')
        self.assertEqual(load_settings().downloads, DownloadSettings())
        self.assertTrue(load_settings().read_subtitle)

    def test_all_download_settings_roundtrip_without_affecting_playback(self):
        options = DownloadSettings(directory=str(self.root / '自定义目录'), create_drama_folder=False,
                                   announce_hidden_complete=False, play_sound=False,
                                   auto_close_hidden=False, escape_hides=False)
        settings = AppSettings(read_subtitle=True, playback_mode='stop', downloads=options)
        save_settings(settings)
        self.assertEqual(load_settings(), settings)
        self.assertEqual([p.name for p in self.root.iterdir()], ['settings.json'])

    def test_bad_fields_do_not_turn_string_false_into_true(self):
        for raw in (None, [], 'bad', {'directory': 1}, {'directory': 'relative'},
                    {'completed_sound': 'false', 'auto_close': 1, 'failed_sound': None}):
            (self.root / 'settings.json').write_text(json.dumps({'downloads': raw}), encoding='utf-8')
            self.assertEqual(load_settings().downloads, DownloadSettings())

    def test_failed_save_keeps_previous_download_settings(self):
        save_settings(AppSettings())
        with patch('app_settings.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                save_settings(AppSettings(downloads=DownloadSettings(auto_close_hidden=False)))
        self.assertEqual(load_settings(), AppSettings())


class BackgroundDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = wx.Frame(None, title='下载测试')
        self.addCleanup(self.destroy, self.parent)
        self.focus = wx.TextCtrl(self.parent)
        self.parent.Show()
        self.items = [MediaItem('sound', i + 1, f'第{i + 1}集') for i in range(3)]
        self.messages = self.enterContext(patch('download_dialog.message_box', return_value=wx.OK))
        self.manager_messages = self.enterContext(patch('download_manager.message_box', return_value=wx.NO))
        self.reader = self.enterContext(patch('download_manager.ScreenReaderAnnouncer'))
        self.success = self.enterContext(patch('download_dialog.play_download_completed_sound'))
        self.failure = self.enterContext(patch('download_dialog.play_download_failed_sound'))
        self.enterContext(patch('requests.Session.request', side_effect=AssertionError('No real network')))

    @staticmethod
    def destroy(window):
        if window and not window.IsBeingDeleted():
            window.Destroy()

    def progress(self, options=None, **kwargs):
        request = DownloadRequest('作品', self.items, self.root / '作品', '', download_root=self.root,
                                  options=options or DownloadSettings(auto_close_hidden=False))
        dialog = DownloadProgressDialog(self.parent, request, **kwargs)
        self.addCleanup(self.destroy, dialog)
        self.addCleanup(dialog.timer.Stop)
        return dialog

    def manager(self):
        manager = DownloadManager(self.parent, self.focus.SetFocus)
        self.addCleanup(manager.dispose)
        return manager

    def pump(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            wx.Yield()
            time.sleep(0.005)
        self.assertTrue(predicate(), 'Operation did not finish')

    def test_default_settings_dialog_browse_save_and_cancel_labels(self):
        save = Mock()
        dialog = DownloadSettingsDialog(self.parent, DownloadSettings(), self.root, save)
        self.addCleanup(self.destroy, dialog)
        self.assertEqual(dialog.path.GetValue(), str(self.root))
        self.assertEqual(dialog.path.GetName(), '默认下载路径')
        self.assertEqual(dialog.FindWindow(wx.ID_OK).GetLabel(), '确定')
        self.assertEqual(dialog.FindWindow(wx.ID_CANCEL).GetLabel(), '取消')
        chosen = self.root / '浏览的文件夹'
        with patch('download_settings.wx.DirDialog') as browse:
            browse.return_value.__enter__.return_value.ShowModal.return_value = wx.ID_OK
            browse.return_value.__enter__.return_value.GetPath.return_value = str(chosen)
            dialog._browse(None)
        dialog.options['create_drama_folder'].SetValue(False)
        dialog.options['auto_close_hidden'].SetValue(True)
        with patch.object(dialog, 'EndModal') as end:
            dialog._save(None)
        save.assert_called_once_with(DownloadSettings(str(chosen), create_drama_folder=False, auto_close_hidden=True))
        end.assert_called_once_with(wx.ID_OK)
        self.assertFalse(chosen.exists(), 'Saving a default must not create or delete download files')

    def test_invalid_path_or_failed_save_keeps_editor_open(self):
        save = Mock(side_effect=OSError('read only'))
        dialog = DownloadSettingsDialog(self.parent, DownloadSettings(), self.root, save)
        self.addCleanup(self.destroy, dialog)
        with patch('download_settings.message_box') as message, patch.object(dialog, 'EndModal') as end:
            for value in ('', 'relative'):
                dialog.path.SetValue(value)
                dialog._save(None)
            save.assert_not_called()
            dialog.path.SetValue(str(self.root))
            dialog._save(None)
            end.assert_not_called()
            self.assertEqual(message.call_count, 3)

    def test_no_drama_folder_hides_publisher_and_uses_exact_download_root(self):
        options = DownloadSettings(create_drama_folder=False)
        selection = DownloadSelection('作品', '发布者', self.items, [1])
        dialog = DownloadDialog(self.parent, selection, '', self.root, options=options)
        self.addCleanup(self.destroy, dialog)
        self.assertFalse(dialog.publisher.IsShown())
        self.assertFalse(dialog.publisher.IsEnabled())
        dialog.publisher.SetValue(True)  # A stale/forced checkbox cannot alter the target.
        with patch.object(dialog, 'EndModal'):
            dialog._start(None)
        self.assertEqual(dialog.request.folder, self.root)
        self.assertEqual(dialog.request.download_root, self.root)
        self.assertEqual(dialog.request.options, options)

    def test_drama_folder_keeps_publisher_option(self):
        selection = DownloadSelection('作品', '发布者', self.items, [0])
        dialog = DownloadDialog(self.parent, selection, '', self.root)
        self.addCleanup(self.destroy, dialog)
        self.assertTrue(dialog.publisher.IsShown())
        dialog.publisher.SetValue(True)
        with patch.object(dialog, 'EndModal'):
            dialog._start(None)
        self.assertEqual(dialog.request.folder, self.root / '作品【发布者】')

    def test_hide_keeps_state_and_restore_keeps_selection(self):
        manager = self.manager()
        dialog = self.progress(on_hidden=manager.focus_main, on_closed=manager._closed)
        manager.window = dialog
        dialog._started = dialog.running = True
        dialog.Show()
        self.assertFalse(dialog.IsModal())
        self.assertTrue(self.parent.IsEnabled())
        dialog.pending_list.Select(0, False)
        dialog.pending_list.Select(2)
        dialog.pending_list.Focus(2)
        dialog.pending_list.SetFocus()
        dialog._hide(None)
        self.assertTrue(dialog.hidden)
        self.assertFalse(dialog.IsShown())
        self.assertIs(wx.Window.FindFocus(), self.focus)
        self.assertTrue(dialog.running)
        self.assertFalse(dialog.cancel.is_set())
        dialog._task_progress(0, '正在下载', 40)
        manager.show_tasks()
        self.assertFalse(dialog.hidden)
        self.assertTrue(dialog.IsShown())
        self.assertEqual(dialog.pending_list.GetFirstSelected(), 2)
        self.assertEqual(dialog.tasks[0].status, '正在下载 40%')

    def test_hidden_completion_popup_and_optional_announcement_happen_once(self):
        announce = Mock()
        dialog = self.progress(announce=announce, on_hidden=self.focus.SetFocus)
        dialog._started = dialog.running = True
        dialog.Show()
        dialog._hide(None)
        self.focus.SetFocus()
        for task in dialog.tasks:
            task.state = 'complete'
        with patch.object(dialog, 'Raise') as raise_window, patch.object(dialog.completed_list, 'SetFocus') as focus:
            dialog._finished(False)
            dialog._finished(False)
            self.messages.assert_called_once()
            self.assertIs(self.messages.call_args.args[3], self.parent)
            announce.assert_called_once()
            self.assertIn('完成 3', announce.call_args.args[0])
            focus.assert_not_called()
            raise_window.assert_not_called()
        self.assertIs(wx.Window.FindFocus(), self.focus)
        dialog.restore()
        self.assertEqual(dialog.completed_list.GetItemCount(), 3)
        self.messages.assert_called_once()
        announce.assert_called_once()

    def test_hidden_completion_notification_and_auto_close_are_independent(self):
        for enabled in (False, True):
            for visible_close in (False, True):
                for hidden_close in (False, True):
                    self.messages.reset_mock()
                    announce = Mock()
                    options = DownloadSettings(announce_hidden_complete=enabled, auto_close_hidden=hidden_close)
                    dialog = self.progress(options, announce=announce)
                    dialog.auto_close.SetValue(visible_close)
                    dialog._started = dialog.running = dialog.hidden = True
                    with patch.object(dialog, '_finish_window') as close:
                        dialog._finished(False)
                        self.assertEqual(close.call_count, int(hidden_close))
                    self.assertEqual(announce.call_count, int(enabled))
                    self.messages.assert_called_once()

    def test_visible_auto_close_uses_current_checkbox_not_hidden_default(self):
        for visible_close in (False, True):
            dialog = self.progress(DownloadSettings(auto_close_hidden=True))
            self.assertFalse(dialog.auto_close.GetValue())
            dialog.auto_close.SetValue(visible_close)
            dialog._started = dialog.running = True
            with patch.object(dialog, '_finish_window') as close:
                dialog._finished(False)
                self.assertEqual(close.call_count, int(visible_close))

    def test_hidden_auto_close_waits_for_completion_popup_confirmation(self):
        manager = self.manager()
        dialog = self.progress(DownloadSettings(auto_close_hidden=True), on_closed=manager._closed)
        manager.window = dialog
        dialog._started = dialog.running = dialog.hidden = True
        events = []
        def popup(*args):
            events.append('popup')
            self.assertTrue(dialog._completion_pending)
            self.assertTrue(dialog.busy)
            self.assertFalse(manager.request_exit(Mock()))
            self.manager_messages.assert_not_called()
            self.assertFalse(dialog._disposed)
            return wx.OK
        self.messages.side_effect = popup
        with patch.object(dialog, '_finish_window', side_effect=lambda code: events.append('close')):
            dialog._finished(False)
        self.assertEqual(events, ['popup', 'close'])

    def test_direct_root_clear_still_only_removes_this_batch_files(self):
        from download_queue import CreatedDownload
        dialog = self.progress(DownloadSettings(create_drama_folder=False))
        dialog.request.folder = self.root
        dialog._started = True
        keep = self.root / '之前下载的文件.mp3'
        keep.write_bytes(b'keep')
        created = self.root / '本次下载.mp3'
        created.write_bytes(b'new')
        dialog._created_files[0] = CreatedDownload.capture(created, self.root)
        dialog.tasks[0].state = 'complete'
        dialog._clear_requested = True
        dialog._begin_clear()
        self.pump(lambda: not dialog._clearing)
        self.assertFalse(created.exists())
        self.assertEqual(keep.read_bytes(), b'keep')

    def test_one_sound_switch_controls_both_cues_and_is_silent_for_skips(self):
        for enabled in (False, True):
            self.success.reset_mock()
            self.failure.reset_mock()
            dialog = self.progress(DownloadSettings(play_sound=enabled))
            dialog.running = dialog.hidden = True
            dialog._task_result(0, 'complete', '')
            dialog._task_result(1, 'failed', 'error')
            dialog._task_result(2, 'skipped', 'already exists')
            self.assertEqual(self.success.call_count, int(enabled))
            self.assertEqual(self.failure.call_count, int(enabled))

    def test_download_webview_parent_does_not_hide_with_task_window(self):
        dialog = self.progress()
        dialog.running = True
        playback = PlaybackInfo(1, 'one', 'https://media.invalid/audio')
        with patch('download_dialog._DownloadPlayer') as player:
            dialog._request_key(playback, threading.Event(), {}, dialog.controls[0])
            player.assert_called_once_with(self.parent, '')
            dialog._hide(None)
            player.return_value.shutdown.assert_not_called()
            player.return_value.stop.assert_not_called()
            self.assertTrue(self.parent.IsShown())
            dialog._key_done(error='test ended')
        with patch('browser_player.set_current_app_volume') as volume:
            _DownloadPlayer._queue_system_volume(None, 0)
            volume.assert_not_called()

    def test_exit_no_retains_running_job_and_previous_pause_state(self):
        manager = self.manager()
        dialog = self.progress(on_closed=manager._closed)
        manager.window = dialog
        dialog._started = dialog.running = True
        for paused in (False, True):
            dialog.cancel.pause() if paused else dialog.cancel.resume()
            callback = Mock()
            self.assertFalse(manager.request_exit(callback))
            self.assertFalse(manager.exiting)
            self.assertTrue(dialog.running)
            self.assertFalse(dialog.cancel.is_set())
            self.assertEqual(dialog.cancel.is_paused(), paused)
            callback.assert_not_called()
        self.assertTrue(self.manager_messages.call_args.args[2] & wx.NO_DEFAULT)

    def test_exit_yes_waits_for_worker_and_retains_completed_files(self):
        manager = self.manager()
        dialog = self.progress(on_closed=manager._closed)
        manager.window = dialog
        dialog._started = dialog.running = True
        path = self.root / 'existing.mp3'
        path.write_bytes(b'keep')
        self.manager_messages.return_value = wx.YES
        callback = Mock()
        with patch('download_manager.wx.CallAfter', side_effect=lambda fn: fn()):
            self.assertFalse(manager.request_exit(callback))
            callback.assert_not_called()
            self.assertTrue(dialog.cancel.is_set())
            self.assertTrue(dialog)
            self.assertFalse(manager.request_exit(callback))
            self.manager_messages.assert_called_once()
            dialog._finished(True)
        self.assertIsNone(manager.window)
        callback.assert_called_once()
        self.assertEqual(path.read_bytes(), b'keep')
        self.messages.assert_not_called()

    def test_exit_before_queued_start_prevents_download_start(self):
        manager = self.manager()
        dialog = self.progress(on_closed=manager._closed)
        manager.window = dialog
        self.manager_messages.return_value = wx.YES
        callback = Mock()
        manager.request_exit(callback)
        with patch('download_dialog.threading.Thread') as thread:
            dialog.start()
            thread.assert_not_called()
        wx.Yield()
        callback.assert_called_once()

    def test_exit_confirmation_defers_completion_without_hidden_popup(self):
        manager = self.manager()
        dialog = self.progress(on_closed=manager._closed)
        manager.window = dialog
        dialog._started = dialog.running = dialog.hidden = True
        def confirm(*args):
            dialog._finished(False)
            self.assertTrue(dialog.running)
            return wx.YES
        self.manager_messages.side_effect = confirm
        callback = Mock()
        manager.request_exit(callback)
        wx.Yield()
        self.assertIsNone(manager.window)
        callback.assert_called_once()
        self.messages.assert_not_called()

    def test_exit_waits_for_in_progress_file_cleanup_without_another_popup(self):
        manager = self.manager()
        dialog = self.progress(on_closed=manager._closed)
        manager.window = dialog
        dialog._started = dialog._clearing = dialog._clear_requested = True
        self.manager_messages.return_value = wx.YES
        callback = Mock()
        manager.request_exit(callback)
        callback.assert_not_called()
        dialog._clear_finished([], {})
        wx.Yield()
        callback.assert_called_once()
        self.messages.assert_not_called()

    def test_cleanup_finishing_inside_exit_confirmation_is_deferred(self):
        manager = self.manager()
        dialog = self.progress(on_closed=manager._closed)
        manager.window = dialog
        dialog._started = dialog._clearing = dialog._clear_requested = True
        def confirm(*args):
            dialog._clear_finished([], {})
            self.assertTrue(dialog._clearing)
            self.assertFalse(dialog._disposed)
            self.messages.assert_not_called()
            return wx.YES
        self.manager_messages.side_effect = confirm
        callback = Mock()
        manager.request_exit(callback)
        wx.Yield()
        self.assertIsNone(manager.window)
        callback.assert_called_once()
        self.messages.assert_not_called()

    def test_close_still_confirms_stop_but_hide_does_not(self):
        dialog = self.progress()
        dialog._started = dialog.running = True
        dialog.Show()
        dialog._hide(None)
        self.messages.assert_not_called()
        dialog.restore()
        self.messages.return_value = wx.NO
        dialog._close(None)
        self.messages.assert_called_once()
        self.assertFalse(dialog.cancel.is_set())
        self.assertTrue(dialog.running)

    def test_close_button_only_shown_when_batch_and_cleanup_are_finished(self):
        dialog = self.progress()
        self.assertFalse(dialog.close_button.IsShown())
        self.assertTrue(dialog.hide_button.IsShown())
        dialog._started = dialog.running = True
        for pause in (False, True):
            dialog.cancel.pause() if pause else dialog.cancel.resume()
            dialog._refresh_controls()
            self.assertFalse(dialog.close_button.IsShown())
            self.assertTrue(dialog.hide_button.IsShown())
        dialog.cancel.set()
        dialog._refresh_controls()
        self.assertFalse(dialog.close_button.IsShown())
        dialog.running = False
        dialog._clearing = True
        dialog._refresh_controls()
        self.assertFalse(dialog.close_button.IsShown())
        dialog._clearing = False
        dialog._refresh_controls()
        self.assertTrue(dialog.close_button.IsShown())
        dialog.running = True
        dialog._refresh_controls()
        self.assertFalse(dialog.close_button.IsShown())

    def test_main_download_menu_save_failure_and_exit_guard(self):
        from account_store import AccountState
        from app import MaoerFrame
        self.enterContext(patch('app.load_accounts', return_value=AccountState()))
        self.enterContext(patch('app.load_settings', return_value=AppSettings(read_subtitle=True)))
        self.enterContext(patch.object(MaoerFrame, 'load_homepage'))
        self.enterContext(patch.object(MaoerFrame, '_refresh_output_devices'))
        self.enterContext(patch('app.clear_webview2_profile'))
        frame = MaoerFrame()
        self.addCleanup(self.destroy, frame)
        self.addCleanup(frame.audio_output_router.close)
        self.addCleanup(frame.download_manager.dispose)
        wx.Yield()
        menu = frame.GetMenuBar()
        labels = [menu.GetMenuLabelText(i) for i in range(menu.GetMenuCount())]
        self.assertIn('下载', labels)
        download = menu.GetMenu(labels.index('下载'))
        self.assertEqual([item.GetItemLabel() for item in download.GetMenuItems()],
                         ['下载任务(&T)', '下载设置(&S)…'])
        options = DownloadSettings(str(self.root), play_sound=False)
        with patch('app.DownloadSettingsDialog') as dialog, patch('app.save_settings') as save:
            def show():
                dialog.call_args.args[3](options)
            dialog.return_value.ShowModal.side_effect = show
            frame.on_download_settings(None)
            self.assertTrue(frame.settings.read_subtitle)
            self.assertEqual(frame.settings.downloads, options)
            save.assert_called_once_with(frame.settings)
            save.side_effect = OSError('disk full')
            with self.assertRaises(OSError):
                dialog.call_args.args[3](DownloadSettings())
            self.assertEqual(frame.settings.downloads, options)
        with patch.object(frame.download_manager, 'request_exit', return_value=False), \
                patch.object(frame.browser_player, 'shutdown') as shutdown:
            event = Mock()
            event.CanVeto.return_value = True
            frame.on_close(event)
            event.Veto.assert_called_once()
            event.Skip.assert_not_called()
            shutdown.assert_not_called()

    def test_active_batch_is_retained_when_requesting_another_download(self):
        manager = self.manager()
        dialog = self.progress()
        manager.window = dialog
        dialog._started = dialog.running = dialog.hidden = True
        with patch('download_manager.show_download_dialog') as selector:
            manager.select(Mock(), '', self.root, DownloadSettings())
        selector.assert_not_called()
        self.assertIs(manager.window, dialog)
        self.assertFalse(dialog.hidden)
        self.assertFalse(dialog.cancel.is_set())

    def test_manager_snapshots_settings_and_keeps_nonmodal_window_reference(self):
        manager = self.manager()
        options = DownloadSettings(str(self.root / 'custom'), play_sound=False)
        window = Mock()
        with patch('download_manager.show_download_dialog', return_value=window) as selector:
            manager.select(Mock(), 'fake-cookie', self.root, options)
        self.assertEqual(selector.call_args.args[3], self.root / 'custom')
        self.assertIs(selector.call_args.kwargs['options'], options)
        self.assertIs(manager.window, window)
        self.reader.assert_called_once_with(manager._region, native_only=True)

    def test_download_worker_keeps_running_while_window_hidden_and_playback_controls_used(self):
        dialog = self.progress(DownloadSettings(play_sound=False, auto_close_hidden=False), announce=Mock())
        release, downloading = threading.Event(), threading.Event()
        def download(api, item, folder, control, get_key, progress, **kwargs):
            downloading.set()
            while not release.wait(0.01):
                control.checkpoint()
            control.checkpoint()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (item.title + '.mp3')
            path.write_bytes(b'local test')
            return path
        self.addCleanup(release.set)
        with patch('download_dialog.MaoerApi'), patch('download_dialog.download_audio', side_effect=download):
            dialog.Show()
            dialog.start()
            self.pump(downloading.is_set)
            dialog._hide(None)
            self.assertTrue(self.parent.IsEnabled())
            self.focus.SetValue('可以搜索其他音频')
            self.assertTrue(dialog.running)
            self.assertFalse(dialog.cancel.is_set())
            release.set()
            self.pump(lambda: not dialog.running)
        self.assertEqual([task.state for task in dialog.tasks], ['complete'] * 3)
        self.assertFalse(dialog.IsShown())
        self.messages.assert_called_once()


if __name__ == '__main__':
    unittest.main()
