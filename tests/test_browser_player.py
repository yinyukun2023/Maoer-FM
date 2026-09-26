import json
import shutil
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch
from wx import html2

from browser_player import CONTROL_SCRIPT, HiddenBrowserPlayer
from maoer_api import PlaybackInfo


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the browser control script")
class AutoplayScriptTests(unittest.TestCase):
    def run_autoplay(self, *, paused: bool, media: bool = False, action: str = "autoplay") -> dict:
        # Model the site's toggle button and the two supported player backends.
        fixture = """
var calls = {click: 0, play: 0, resume: 0};
var sound = {
  playState: 1, paused: PAUSED, position: 2000, duration: 30000,
  setPosition: function(ms) { this.position = ms; },
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
        if action == "resume":
            script += "control('seek_to', 12.345);\n"
        script += f"for (var i=0; i<3; i++) {{ control({json.dumps(action)}, 100); }}\n"
        script += "console.log(JSON.stringify({paused: target.paused, calls: calls, position: target.currentTime || target.position / 1000}));"
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

    def test_seek_then_explicit_resume_plays_without_toggling_or_resetting_time(self):
        for media in (False, True):
            for paused in (False, True):
                with self.subTest(media=media, paused=paused):
                    result = self.run_autoplay(paused=paused, media=media, action="resume")
                    self.assertEqual(result["position"], 12.345)
                    self.assertFalse(result["paused"])
                    self.assertEqual(result["calls"]["click"], 0)
                    self.assertEqual(result["calls"]["play"] + result["calls"]["resume"], int(paused))

    def test_absolute_seek_sets_position_instead_of_adding_offset(self) -> None:
        for use_media in (False, True):
            with self.subTest(use_media=use_media):
                fixture = """
var sound = {position: 2000, duration: 300000,
  setPosition: function(ms) { this.position = ms; }};
var media = {paused: false, currentTime: 2, duration: 300, src: 'test.mp3'};
var window = USE_MEDIA ? {} : {index: {mo: {soundDemo: sound}}};
var index = window.index;
var document = {querySelectorAll: function(selector) {
  return USE_MEDIA && selector === 'video,audio' ? [media] : [];
}};
""".replace("USE_MEDIA", json.dumps(use_media))
                script = fixture + "\nvar control = " + CONTROL_SCRIPT + ";\n"
                script += "var response = JSON.parse(control('seek_to', 250));\n"
                script += "console.log(JSON.stringify({response: response, position: USE_MEDIA ? media.currentTime : sound.position}));"
                script = script.replace("USE_MEDIA", json.dumps(use_media))
                result = subprocess.run(
                    [shutil.which("node"), "-e", script],
                    capture_output=True, text=True, check=True, timeout=5,
                )
                output = json.loads(result.stdout)
                self.assertTrue(output["response"]["ok"])
                self.assertEqual(output["position"], 250 if use_media else 250000)

    def test_native_quarter_step_speeds_are_not_rounded_to_tenths(self) -> None:
        for rate in (1.25, 1.75):
            with self.subTest(rate=rate):
                fixture = """
var media = {paused: false, currentTime: 2, duration: 300, src: 'test.mp3',
  playbackRate: 1, defaultPlaybackRate: 1};
