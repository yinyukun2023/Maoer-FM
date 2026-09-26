from dataclasses import asdict, replace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_settings import AppSettings, SubtitleFilterPreset, SubtitleFilterRules, default_filter_presets, load_settings, save_settings


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

    def test_only_two_built_in_presets_match_the_saved_drama_and_book_rules(self):
        presets = default_filter_presets()
        self.assertEqual([preset.name for preset in presets], ["广播剧过滤方案", "有声书过滤方案"])
        self.assertEqual([preset.rules.dialogue_mode for preset in presets], ["mute", "role"])
        self.assertEqual([preset.rules.speaker_transitions_only for preset in presets], [False, True])
        for preset in presets:
            self.assertEqual(preset.rules.info_labels, ("系统", "提示音", "报幕"))
            self.assertTrue(preset.rules.os_body)
            self.assertTrue(preset.rules.story_barrage)
            self.assertTrue(preset.rules.info_label_only)
            self.assertEqual(preset.rules.keywords, ())

    def test_settings_survive_reload_and_leave_no_temporary_file(self):
        settings = AppSettings(False, True, True)
        save_settings(settings)
        self.assertEqual(load_settings(), settings)
        self.assertEqual([path.name for path in self.path.iterdir()], ["settings.json"])

    def test_output_device_is_saved_by_stable_id_and_defaults_to_system(self):
        self.assertEqual(load_settings().output_device_id, "")
        save_settings(AppSettings(output_device_id="{endpoint-id}"))
        self.assertEqual(load_settings().output_device_id, "{endpoint-id}")
        (self.path / "settings.json").write_text('{"output_device_id": false}', encoding="utf-8")
        self.assertEqual(load_settings().output_device_id, "")

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

    def test_filter_rules_survive_reload_and_older_settings_use_current_defaults(self):
        self.assertEqual(load_settings().subtitle_filter_presets, default_filter_presets())
        presets = list(default_filter_presets())
        presets.extend(SubtitleFilterPreset(f"自定义 {number}") for number in range(3, 11))
        presets[9] = SubtitleFilterPreset("安静模式", SubtitleFilterRules(
            dialogue_mode="mute", story_barrage=False, os_body=False,
            info_label_only=False, info_labels=("系统",), keywords=("广告", "剧透"),
        ))
        settings = AppSettings(subtitle_filter_presets=tuple(presets), active_subtitle_filter_slot=9)
        save_settings(settings)
        self.assertEqual(load_settings(), settings)
        (self.path / "settings.json").write_text('{"read_subtitle": true}', encoding="utf-8")
        self.assertEqual(load_settings().subtitle_filter_presets, default_filter_presets())

    def test_invalid_filter_rule_values_do_not_break_loading(self):
        (self.path / "settings.json").write_text(
            '{"subtitle_filter": {"dialogue": "false", "story_barrage": false, '
            '"keywords": ["广告", 1]}}', encoding="utf-8"
        )
        self.assertEqual(load_settings().subtitle_filter_presets[0].rules,
                         SubtitleFilterRules(story_barrage=False))

    def test_previous_single_rule_settings_migrate_to_first_preset(self):
        (self.path / "settings.json").write_text(
            '{"subtitle_filter": {"dialogue": false, "keywords": ["广告"]}}', encoding="utf-8"
        )
        self.assertEqual(load_settings().subtitle_filter_presets[0].rules,
                         SubtitleFilterRules(dialogue_mode="full", keywords=("广告",)))
        self.assertEqual(len(load_settings().subtitle_filter_presets), 2)

    def test_old_dialogue_filter_preserves_muted_dialogue_behavior(self):
        (self.path / "settings.json").write_text(
            '{"subtitle_filter": {"dialogue": true, "story_barrage": true}}', encoding="utf-8"
        )
        self.assertEqual(load_settings().subtitle_filter_presets[0].rules.dialogue_mode, "mute")

    def test_short_or_invalid_preset_list_keeps_only_two_builtins(self):
        (self.path / "settings.json").write_text(
            '{"subtitle_filter_presets": [{"name": "安静", "rules": {"dialogue_mode": "mute"}}], '
            '"active_subtitle_filter_slot": 99}', encoding="utf-8"
        )
        settings = load_settings()
        self.assertEqual(len(settings.subtitle_filter_presets), 2)
        self.assertEqual(settings.subtitle_filter_presets[0].name, "安静")
        self.assertEqual(settings.subtitle_filter_presets[0].rules.dialogue_mode, "mute")
        self.assertEqual(settings.active_subtitle_filter_slot, 0)

    def test_newly_added_unchanged_third_preset_survives_reload(self):
        settings = AppSettings(
            subtitle_filter_presets=(*default_filter_presets(), SubtitleFilterPreset("方案 3")),
            active_subtitle_filter_slot=2,
        )
        save_settings(settings)
        self.assertEqual(load_settings(), settings)

    def test_old_ten_slot_settings_hide_placeholders_but_keep_custom_slots(self):
        legacy = [
            {"name": "广播剧过滤模式", "rules": {
                key: value for key, value in asdict(SubtitleFilterRules(dialogue_mode="mute")).items()
                if key != "speaker_transitions_only"
            }},
            {"name": "有声书过滤模式", "rules": {
                key: value for key, value in asdict(SubtitleFilterRules(dialogue_mode="role")).items()
                if key != "speaker_transitions_only"
            }},
        ]
        legacy.extend({"name": f"方案 {(index + 1) % 10}", "rules": asdict(SubtitleFilterRules())}
                      for index in range(2, 10))
        old_file = self.path / "settings.json"
        old_file.write_text(json.dumps({"subtitle_filter_presets": legacy,
                                        "active_subtitle_filter_slot": 1}, ensure_ascii=False),
                            encoding="utf-8")
        settings = load_settings()
        self.assertEqual(settings.subtitle_filter_presets, tuple(
            replace(preset, rules=replace(preset.rules, info_labels=("系统", "提示音")))
            for preset in default_filter_presets()
        ))
        self.assertEqual(settings.active_subtitle_filter_slot, 1)

        legacy[4]["name"] = "自己新增的方案"
        old_file.write_text(json.dumps({"subtitle_filter_presets": legacy,
                                        "active_subtitle_filter_slot": 4}, ensure_ascii=False),
                            encoding="utf-8")
        settings = load_settings()
        self.assertEqual(len(settings.subtitle_filter_presets), 3)
        self.assertEqual(settings.subtitle_filter_presets[2].name, "自己新增的方案")
        self.assertEqual(settings.active_subtitle_filter_slot, 2)


if __name__ == "__main__":
    unittest.main()
