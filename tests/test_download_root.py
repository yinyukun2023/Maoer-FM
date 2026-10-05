from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import wx

from download_dialog import DownloadDialog, DownloadProgressDialog, DownloadRequest
from downloads import DownloadSelection
from maoer_api import MediaItem


class DownloadRootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = wx.GetApp() or wx.App(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.selection = DownloadSelection('作品', '发布者', [MediaItem('sound', 1, '第一集')], [0])

    def check_open_root(self, selector, expected_root, folder_name):
        self.addCleanup(selector.Destroy)
        with patch.object(selector, 'EndModal'):
            selector._start(None)
        self.assertEqual(selector.request.folder, expected_root / folder_name,
                         'The real audio destination must still be the drama folder')
        progress = DownloadProgressDialog(None, selector.request)
        self.addCleanup(progress.Destroy)
        self.addCleanup(progress.timer.Stop)
        with patch('download_dialog.os.startfile') as open_folder:
            progress._open_folder(None)
        open_folder.assert_called_once_with(str(expected_root))
        self.assertTrue(expected_root.is_dir())
        self.assertFalse((expected_root / folder_name).exists(),
                         'Opening the root must not create a drama folder or start a download')

    def test_default_download_root_is_opened_not_drama_folder(self):
        root = self.root / '默认下载'
        self.check_open_root(DownloadDialog(None, self.selection, '', root), root, '作品')

    def test_browsed_directory_is_kept_separate_from_drama_and_publisher_folder(self):
        selector = DownloadDialog(None, self.selection, '', self.root / '默认下载')
        chosen = self.root / '浏览选择的目录'
        with patch('download_dialog.wx.DirDialog') as directory_dialog:
            directory_dialog.return_value.__enter__.return_value.ShowModal.return_value = wx.ID_OK
            directory_dialog.return_value.__enter__.return_value.GetPath.return_value = str(chosen)
            selector._browse(None)
        selector.publisher.SetValue(True)
        self.check_open_root(selector, chosen, '作品【发布者】')

    def test_manually_edited_path_is_the_open_target(self):
        selector = DownloadDialog(None, self.selection, '', self.root / '默认下载')
        chosen = self.root / '自己输入的目录'
        selector.path.SetValue(str(chosen))
        self.check_open_root(selector, chosen, '作品')

    def test_request_without_explicit_root_opens_drama_parent(self):
        chosen = self.root / '下载根目录'
        request = DownloadRequest('作品', self.selection.items, chosen / '作品', '')
        progress = DownloadProgressDialog(None, request)
        self.addCleanup(progress.Destroy)
        self.addCleanup(progress.timer.Stop)
        with patch('download_dialog.os.startfile') as open_folder:
            progress._open_folder(None)
        open_folder.assert_called_once_with(str(chosen))

    def test_confirmed_labels_keep_hotkeys_and_include_current_without_changing_prior_rows(self):
        items = [MediaItem('sound', index, f'第{index}集') for index in range(3)]
        selector = DownloadDialog(None, DownloadSelection('作品', '', items, []), '', self.root)
        self.addCleanup(selector.Destroy)
        self.assertEqual(selector.from_button.GetLabel(), '选择当前集至列表末尾(&S)')
        self.assertEqual(selector.uncheck_from_button.GetLabel(), '取消勾选当前集至列表末尾(&E)')
        selector.list.SetSelection(1)
        selector._check_from_current(True)
        self.assertEqual(selector.list.GetCheckedItems(), (1, 2))
        selector.list.Check(0, True)
        selector._check_from_current(False)
        self.assertEqual(selector.list.GetCheckedItems(), (0,))
        self.assertEqual(selector.list.GetSelection(), 1)


if __name__ == '__main__':
    unittest.main()
