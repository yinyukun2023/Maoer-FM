from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import tempfile

from app_paths import app_data_dir


SETTINGS_FILENAME = "settings.json"


@dataclass(frozen=True)
class SubtitleFilterRules:
    dialogue_mode: str = "role"
    speaker_transitions_only: bool = False
    story_barrage: bool = True
    os_body: bool = True
    info_label_only: bool = True
    info_labels: tuple[str, ...] = ("系统", "提示音")
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubtitleFilterPreset:
    name: str
    rules: SubtitleFilterRules = field(default_factory=SubtitleFilterRules)


def default_filter_presets() -> tuple[SubtitleFilterPreset, ...]:
    return (
        SubtitleFilterPreset("广播剧过滤方案", SubtitleFilterRules(
            dialogue_mode="mute", info_labels=("系统", "提示音", "报幕"),
        )),
        SubtitleFilterPreset("有声书过滤方案", SubtitleFilterRules(
            dialogue_mode="role", speaker_transitions_only=True, info_labels=("系统", "提示音", "报幕"),
        )),
    )


@dataclass(frozen=True)
class AppSettings:
    startup_sound: bool = True
    read_subtitle: bool = False
    read_danmaku: bool = False
    subtitle_filter_presets: tuple[SubtitleFilterPreset, ...] = field(default_factory=default_filter_presets)
    active_subtitle_filter_slot: int = 0
    filter_presets_version: int = 2
    output_device_id: str = ""


def _clean_strings(value: object, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return fallback
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


def _load_filter_rules(raw: object, defaults: SubtitleFilterRules | None = None) -> SubtitleFilterRules:
    defaults = defaults or SubtitleFilterRules()
    if not isinstance(raw, dict):
        return defaults
    mode = raw.get("dialogue_mode")
    if mode not in ("mute", "role", "full"):
        legacy = raw.get("dialogue")
        mode = ("mute" if legacy else "full") if isinstance(legacy, bool) else defaults.dialogue_mode
    return SubtitleFilterRules(
        dialogue_mode=mode,
        speaker_transitions_only=(raw.get("speaker_transitions_only")
                                  if isinstance(raw.get("speaker_transitions_only"), bool)
                                  else defaults.speaker_transitions_only),
        story_barrage=raw.get("story_barrage") if isinstance(raw.get("story_barrage"), bool)
                      else defaults.story_barrage,
        os_body=raw.get("os_body") if isinstance(raw.get("os_body"), bool) else defaults.os_body,
        info_label_only=raw.get("info_label_only") if isinstance(raw.get("info_label_only"), bool)
                        else defaults.info_label_only,
        info_labels=_clean_strings(raw.get("info_labels"), defaults.info_labels),
        keywords=_clean_strings(raw.get("keywords"), defaults.keywords),
    )


def load_settings() -> AppSettings:
    path = app_data_dir(create=False) / SETTINGS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return AppSettings()
    if not isinstance(data, dict):
        return AppSettings()

    defaults = AppSettings()
    values = {
        name: value if isinstance(value, bool) else getattr(defaults, name)
        for name in ("startup_sound", "read_subtitle", "read_danmaku")
        for value in (data.get(name),)
    }
    presets = list(default_filter_presets())
    slot_map = {0: 0, 1: 1}
    raw_presets = data.get("subtitle_filter_presets")
    if isinstance(raw_presets, list):
        legacy_fixed_slots = len(raw_presets) == 10 and data.get("filter_presets_version") != 2
        for index, raw_preset in enumerate(raw_presets[:10]):
            if not isinstance(raw_preset, dict):
                continue
            name = raw_preset.get("name")
            if not isinstance(name, str) or not name.strip():
                name = presets[index].name if index < 2 else f"方案 {(index + 1) % 10}"
            name = name.strip()
            if index < 2 and name in (f"方案 {index + 1}",
                                      ("广播剧过滤模式", "有声书过滤模式")[index]):
                name = presets[index].name
            fallback_rules = presets[index].rules if index < 2 else SubtitleFilterRules()
            preset = SubtitleFilterPreset(name, _load_filter_rules(raw_preset.get("rules"), fallback_rules))
            if index < 2:
                presets[index] = preset
            elif not (legacy_fixed_slots and name == f"方案 {(index + 1) % 10}"
                      and preset.rules == SubtitleFilterRules()):
                slot_map[index] = len(presets)
                presets.append(preset)
    elif isinstance(data.get("subtitle_filter"), dict):
        # Migrate the previous single-rule settings into slot 1.
        presets[0] = SubtitleFilterPreset(presets[0].name, _load_filter_rules(data["subtitle_filter"]))
    values["subtitle_filter_presets"] = tuple(presets)
    slot = data.get("active_subtitle_filter_slot")
    values["active_subtitle_filter_slot"] = slot_map.get(slot, 0) if type(slot) is int else 0
    device_id = data.get("output_device_id", "")
    values["output_device_id"] = device_id if isinstance(device_id, str) else ""
    return AppSettings(**values)


def save_settings(settings: AppSettings) -> None:
    directory = app_data_dir()
    path = directory / SETTINGS_FILENAME
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix="settings-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(asdict(settings), temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
