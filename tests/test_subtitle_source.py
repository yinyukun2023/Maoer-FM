"""One subtitle source per episode, independently of ordinary danmaku."""
import json
import unittest
from unittest.mock import patch

import requests

from maoer_api import ApiError, MaoerApi


SUBTITLE_URL = "https://static.example/subtitle.json"
CANGNAN_XML = '''<i>
<d p="30.54,4,25,0">许南珩：（烦躁）啧</d>
<d p="34.32,4,25,0">（想要调整情绪，长长呼出口气）呼——</d>
<d p="61.38,4,25,0">许南珩：昨天有点不舒服，睡的有点久</d>
<d p="144.49,4,25,0">【不远处的路边有几个摊贩，许南珩在路边停下】</d>
<d p="200,4,25,0">只在弹幕式字幕中存在的说明</d>
<d p="50,1,25,0">普通观众弹幕</d></i>'''
CANGNAN_JSON = [
    {"start_time": 31025, "role": "许南珩", "content": "啧"},
    {"start_time": 33725, "role": "许南珩", "content": "（想要调整情绪）呼——"},
    {"start_time": 61250, "role": "许南珩", "content": "昨天有点不舒服 睡得有点久"},
    {"start_time": 131950, "content": "【不远处的路边有几个摊贩 许南珩在路边停下】"},
]


class SubtitleSourceSelectionTests(unittest.TestCase):
    def load(self, json_data, xml_data=CANGNAN_XML, url=SUBTITLE_URL):
        api = MaoerApi(cookie="")
        def get_text(path, params=None):
            result = xml_data if path == "/sound/getdm" else json_data
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(api, "_get_text", side_effect=get_text):
            return api.sound_danmaku(10998432, subtitle_url=url)

    def test_cangnan_uses_only_json_subtitles_and_keeps_ordinary_danmaku(self):
        items = self.load(json.dumps(CANGNAN_JSON))
        self.assertEqual([item.time for item in items if item.mode == 4], [31.025, 33.725, 61.25, 131.95])
        self.assertEqual([item.text for item in items if item.mode != 4], ["普通观众弹幕"])
        self.assertEqual([item.time for item in items], sorted(item.time for item in items))

    def test_unavailable_independent_source_falls_back_to_whole_xml_track(self):
        for data in ("[]", "{}", "null", "<html>error</html>", "[null, 1, {}]",
                     requests.Timeout("timeout"), requests.HTTPError("404"), ApiError("failed")):
            with self.subTest(data=repr(data)):
                items = self.load(data)
                self.assertEqual([item.time for item in items if item.mode == 4], [30.54, 34.32, 61.38, 144.49, 200])
                self.assertEqual(len(items), 6)

    def test_bad_rows_do_not_make_an_unusable_source_look_valid(self):
        for start in (None, "", "broken", -1, float("nan"), float("inf"), True):
            with self.subTest(start=start):
                items = self.load(json.dumps([{"start_time": start, "content": "invalid"}]))
                self.assertEqual(sum(item.mode == 4 for item in items), 5)

    def test_partly_valid_json_does_not_fill_its_gaps_from_xml(self):
        items = self.load(json.dumps([{"content": "missing time"}, CANGNAN_JSON[0], None]))
        self.assertEqual([item.time for item in items if item.mode == 4], [31.025])

    def test_no_subtitle_url_uses_xml_without_fetching_json(self):
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", return_value=CANGNAN_XML) as get:
            items = api.sound_danmaku(1, subtitle_url="")
        get.assert_called_once_with("/sound/getdm", {"soundid": 1})
        self.assertEqual(len(items), 6)

    def test_metadata_discovery_failure_can_still_use_xml(self):
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", return_value=CANGNAN_XML), \
             patch.object(api, "_get", side_effect=requests.Timeout("timeout")):
            items = api.sound_danmaku(1)
        self.assertEqual(len(items), 6)

    def test_independent_subtitles_work_when_danmaku_is_unavailable(self):
        for xml in ("<invalid", requests.Timeout("timeout")):
            with self.subTest(xml=repr(xml)):
                items = self.load(json.dumps(CANGNAN_JSON), xml)
                self.assertEqual([item.time for item in items], [31.025, 33.725, 61.25, 131.95])

    def test_both_sources_failing_is_not_reported_as_success(self):
        with self.assertRaises(ApiError):
            self.load(requests.Timeout("JSON failed"), requests.Timeout("XML failed"))

    def test_source_selection_is_per_episode_not_cached_from_previous(self):
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", side_effect=[CANGNAN_XML, json.dumps(CANGNAN_JSON), CANGNAN_XML, "[]"]):
            first = api.sound_danmaku(1, subtitle_url=SUBTITLE_URL)
            second = api.sound_danmaku(2, subtitle_url=SUBTITLE_URL)
        self.assertEqual(sum(i.mode == 4 for i in first), 4)
        self.assertEqual(sum(i.mode == 4 for i in second), 5)

    def test_later_repeated_cues_in_selected_source_are_not_globally_removed(self):
        data = [{"start_time": time, "role": "甲", "content": "你好"} for time in (1000, 3000)]
        self.assertEqual([i.time for i in self.load(json.dumps(data), "<i/>")], [1, 3])
