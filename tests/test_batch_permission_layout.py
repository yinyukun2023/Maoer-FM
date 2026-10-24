import unittest
from unittest.mock import patch

from maoer_api import MaoerApi, MediaItem
from download_dialog import DownloadPartResult
from tests.test_download_partial_results import PartialDownloadTests


class PlaybackPermissionRequestTests(unittest.TestCase):
    def test_fresh_free_sound_never_scans_full_account_purchases(self):
        api=MaoerApi(cookie='test=keep')
        self.addCleanup(api.session.close)
        with patch.object(api,'_get',return_value={'info':{'sound':{'need_pay':0,'soundurl':'https://example.test/a.mp3'}}}), patch.object(api,'_purchased_full_drama_ids',side_effect=AssertionError('free sound must not fetch purchase history')):
            for index in range(31):
                self.assertTrue(api.playback_info(MediaItem('sound',index+1,'集',drama_id=22919)).url)

    def test_already_purchased_single_sound_does_not_scan_drama_purchases(self):
        api=MaoerApi(cookie='test=keep')
        self.addCleanup(api.session.close)
        with patch.object(api,'_get',return_value={'info':{'sound':{'need_pay':1,'soundurl':'https://example.test/a.mp3'}}}), patch.object(api,'_purchased_full_drama_ids',side_effect=AssertionError('purchased single sound must not scan full dramas')):
            self.assertTrue(api.playback_info(MediaItem('sound',1,'集',drama_id=22919,raw={'_purchased_sound':True})).url)


class FailureListGeometryTests(PartialDownloadTests):
    def test_both_failure_lists_have_space_to_read_and_retry_rows(self):
        window=self.window()
        window.SetSize((808,658))
        task=window.tasks[0]
        task.state='failed'
        task.parts={kind:DownloadPartResult('failed',kind+' failed') for kind in ('audio','subtitle')}
        window._refresh_lists()
        window.Layout()
        for control in (window.audio_failed_list,window.subtitle_failed_list):
            self.assertEqual(control.GetItemCount(),1)
            row=control.GetItemRect(0)
            self.assertGreaterEqual(control.GetClientSize().height,row.bottom+2,
                f'{control.GetName()} squeezed: height={control.GetClientSize().height}, row={row}')
