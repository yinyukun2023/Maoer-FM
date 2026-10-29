import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from browser_player import CONTROL_SCRIPT, HiddenBrowserPlayer
from downloads import download_audio, download_subtitle
from maoer_api import ApiError, MaoerApi, MediaItem, PlaybackInfo, PurchaseRequired


class PlaybackLimitTests(unittest.TestCase):
    def api(self, limit):
        api = MaoerApi(cookie='test=account', cache_sound_metadata=True)
        self.addCleanup(api.session.close)
        api._get = Mock(return_value={'info': {'sound': {
            'id': 13816686, 'need_pay': 2, 'duration': 2549912, 'limit_type': limit}}})
        return api

    def test_purchased_sound_reports_official_restriction(self):
        for limit, message in ((1, '同时播放设备超限'), ('2', '会员'), (5, '地区'), (99, '限制代码 99')):
            with self.subTest(limit=limit):
                api = self.api(limit)
                with self.assertRaisesRegex(ApiError, message):
                    api.playback_info(MediaItem('sound', 13816686, '第十四集'))
                api._get.assert_called_once()

    def test_download_parts_report_restriction_before_media_preparation(self):
        api = self.api(1)
        item = MediaItem('sound', 13816686, '第十四集')
        prepare = Mock(side_effect=AssertionError('Restricted content must not prepare media'))
        with tempfile.TemporaryDirectory() as folder:
            for operation in (
                lambda: download_audio(api, item, Path(folder), threading.Event(), prepare, Mock()),
                lambda: download_subtitle(api, item, Path(folder), threading.Event(), Mock()),
            ):
                with self.assertRaisesRegex(ApiError, '同时播放设备超限'):
                    operation()
            self.assertEqual(list(Path(folder).iterdir()), [])
        api._get.assert_called_once()
        prepare.assert_not_called()

    def test_purchase_check_still_runs(self):
        api = self.api(1)
        api._get.return_value['info']['sound']['need_pay'] = 1
        with self.assertRaises(PurchaseRequired):
            api.playback_info(MediaItem('sound', 1, '未购买'))

    def test_zero_limit_leaves_playable_response_unchanged(self):
        api = self.api(0)
        api._get.return_value['info']['sound']['soundurl'] = 'https://example.test/a.mp3'
        self.assertTrue(api.playback_info(MediaItem('sound', 1, '已购买')).url)

    def test_missing_address_is_not_misreported_as_incomplete_file(self):
        api = Mock()
        api.playback_info.return_value = PlaybackInfo(1, '第十四集', '')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ApiError, '未返回.*音频地址'):
                download_audio(api, MediaItem('sound', 1, '第十四集'), Path(folder),
                               threading.Event(), Mock(), Mock())

    def test_visible_official_restriction_does_not_reload_the_player(self):
        player = HiddenBrowserPlayer(None)
        player._current = PlaybackInfo(1, '第十四集', '')
        player._run_control_callback = Mock(side_effect=lambda a, v, cb: cb({
            'ok': False, 'error': 'playback-restricted', 'message': '同时播放设备超限'}))
        with patch('browser_player.wx.CallLater') as later:
            player._autoplay_step(player._load_generation, 12)
        self.assertIn('同时播放设备超限', player._startup_error)
        self.assertTrue(player._suppress_autoplay)
        later.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_control_reads_visible_official_restriction(self):
        fixture = '''
var window={};
var modal={getClientRects:function(){return [1];},querySelector:function(s){
return {textContent:s==='.title'?'同时播放设备超限':'您可切换账号后继续收听'};}};
var document={title:'音频',body:{innerText:''},querySelectorAll:function(){return [];},
querySelector:function(s){return s==='.limited-modal'?modal:null;}};
'''
        script = fixture + '\nvar control=' + CONTROL_SCRIPT + ";console.log(control('autoplay',0));"
        result = subprocess.run([shutil.which('node'), '-'], input=script, encoding='utf-8',
                                capture_output=True, check=True, timeout=5)
        data = json.loads(result.stdout)
        self.assertEqual(data.get('error'), 'playback-restricted')
        self.assertIn('同时播放设备超限', data['message'])
