from __future__ import annotations

import hashlib
import ctypes
import io
import mmap
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import av
import wx

from app import MaoerFrame
from download_dialog import DownloadDialog, _DownloadPlayer
from downloads import (DownloadCancelled, DownloadSelection, download_audio, file_name,
                       folder_name, load_selection, normalize_empty_saiz, remux_audio)
from maoer_api import ApiError, MaoerApi, MediaItem, PlaybackInfo, PurchaseRequired


class DownloadTests(unittest.TestCase):
    def test_completion_sound_plays_for_each_completed_file_before_batch_finishes(self):
        dialog = DownloadDialog.__new__(DownloadDialog)
        dialog.cancel = threading.Event()
        dialog.cookie = ''
        dialog.player = None
        dialog._key_done = Mock()
        dialog._enable_options = Mock()
        dialog.close_button = Mock()
        dialog._status = Mock()
        dialog.close_requested = False
        dialog.results = Mock()
        dialog.list = Mock()
        dialog.path = Mock()
        dialog.publisher = Mock()
        dialog.publisher.GetValue.return_value = False
        for outcomes in ((None,), (None, None, None),
                         (None, ApiError('失败'), FileExistsError(), None, DownloadCancelled())):
            with self.subTest(outcomes=outcomes), tempfile.TemporaryDirectory() as directory:
                dialog.running = False
                dialog.path.GetValue.return_value = directory
                dialog.selection = DownloadSelection('剧名', '',
                    [MediaItem('sound', i, str(i)) for i in range(len(outcomes))], [])
                dialog.list.GetCheckedItems.return_value = tuple(range(len(outcomes)))
                with patch('download_dialog.MaoerApi'), \
                        patch('download_dialog.threading.Thread') as worker, \
                        patch('download_dialog.wx.CallAfter', side_effect=lambda f, *a, **kw: f(*a, **kw)), \
                        patch('download_dialog.play_download_completed_sound') as play, \
                        patch('download_dialog.download_audio') as download:
                    def finish_file(api, item, folder, cancel, get_key, progress):
                        # Earlier files must already have sounded, even if a later file fails.
                        self.assertEqual(play.call_count, sum(value is None for value in outcomes[:item.id]))
                        outcome = outcomes[item.id]
                        if isinstance(outcome, DownloadCancelled):
                            cancel.set()
                        if outcome is not None:
                            raise outcome
                        return folder / (item.title + '.m4a')
                    download.side_effect = finish_file
                    dialog._start(None)
                    worker.call_args.kwargs['target']()
                    self.assertEqual(play.call_count, sum(value is None for value in outcomes))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native accessibility')
    def test_dialog_check_states_names_and_explicit_start(self):
        import comtypes
        from comtypes.client import GetModule
        from ctypes import wintypes

        app = wx.GetApp() or wx.App(False)
        owner = wx.Frame(None)
        selection = DownloadSelection('剧名', '发布者',
                                      [MediaItem('sound', 1, '第一集'), MediaItem('sound', 2, '第二集')], [1])
        dialog = DownloadDialog(owner, selection, '', Path('C:/下载'))
        try:
            accessible_type = GetModule('oleacc.dll').IAccessible
            get_accessible = ctypes.oledll.oleacc.AccessibleObjectFromWindow
            get_accessible.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.POINTER(comtypes.GUID),
                                      ctypes.POINTER(ctypes.POINTER(accessible_type)))
            accessible = ctypes.POINTER(accessible_type)()
            get_accessible(dialog.list.GetHandle(), 0xFFFFFFFC,
                           ctypes.byref(accessible_type._iid_), ctypes.byref(accessible))
            self.assertEqual(accessible.accName[0], '下载音频')
            self.assertEqual(accessible.accName[1], '第一集')
            self.assertEqual(accessible.accName[2], '第二集')
            self.assertFalse(accessible.accState[1] & 0x10)  # STATE_SYSTEM_CHECKED
            self.assertTrue(accessible.accState[2] & 0x10)
            with patch('download_dialog.download_audio') as download:
                dialog.list.SetSelection(0)
                self.assertEqual(dialog.list.GetCheckedItems(), (1,))
                send = ctypes.windll.user32.SendMessageW
                send.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
                send(dialog.list.GetHandle(), 0x0100, 0x20, 0)  # native Space key
                self.assertEqual(dialog.list.GetCheckedItems(), (0, 1))
                dialog._check_all(True)
                self.assertEqual(dialog.list.GetCheckedItems(), (0, 1))
                dialog._check_all(False)
                self.assertEqual(dialog.list.GetCheckedItems(), ())
                dialog.publisher.SetValue(True)
                dialog._update_folder()
                self.assertEqual(dialog.destination.GetLabel(), '剧集文件夹：剧名【发布者】')
                download.assert_not_called()
            self.assertEqual(dialog.path.GetName(), '下载路径')
            self.assertEqual(dialog.results.GetName(), '下载结果')
            frame = MaoerFrame.__new__(MaoerFrame)
            frame.api = Mock(cookie_header='')
            frame.list = Mock()
            frame.show_download = Mock()
            for kind, action in (('sound', '下载本集'), ('sound', '下载该剧集'), ('drama', '下载该剧集')):
                item = MediaItem(kind, 1, '名称')
                def choose(menu, position):
                    entries = menu.GetMenuItems()
                    names = [entry.GetItemLabelText().split('(')[0] for entry in entries]
                    self.assertIn('下载该剧集', names)
                    self.assertEqual('下载本集' in names, kind == 'sound')
                    return next(entry.GetId() for entry, name in zip(entries, names) if name == action)
                frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
                frame._display_item_menu(wx.Point(0, 0), item, None)
                if action == '下载本集':
                    frame.show_download.assert_called_with(item)
                else:
                    frame.show_download.assert_called_with(item, whole_drama=True)
            # Download component must never touch app-wide Core Audio volume.
            silent = _DownloadPlayer.__new__(_DownloadPlayer)
            with patch('browser_player.set_current_app_volume') as volume:
                silent._queue_system_volume(0)
            volume.assert_not_called()
        finally:
            dialog.reader.close()
            dialog.Destroy()
            owner.Destroy()

    def test_catalog_names_and_defaults_keep_all_groups_and_vip_metadata(self):
        api = Mock()
        drama = MediaItem('drama', 1, '官网剧名')
        current = MediaItem('sound', 3, '列表中的第二集', drama_id=1, raw={'vip': 2})
        episodes = [MediaItem('sound', 2, '预告'), current, MediaItem('sound', 4, '花絮')]
        api.drama_for_sound.return_value = drama
        api.drama_episodes.return_value = episodes
        api.publisher_name_for_item.return_value = '发布者'
        search_sound = MediaItem('sound', 3, '搜索中的长标题')
        single = load_selection(api, search_sound, False)
        batch = load_selection(api, search_sound, True)
        all_items = load_selection(api, drama, True)
        self.assertEqual(single.items, [current])
        self.assertEqual(single.checked, [0])
        self.assertEqual(batch.checked, [1])
        self.assertEqual(batch.items, episodes)
        self.assertEqual(all_items.checked, [0, 1, 2])
        self.assertEqual(single.items[0].raw['vip'], 2)
        self.assertEqual(folder_name(single, False), '官网剧名')
        self.assertEqual(folder_name(single, True), '官网剧名【发布者】')
        self.assertEqual(file_name('第01集·国子监'), '第01集·国子监')
        self.assertEqual(file_name('../a:b?'), '..／a：b？')
        self.assertEqual(file_name('CON.txt'), '_CON.txt')
        self.assertEqual(file_name('末尾. '), '末尾．　')
        api.drama_for_sound.side_effect = ApiError('该音频没有关联的剧集')
        self.assertEqual(load_selection(api, search_sound, False).title, search_sound.title)
        with self.assertRaises(ApiError):
            load_selection(api, search_sound, True)

    def test_download_shortcuts_route_by_focused_item_without_playing(self):
        for kind in ('sound', 'drama', 'category'):
            frame = MaoerFrame.__new__(MaoerFrame)
            item = MediaItem(kind, 1, '名称')
            frame.list = Mock()
            frame.items = [item]
            frame._selected_index = Mock(return_value=0)
            frame.open_item = Mock()
            frame.show_download = Mock()
            with patch.object(MaoerFrame, 'FindFocus', return_value=frame.list):
                for key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                    for modifiers in (wx.MOD_CONTROL, wx.MOD_CONTROL | wx.MOD_SHIFT):
                        event = Mock()
                        event.GetKeyCode.return_value = key
                        event.GetModifiers.return_value = modifiers
                        frame.on_char_hook(event)
                        event.Skip.assert_called_once()
                frame.on_item_download_shortcut(None)
                frame.on_drama_download_shortcut(None)
            if kind == 'category':
                frame.show_download.assert_not_called()
            else:
                self.assertEqual([call.kwargs for call in frame.show_download.call_args_list],
                                 [{'whole_drama': kind == 'drama'}, {'whole_drama': True}])
            frame.open_item.assert_not_called()
            frame.show_download.reset_mock()
            with patch.object(MaoerFrame, 'FindFocus', return_value=Mock()):
                frame.on_item_download_shortcut(None)
                frame.on_drama_download_shortcut(None)
            frame.show_download.assert_not_called()

    def test_complete_transfer_no_cookie_forwarding_no_overwrite_and_cancel_cleanup(self):
        item = MediaItem('sound', 1, '第一集')
        api = Mock()
        api.playback_info.return_value = PlaybackInfo(1, '不要使用播放器长标题', 'https://media.invalid/sound.mp3')
        response = Mock(status_code=200, headers={'Content-Length': '6'})
        response.iter_content.return_value = iter([b'abc', b'def'])
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.get.return_value = response
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as directory, patch('downloads.MaoerApi') as anonymous, \
                patch('downloads.av.open') as media:
            media.return_value.__enter__.return_value.decode.return_value = iter([object()])
            anonymous.return_value.session = session
            folder = Path(directory) / '剧名'
            result = download_audio(api, item, folder, cancel, Mock(), Mock())
            self.assertEqual(result.name, '第一集.mp3')
            self.assertEqual(result.read_bytes(), b'abcdef')
            anonymous.assert_called_once_with(cookie='')
            with self.assertRaises(FileExistsError):
                download_audio(api, item, folder, cancel, Mock(), Mock())
            self.assertEqual(session.get.call_count, 1)
            item.title = '第二集'
            response.iter_content.return_value = iter([b'abc'])
            with self.assertRaises(ApiError):
                download_audio(api, item, folder, cancel, Mock(), Mock())
            self.assertEqual([p.name for p in folder.iterdir()], ['第一集.mp3'])
            response.iter_content.return_value = iter([b'abc', b'def'])
            with self.assertRaises(DownloadCancelled):
                download_audio(api, item, folder, cancel, Mock(),
                               lambda stage, percent: cancel.set() if stage == '正在下载' else None)
            self.assertEqual([p.name for p in folder.iterdir()], ['第一集.mp3'])
            cancel.clear()
            api.playback_info.side_effect = PurchaseRequired('需要购买')
            with self.assertRaises(PurchaseRequired):
                download_audio(api, item, folder, cancel, Mock(), Mock())

    def test_native_video_download_keeps_original_mp4_despite_unused_encrypted_dash(self):
        api = MaoerApi(cookie='')
        video_url = 'https://media.invalid/extras.mp4'
        sound = {'soundurl': 'https://media.invalid/drm/audio.m3u8',
                 'videourl': video_url, 'need_pay': 0,
                 'has_video': False, 'video_transcode_ready': False,
                 'dash': {'audio': [{'base_url': 'https://media.invalid/audio.m4s',
                                     'bilidrm_uri': 'uri:bili://unused-key-id', 'size': 1}]}}
        api._get = Mock(return_value={'info': {'sound': sound}})
        payload = b'original MP4 bytes, including video and audio'
        get_key = Mock(side_effect=AssertionError('Native video must not wait for a DASH key'))
        item = MediaItem('sound', 1, '花絮')
        with tempfile.TemporaryDirectory() as directory, patch('downloads.MaoerApi') as anonymous, \
                patch('downloads.av.open') as media, patch('downloads.remux_audio') as remux:
            session = anonymous.return_value.session.__enter__.return_value
            response = session.get.return_value.__enter__.return_value
            response.status_code = 200
            response.headers = {'Content-Length': str(len(payload))}
            response.iter_content.return_value = [payload]
            media.return_value.__enter__.return_value.decode.return_value = iter([object()])
            saved = download_audio(api, item, Path(directory), threading.Event(), get_key, Mock())
            self.assertEqual(saved.name, '花絮.mp4')
            self.assertEqual(saved.read_bytes(), payload)
            anonymous.assert_called_once_with(cookie='')
            self.assertEqual(session.get.call_args.args[0], video_url)
            get_key.assert_not_called()
            remux.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [saved])
            # A video URL never bypasses the existing account permission check.
            sound['need_pay'] = 1
            with self.assertRaises(PurchaseRequired):
                download_audio(api, item, Path(directory), threading.Event(), get_key, Mock())
            self.assertEqual(session.get.call_count, 1)
        api.session.close()

    def test_only_valid_empty_cbcs_metadata_is_normalized(self):
        def box(name, data):
            return struct.pack('>I4s', len(data) + 8, name) + data
        tenc = bytearray(41)
        tenc[0] = 1
        tenc[5:8] = bytes([0, 1, 0])
        tenc[24] = 16
        init = box(b'moov', box(b'schm', b'cbcs') + box(b'tenc', tenc))
        senc = box(b'senc', bytes(4) + struct.pack('>I', 10))
        saiz = box(b'saiz', bytes(9))
        fragment = box(b'moof', box(b'traf', senc + saiz))
        data = init + fragment + box(b'mdat', b'original-packets')
        with tempfile.TemporaryFile() as file:
            file.write(data)
            file.flush()
            with mmap.mmap(file.fileno(), 0) as mapped:
                self.assertEqual(normalize_empty_saiz(mapped), 1)
                self.assertEqual(mapped[:], data.replace(b'saiz', b'free'))
                mapped[:] = data.replace(b'cbcs', b'cenc')
                with self.assertRaises(ApiError):
                    normalize_empty_saiz(mapped)
                mapped[:] = data
                mapped[0:4] = struct.pack('>I', len(data) + 100)
                with self.assertRaises(ApiError):
                    normalize_empty_saiz(mapped)

    def test_remux_preserves_encoded_aac_packets(self):
        # Generate a small local source; tests never fetch an account or media URL.
        memory = io.BytesIO()
        with av.open(memory, 'w', format='mp4') as source:
            stream = source.add_stream('aac', rate=48000)
            stream.layout = 'mono'
            for index in range(5):
                frame = av.AudioFrame(format='fltp', layout='mono', samples=1024)
                frame.sample_rate = 48000
                frame.pts = index * 1024
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                source.mux(stream.encode(frame))
            source.mux(stream.encode(None))
        payload = memory.getvalue()
        def fingerprint(file):
            result = hashlib.sha256()
            with av.open(file) as audio:
                for packet in audio.demux(audio=0):
                    if packet.pts is not None:
                        result.update(bytes(packet))
            return result.digest()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / '第一集.m4a'
            remux_audio(io.BytesIO(payload), output, None, threading.Event(), None)
            self.assertEqual(fingerprint(io.BytesIO(payload)), fingerprint(str(output)))
            self.assertNotIn(b'pssh', output.read_bytes())


if __name__ == '__main__':
    unittest.main()
