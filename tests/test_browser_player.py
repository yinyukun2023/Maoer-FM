import json
import shutil
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from browser_player import CONTROL_SCRIPT, HiddenBrowserPlayer
from maoer_api import PlaybackInfo


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the browser control script")
class AutoplayScriptTests(unittest.TestCase):
    def run_autoplay(self, *, paused: bool, media: bool = False) -> dict:
        # Model the site's toggle button and the two supported player backends.
        fixture = """
var calls = {click: 0, play: 0, resume: 0};
var sound = {
  playState: 1, paused: PAUSED, position: 2000, duration: 30000,
  play: function() { calls.play++; this.paused = false; },
  resume: function() { calls.resume++; this.paused = false; }
};
var media = {
  paused: PAUSED, currentTime: 2, duration: 30, src: 'test.mp3',
  play: function() { calls.play++; this.paused = false; return Promise.resolve(); }
};
var target = USE_MEDIA ? media : sound;
var button = {click: function() { calls.click++; target.paused = !target.paused; }};
var window = USE_MEDIA ? {} : {index: {mo: {soundDemo: sound}}};
var index = window.index;
var document = {
  querySelectorAll: function(selector) { return USE_MEDIA && selector === 'video,audio' ? [media] : []; },
  querySelector: function(selector) { return selector === '#mpi' ? button : null; }
};
""".replace("PAUSED", json.dumps(paused)).replace("USE_MEDIA", json.dumps(media))
        script = fixture + "\nvar control = " + CONTROL_SCRIPT + ";\n"
        script += "for (var i=0; i<3; i++) { control('autoplay', 100); }\n"
        script += "console.log(JSON.stringify({paused: target.paused, calls: calls}));"
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return json.loads(result.stdout)

    def test_already_playing_audio_is_not_toggled_off(self) -> None:
        for media in (False, True):
            with self.subTest(media=media):
                result = self.run_autoplay(paused=False, media=media)
                self.assertFalse(result["paused"])
                self.assertEqual(result["calls"]["click"], 0)
                self.assertEqual(result["calls"]["play"], 0)

    def test_paused_audio_is_resumed_without_toggle_button(self) -> None:
        for media in (False, True):
            with self.subTest(media=media):
                result = self.run_autoplay(paused=True, media=media)
                self.assertFalse(result["paused"])
                self.assertEqual(result["calls"]["click"], 0)
                self.assertEqual(result["calls"]["play"] + result["calls"]["resume"], 1)


class AutoplaySchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.player = HiddenBrowserPlayer(None)
        self.player._current = PlaybackInfo(123, "sound", "https://example.com/audio.mp3")
        self.player._load_generation = 1
        self.player._webview = Mock()
        self.calls = []
        self.pending = []

        def run(script, callback_id=None):
            action, value = json.loads("[" + script[len(CONTROL_SCRIPT):].strip()[1:-1] + "]")
            self.calls.append(action)
            if callback_id is not None:
                self.pending.append((action, callback_id))

        self.player._webview.RunScriptAsync.side_effect = run
        self.timer = self.enterContext(patch("browser_player.wx.CallLater"))
        self.native_volume = self.enterContext(patch("browser_player.set_current_app_volume", return_value=False))

    def respond(self, **status) -> None:
        action, callback_id = self.pending.pop(0)
        event = Mock()
        event.GetInt.return_value = callback_id
        event.GetString.return_value = json.dumps({"action": action, **status})
        self.player._on_script_result(event)

    def test_repeated_loaded_notifications_start_one_autoplay_request(self) -> None:
        self.player._schedule_autoplay(1)
        self.player._schedule_autoplay(1)
        self.player._on_loaded(Mock())
        self.assertEqual(self.calls.count("autoplay"), 1)
        self.native_volume.assert_not_called()

    def test_playback_success_stops_retrying(self) -> None:
        self.player._apply_volume = Mock()
        self.player._schedule_autoplay(1)
        self.respond(ok=True, playing=True)
        self.player._on_loaded(Mock())
        self.assertEqual(self.calls.count("autoplay"), 1)
        self.player._apply_volume.assert_called_once_with(100)

    def test_stopped_player_ignores_late_autoplay_callback(self) -> None:
        self.player._apply_volume = Mock()
        self.player._schedule_autoplay(1)
        self.player.stop()
        self.respond(ok=True, playing=True)
        self.player._apply_volume.assert_not_called()


class SystemVolumeTests(unittest.TestCase):
    def test_audio_session_work_runs_off_ui_thread_and_targets_own_processes(self) -> None:
        player = HiddenBrowserPlayer(None)
        player._webview = Mock()
        threads = []
        finished = threading.Event()

        def set_volume(*args, **kwargs):
            threads.append(threading.current_thread())
            finished.set()
            return True

        with patch("browser_player.set_current_app_volume", side_effect=set_volume) as native, \
                patch("browser_player.wx.CallLater"), patch.object(player, "_run_control"):
            player._apply_volume(40)
            self.assertTrue(finished.wait(2))
            self.assertIsNot(threads[0], threading.current_thread())
            native.assert_called_once_with(40)


if __name__ == "__main__":
    unittest.main()
