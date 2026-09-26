import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import requests

from maoer_api import AccountInfo, BASE_URL, ApiError, DANMAKU_MODE_SUBTITLE, MaoerApi, MediaItem, PublisherProfile, PurchaseRequired


SUBTITLE_URL = "https://static.example/subtitle.json"


class PublisherNavigationTests(unittest.TestCase):
    def test_followed_accounts_come_from_signed_in_account(self):
        api = MaoerApi(cookie="test-cookie")
        api.account_info = Mock(return_value=AccountInfo(123, "我", ""))
        api._get = Mock(return_value={"success": True, "info": {"Datas": [
            {"id": 10452520, "username": "三米造事务所", "attention": 1},
        ]}})

        items = api.followed_accounts(page=2)

        api._get.assert_called_once_with("/person/getuserattention", {
            "type": 0, "user_id": 123, "p": 2, "page_size": 30,
        })
        self.assertEqual([(item.kind, item.id, item.title) for item in items],
                         [("publisher_account", 10452520, "三米造事务所")])

    def test_followed_accounts_require_login(self):
        api = MaoerApi(cookie="")
        api._get = Mock()
        with self.assertRaisesRegex(ApiError, "请先登录"):
            api.followed_accounts()
        api._get.assert_not_called()

    def test_sound_opens_only_its_actual_drama(self):
        api = MaoerApi(cookie="")
        api._get = Mock(return_value={"success": True, "info": {
            "drama": {"id": 94733, "name": "灯花笑 上季", "user_id": 10452520,
                      "username": "三米造事务所", "author": "千山茶客"},
        }})

        item = api.drama_for_sound(13042654)

        api._get.assert_called_once_with("/dramaapi/getdramabysound", {"sound_id": 13042654})
        self.assertEqual((item.kind, item.id, item.title), ("drama", 94733, "灯花笑 上季"))
        self.assertEqual(item.raw["username"], "三米造事务所")

    def test_standalone_sound_reports_no_drama(self):
        api = MaoerApi(cookie="")
        api._get = Mock(return_value={"success": True, "info": {}})
        with self.assertRaisesRegex(ApiError, "没有关联的剧集"):
            api.drama_for_sound(123)

    def test_sound_publisher_uses_uploader_account(self):
        api = MaoerApi(cookie="")
        api._get = Mock(side_effect=[
            {"info": {"sound": {"id": 13042654, "user_id": 10452520,
                                  "username": "三米造事务所"},
                      "user": {"id": 10452520, "username": "三米造事务所"}}},
            {"success": True, "info": {"id": 10452520, "username": "三米造事务所",
                                       "fansnum": 0, "follownum": 1,
                                       "userintro": "<p>同名微博：三米造事务所</p>"}},
        ])

        profile = api.publisher_profile_for_item(MediaItem(kind="sound", id=13042654, title="预告"))

        self.assertEqual(profile.user_id, 10452520)
        self.assertEqual(profile.name, "三米造事务所")
        self.assertEqual((profile.followers, profile.following), (0, 1))
        self.assertEqual(profile.bio, "同名微博：三米造事务所")

    def test_profile_preloads_follow_status_and_follow_action_uses_official_type(self):
        api = MaoerApi(cookie="test-cookie")
        api._get = Mock(return_value={"success": True, "info": {
            "id": 10452520, "username": "三米造事务所", "fansnum": 3,
            "follownum": 1, "followed": 1,
        }})
        api._post_form_api = Mock(side_effect=[
            {"success": True, "info": "取消关注成功"},
            {"success": True, "info": "关注成功"},
        ])

        profile = api.publisher_profile(10452520)
        cancel_message = api.set_publisher_follow(profile.user_id, False)
        follow_message = api.set_publisher_follow(profile.user_id, True)

        self.assertTrue(profile.followed)
        self.assertEqual((cancel_message, follow_message), ("取消关注成功", "关注成功"))
        self.assertEqual(api._post_form_api.call_args_list[0].args[1],
                         {"attentionid": 10452520, "type": 0})
        self.assertEqual(api._post_form_api.call_args_list[1].args[1],
                         {"attentionid": 10452520, "type": 1})

    def test_drama_publisher_is_not_original_author(self):
        api = MaoerApi(cookie="")
        api._get = Mock(side_effect=[
            {"info": {"drama": {"id": 94733, "user_id": 10452520, "author": "千山茶客"}}},
            {"success": True, "info": {"id": 10452520, "username": "三米造事务所",
                                       "fansnum": 194648, "follownum": 1, "userintro": "简介"}},
        ])

        profile = api.publisher_profile_for_item(MediaItem(kind="drama", id=94733, title="灯花笑"))

        self.assertEqual(profile.name, "三米造事务所")

    def test_publisher_lists_use_published_works_not_followed_works(self):
        api = MaoerApi(cookie="")
        profile = PublisherProfile(10452520, "三米造事务所", 100, 1, "简介")
        api._get = Mock(side_effect=[
            {"success": True, "info": {"Datas": [{"id": 94733, "name": "灯花笑 上季"}]}},
            {"success": True, "info": {"Datas": [{"id": 13042654, "soundstr": "预告·归路"}]}},
        ])

        dramas = api.publisher_dramas(profile, page=2)
        sounds = api.publisher_sounds(profile, page=2)

        self.assertEqual(api._get.call_args_list[0].args[0], "/dramaapi/getuserdramas")
        self.assertEqual(api._get.call_args_list[1].args[0], "/person/getusersound")
        self.assertEqual([item.id for item in dramas], [94733])
        self.assertEqual([item.id for item in sounds], [13042654])
        self.assertEqual(dramas[0].raw["_publisher_name"], profile.name)
        self.assertEqual(sounds[0].raw["_publisher_name"], profile.name)


