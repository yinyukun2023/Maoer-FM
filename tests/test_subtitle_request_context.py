"""Intermittent logged-in metadata 'not found' in the actual subtitle-only path."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from downloads import DownloadCancelled, download_subtitle
from maoer_api import ApiError, MaoerApi, MediaItem, PurchaseRequired


class SubtitleMetadataRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.api = MaoerApi(cookie='test_session=keep')
        self.addCleanup(self.api.session.close)
        self.item = MediaItem('sound', 9026442, '花絮·围读')
        self.calls = []

    def run_download(self, failures=1, guest_paid=False, guest_missing=False, failure=None):
        def server(session, url, params=None, timeout=None, **kwargs):
            response = Mock()
            authenticated = bool(session.headers.get('Cookie'))
            if url.endswith('/sound/getsound'):
                sound_id = params['soundid']
                self.calls.append((authenticated, sound_id))
                attempt = sum(auth for auth, _id in self.calls)
                if authenticated and failure is not None:
                    if isinstance(failure, requests.RequestException):
                        raise failure
                    response.json.return_value = {'success': False, 'info': failure}
                elif (authenticated and attempt <= failures) or (not authenticated and guest_missing):
                    response.json.return_value = {'success': False, 'code': 100010007, 'info': '该音频不存在'}
                else:
                    response.json.return_value = {'info': {'sound': {
                        'id': sound_id, 'soundstr': '正常存在的花絮', 'duration': 10000,
                        'need_pay': 1 if (guest_paid or failure == 'paid') else 0}}}
            elif url.endswith('/sound/getdm'):
                self.assertTrue(authenticated, 'XML仍使用本次账号，而非切换账号')
                response.text = '<i><d p="1.25,4,25,0">角色：围读字幕</d></i>'
            else:
                raise AssertionError(url)
            return response
        with patch('requests.Session.get', autospec=True, side_effect=server):
            return download_subtitle(self.api, self.item, self.folder, threading.Event(), Mock())

    def test_transient_not_found_is_rechecked_with_same_account_and_id(self):
        for failures in (1, 2):
            with self.subTest(failures=failures):
                self.calls.clear()
                saved = self.run_download(failures=failures)
                self.assertIn('角色：围读字幕', saved.read_text(encoding='utf-8'))
                self.assertEqual(self.calls, [(True, self.item.id)] * (failures + 1))
                self.assertEqual(self.api.cookie_header, 'test_session=keep')
                self.assertEqual(self.api.session.headers['Cookie'], 'test_session=keep')
                saved.unlink()

    def test_three_logged_in_misreports_can_use_confirmed_public_free_metadata(self):
        saved = self.run_download(failures=99)
        self.assertIn('角色：围读字幕', saved.read_text(encoding='utf-8'))
        self.assertEqual(self.calls, [(True, self.item.id)] * 3 + [(False, self.item.id)])
        self.assertEqual(self.api.session.headers['Cookie'], 'test_session=keep')

    def test_fallback_never_treats_paid_guest_metadata_as_purchased(self):
        self.item.raw['_purchased_sound'] = True
        with self.assertRaisesRegex(ApiError, '音频信息'):
            self.run_download(failures=99, guest_paid=True)
        self.assertEqual(self.calls, [(True, self.item.id)] * 3 + [(False, self.item.id)])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_really_missing_sound_still_fails_after_bounded_checks(self):
        with self.assertRaisesRegex(ApiError, '该音频不存在'):
            self.run_download(failures=99, guest_missing=True)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_other_api_errors_or_network_errors_are_not_retried(self):
        for error in ('请先登录', '内容不可用', requests.Timeout('network')):
            with self.subTest(error=type(error).__name__):
                self.calls.clear()
                with self.assertRaises((ApiError, requests.Timeout)):
                    self.run_download(failure=error)
                self.assertEqual(len(self.calls), 1)

    def test_permission_denied_by_current_account_does_not_trigger_recovery(self):
        self.api.playback_info = Mock(side_effect=PurchaseRequired('尚未购买'))
        with self.assertRaises(PurchaseRequired), patch('downloads.MaoerApi') as public:
            download_subtitle(self.api, self.item, self.folder, threading.Event(), Mock())
        self.api.playback_info.assert_called_once_with(self.item)
        public.assert_not_called()

    def test_cancelled_download_does_not_issue_any_metadata_request(self):
        control = threading.Event()
        control.set()
        with self.assertRaises(DownloadCancelled), patch.object(self.api, 'playback_info') as metadata:
            download_subtitle(self.api, self.item, self.folder, control, Mock())
        metadata.assert_not_called()


if __name__ == '__main__':
    unittest.main()
