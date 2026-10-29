import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from browser_player import CONTROL_SCRIPT, HiddenBrowserPlayer
from maoer_api import BASE_URL, PlaybackInfo


class PlaybackNavigationTests(unittest.TestCase):
    def player(self, current_url=BASE_URL + '/', cookie='test=account'):
        player = HiddenBrowserPlayer(None, cookie=cookie, reuse_cookie_primer=True)
        player._webview = Mock()
        player._webview.GetCurrentURL.return_value = current_url
        player._current = PlaybackInfo(13816686, '第十四集', '')
        return player

    def assert_site_navigation(self, player, target):
        player._webview.LoadURL.assert_not_called()
        script = player._webview.RunScriptAsync.call_args.args[0]
        self.assertIn('location.assign(', script)
        self.assertIn(json.dumps(target), script)

    def test_primed_playback_keeps_real_homepage_referrer(self):
        player = self.player()
        target = BASE_URL + '/sound/player?id=13816686'
        player._cookie_primer_target = target
        with patch('browser_player.wx.CallLater'):
            player._load_cookie_primer_target(player._load_generation)
        self.assert_site_navigation(player, target)
        self.assertIsNone(player._cookie_primer_target)

    def test_next_episode_and_startup_retry_keep_site_navigation(self):
        for current in (BASE_URL + '/sound/player?id=13788017', BASE_URL + '/sound/player?id=13816686'):
            with self.subTest(current=current):
                player = self.player(current)
                with patch('browser_player.wx.CallLater'), patch.object(player, '_install_user_scripts'):
                    player._retry_startup_page(player._load_generation)
                self.assert_site_navigation(player, BASE_URL + '/sound/player?id=13816686')

    def test_resume_reload_keeps_site_navigation(self):
        player = self.player(BASE_URL + '/sound/player?id=13816686')
        request = Mock(reloaded=False)
        with patch.object(player, '_resume_is_current', return_value=True), patch.object(player, '_install_user_scripts'), patch('browser_player.wx.CallLater'):
            player._reload_for_resume(request)
        self.assertTrue(request.reloaded)
        self.assert_site_navigation(player, BASE_URL + '/sound/player?id=13816686')

    def test_guest_first_play_also_enters_through_homepage(self):
        player = self.player('about:blank', cookie='')
        with patch.object(player, '_ensure_webview', return_value=player._webview), patch.object(player, '_install_user_scripts'), patch.object(player, '_run_control'), patch('browser_player.wx.CallLater'):
            player.play(PlaybackInfo(13240395, '预告', ''))
        player._webview.LoadURL.assert_called_once_with(BASE_URL + '/')
        self.assertEqual(player._cookie_primer_target, BASE_URL + '/sound/player?id=13240395')

    def test_no_script_navigation_from_foreign_or_blank_document(self):
        for current in ('about:blank', 'https://www.missevan.com.example.org/', 'http://www.missevan.com/', 'https://other.example/'):
            with self.subTest(current=current):
                player = self.player(current)
                with patch('browser_player.wx.CallLater'):
                    player._load_playback_url(BASE_URL + '/sound/player?id=13816686', player._load_generation)
                player._webview.RunScriptAsync.assert_not_called()

    def test_stale_load_cannot_navigate_after_switching_episode(self):
        player = self.player()
        with patch('browser_player.wx.CallLater'):
            player._load_playback_url(BASE_URL + '/sound/player?id=13788017', player._load_generation - 1)
        player._webview.LoadURL.assert_not_called()
        player._webview.RunScriptAsync.assert_not_called()


@unittest.skipUnless(shutil.which('node'), 'Node.js required for player script checks')
class ResumeLoaderTests(unittest.TestCase):
    def test_resume_does_not_reinitialize_existing_official_html_player(self):
        # The official DASH wrapper has element, but no SoundManager readyState.
        # Calling its load() again changes the URL and triggers the next-track
        # guard even though it is still the same authorized episode.
        fixture = '''
let reloads=0;
const media={play(){}, pause(){}, readyState:4, currentTime:0};
const sound={element:media, load(){reloads++;}};
const window={index:{mo:{soundDemo:sound}}, __maoerResumeHold:true};
const index=window.index;
const document={querySelectorAll(){return [];}};
'''
        script = fixture + '\nconst control=' + CONTROL_SCRIPT + ';\n'
        script += "control('prepare_resume'); control('prepare_resume'); console.log(JSON.stringify({reloads,hold:window.__maoerResumeHold}));"
        result = subprocess.run([shutil.which('node'), '-'], input=script, text=True,
                                capture_output=True, check=True, timeout=5)
        data = json.loads(result.stdout)
        self.assertEqual(data['reloads'], 0)
        self.assertTrue(data['hold'])