class PlaybackHistoryTests(unittest.TestCase):
    def test_account_history_preserves_dates_order_and_sound_ids(self):
        api = MaoerApi(cookie="test-cookie")
        api._get = Mock(return_value={
            "success": True,
            "info": [
                {"time": "今天", "sound": [
                    {"id": 12, "soundstr": "第一集"},
                    {"id": 13, "soundstr": "第二集"},
                ]},
                {"time": "昨天", "sound": [{"id": 9, "soundstr": "旧音频"}]},
            ],
        })

        items = api.playback_history()

        api._get.assert_called_once_with("/mperson/gethistory")
        self.assertEqual([(item.kind, item.id, item.title) for item in items], [
            ("sound", 12, "第一集"),
            ("sound", 13, "第二集"),
            ("sound", 9, "旧音频"),
        ])
        self.assertEqual([item.raw["_history_date"] for item in items], ["今天", "今天", "昨天"])
        self.assertNotIn("position", items[0].raw)

    def test_history_requires_login_and_rejects_failed_response(self):
        api = MaoerApi(cookie="")
        api._get = Mock()
        with self.assertRaisesRegex(ApiError, "请先登录"):
            api.playback_history()
        api._get.assert_not_called()

        api = MaoerApi(cookie="test-cookie")
        api._get = Mock(return_value={"success": False, "info": "需要登录"})
        with self.assertRaisesRegex(ApiError, "需要登录"):
            api.playback_history()


class AccountMembershipTests(unittest.TestCase):
    def make_api(self, vip_info):
        api = MaoerApi(cookie="test-cookie")
        api._get = Mock(return_value={"info": {"id": 123, "nickname": "测试账号"}})
        api._account_attention_count = Mock(return_value=None)
        api._get_json_allow_failure = Mock(return_value={
            "code": 0,
            "data": {"vip_info": vip_info},
        })
        return api

    def test_member_account_shows_expiry(self):
        end_time = 1893456000
        api = self.make_api({"status": 1, "end_time": end_time})

        account = api.account_info()

        expected = datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M")
        self.assertIn(f"会员到期时间：{expected}", account.text)

    def test_nonmember_does_not_show_expiry(self):
        api = self.make_api({"status": 0, "end_time": 1893456000})

        self.assertNotIn("会员到期时间", api.account_info().text)

    def test_missing_expiry_or_vip_error_keeps_account_info(self):
        api = self.make_api({"status": 1})
        self.assertNotIn("会员到期时间", api.account_info().text)

        api = self.make_api({"status": 1, "end_time": 1893456000})
        api._get_json_allow_failure.side_effect = requests.RequestException("offline")
        account = api.account_info()
        self.assertIn("昵称：测试账号", account.text)
        self.assertNotIn("会员到期时间", account.text)

    def test_other_user_info_does_not_show_my_membership(self):
        api = self.make_api({"status": 1, "end_time": 1893456000})

        account = api.account_info(user_id=123)

        self.assertNotIn("会员到期时间", account.text)
        api._get_json_allow_failure.assert_not_called()


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


class FakeMemberPlaybackApi(MaoerApi):
    def __init__(self) -> None:
        super().__init__(cookie="member-cookie")

    def _get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
        if path == "/x/vip/subscribe-info":
            return {
                "code": 0,
                "data": {
                    "user_vip_info": {
                        "status": 1,
                    },
                },
            }
        if path == "/mperson/getdramabought":
            return {"info": {"data": []}}
        if path == "/sound/getsound":
            return {
                "info": {
                    "sound": {
                        "soundstr": "会员限免剧",
                        "soundurl": "https://static.example/member-free.mp3",
                        "need_pay": 1,
                        "vip": 1,
                    },
                },
            }
        raise AssertionError(f"unexpected request: {path}")


