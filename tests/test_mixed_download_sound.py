import io
import wave
from dataclasses import replace
from unittest.mock import patch

from startup_sound import _mixed_download_wave
from maoer_api import ApiError
from tests.test_download_partial_results import PartialDownloadTests


class MixedDownloadSoundTests(PartialDownloadTests):
    def test_audio_success_subtitle_failure_plays_both_cues(self):
        window = self.window()
        window.options = replace(window.options, play_sound=True)
        with patch('download_dialog.play_download_mixed_sound') as mixed:
            self.run_jobs(window, subtitle_error=ApiError('无字幕'))
            mixed.assert_called_once_with()

    def test_subtitle_success_audio_failure_plays_both_cues(self):
        window = self.window()
        window.options = replace(window.options, play_sound=True)
        path = self.folder / '番外.srt'
        path.write_text('subtitle', encoding='utf-8')
        with patch('download_dialog.play_download_mixed_sound') as mixed:
            self.run_jobs(window, audio_error=ApiError('音频失败'), subtitle_result=path)
            mixed.assert_called_once_with()

    def test_disabled_sound_does_not_play_mixed_cue(self):
        window = self.window()
        window.options = replace(window.options, play_sound=False)
        with patch('download_dialog.play_download_mixed_sound') as mixed:
            self.run_jobs(window, subtitle_error=ApiError('无字幕'))
            mixed.assert_not_called()

    def test_mixed_wave_contains_both_original_cues(self):
        with wave.open(io.BytesIO(_mixed_download_wave())) as sound:
            self.assertEqual((sound.getnchannels(), sound.getsampwidth(), sound.getframerate()), (2, 2, 44100))
            self.assertEqual(sound.getnframes(), 46566)
            self.assertTrue(any(sound.readframes(sound.getnframes())))
