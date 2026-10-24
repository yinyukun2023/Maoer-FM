import unittest
from unittest.mock import Mock,patch
from browser_player import HiddenBrowserPlayer
from maoer_api import PlaybackInfo

class StartupNoProgressTests(unittest.TestCase):
    def test_silent_stall_reloads_once_then_reports_failure(self):
        player=HiddenBrowserPlayer(None)
        player._current=PlaybackInfo(1,'一','')
        player._webview=Mock()
        player._run_control_callback=Mock(side_effect=lambda a,v,cb:cb({'ok':True,'playing':False}))
        with patch('browser_player.wx.CallLater') as later:
            player._autoplay_step(0,1)
            self.assertTrue(player._startup_retry_pending)
            self.assertEqual(later.call_args.args[1],player._retry_startup_page)
            player._startup_retry_pending=False
            player._autoplay_step(0,1)
            self.assertIn('未能',player._startup_error)
            self.assertEqual(later.call_count,1)