class SubtitleCompatibilityTests(unittest.TestCase):
    def test_json_display_segments_are_not_rewritten_using_xml_evidence(self):
        api = MaoerApi(cookie="")
        xml = '<i><d p="587.42,4,25,0">露西：宝贝，是我把你累着了吗？</d></i>'
        data = json.dumps([
            {"start_time": 586520, "end_time": 587270, "role": "露西", "content": "宝贝 是我把你累着了吗？"},
            {"start_time": 587270, "end_time": 589025, "role": "系统", "content": "系统的文字"},
            {"start_time": 587270, "end_time": 589025, "role": "露西", "content": "宝贝 是我把你累着了吗？"},
        ])
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
            items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)
        self.assertEqual([item.time for item in items], [586.52, 587.27, 587.27])

    def test_continuous_same_json_words_remain_when_xml_has_two_distinct_utterances(self):
        api = MaoerApi(cookie="")
        xml = '<i><d p="1.1,4,25,0">甲：你到底有没有听见</d><d p="3.1,4,25,0">甲：你到底有没有听见</d></i>'
        data = json.dumps([
            {"start_time": 1000, "end_time": 3000, "role": "甲", "content": "你到底有没有听见"},
            {"start_time": 3000, "end_time": 5000, "role": "甲", "content": "你到底有没有听见"},
        ])
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
            items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)
        self.assertEqual([item.time for item in items], [1, 3])

    def test_missing_json_end_times_are_not_evidence_of_continuous_display(self):
        api = MaoerApi(cookie="")
        xml = '<i><d p="1.1,4,25,0">甲：你到底有没有听见</d></i>'
        data = json.dumps([
            {"start_time": 1000, "role": "甲", "content": "你到底有没有听见"},
            {"start_time": 3000, "role": "甲", "content": "你到底有没有听见"},
        ])
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
            items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)
        self.assertEqual([item.time for item in items], [1, 3])

    def test_scene_copies_and_xml_only_later_repeat_are_not_added_to_json_track(self):
        api = MaoerApi(cookie="")
        for scene in ("【酒吧】", "【酒店 宴会厅彩排现场】"):
            with self.subTest(scene=scene):
                xml = f'<i><d p="100,4,25,0">{scene}</d><d p="300,4,25,0">{scene}</d></i>'
                data = json.dumps([{"start_time": 108500, "end_time": 110000, "content": scene}])
                with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
                    items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)
                self.assertEqual([item.time for item in items], [108.5])

    def test_json_continuation_does_not_import_xml_contributor_or_content(self):
        api = MaoerApi(cookie="")
        xml = '<i><d p="463.22,4,25,0,0,0,3586609">竟然觉得她今晚看着还挺顺眼的</d></i>'
        data = '[{"start_time":463187,"end_time":466634,"role":"栾念","content":"竟然觉得她今晚看着还挺顺眼"}]'
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
            items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].user_id, "")
        self.assertEqual(items[0].text, "栾念：竟然觉得她今晚看着还挺顺眼")
        self.assertEqual(items[0].role, "栾念")

    def test_other_xml_roles_and_changed_meaning_do_not_supplement_json(self):
        api = MaoerApi(cookie="")
        for text in ("乙：今天晚上的晚会大家都参加的", "甲：今天晚上的晚会大家都不参加", "今天晚上的晚会大家都参加的好多啊"):
            with self.subTest(text=text):
                xml = f'<i><d p="1.2,4,25,0">{text}</d></i>'
                data = '[{"start_time":1000,"end_time":3000,"role":"甲","content":"今天晚上的晚会大家都参加"}]'
                with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
                    items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)
                self.assertEqual([item.text for item in items], ["甲：今天晚上的晚会大家都参加"])

    def test_short_role_labelled_caption_merges_despite_small_source_timing_drift(self):
        api = MaoerApi(cookie="")
        for role, content, start, end, xml_time in (
            ("栾念", "嗯？", 778269, 779992, 778.79),
            ("尚之桃", "……", 2119256, 2122000, 2119.79),
            ("甲", "好", 1000, 3000, 1.7),
            ("尚之桃", "可以呀 其实我们是第一次做这么大的项目", 154063, 157000, 153.5),
            ("姜澜", "当然啊", 280152, 282000, 279.43),
            ("甲", "好", 1700, 3000, 1.0),
        ):
            with self.subTest(role=role):
                xml = f'<i><d p="{xml_time},4,25,0">{role}：{content}</d></i>'
                data = json.dumps([{"start_time": start, "end_time": end, "role": role, "content": content}])
                with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
                    items = api.sound_danmaku(9100588, subtitle_url=SUBTITLE_URL)
                self.assertEqual([(item.time, item.text) for item in items], [(start / 1000, f"{role}：{content}")])

    def test_short_xml_lines_are_excluded_regardless_of_role_or_timing(self):
        api = MaoerApi(cookie="")
        for text in ("嗯？", "乙：嗯？"):
            with self.subTest(text=text):
                xml = f'<i><d p="1.65,4,25,0">{text}</d></i>'
                data = '[{"start_time":1000,"end_time":3000,"role":"甲","content":"嗯？"}]'
                with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
                    items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)
                self.assertEqual([item.text for item in items], ["甲：嗯？"])

    def test_xml_response_order_does_not_change_selected_json(self):
        api = MaoerApi(cookie="")
        data = '[{"start_time":1000,"end_time":3000,"role":"甲","content":"嗯？"}]'
        for times in ((1.55, 1.70), (1.70, 1.55)):
            with self.subTest(times=times):
                xml = '<i>' + ''.join(f'<d p="{time},4,25,0">甲：嗯？</d>' for time in times) + '</i>'
                with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
                    items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)
                self.assertEqual([item.time for item in items], [1.0])

    def test_split_xml_captions_do_not_repeat_a_complete_json_line(self) -> None:
        api = MaoerApi(cookie="")
        xml = (
            '<i><d p="151.02,4,25,0,0,0,10283562,1">陆驿站：你这几天还好吗？</d>'
            '<d p="152.61,4,25,0,0,0,10283562,2">难过？生气？</d>'
            '<d p="153.20,4,25,0,0,0,10283562,3">【开门声】</d></i>'
        )
        json_subtitles = json.dumps([{
            "start_time": 150560, "end_time": 154770, "role": "陆驿站",
            "content": "你这几天还好吗？难过？生气？",
        }], ensure_ascii=False)
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else json_subtitles):
            items = api.sound_danmaku(7741548, subtitle_url=SUBTITLE_URL)

        self.assertEqual([(item.time, item.text) for item in items], [
            (150.56, "陆驿站：你这几天还好吗？难过？生气？"),
        ])

    def test_nearby_punctuation_variant_is_one_subtitle(self) -> None:
        api = MaoerApi(cookie="")
        xml = '<i><d p="148.14,4,25,0,0,0,4034642,1">白柳：嗯，差不多是这样</d></i>'
        json_subtitles = json.dumps([{
            "start_time": 147740, "end_time": 150000, "role": "白柳",
            "content": "嗯 差不多是这样",
        }], ensure_ascii=False)
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else json_subtitles):
            items = api.sound_danmaku(7741548, subtitle_url=SUBTITLE_URL)

        self.assertEqual([item.text for item in items], ["白柳：嗯 差不多是这样"])

    def test_separate_xml_repeated_line_does_not_supplement_json(self) -> None:
        api = MaoerApi(cookie="")
        xml = (
            '<i><d p="1.1,4,25,0,0,0,1,1">陆驿站：你这几天还好吗？</d>'
            '<d p="3.0,4,25,0,0,0,1,2">陆驿站：你这几天还好吗？</d></i>'
        )
        json_subtitles = json.dumps([{
            "start_time": 1000, "end_time": 3500, "role": "陆驿站",
            "content": "你这几天还好吗？",
        }], ensure_ascii=False)
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else json_subtitles):
            items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)

        self.assertEqual([(item.time, item.text) for item in items], [
            (1.0, "陆驿站：你这几天还好吗？"),
        ])

    def test_xml_content_only_merges_with_nearby_structured_dialogue(self) -> None:
        api = MaoerApi(cookie="")
        xml = '<i><d p="1.0,4,25,0">你好</d><d p="3.0,4,25,0">你好</d></i>'
        json_subtitles = '[{"start_time":1250,"role":"甲","content":"你好"}]'
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else json_subtitles):
            items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)

        self.assertEqual([(item.time, item.text) for item in items],
                         [(1.25, "甲：你好")])
        self.assertEqual((items[0].role, items[0].content), ("甲", "你好"))

    def test_overlapping_sources_use_json_and_exclude_xml_only_repeat(self) -> None:
        api = MaoerApi(cookie="")
        xml = (
            '<i><d p="1.0,4,25,0">甲:你好</d>'
            '<d p="3.0,4,25,0">甲：你好</d></i>'
        )
        json_subtitles = '[{"start_time":1250,"role":"甲","content":"你好"}]'
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else json_subtitles):
            items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)

        self.assertEqual([(item.time, item.text) for item in items],
                         [(1.25, "甲：你好")])
        self.assertEqual((items[0].role, items[0].content), ("甲", "你好"))

    def test_json_content_already_containing_speaker_is_not_doubled(self) -> None:
        api = MaoerApi(cookie="")
        xml = '<i><d p="1.0,4,25,0">陆驿站：你这几天还好吗？</d></i>'
        json_subtitles = '[{"start_time":1250,"role":"陆驿站","content":"陆驿站：你这几天还好吗？"}]'
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else json_subtitles):
            items = api.sound_danmaku(7741548, subtitle_url=SUBTITLE_URL)

        self.assertEqual([(item.time, item.text) for item in items],
                         [(1.25, "陆驿站：你这几天还好吗？")])
        self.assertEqual((items[0].role, items[0].content), ("陆驿站", "你这几天还好吗？"))

    def test_speaker_prefix_normalisation_handles_other_roles_and_colons(self) -> None:
        api = MaoerApi(cookie="")
        json_subtitles = json.dumps([
            {"start_time": 1000, "role": "白柳", "content": "白柳: 你在这里啊"},
            {"start_time": 2000, "role": "裴云暎", "content": "裴云暎：裴云暎：我知道了"},
            {"start_time": 3000, "role": "陆驿站", "content": "白柳：你这几天还好吗？"},
        ], ensure_ascii=False)
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: "<i/>" if path == "/sound/getdm" else json_subtitles):
            items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)

        self.assertEqual([item.text for item in items], [
            "白柳：你在这里啊", "裴云暎：我知道了", "陆驿站：白柳：你这几天还好吗？",
        ])

    def test_legacy_xml_subtitle_with_repeated_speaker_is_readable_once(self) -> None:
        api = MaoerApi(cookie="")
        xml = '<i><d p="1.0,4,25,0">陆驿站：陆驿站：你这几天还好吗？</d></i>'
        with patch.object(api, "_get_text", return_value=xml):
            items = api.sound_danmaku(7741548, subtitle_url="")

        self.assertEqual([item.text for item in items], ["陆驿站：你这几天还好吗？"])

    def test_loads_independent_json_subtitles(self) -> None:
        api = FakeSubtitleApi()

        items = api.sound_danmaku(123, subtitle_url=SUBTITLE_URL)

        self.assertEqual([item.text for item in items], ["旁白", "甲：你好", "普通弹幕"])
        self.assertEqual([item.time for item in items], [0.5, 1.25, 2.5])
        self.assertEqual([item.mode for item in items], [DANMAKU_MODE_SUBTITLE, DANMAKU_MODE_SUBTITLE, 1])
        self.assertEqual(items[1].color, 1122867)
        self.assertEqual([(item.role, item.content) for item in items[:2]],
                         [("", "旁白"), ("甲", "你好")])
        self.assertEqual(api.requested_paths, ["/sound/getdm", SUBTITLE_URL])

    def test_playback_info_preserves_subtitle_url(self) -> None:
        playback = FakePlaybackApi().playback_info(MediaItem(kind="sound", id=123, title="新剧"))

        self.assertEqual(playback.subtitle_url, SUBTITLE_URL)


