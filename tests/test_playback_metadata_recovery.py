import unittest
from unittest.mock import patch

import requests

from maoer_api import ApiError, MaoerApi, MediaItem, PurchaseRequired


class PlaybackMetadataRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = MaoerApi(cookie='test_session=keep')
        self.addCleanup(self.api.session.close)
        self.item = MediaItem('sound', 9026442, '花絮')

    def test_playback_recovers_same_account_transient_not_found(self):
        for failures in (1, 2):
            with self.subTest(failures=failures):
                payload = {'info': {'sound': {'soundstr': '花絮', 'need_pay': 0,
                           'soundurl': 'https://example.test/audio.mp3'}}}
                with patch.object(self.api, '_get', side_effect=[ApiError('该音频不存在')] * failures + [payload]) as get:
                    result = self.api.playback_info(self.item)
                self.assertEqual(result.url, 'https://example.test/audio.mp3')
                self.assertEqual(get.call_count, failures + 1)
                self.assertTrue(all(call.args == ('/sound/getsound', {'soundid': self.item.id}) for call in get.call_args_list))
                self.assertEqual(self.api.session.headers['Cookie'], 'test_session=keep')

    def test_persistent_not_found_is_bounded(self):
        with patch.object(self.api, '_get', side_effect=ApiError('该音频不存在')) as get:
            with self.assertRaisesRegex(ApiError, '该音频不存在'):
                self.api.playback_info(self.item)
        self.assertEqual(get.call_count, 3)

    def test_other_errors_not_retried(self):
        for error in (ApiError('请先登录'), requests.Timeout('timeout')):
            with self.subTest(error=str(error)), patch.object(self.api, '_get', side_effect=error) as get:
                with self.assertRaises(type(error)):
                    self.api.playback_info(self.item)
                self.assertEqual(get.call_count, 1)

    def test_retry_does_not_bypass_payment(self):
        with patch.object(self.api, '_get', side_effect=[ApiError('该音频不存在'),
                {'info': {'sound': {'need_pay': 1}}}]) as get:
            with self.assertRaises(PurchaseRequired):
                self.api.playback_info(self.item)
        self.assertEqual(get.call_count, 2)
