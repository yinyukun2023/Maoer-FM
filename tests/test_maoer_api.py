import json
import unittest
from datetime import datetime
from unittest.mock import Mock

import requests

from maoer_api import BASE_URL, ApiError, DANMAKU_MODE_SUBTITLE, MaoerApi, MediaItem, PurchaseRequired


SUBTITLE_URL = "https://static.example/subtitle.json"


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