class MemberEntitlementTests(unittest.TestCase):
    def test_member_can_play_vip_limited_free_sound(self) -> None:
        item = MediaItem(
            kind="sound",
            id=456,
            title="会员限免剧",
            need_pay=True,
            raw={"vip": 1},
        )

        playback = FakeMemberPlaybackApi().playback_info(item)

        self.assertEqual(playback.url, "https://static.example/member-free.mp3")


class DramaPurchaseDisplayTests(unittest.TestCase):
    def test_drama_episode_lists_contain_only_sounds(self) -> None:
        api = MaoerApi(cookie="")
        api._drama_detail_data = Mock(return_value={
            "drama": {"id": 12, "name": "付费剧", "pay_type": 2, "need_pay": 1, "price": 30},
            "episodes": {"episode": [{"sound_id": 34, "name": "第一集", "need_pay": 1, "pay_type": 2}]},
        })
        api._is_full_drama_purchased = Mock(return_value=False)
        api._get = Mock(return_value={"info": {"Datas": [{"id": 34, "soundstr": "第一集"}]}})

        for items in (api.drama_episodes(12), api.drama_episodes_page(12, 1)):
            self.assertEqual([item.kind for item in items], ["sound"])
            self.assertTrue(items[0].need_pay)

    def test_paid_vip_episode_is_marked_limited_free_only_for_active_member(self) -> None:
        for active in (True, False):
            with self.subTest(active=active):
                api = MaoerApi(cookie="member-cookie" if active else "")
                api._drama_detail_data = Mock(return_value={
                    "drama": {"id": 12, "name": "限免剧"},
                    "episodes": {"episode": [
                        {"sound_id": 34, "name": "第一集", "need_pay": 1, "vip": 2},
                        {"sound_id": 35, "name": "第二集", "need_pay": 1, "vip": 0},
                    ]},
                })
                api._is_full_drama_purchased = Mock(return_value=False)
                api._is_member_vip_active = Mock(return_value=active)
                api._get = Mock(return_value={"info": {"Datas": [
                    {"id": 34, "soundstr": "第一集", "vip": 2},
                    {"id": 35, "soundstr": "第二集", "vip": 0},
                ]}})

                for items in (api.drama_episodes(12), api.drama_episodes_page(12, 1)):
                    self.assertEqual([item.need_pay for item in items], [True, True])
                    self.assertEqual(bool(items[0].raw.get("_member_vip_limited_free")), active)
                    self.assertFalse(items[1].raw.get("_member_vip_limited_free", False))

    def test_server_paywall_prompts_even_if_episode_list_did_not_mark_paid(self) -> None:
        api = FakePlaybackApi()
        api._get = Mock(return_value={"info": {"sound": {
            "soundstr": "第一集", "soundurl": "https://static.example/audio.mp3", "need_pay": 1,
        }}})
        item = MediaItem(kind="sound", id=34, title="第一集", need_pay=False)

        with self.assertRaises(PurchaseRequired):
            api.playback_info(item)

        item.raw["_purchased_sound"] = True
        self.assertEqual(api.playback_info(item).url, "https://static.example/audio.mp3")


