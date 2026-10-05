from __future__ import annotations

import copy
import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from wx import html2

from browser_player import HIGHEST_AUDIO_QUALITY_SCRIPT, HiddenBrowserPlayer
from maoer_api import MaoerApi, MediaItem


class AudioQualityTests(unittest.TestCase):
    def test_download_chooses_bandwidth_not_id_or_list_order(self):
        low = {'id': 128, 'bandwidth': 135695, 'base_url': 'https://media.invalid/low.m4s'}
        high = {'id': 64, 'bandwidth': '357202', 'base_url': 'https://media.invalid/high.m4s'}
        unknown = {'id': 256, 'bandwidth': 'invalid', 'base_url': 'https://media.invalid/unknown.m4s'}
        for audio, expected in (
            ([low, high], high), ([high, low], high), ([low], low),
            ([unknown, low, {'bandwidth': 9999999}, high], high),
            ([unknown], unknown), ([], {}), (None, {}),
        ):
            with self.subTest(audio=audio):
                sound = {'dash': {'audio': audio}, 'videourl': 'https://media.invalid/video.mp4'}
                api = MaoerApi(cookie='')
                try:
                    api._get = Mock(return_value={'info': {'sound': sound}})
                    playback = api.playback_info(MediaItem('sound', 1, '测试'))
                    self.assertEqual(playback.dash_audio, expected)
                    self.assertEqual(playback.video_url, sound['videourl'])
                finally:
                    api.session.close()

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed to execute the website hook')
    def test_playback_filters_before_loader_and_preserves_other_media_data(self):
        source = {'dash': {'audio': [
            {'id': 128, 'bandwidth': 135695, 'base_url': 'low', 'bilidrm_uri': 'low-key'},
            {'id': 64, 'bandwidth': '357202', 'base_url': 'high', 'bilidrm_uri': 'high-key'},
            {'id': 256, 'bandwidth': 'invalid', 'base_url': 'unknown'},
            {'id': 512, 'bandwidth': 9999999},
        ], 'video': [{'id': 1080}]}, 'videourl': 'original.mp4', 'need_pay': 1}
        cases = [source, {**source, 'dash': {**source['dash'], 'audio': list(reversed(source['dash']['audio']))}},
                 {'dash': {'audio': [source['dash']['audio'][0]]}}, {'dash': {'audio': []}},
                 {'soundurl': 'audio.mp3'}, None]
        fixture = """
let ready, calls = 0;
const document = {readyState: 'loading', addEventListener: (event, fn) => { ready = fn; }};
const window = {};
"""
        script = fixture + HIGHEST_AUDIO_QUALITY_SCRIPT + """
window.R = {player: {api: {kept: true}, createPlayer: function(sound) {
  calls++; return sound;
}}};
ready();
ready();
const before = calls;
const results = CASES.map(sound => window.R.player.createPlayer(sound));
console.log(JSON.stringify({before, calls, results, apiKept: window.R.player.api.kept}));
""".replace('CASES', json.dumps(cases))
        result = subprocess.run([shutil.which('node'), '-'], input=script, capture_output=True,
                                text=True, check=True, timeout=5)
        actual = json.loads(result.stdout)
        expected = copy.deepcopy(cases)
        for item in expected[:2]:
            item['dash']['audio'] = [source['dash']['audio'][1]]
        self.assertEqual(actual, {'before': 0, 'calls': len(cases), 'results': expected, 'apiKept': True})

    def test_playback_hook_is_installed_before_page_scripts(self):
        view = Mock()
        HiddenBrowserPlayer(None)._install_user_scripts(view)
        view.AddUserScript.assert_any_call(HIGHEST_AUDIO_QUALITY_SCRIPT, html2.WEBVIEW_INJECT_AT_DOCUMENT_START)


if __name__ == '__main__':
    unittest.main()