var window = {};
var document = {querySelectorAll: function(selector) {
  return selector === 'video,audio' ? [media] : [];
}};
"""
                script = fixture + "\nvar control = " + CONTROL_SCRIPT + ";\n"
                script += f"var response = JSON.parse(control('rate', {rate}));\n"
                script += "console.log(JSON.stringify({response: response, rate: media.playbackRate}));"
                result = subprocess.run(
                    [shutil.which("node"), "-e", script],
                    capture_output=True, text=True, check=True, timeout=5,
                )
                output = json.loads(result.stdout)
                self.assertTrue(output["response"]["ok"])
                self.assertEqual(output["rate"], rate)
                self.assertEqual(HiddenBrowserPlayer._clamp_playback_rate(rate), rate)


class SeekResumeTests(unittest.TestCase):
    def setUp(self):
        self.player = HiddenBrowserPlayer(None)
        self.player._paused = True
        self.player._run_control = Mock()
        self.callback = Mock()
        self.pending = []
        self.player._run_control_callback = Mock(side_effect=lambda action, value, callback: self.pending.append((action, value, callback)))

    def respond(self, action, **result):
        actual, _value, callback = self.pending.pop(0)
        self.assertEqual(actual, action)
        callback(result)

    def test_seek_is_followed_by_explicit_resume_and_confirmed_playing_state(self):
        self.player.seek_to(12.345, self.callback, resume=True)
        self.assertEqual(self.pending[0][1], 12.345)
        self.respond("seek_to", ok=True, position=12.345)
        self.callback.assert_not_called()
        self.respond("resume", ok=True)
        self.respond("status", ok=True, paused=False, ended=False)
        self.assertFalse(self.player.is_paused())
        self.callback.assert_called_once_with({"ok": True, "position": 12.345, "paused": False})
        self.assertTrue(self.player.toggle_pause())

    def test_failed_seek_never_starts_playback(self):
        self.player.seek_to(20, self.callback, resume=True)
        self.respond("seek_to", ok=False)
        self.assertEqual(self.pending, [])
        self.callback.assert_called_once_with({"ok": False})

    def test_failed_resume_preserves_successful_seek_and_reports_failure(self):
        self.player.seek_to(20, self.callback, resume=True)
        self.respond("seek_to", ok=True, position=20)
        self.respond("resume", ok=False)
        self.callback.assert_called_once_with({"ok": True, "position": 20, "paused": True, "resume_error": True})

    def test_slow_resume_waits_for_status_without_repeatedly_issuing_play(self):
        self.player.seek_to(20, self.callback, resume=True)
        self.respond("seek_to", ok=True, position=20)
        self.respond("resume", ok=True)
        with patch("browser_player.wx.CallLater") as later:
            self.respond("status", ok=True, paused=True)
        self.callback.assert_not_called()
        args = later.call_args.args
        args[1](*args[2:])
        self.respond("status", ok=True, paused=False)
        self.callback.assert_called_once()
        self.assertEqual([call.args[0] for call in self.player._run_control_callback.call_args_list].count("resume"), 1)

    def test_resume_timeout_does_not_claim_playback_started(self):
        self.player.seek_to(20, self.callback, resume=True)
        self.respond("seek_to", ok=True, position=20)
        self.respond("resume", ok=True)
        with patch("browser_player.wx.CallLater", side_effect=lambda delay, fn, *args: fn(*args)):
            while self.pending:
                self.respond("status", ok=True, paused=True)
        self.assertTrue(self.callback.call_args.args[0]["resume_error"])
        self.assertTrue(self.player.is_paused())

    def test_stop_or_user_pause_cancels_delayed_resume(self):
        for operation in (self.player.stop, self.player.toggle_pause):
            with self.subTest(operation=operation.__name__):
                self.callback.reset_mock()
                self.player.seek_to(20, self.callback, resume=True)
                operation()
                self.respond("seek_to", ok=True, position=20)
                self.assertEqual(self.pending, [])
                self.callback.assert_called_once_with(None)

    def test_plain_seek_does_not_change_playback_policy(self):
        self.player.seek_to(20, self.callback)
        self.respond("seek_to", ok=True, position=20)
        self.assertTrue(self.player.is_paused())
        self.assertEqual(self.pending, [])


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


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the browser script")
class HiddenBrowserAccessibilityTests(unittest.TestCase):
    def test_page_is_hidden_from_accessibility_without_stopping_media(self) -> None:
        script = HiddenBrowserPlayer._hide_embedded_page_from_screen_readers_script()
        fixture = """
var handlers = {};
var root = {attrs: {}, getAttribute: function(name) { return this.attrs[name]; },
  setAttribute: function(name, value) { this.attrs[name] = value; }};
var body = {attrs: {}, getAttribute: root.getAttribute, setAttribute: root.setAttribute};
var document = {documentElement: null, body: null,
  addEventListener: function(name, fn) { handlers[name] = fn; }};
var observer;
function MutationObserver(fn) { observer = fn; this.observe = function() {}; }
var media = {playing: true};
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", fixture + script + """
document.documentElement = root;
document.body = body;
handlers.DOMContentLoaded();
root.setAttribute('aria-hidden', 'false');
observer();
console.log(JSON.stringify({root: root.attrs['aria-hidden'],
  body: body.attrs['aria-hidden'], playing: media.playing}));
"""],
            capture_output=True, text=True, check=True, timeout=5,
        )
        self.assertEqual(json.loads(result.stdout), {"root": "true", "body": "true", "playing": True})

    def test_accessibility_guard_runs_at_document_start(self) -> None:
        player = HiddenBrowserPlayer(None)
        webview = Mock()
        player._install_user_scripts(webview)
        self.assertEqual(
            webview.AddUserScript.call_args_list[0].args,
            (player._hide_embedded_page_from_screen_readers_script(),
             html2.WEBVIEW_INJECT_AT_DOCUMENT_START),
        )


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