class PublisherResolutionTests(unittest.TestCase):
    def test_album_detail_uses_uploader_when_list_only_has_promo_text(self):
        api = MaoerApi(cookie="")
        api._get = Mock(return_value={"info": {"album": {
            "id": 78, "user_id": 12, "username": "歌单发布账号",
        }}})
        item = MediaItem(kind="album", id=78, title="歌单", subtitle="首页推荐", raw={})

        self.assertEqual(api.publisher_name_for_item(item), "歌单发布账号")
        api._get.assert_called_once_with("/sound/soundalllist", {"albumid": 78})

    def test_sound_detail_uses_verified_uploader_and_caches_it(self):
        api = MaoerApi(cookie="")
        api._get = Mock(return_value={"info": {
            "sound": {"id": 34, "user_id": 12, "username": "发布账号"},
            "user": {"id": 12, "username": "发布账号"},
        }})
        item = MediaItem(kind="sound", id=34, title="测试", subtitle="首页热门声音", raw={})

        self.assertEqual(api.publisher_name_for_item(item), "发布账号")
        self.assertEqual(api.publisher_name_for_item(item), "发布账号")
        api._get.assert_called_once_with("/sound/getsound", {"soundid": 34})

    def test_drama_and_detail_use_account_not_author_credit(self):
        api = MaoerApi(cookie="")
        api._drama_detail_data = Mock(return_value={
            "drama": {"id": 56, "name": "测试剧", "user_id": 12, "author": "原著作者及出品说明"},
            "episodes": {"episode": [{"sound_id": 34}]},
            "cvs": [],
        })
        api._get = Mock(return_value={"info": {
            "sound": {"id": 34, "user_id": 12, "username": "发布账号"},
            "user": {"id": 12, "username": "发布账号"},
        }})
        item = MediaItem(kind="drama", id=56, title="测试剧", subtitle="首页推荐",
                         raw={"author": "原著作者及出品说明"})

        self.assertEqual(api.publisher_name_for_item(item), "发布账号")
        detail = api.drama_detail_text(56)
        self.assertIn("发布：发布账号", detail)
        self.assertNotIn("作者：原著作者及出品说明", detail)
        api._get.assert_called_once()

    def test_drama_rejects_mismatched_episode_uploader(self):
        api = MaoerApi(cookie="")
        api._drama_detail_data = Mock(return_value={
            "drama": {"id": 56, "user_id": 12},
            "episodes": {"episode": [{"sound_id": 34}, {"sound_id": 35}]},
        })
        api._get = Mock(side_effect=[
            {"info": {"sound": {"id": 34, "user_id": 99, "username": "别人的账号"}}},
            {"info": {"sound": {"id": 35, "user_id": 12, "username": "真正的发布账号"}}},
        ])

        self.assertEqual(api.publisher_name_for_item(MediaItem(kind="drama", id=56, title="剧")),
                         "真正的发布账号")

    def test_drama_without_episodes_checks_owner_uploaded_sounds(self):
        api = MaoerApi(cookie="")
        api._drama_detail_data = Mock(return_value={
            "drama": {"id": 56, "user_id": 12}, "episodes": {},
        })
        api._get = Mock(side_effect=[
            {"info": {"Datas": [{"id": 34}]}},
            {"info": {"sound": {"id": 34, "user_id": 12, "username": "发布账号"}}},
        ])

        self.assertEqual(api.publisher_name_for_item(MediaItem(kind="drama", id=56, title="预告中")),
                         "发布账号")
        self.assertEqual(api._get.call_args_list[0].args[0], "/person/getusersound")

    def test_cv_group_uses_official_value_and_blank_is_not_free_person(self):
        api = MaoerApi(cookie="")
        rows = api._drama_cv_lines({"cvs": [
            {"character": "周子舒", "cv_info": {"name": "夏磊", "group": "音熊联萌工作室"}},
            {"character": "旁白", "cv_info": {"name": "三石", "group": "自由人"}},
            {"character": "路人", "cv_info": {"name": "孙晔", "group": ""}},
        ]})
        self.assertEqual(rows[1:], [
            "周子舒：夏磊（音熊联萌工作室）",
            "旁白：三石（自由人）",
            "路人：孙晔（无）",
        ])


