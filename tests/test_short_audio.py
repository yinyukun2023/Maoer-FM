import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from browser_player import CONTROL_SCRIPT, HiddenBrowserPlayer
from maoer_api import PlaybackInfo


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class ShortAudioGuardTests(unittest.TestCase):
    def run_guard(self, *, duration=200, position=0, media=False, tail=''):
        fixture = r'''
var pauses=0, plays=0, ticks=[];
var sound={duration:DURATION, position:POSITION, readyState:1, playState:0, paused:false,
  pause:function(){pauses++;this.paused=true;}, stop:function(){},
  play:function(){plays++;this.playState=1;this.paused=false;},
  resume:function(){plays++;this.playState=1;this.paused=false;},
  setPosition:function(ms){this.position=ms;}};
var media={duration:DURATION/1000,currentTime:POSITION/1000,readyState:1,paused:true,ended:false,src:'test.wav',
  addEventListener:function(){},pause:function(){pauses++;this.paused=true;},
  play:function(){plays++;this.paused=false;return Promise.resolve();}};
var window={setInterval:function(fn){ticks.push(fn);}};
if(!USE_MEDIA)window.index={mo:{soundDemo:sound}};
var index=window.index;
var document={addEventListener:function(){},querySelector:function(){return null;},
  querySelectorAll:function(s){return USE_MEDIA&&s==='video,audio'?[media]:[];}};
'''.replace('DURATION', str(duration)).replace('POSITION', str(position)).replace('USE_MEDIA', json.dumps(media))
        script = fixture + '\nvar control=' + CONTROL_SCRIPT + ";\ncontrol('guard',{soundId:1,generation:1});control('autoplay',100);" + tail + "console.log(JSON.stringify({handled:window.__maoerSingleSoundGuard.handled,pauses:pauses,plays:plays}));"
        result = subprocess.run([shutil.which('node'), '-'], input=script, text=True,
                                capture_output=True, check=True, timeout=5)
        return json.loads(result.stdout)

    def test_initial_partial_duration_must_not_stop_before_playback(self):
        for media in (False, True):
            with self.subTest(media=media):
                actual = self.run_guard(media=media)
                self.assertFalse(actual['handled'], 'initial duration estimate treated as finished')
                self.assertEqual(actual['pauses'], 0)
                self.assertEqual(actual['plays'], 1)

    def test_twenty_second_audio_starts(self):
        for media in (False, True):
            with self.subTest(media=media):
                self.assertFalse(self.run_guard(duration=20000, media=media)['handled'])

    def test_real_progress_reaches_end_and_guard_still_stops(self):
        for media in (False, True):
            with self.subTest(media=media):
                actual = self.run_guard(duration=20000, media=media,
                    tail='sound.readyState=3;sound.position=19950;media.readyState=4;media.currentTime=19.95;ticks[0]();')
                self.assertTrue(actual['handled'])
                self.assertGreater(actual['pauses'], 0)

    def test_stale_sound_duration_cannot_override_native_clock(self):
        actual = self.run_guard(duration=20000, media=True,
            tail='window.index={mo:{soundDemo:sound}};index=window.index;sound.duration=200;sound.position=200;sound.readyState=3;media.readyState=4;media.currentTime=2;ticks[0]();')
        self.assertFalse(actual['handled'])

    def test_risk_error_page_is_not_an_unready_player(self):
        script = "var window={};var document={title:'出错啦! - bilibili.com',body:{innerText:'错误号: 412 security control policy'},querySelector:function(){return null;},querySelectorAll:function(){return [];}};var control=" + CONTROL_SCRIPT + ";console.log(control('autoplay',100));"
        result = subprocess.run([shutil.which('node'), '-'], input=script, text=True,
                                capture_output=True, check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout).get('error'), 'page-rejected')


class ShortAudioStartupRetryTests(unittest.TestCase):
    def make_player(self):
        player = HiddenBrowserPlayer(None, cookie='test=keep')
        player._current = PlaybackInfo(9166178, '铃声', '', page_url='https://www.missevan.com/sound/player?id=9166178')
        player._webview = Mock()
        player._run_control_callback = Mock(side_effect=lambda a,v,cb: cb({'ok':False,'error':'page-rejected'}))
        return player

    def test_rejected_page_is_reloaded_only_once(self):
        player = self.make_player()
        with patch('browser_player.wx.CallLater') as later, patch.object(player, '_install_user_scripts'):
            player._autoplay_step(0, 12)
            retry = next(call for call in later.call_args_list if call.args[1].__name__ == '_retry_startup_page')
            retry.args[1](*retry.args[2:])
            player._webview.LoadURL.assert_called_once_with(player._current.page_url)
            self.assertEqual(player.cookie, 'test=keep')
            player._autoplay_step(player._load_generation, 12)
            self.assertEqual(sum(call.args[1].__name__ == '_retry_startup_page' for call in later.call_args_list), 1)

    def test_old_retry_cannot_interrupt_pause_stop_or_new_track(self):
        for state in ('pause', 'stop', 'new'):
            with self.subTest(state=state):
                player = self.make_player()
                if state == 'pause': player._suppress_autoplay = True
                elif state == 'stop': player._current = None
                else: player._load_generation = 1
                with patch('browser_player.wx.CallLater'):
                    player._retry_startup_page(0)
                player._webview.LoadURL.assert_not_called()

    def test_main_page_error_retains_track_and_schedules_bounded_retry(self):
        player = self.make_player()
        current = player._current
        event = Mock()
        event.GetURL.return_value = current.page_url
        with patch('browser_player.wx.CallLater') as later:
            player._on_error(event)
            self.assertIs(player._current, current)
            self.assertTrue(player._startup_reload_attempted)
            player._on_error(event)
            self.assertEqual(later.call_count, 1)
        event.Skip.assert_called()

    def test_unrelated_or_old_navigation_error_does_not_clear_track(self):
        for url in ('https://static.example/image.png', 'https://www.missevan.com/sound/player?id=123'):
            player = self.make_player()
            current = player._current
            event = Mock()
            event.GetURL.return_value = url
            with patch('browser_player.wx.CallLater') as later:
                player._on_error(event)
            self.assertIs(player._current, current)
            later.assert_not_called()
