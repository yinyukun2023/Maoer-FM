from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import wx

from app_settings import AppSettings, DownloadSettings, load_settings, save_settings
from download_dialog import DownloadDialog, DownloadProgressDialog, DownloadRequest
from download_manager import DownloadManager
from download_settings import DownloadSettingsDialog
from downloads import DownloadControl, DownloadSelection, download_audio, load_selection
from maoer_api import ApiError, MediaItem, PlaybackInfo


class DownloadRefinementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent = wx.Frame(None)
        self.addCleanup(self.parent.Destroy)
        self.items = [MediaItem('sound', 1, '第1集'), MediaItem('sound', 2, '第2集')]
        self.enterContext(patch('download_dialog.play_download_completed_sound'))
        self.enterContext(patch('download_dialog.play_download_failed_sound'))
        self.enterContext(patch('download_dialog.message_box', return_value=wx.OK))
        self.enterContext(patch('requests.Session.request', side_effect=AssertionError('No real network')))

    def progress(self, options=None, **kwargs):
        request = DownloadRequest('作品', self.items, self.root, '',
                                  options=options or DownloadSettings(auto_close_hidden=False))
        window = DownloadProgressDialog(self.parent, request, **kwargs)
        self.addCleanup(lambda: window.Destroy() if window and not window.IsBeingDeleted() else None)
        self.addCleanup(window.timer.Stop)
        window._started = window.running = True
        return window

    def test_hidden_each_completed_episode_announces_before_batch_finishes(self):
        announce = Mock()
        window = self.progress(announce=announce)
        window._hide(None)
        window._task_result(0, 'complete', '')
        self.assertEqual(window.tasks[1].state, 'pending')
        self.assertTrue(window.running)
        announce.assert_called_once_with('第1集下载完成')

    def test_each_success_speaks_once_without_waiting_for_completion_popup(self):
        announce = Mock()
        window = self.progress(DownloadSettings(play_sound=False), announce=announce)
        window._hide(None)
        for index in (0, 0, 1, 1):
            window._task_result(index, 'complete', '')
        self.assertEqual(announce.call_args_list, [call('第1集下载完成'), call('第2集下载完成')])

    def test_disabled_skipped_cancelled_progress_and_exit_do_not_speak(self):
        for hidden, enabled, state, exiting, clearing in (
                (False, False, 'complete', False, False), (True, False, 'complete', False, False),
                (False, False, 'failed', False, False), (True, False, 'failed', False, False),
                (True, True, 'skipped', False, False),
                (True, True, 'cancelled', False, False), (True, True, 'complete', True, False),
                (True, True, 'complete', False, True), (False, True, 'failed', True, False),
                (False, True, 'failed', False, True)):
            with self.subTest(hidden=hidden, enabled=enabled, state=state, exiting=exiting, clearing=clearing):
                announce = Mock()
                window = self.progress(DownloadSettings(announce_hidden_complete=enabled), announce=announce)
                window.hidden, window._exiting, window._clear_requested = hidden, exiting, clearing
                window._task_progress(0, '正在下载', 50)
                window._task_result(0, state, '')
                announce.assert_not_called()

    def test_visible_and_hidden_success_and_failure_announce_once_with_title_first(self):
        for hidden in (False, True):
            announce = Mock()
            window = self.progress(announce=announce)
            window.hidden = hidden
            window._task_result(0, 'complete', '')
            window._task_result(1, 'failed', '网络连接中断')
            window._task_result(0, 'complete', '')
            window._task_result(1, 'failed', '网络连接中断')
            self.assertEqual(announce.call_args_list, [call('第1集下载完成'), call('第2集下载失败：网络连接中断')])

    def test_failure_without_reason_still_announces_without_empty_separator(self):
        announce = Mock()
        window = self.progress(announce=announce)
        window._task_result(0, 'failed', '')
        announce.assert_called_once_with('第1集下载失败')

    def test_download_settings_omit_static_explanation_and_keep_path_accessible_name(self):
        dialog = DownloadSettingsDialog(self.parent, DownloadSettings(announce_hidden_complete=False),
                                        self.root, Mock())
        self.addCleanup(dialog.Destroy)
        labels = [control.GetLabel() for control in dialog.GetChildren() if isinstance(control, wx.StaticText)]
        self.assertEqual(labels, ['默认下载路径(&L)：'])
        self.assertEqual(dialog.path.GetName(), '默认下载路径')
        checkbox = dialog.options['announce_hidden_complete']
        self.assertEqual(checkbox.GetLabel(), '每个音频下载成功或失败时通过活动区域朗读(&R)')
        self.assertFalse(checkbox.GetValue())

    def test_escape_default_is_on_and_saved_false_survives_reload(self):
        with patch('app_settings.app_data_dir', return_value=self.root):
            self.assertTrue(load_settings().downloads.escape_hides)
            save_settings(AppSettings(downloads=DownloadSettings(escape_hides=False)))
            self.assertFalse(load_settings().downloads.escape_hides)
            save_settings(AppSettings(downloads=DownloadSettings()))
            self.assertTrue(load_settings().downloads.escape_hides)

    def test_default_settings_labels_save_escape_and_explain_each_episode(self):
        save = Mock()
        dialog = DownloadSettingsDialog(self.parent, DownloadSettings(), self.root, save)
        self.addCleanup(dialog.Destroy)
        self.assertTrue(dialog.options['escape_hides'].GetValue())
        self.assertIn('每个音频', dialog.options['announce_hidden_complete'].GetLabel())
        dialog.options['escape_hides'].SetValue(False)
        with patch.object(dialog, 'EndModal'):
            dialog._save(None)
        self.assertFalse(save.call_args.args[0].escape_hides)

    def test_download_settings_has_one_sound_switch_and_no_visible_auto_close_default(self):
        dialog = DownloadSettingsDialog(self.parent, DownloadSettings(), self.root, Mock())
        self.addCleanup(dialog.Destroy)
        self.assertEqual(dialog.GetTitle(), '下载设置')
        self.assertEqual(dialog.options['play_sound'].GetLabel(), '播放下载提示音(&S)')
        self.assertTrue(dialog.options['play_sound'].GetValue())
        self.assertTrue(dialog.options['auto_close_hidden'].GetValue())
        for removed in ('completed_sound', 'failed_sound', 'auto_close'):
            self.assertNotIn(removed, dialog.options)
        window = self.selector()
        self.assertEqual(window.by_list_order.GetLabel(), '音频文件名按发布顺序数字编号命名(&O)')

    def test_old_sound_settings_migrate_without_turning_explicit_opt_outs_back_on(self):
        with patch('app_settings.app_data_dir', return_value=self.root):
            for completed, failed in ((True, True), (True, False), (False, True), (False, False)):
                raw = {'downloads': {'completed_sound': completed, 'failed_sound': failed,
                                     'auto_close': True, 'auto_close_hidden': False}}
                (self.root / 'settings.json').write_text(json.dumps(raw), encoding='utf-8')
                options = load_settings().downloads
                self.assertEqual(options.play_sound, completed and failed)
                self.assertFalse(options.auto_close_hidden)
                save_settings(AppSettings(downloads=options))
                stored = json.loads((self.root / 'settings.json').read_text(encoding='utf-8'))['downloads']
                self.assertEqual(stored['play_sound'], completed and failed)
                self.assertFalse(any(name in stored for name in ('completed_sound', 'failed_sound', 'auto_close')))
                window = self.progress(options)
                self.assertFalse(window.auto_close.GetValue())

    def test_new_sound_setting_takes_precedence_and_hidden_default_preserves_user_choice(self):
        with patch('app_settings.app_data_dir', return_value=self.root):
            self.assertTrue(load_settings().downloads.auto_close_hidden)
            for value in (True, False):
                raw = {'downloads': {'play_sound': value, 'completed_sound': not value,
                                     'failed_sound': not value, 'auto_close_hidden': value}}
                (self.root / 'settings.json').write_text(json.dumps(raw), encoding='utf-8')
                self.assertEqual(load_settings().downloads.play_sound, value)
                self.assertEqual(load_settings().downloads.auto_close_hidden, value)

    def test_default_hidden_completion_closes_only_after_popup(self):
        window = self.progress(DownloadSettings())
        window._hide(None)
        events = []
        with patch('download_dialog.message_box', side_effect=lambda *args: events.append('popup') or wx.OK), \
                patch.object(window, '_finish_window', side_effect=lambda *args: events.append('close')):
            window._finished(False)
        self.assertEqual(events, ['popup', 'close'])

    def test_results_update_native_live_region_with_title_first_and_never_use_tolk(self):
        from uia_live_region import EVENT_OBJECT_LIVEREGIONCHANGED, OBJID_CLIENT_LONG, CHILDID_SELF
        with patch('uia_live_region.TolkBridge') as tolk:
            manager = DownloadManager(self.parent, Mock())
            self.addCleanup(manager.dispose)
            window = self.progress(announce=manager._reader.announce)
            window.tasks[0].item.title = '正式预告'
            events = Mock()
            manager._reader._live_region_ready = True
            with patch('uia_live_region._automation_objects', return_value={'user32': events}):
                window._task_result(0, 'complete', '')
                window._task_result(0, 'complete', '')
            self.assertEqual(manager._region.GetLabel(), '正式预告下载完成')
            self.assertEqual(manager._region.GetName(), '正式预告下载完成')
            events.NotifyWinEvent.assert_called_once_with(
                EVENT_OBJECT_LIVEREGIONCHANGED, manager._region.GetHandle(), OBJID_CLIENT_LONG, CHILDID_SELF)
            events.reset_mock()
            with patch('uia_live_region._automation_objects', return_value={'user32': events}):
                window._task_result(1, 'failed', '无法连接服务器')
            self.assertEqual(manager._region.GetLabel(), '第2集下载失败：无法连接服务器')
            self.assertEqual(manager._region.GetName(), '第2集下载失败：无法连接服务器')
            events.NotifyWinEvent.assert_called_once_with(
                EVENT_OBJECT_LIVEREGIONCHANGED, manager._region.GetHandle(), OBJID_CLIENT_LONG, CHILDID_SELF)
            tolk.assert_not_called()

    @staticmethod
    def event(source, key=ord('X'), modifiers=wx.MOD_CONTROL | wx.MOD_SHIFT, repeat=False):
        event = Mock()
        event.GetEventType.return_value = wx.EVT_CHAR_HOOK.typeId
        event.GetEventObject.return_value = source
        event.GetKeyCode.return_value = key
        event.GetModifiers.return_value = modifiers
        event.IsAutoRepeat.return_value = repeat
        return event

    def manager(self):
        reader = self.enterContext(patch('download_manager.ScreenReaderAnnouncer'))
        focus = Mock()
        manager = DownloadManager(self.parent, focus)
        self.addCleanup(manager.dispose)
        self.assertTrue(reader.call_args.kwargs['native_only'])
        return manager

    def test_escape_hides_only_task_window_when_enabled(self):
        for enabled in (True, False):
            window = self.progress(DownloadSettings(escape_hides=enabled))
            window.Show()
            event = self.event(window, wx.WXK_ESCAPE, wx.MOD_NONE)
            with patch.object(window, '_cancel_download') as cancel:
                window._on_window_key(event)
                self.assertEqual(cancel.call_count, int(not enabled))
            self.assertEqual(window.hidden, enabled)
            event.Skip.assert_not_called()
            self.assertTrue(window.running)
            self.assertFalse(window.cancel.is_set())

    def test_titlebar_and_cancel_button_keep_stop_confirmation(self):
        window = self.progress()
        for event in (wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW), wx.CommandEvent(wx.wxEVT_BUTTON, wx.ID_CANCEL)):
            with patch.object(window, '_cancel_download') as cancel:
                window._close(event)
                cancel.assert_called_once_with(True)
                self.assertFalse(window.hidden)

    def test_toggle_from_main_playback_and_task_keeps_same_window_selection_and_queue(self):
        manager = self.manager()
        window = self.progress(on_hidden=manager.focus_main)
        manager.window = window
        player = wx.Frame(self.parent)
        self.addCleanup(player.Destroy)
        self.parent.player_frame = player
        control = wx.TextCtrl(self.parent)
        window.Show()
        window.pending_list.Select(0, False)
        window.pending_list.Select(1)
        for source in (window.pending_list, control, player, window.hide_button):
            was_hidden = window.hidden
            self.assertEqual(manager._keys.FilterEvent(self.event(source)), wx.EventFilter.Event_Processed)
            self.assertEqual(window.hidden, not was_hidden)
            self.assertIs(manager.window, window)
            self.assertTrue(window.running)
            self.assertFalse(window.cancel.is_set())
            self.assertEqual(window.pending_list.GetFirstSelected(), 1)
        self.assertNotIn('&H', window.hide_button.GetLabel())
        self.assertIn('Ctrl+Shift+X', window.hide_button.GetLabel())

    def test_visibility_key_ignores_other_modifiers_dialogs_foreign_windows_and_repeat(self):
        manager = self.manager()
        window = self.progress()
        manager.window = window
        settings = DownloadSettingsDialog(self.parent, DownloadSettings(), self.root, Mock())
        self.addCleanup(settings.Destroy)
        other = wx.Frame(None)
        self.addCleanup(other.Destroy)
        for source in (settings, settings.path, other):
            self.assertEqual(manager._keys.FilterEvent(self.event(source)), wx.EventFilter.Event_Skip)
        for modifiers in (wx.MOD_NONE, wx.MOD_CONTROL, wx.MOD_ALT, wx.MOD_SHIFT,
                          wx.MOD_CONTROL | wx.MOD_SHIFT | wx.MOD_ALT):
            self.assertEqual(manager._keys.FilterEvent(self.event(window, modifiers=modifiers)), wx.EventFilter.Event_Skip)
        with patch.object(manager, 'toggle_tasks') as toggle:
            self.assertEqual(manager._keys.FilterEvent(self.event(window, repeat=True)), wx.EventFilter.Event_Processed)
            toggle.assert_not_called()
        self.assertFalse(window.hidden)

    def test_visibility_key_does_not_fall_through_to_speed_key_without_a_task(self):
        manager = self.manager()
        self.assertIsNone(manager.window)
        self.assertEqual(manager._keys.FilterEvent(self.event(self.parent)), wx.EventFilter.Event_Processed)
        self.assertIsNone(manager.window)

    def test_visibility_key_cannot_hide_during_confirmation_or_completion(self):
        manager = self.manager()
        window = self.progress()
        manager.window = window
        for flag in ('_confirming_cancel', '_completion_pending'):
            setattr(window, flag, True)
            manager.toggle_tasks()
            self.assertFalse(window.hidden)
            setattr(window, flag, False)

    def test_dispose_removes_shortcut_filter_once(self):
        manager = self.manager()
        with patch('download_manager.wx.EvtHandler.RemoveFilter', wraps=wx.EvtHandler.RemoveFilter) as remove:
            manager.dispose()
            manager.dispose()
            remove.assert_called_once()

    def selector(self, items=None, checked=None, width=2):
        selection = DownloadSelection('作品', '发布者', items or self.items, checked or [1], width)
        dialog = DownloadDialog(self.parent, selection, '', self.root)
        self.addCleanup(dialog.Destroy)
        return dialog

    def test_list_prefix_default_off_and_subset_keeps_original_positions(self):
        window = self.selector()
        self.assertFalse(window.by_list_order.GetValue())
        with patch.object(window, 'EndModal'):
            window._start(None)
            self.assertEqual(window.request.filename_prefixes, {})
            window.by_list_order.SetValue(True)
            window._start(None)
        self.assertEqual(window.request.filename_prefixes, {2: '02 '})
        self.assertEqual(window.request.items, [self.items[1]])
        self.assertEqual(self.items[1].title, '第2集')

    def test_single_download_keeps_its_complete_drama_position_and_width(self):
        api = Mock()
        api.drama_for_sound.return_value = MediaItem('drama', 90, '整部作品')
        api.publisher_name_for_item.return_value = '发布者'
        for count in (3, 100):
            with self.subTest(count=count):
                episodes = [MediaItem('sound', 2000 - i, f'声音{i}') for i in range(count)]
                api.drama_episodes.reset_mock()
                api.drama_episodes.return_value = episodes
                current = episodes[2]
                selection = load_selection(api, current, False)
                api.drama_episodes.assert_called_once_with(90)
                self.assertEqual(selection.items, [current])
                dialog = DownloadDialog(self.parent, selection, '', self.root)
                self.addCleanup(dialog.Destroy)
                dialog.by_list_order.SetValue(True)
                with patch.object(dialog, 'EndModal'):
                    dialog._start(None)
                self.assertEqual(dialog.request.filename_prefixes,
                                 {current.id: '003 ' if count >= 100 else '03 '})

    def test_batch_and_single_use_same_full_drama_prefix_even_with_bonus_tracks(self):
        api = Mock()
        api.drama_for_sound.return_value = MediaItem('drama', 90, '整部作品')
        api.publisher_name_for_item.return_value = '发布者'
        episodes = [MediaItem('sound', 4, '预告'), MediaItem('sound', 1, '第一期'),
                    MediaItem('sound', 3, '花絮', subtitle='作品 / 花絮')]
        api.drama_episodes.return_value = episodes
        prefixes = []
        for whole in (True, False):
            api.drama_episodes.reset_mock()
            selection = load_selection(api, episodes[2], whole)
            window = DownloadDialog(self.parent, selection, '', self.root)
            self.addCleanup(window.Destroy)
            window.by_list_order.SetValue(True)
            with patch.object(window, 'EndModal'):
                window._start(None)
            prefixes.append(window.request.filename_prefixes)
            api.drama_episodes.assert_called_once_with(90)
        self.assertEqual(prefixes, [{3: '03 '}, {3: '03 '}])

    def test_standalone_sound_number_starts_at_one_without_fake_drama_fetch(self):
        api = Mock()
        api.drama_for_sound.side_effect = ApiError('该音频没有关联的剧集')
        api.publisher_name_for_item.return_value = '发布者'
        selection = load_selection(api, self.items[0], False)
        window = DownloadDialog(self.parent, selection, '', self.root)
        self.addCleanup(window.Destroy)
        window.by_list_order.SetValue(True)
        with patch.object(window, 'EndModal'):
            window._start(None)
        self.assertEqual(window.request.filename_prefixes, {1: '01 '})
        api.drama_episodes.assert_not_called()

    def test_prefixes_follow_list_not_ids_and_use_whole_list_width(self):
        for count, expected in ((99, '02 '), (100, '002 '), (1000, '0002 ')):
            items = [MediaItem('sound', 2000 - i, f'音频{i}') for i in range(count)]
            window = self.selector(items=items)
            window.by_list_order.SetValue(True)
            with patch.object(window, 'EndModal'):
                window._start(None)
            self.assertEqual(window.request.filename_prefixes, {items[1].id: expected})

    def test_filename_prefix_combines_with_numbered_title_and_never_overwrites(self):
        item = MediaItem('sound', 1, '第一期', subtitle='正剧')
        api = Mock()
        api.playback_info.return_value = PlaybackInfo(1, item.title, 'https://example.test/media.mp3')
        response = Mock(status_code=200, headers={'Content-Length': '7'})
        response.iter_content.return_value = [b'payload']
        with patch('downloads.MaoerApi') as anonymous, patch('downloads.av.open') as media:
            anonymous.return_value.session.__enter__.return_value.get.return_value.__enter__.return_value = response
            media.return_value.__enter__.return_value.decode.return_value = iter([object()])
            output = download_audio(api, item, self.root, DownloadControl(), Mock(), Mock(),
                                    number_width=2, filename_prefix='03 ')
            self.assertEqual(output.name, '03 第01期.mp3')
            self.assertEqual(output.read_bytes(), b'payload')
            with self.assertRaises(FileExistsError):
                download_audio(api, item, self.root, DownloadControl(), Mock(), Mock(),
                               number_width=2, filename_prefix='03 ')
            response.iter_content.assert_called_once()
        self.assertEqual(item.title, '第一期')
        self.assertEqual(list(self.root.iterdir()), [output])

    def test_worker_passes_fixed_prefixes_to_original_download_implementation(self):
        announce = Mock()
        window = self.progress(announce=announce)
        window._hide(None)
        window.request.filename_prefixes = {1: '04 ', 2: '09 '}
        queued = []
        with patch('download_dialog.MaoerApi'), patch('download_dialog.download_audio') as download, \
                patch('download_dialog.wx.CallAfter', side_effect=lambda fn, *args: queued.append((fn, args))):
            def create(api, item, folder, *args, **kwargs):
                target = folder / (kwargs['filename_prefix'] + item.title + '.mp3')
                target.write_bytes(b'download')
                return target
            download.side_effect = create
            window._run()
            for callback, args in queued:
                callback(*args)
        self.assertEqual([c.kwargs for c in download.call_args_list], [
            {'number_width': None, 'filename_prefix': '04 '},
            {'number_width': None, 'filename_prefix': '09 '},
        ])
        self.assertEqual([task.item.title for task in window.tasks], ['第1集', '第2集'])
        self.assertEqual(announce.call_args_list[:2], [call('第1集下载完成'), call('第2集下载完成')])
        self.assertIn('恭喜', announce.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