class SoundIntroTests(unittest.TestCase):
    def test_uses_native_sound_intro_not_drama_abstract(self):
        api = MaoerApi(cookie="")
        api._get = Mock(return_value={"info": {"sound": {
            "id": 34, "soundstr": "第一集", "intro": "<p>单集自己的介绍<br>第二行&amp;补充</p>",
        }}})

        self.assertEqual(api.sound_intro_text(34), "单集自己的介绍\n第二行&补充")
        api._get.assert_called_once_with("/sound/getsound", {"soundid": 34})

    def test_empty_intro_never_falls_back_to_drama_description(self):
        api = MaoerApi(cookie="")
        api._get = Mock(return_value={"info": {"sound": {"id": 34, "intro": ""}}})
        self.assertEqual(api.sound_intro_text(34), "该音频暂无简介")


class DramaFollowTests(unittest.TestCase):
    def test_cached_follow_status_reuses_authenticated_detail_without_request(self):
        api = MaoerApi(cookie="test-cookie")
        api._get = Mock()
        api._drama_detail_cache[12] = {"like": 1}
        self.assertTrue(api.cached_drama_follow_status(12))
        api._drama_detail_cache[12] = {"like": 0}
        self.assertFalse(api.cached_drama_follow_status(12))
        api._get.assert_not_called()

        api.set_cookie("another-cookie")
        self.assertIsNone(api.cached_drama_follow_status(12))

    def test_status_reads_account_specific_detail_without_using_cache(self):
        api = MaoerApi(cookie="test-cookie")
        api._drama_detail_cache[12] = {"like": 0}
        api._get = Mock(return_value={"info": {"like": 1}})

        self.assertTrue(api.drama_follow_status(12))
        api._get.assert_called_once_with("/dramaapi/getdrama", {"drama_id": 12})

    def test_follow_and_unfollow_submit_explicit_target_once(self):
        api = MaoerApi(cookie="test-cookie")
        api._post_form_api = Mock(return_value={"success": True, "info": {"subscribe": 1}})
        api._get = Mock(side_effect=[{"info": {"like": 1}}, {"info": {"like": 0}}])

        self.assertTrue(api.set_drama_follow(12, follow=True))
        self.assertFalse(api.set_drama_follow(12, follow=False))
        self.assertEqual(api._post_form_api.call_count, 2)
        self.assertEqual(api._post_form_api.call_args_list[0].args[:2], (
            "/dramaapi/subscribe", {"drama_id": 12, "type": 1}
        ))
        self.assertEqual(api._post_form_api.call_args_list[1].args[:2], (
            "/dramaapi/subscribe", {"drama_id": 12, "type": 0}
        ))

    def test_follow_result_preserves_server_success_message(self):
        api = MaoerApi(cookie="test-cookie")
        api._post_form_api = Mock(return_value={"success": True, "info": {
            "msg": "喵！自己追的剧，跪着也要看完哦！", "subscribe": 1,
        }})
        api.drama_follow_status = Mock(return_value=True)

        result = api.set_drama_follow_result(12, follow=True)

        self.assertTrue(result.followed)
        self.assertEqual(result.message, "喵！自己追的剧，跪着也要看完哦！")

    def test_missing_login_or_unknown_status_never_posts(self):
        api = MaoerApi(cookie="")
        api._post_form_api = Mock()
        with self.assertRaisesRegex(ApiError, "需要登录"):
            api.set_drama_follow(12, follow=True)
        api._post_form_api.assert_not_called()

        api.set_cookie("test-cookie")
        api._get = Mock(return_value={"info": {}})
        with self.assertRaisesRegex(ApiError, "没有返回追剧状态"):
            api.drama_follow_status(12)

    def test_failed_post_does_not_claim_success_or_fetch_status(self):
        api = MaoerApi(cookie="test-cookie")
        api._post_form_api = Mock(return_value={"success": False, "info": "请求失败"})
        api._get = Mock()
        with self.assertRaises(ApiError):
            api.set_drama_follow(12, follow=True)
        api._post_form_api.assert_called_once()
        api._get.assert_not_called()


