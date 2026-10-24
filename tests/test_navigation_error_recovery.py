import unittest
from unittest.mock import Mock,patch
from browser_player import HiddenBrowserPlayer
from maoer_api import PlaybackInfo

class NavigationErrorRecoveryTests(unittest.TestCase):
    def player(self):
        player=HiddenBrowserPlayer(None)
        player._current=PlaybackInfo(1,'一','')
        player._webview=Mock()
        player._startup_reload_attempted=True
        return player

    def test_navigation_error_alone_is_not_a_confirmed_media_failure(self):
        player=self.player()
        event=Mock()
        event.GetURL.return_value='https://www.missevan.com/sound/player?id=1'
        player._on_error(event)
        self.assertEqual(player._startup_error,'','native navigation event must not fail an otherwise usable player')

    def test_successful_start_clears_a_previous_startup_error(self):
        player=self.player()
        player._startup_error='官网播放页加载失败，请检查网络或稍后重试'
        player._run_control_callback=Mock(side_effect=lambda a,v,cb:cb({'ok':True,'playing':True}))
        with patch.object(player,'_apply_volume'),patch.object(player,'_run_control'):
            player._autoplay_step(0,2)
        self.assertEqual(player._startup_error,'')
