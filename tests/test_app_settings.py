import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_settings import AppSettings, load_settings, save_settings


class AppSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.app_data_patch = patch("app_settings.app_data_dir", return_value=self.path)
        self.app_data_patch.start()
        self.addCleanup(self.app_data_patch.stop)

    def test_missing_settings_use_existing_behavior(self):
        self.assertEqual(load_settings(), AppSettings(True, False, False))

    def test_settings_survive_reload_and_leave_no_temporary_file(self):
        settings = AppSettings(False, True, True)
        save_settings(settings)
        self.assertEqual(load_settings(), settings)
        self.assertEqual([path.name for path in self.path.iterdir()], ["settings.json"])

    def test_corrupt_or_wrongly_typed_values_use_safe_defaults(self):
        settings_file = self.path / "settings.json"
        settings_file.write_text("not json", encoding="utf-8")
        self.assertEqual(load_settings(), AppSettings())
        settings_file.write_text(
            '{"startup_sound": "false", "read_subtitle": true, "read_danmaku": 1}',
            encoding="utf-8",
        )
        self.assertEqual(load_settings(), AppSettings(True, True, False))

    def test_failed_save_preserves_previous_settings(self):
        save_settings(AppSettings())
        with patch("app_settings.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                save_settings(AppSettings(False, True, True))
        self.assertEqual(load_settings(), AppSettings())
        self.assertEqual([path.name for path in self.path.iterdir()], ["settings.json"])


if __name__ == "__main__":
    unittest.main()