class VipCatalogTests(unittest.TestCase):
    def make_api(self, module_id=400, max_page=3, page=1, dramas=None):
        api = MaoerApi(cookie="")
        api._get_json_allow_failure = Mock(return_value={
            "code": 0,
            "success": True,
            "info": {
                "id": module_id,
                "elements": {
                    "Datas": dramas if dramas is not None else [{"id": 12, "name": "剧集", "is_subscribe": True}],
                    "pagination": {"p": page, "maxpage": max_page, "pagesize": 30},
                },
            },
        })
        return api

    def test_two_catalogs_use_distinct_official_modules(self):
        for kind, module_id, label in (("free", 400, "会员限免"), ("discount", 401, "会员折扣")):
            with self.subTest(kind=kind):
                api = self.make_api(module_id=module_id)
                items = api.vip_dramas(kind)
                api._get_json_allow_failure.assert_called_once_with(
                    "/theatre/module-details", {"module_id": module_id, "page": 1}
                )
                self.assertEqual((items[0].kind, items[0].id), ("drama", 12))
                self.assertIn(label, items[0].subtitle)
                self.assertTrue(items[0].raw["_hide_author"])
                self.assertNotIn("_purchased_full_drama", items[0].raw)
                self.assertIsNone(items[0].price)

    def test_page_parameter_and_server_limit_are_used(self):
        api = self.make_api(max_page=2, page=2)
        self.assertEqual(len(api.vip_dramas("free", 2)), 1)
        api._get_json_allow_failure.assert_called_once_with(
            "/theatre/module-details", {"module_id": 400, "page": 2}
        )
        self.assertEqual(api.vip_dramas("free", 3), [])
        self.assertEqual(api._get_json_allow_failure.call_count, 1)
        api.vip_dramas("free", 1)
        self.assertEqual(api._get_json_allow_failure.call_count, 2)

    def test_empty_page_is_valid(self):
        self.assertEqual(self.make_api(max_page=0, dramas=[]).vip_dramas("free"), [])

    def test_malformed_response_or_wrong_module_is_not_empty_success(self):
        for payload in ({"code": 0, "info": {}}, {"code": 0, "info": {"id": 400, "elements": {}}},
                        {"code": 100010006}, {"code": 0, "info": {"id": 401}}):
            with self.subTest(payload=payload):
                api = self.make_api()
                api._get_json_allow_failure.return_value = payload
                with self.assertRaises(ApiError):
                    api.vip_dramas("free")

    def test_invalid_and_duplicate_rows_are_skipped(self):
        api = self.make_api(dramas=[None, {}, {"id": 12, "name": "甲"}, {"id": 12}, {"id": 13, "name": "乙"}])
        self.assertEqual([item.id for item in api.vip_dramas("free")], [12, 13])


class FormPostResponseTests(unittest.TestCase):
    class Response:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code
            self.ok = status_code < 400

        def json(self):
            return self.payload

        def raise_for_status(self):
            if not self.ok:
                raise requests.HTTPError(f"HTTP {self.status_code}")

    def test_form_post_uses_target_host_for_origin_and_default_referer(self):
        api = MaoerApi(cookie="test-cookie")
        api.session.post = Mock(return_value=self.Response({"code": 0}))

        api._post_form_api(BASE_URL + "/financial/buydrama", {})

        kwargs = api.session.post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Origin"], BASE_URL)
        self.assertEqual(kwargs["headers"]["Referer"], BASE_URL + "/")

    def test_http_error_preserves_server_message_from_json_body(self):
        api = MaoerApi(cookie="test-cookie")
        api.session.post = Mock(return_value=self.Response({"code": 123, "message": "请求被拒绝"}, 403))

        with self.assertRaisesRegex(ApiError, "请求被拒绝"):
            api._post_form_api(BASE_URL + "/financial/buydrama", {})


if __name__ == "__main__":
    unittest.main()
