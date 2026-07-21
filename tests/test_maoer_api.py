import json
import unittest

from maoer_api import DANMAKU_MODE_SUBTITLE, MaoerApi, MediaItem


SUBTITLE_URL = "https://static.example/subtitle.json"


class FakeSubtitleApi(MaoerApi):
    def __init__(self) -> None:
        self.requested_paths: list[str] = []

    def _get_text(self, path: str, params: dict[str, object] | None = None) -> str:
        self.requested_paths.append(path)
        if path == "/sound/getdm":
            return '<i><d p="2.5,1,25,16777215,0,0,user,dm">普通弹幕</d></i>'
        if path == SUBTITLE_URL:
            return json.dumps(
                [
                    {"start_time": 1250, "role": "甲", "content": "你好", "color": 1122867},
                    {"start_time": 500, "role": "", "content": "旁白"},
                ]
            )
        raise AssertionError(f"unexpected request: {path}")


class FakePlaybackApi(MaoerApi):
    def __init__(self) -> None:
        pass

    def _get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "info": {
                "sound": {
                    "soundstr": "新剧",
                    "soundurl": "https://static.example/audio.mp3",
                    "subtitle_url": SUBTITLE_URL,
                }
            }
        }

    def _is_full_drama_purchased(self, drama_id: int | None) -> bool:
        return False


class SubtitleCompatibilityTests(unittest.TestCase):
    def test_loads_independent_json_subtitles(self) -> None:
        api = FakeSubtitleApi()

        items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)

        self.assertEqual([item.text for item in items], ["旁白", "甲：你好", "普通弹幕"])
        self.assertEqual([item.time for item in items], [0.5, 1.25, 2.5])
        self.assertEqual([item.mode for item in items], [DANMAKU_MODE_SUBTITLE, DANMAKU_MODE_SUBTITLE, 1])
        self.assertEqual(items[1].color, 1122867)
        self.assertEqual(api.requested_paths, ["/sound/getdm", SUBTITLE_URL])

    def test_playback_info_preserves_subtitle_url(self) -> None:
        playback = FakePlaybackApi().playback_info(MediaItem(kind="sound", id=123, title="新剧"))

        self.assertEqual(playback.subtitle_url, SUBTITLE_URL)


if __name__ == "__main__":
    unittest.main()
