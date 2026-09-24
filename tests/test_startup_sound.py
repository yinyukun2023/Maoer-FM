import unittest
from unittest.mock import Mock, patch

import startup_sound
from app import MaoerApp
from app_settings import AppSettings


class StartupSoundTests(unittest.TestCase):
    def test_startup_sound_uses_nonblocking_windows_wave_playback(self):
        with patch("startup_sound.winsound.PlaySound") as play:
            startup_sound.play_startup_sound()

        play.assert_called_once_with(
            str(startup_sound.STARTUP_SOUND),
            startup_sound.winsound.SND_FILENAME
            | startup_sound.winsound.SND_ASYNC
            | startup_sound.winsound.SND_NODEFAULT,
        )
        self.assertEqual(startup_sound.STARTUP_SOUND.suffix, ".wav")

    def test_missing_audio_device_does_not_block_startup(self):
        with patch("startup_sound.winsound.PlaySound", side_effect=RuntimeError("no device")):
            startup_sound.play_startup_sound()

    def test_every_startup_requests_the_cat_sound(self):
        with patch("startup_sound.winsound.PlaySound") as play:
            startup_sound.play_startup_sound()
            startup_sound.play_startup_sound()
        self.assertEqual(play.call_count, 2)

    def test_missing_audio_file_does_not_play_system_default_sound(self):
        with patch.object(startup_sound.STARTUP_SOUND.__class__, "is_file", return_value=False), \
             patch("startup_sound.winsound.PlaySound") as play:
            startup_sound.play_startup_sound()
        play.assert_not_called()

    def test_app_plays_sound_after_showing_window(self):
        calls = []
        frame = Mock()
        frame.settings = AppSettings()
        frame.Show.side_effect = lambda: calls.append("show")
        with patch("app.run_startup_update_check", return_value=True), \
             patch("app.MaoerFrame", return_value=frame), \
             patch("app.play_startup_sound", side_effect=lambda: calls.append("sound")):
            self.assertTrue(MaoerApp.OnInit(None))

        self.assertEqual(calls, ["show", "sound"])

    def test_disabled_startup_sound_is_not_played(self):
        frame = Mock()
        frame.settings = AppSettings(startup_sound=False)
        with patch("app.run_startup_update_check", return_value=True), \
             patch("app.MaoerFrame", return_value=frame), \
             patch("app.play_startup_sound") as play:
            self.assertTrue(MaoerApp.OnInit(None))
        frame.Show.assert_called_once_with()
        play.assert_not_called()


if __name__ == "__main__":
    unittest.main()
