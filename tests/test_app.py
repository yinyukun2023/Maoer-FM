import unittest
from dataclasses import replace
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import wx

from app import JumpTimeDialog, SubtitleJumpDialog, MaoerFrame, MediaDetailDialog, NavigationState, PlaybackFrame, SubtitleFilterRulesDialog, is_character_dialogue_subtitle, mark_dialogue_continuations
from app_settings import AppSettings, SubtitleFilterPreset, SubtitleFilterRules, default_filter_presets, load_settings
from audio_output import OutputDevice, SYSTEM_OUTPUT
from maoer_api import DANMAKU_MODE_SUBTITLE, DanmakuItem, DramaFollowResult, DramaPurchaseInfo, MaoerApi, MediaItem, PlaybackInfo, PublisherProfile


class PlaybackAnnouncementTests(unittest.TestCase):
    def test_bracketed_scene_with_colon_is_read_while_dialogue_is_filtered(self):
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.screen_reader = Mock()
        frame.read_subtitle_enabled = True
        frame.subtitle_filter_enabled = True
        frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="mute", info_label_only=False)
        cases = (
            ("【梦境", "永昌三十二年冬，整个常武县笼罩在一片萧条中】"),
            ("【场景", "夜幕降临】"),
            ("（回忆", "大门缓缓打开）"),
        )
        for role, content in cases:
            with self.subTest(role=role):
                frame.screen_reader.reset_mock()
                item = DanmakuItem(0.0, f"{role}：{content}", DANMAKU_MODE_SUBTITLE,
                                   role=role, content=content)
                self.assertFalse(is_character_dialogue_subtitle(item))
                frame._on_subtitle_due(item)
                frame.screen_reader.announce.assert_called_once_with(item.text)

        # A bracketed cue followed by speech still belongs to the speaker.
        dialogue = DanmakuItem(1.0, "甲：【场景】我来了", DANMAKU_MODE_SUBTITLE,
                               role="甲", content="【场景】我来了")
        self.assertTrue(is_character_dialogue_subtitle(dialogue))
        # Another drama uses a bracketed location before the speaker name.
        offscreen_speaker = DanmakuItem(2.0, "【隔壁】赵飞燕：你再说一遍？", DANMAKU_MODE_SUBTITLE,
                                        role="【隔壁】赵飞燕", content="你再说一遍？")
        self.assertTrue(is_character_dialogue_subtitle(offscreen_speaker))
        credits = DanmakuItem(3.0, "韩文清/王杰希：本作品由阅文集团提供，蝴蝶蓝原著",
                              DANMAKU_MODE_SUBTITLE)
        self.assertFalse(is_character_dialogue_subtitle(credits))
        spoken_credits = DanmakuItem(4.0, "步重华：（报幕）晋江文学城原著",
                                     DANMAKU_MODE_SUBTITLE, role="步重华", content="（报幕）晋江文学城原著")
        self.assertFalse(is_character_dialogue_subtitle(spoken_credits))

    def test_subtitles_and_danmaku_content_share_direct_speech_channel(self):
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.screen_reader = Mock()
        frame.status_reader = Mock()
        frame.read_subtitle_enabled = True
        frame.subtitle_filter_enabled = False
        frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="mute", info_label_only=False)
        frame.read_danmaku_enabled = True

        frame._on_subtitle_due(Mock(text=" 字幕内容 "))
        frame._on_danmaku_due(Mock(text=" 弹幕内容 "))

        self.assertEqual([call.args[0] for call in frame.screen_reader.announce.call_args_list],
                         ["字幕内容", "弹幕内容"])
        frame.status_reader.announce.assert_not_called()

    def test_danmaku_content_does_not_depend_on_subtitle_toggle_or_filter(self):
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.screen_reader = Mock()
        frame.status_reader = Mock()
        frame.read_subtitle_enabled = False
        frame.subtitle_filter_enabled = True
        frame.read_danmaku_enabled = True
        frame._on_danmaku_due(Mock(text=" 弹幕正文 "))
        frame._on_danmaku_due(Mock(text="  "))
        frame.read_danmaku_enabled = False
        frame._on_danmaku_due(Mock(text="关闭后不读"))
        frame.screen_reader.announce.assert_called_once_with("弹幕正文")
        frame.status_reader.announce.assert_not_called()

    def test_disabled_readers_do_not_announce(self):
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.screen_reader = Mock()
        frame.read_subtitle_enabled = False
        frame.subtitle_filter_enabled = False
        frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="mute", info_label_only=False)
        frame.read_danmaku_enabled = False

        frame._on_subtitle_due(Mock(text="字幕内容"))
        frame._on_danmaku_due(Mock(text="弹幕内容"))

        frame.screen_reader.announce.assert_not_called()

    def test_dialogue_filter_keeps_narration_and_pure_sound_cues(self):
        cases = (
            ("甲", "你好", True),
            ("", "夜幕降临", False),
            ("旁白", "夜幕降临", False),
            ("报幕", "第一集", False),
            ("音效", "脚步声", False),
            ("系统", "直播准备就绪", False),
            ("提示音", "主播已隐藏弹幕", False),
            ("甲", "（脚步声）", False),
            ("甲", "【脚步声】", False),
            ("甲", "（脚步声）你好", True),
        )
        for role, content, expected in cases:
            with self.subTest(role=role, content=content):
                item = DanmakuItem(1.0, content, DANMAKU_MODE_SUBTITLE, role=role, content=content)
                self.assertEqual(is_character_dialogue_subtitle(item), expected)

        for text, expected in (
            ("甲：你好", True),
            ("甲:你好", True),
            ("旁白：夜幕降临", False),
            ("系统：欢迎进入梦魇直播间", False),
            ("提示音：主播已隐藏弹幕", False),
            ("甲：（脚步声）", False),
            ("第一集", False),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    is_character_dialogue_subtitle(DanmakuItem(1.0, text, DANMAKU_MODE_SUBTITLE)),
                    expected,
                )

    def test_filter_silences_story_barrage_but_reads_system_prompts(self):
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.screen_reader = Mock()
        frame.read_subtitle_enabled = True
        frame.subtitle_filter_enabled = True
        frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="mute", info_label_only=False)
        items = mark_dialogue_continuations([
            DanmakuItem(1.0, "弹幕：没想到主播还挺淡定的", DANMAKU_MODE_SUBTITLE,
                        role="弹幕", content="没想到主播还挺淡定的", user_id="submitter"),
            DanmakuItem(2.0, "很少有一点都不害怕的新人", DANMAKU_MODE_SUBTITLE,
                        user_id="submitter"),
            DanmakuItem(3.0, "弹幕1：虽然但是他好好看啊", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(4.0, "弹幕：（OS）不该读出这句", DANMAKU_MODE_SUBTITLE,
                        role="弹幕", content="（OS）不该读出这句"),
            DanmakuItem(5.0, "系统：欢迎进入梦魇直播间", DANMAKU_MODE_SUBTITLE,
                        role="系统", content="欢迎进入梦魇直播间"),
            DanmakuItem(6.0, "提示音：主播已隐藏弹幕", DANMAKU_MODE_SUBTITLE,
                        role="提示音", content="主播已隐藏弹幕"),
        ])
        self.assertEqual(items[1].role, "弹幕")
        for item in items:
            frame._on_subtitle_due(item)
        self.assertEqual(
            [call.args[0] for call in frame.screen_reader.announce.call_args_list],
            ["系统：欢迎进入梦魇直播间", "提示音：主播已隐藏弹幕"],
        )

        frame.subtitle_filter_enabled = False
        frame.screen_reader.reset_mock()
        frame._on_subtitle_due(items[0])
        frame.screen_reader.announce.assert_called_once_with(items[0].text)


class PlaybackMenuTests(unittest.TestCase):
    def test_d_key_toggle_uses_native_notifications_while_content_uses_direct_speech(self):
        self.frame.read_danmaku_enabled = False
        event = Mock()
        event.GetKeyCode.return_value = ord("D")
        self.frame.on_char_hook(event)
        self.frame._on_danmaku_due(DanmakuItem(1, "普通弹幕正文", 1))
        self.frame.on_char_hook(event)
        self.frame._on_danmaku_due(DanmakuItem(2, "已关闭，不读", 1))
        self.assertEqual([call.args[0] for call in self.frame.status_reader.announce.call_args_list],
                         ["弹幕朗读已开启", "弹幕朗读已关闭"])
        self.frame.screen_reader.announce.assert_called_once_with("普通弹幕正文")

    def _zaochun_os_items(self, role="栾念"):
        api = MaoerApi(cookie="")
        xml = (
            '<i><d p="460.67,4,25,5351645,0,0,3586609">'
            f'{role}：(os)八成是空窗久了</d>'
            '<d p="463.22,4,25,5351645,0,0,3586609">竟然觉得她今晚看着还挺顺眼的</d></i>'
        )
        data = json.dumps([
            {"start_time": 460366, "end_time": 463187, "role": role, "content": "(os)八成是空窗久了"},
            {"start_time": 463187, "end_time": 466634, "role": role, "content": "竟然觉得她今晚看着还挺顺眼"},
        ])
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
            return api.sound_danmaku(9100588, subtitle_url="https://static.example/subtitle.json")

    def test_zaochun_jump_list_uses_one_canonical_copy_despite_edit_and_timing_differences(self):
        api = MaoerApi(cookie="")
        xml = '<i><d p="2.64,4,25,0">【酒店 宴会厅彩排现场】</d></i>'
        data = '[{"start_time":6788,"end_time":10048,"content":"【酒店 宴会厅彩排现场】"}]'
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else data):
            items = api.sound_danmaku(9100588, subtitle_url="https://static.example/subtitle.json")
        items += self._zaochun_os_items()
        self.frame._set_danmaku_items(self.frame.load_generation, items)
        dialog = SubtitleJumpDialog(self.frame, self.frame.danmaku_canvas.items, 0)
        self.addCleanup(dialog.Destroy)
        self.assertEqual([item.time for item in dialog.items], [6.788, 460.366, 463.187])


    def test_selected_json_role_boundaries_do_not_borrow_discarded_xml_context(self):
        # The old merged policy inferred OS from the discarded XML continuation.
        # With one source, JSON's own explicit role boundary remains authoritative.
        self.frame._set_danmaku_items(self.frame.load_generation, self._zaochun_os_items())
        first, second = self.frame.danmaku_canvas.items
        self.assertIn(id(first), self.frame.subtitle_os_items)
        self.assertNotIn(id(second), self.frame.subtitle_os_items)
        self.assertEqual(second.text, "栾念：竟然觉得她今晚看着还挺顺眼")

    def test_cangnan_single_track_is_shared_by_display_reading_and_jump_list(self):
        from test_subtitle_source import CANGNAN_JSON, CANGNAN_XML, SUBTITLE_URL
        api = MaoerApi(cookie="")
        data = json.dumps(CANGNAN_JSON)
        for payload, expected_times in (
            (data, [31.025, 33.725, 61.25, 131.95]),
            ("[]", [30.54, 34.32, 61.38, 144.49, 200]),
        ):
            with self.subTest(fallback=payload == "[]"):
                with patch.object(api, "_get_text", side_effect=lambda path, params=None: CANGNAN_XML if path == "/sound/getdm" else payload):
                    items = api.sound_danmaku(10998432, subtitle_url=SUBTITLE_URL)
                self.frame.subtitle_filter_enabled = False
                self.frame.screen_reader.reset_mock()
                self.frame._set_danmaku_items(self.frame.load_generation, items)
                canvas = self.frame.danmaku_canvas
                dialog = SubtitleJumpDialog(self.frame, canvas.items, 0)
                try:
                    self.assertEqual([item.time for item in dialog.items], expected_times)
                    displayed = [item for item in canvas.items if item.mode == 4]
                    self.assertEqual(dialog.items, displayed)
                    canvas.seek(-canvas.position)
                    with patch.object(canvas, "_spawn_item"):
                        for position in expected_times:
                            canvas.position = position
                            canvas._spawn_due_items()
                    self.assertEqual(
                        [call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                        [item.text for item in dialog.items])
                finally:
                    dialog.Destroy()
    def test_zaochun_episode5_short_caption_is_spoken_once_in_full_reading(self):
        api = MaoerApi(cookie="")
        xml = '<i><d p="778.79,4,25,5351645,1707559701,160,3586609,249057640">栾念：嗯？</d></i>'
        captions = json.dumps([{"start_time": 778269, "end_time": 779992,
                                "role": "栾念", "content": "嗯？", "color": 5351645}], ensure_ascii=False)
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else captions):
            items = api.sound_danmaku(9100588, subtitle_url="https://static.example/subtitle.json")
        self.frame.subtitle_filter_enabled = False
        self.frame._set_danmaku_items(self.frame.load_generation, items)
        canvas = self.frame.danmaku_canvas
        with patch.object(canvas, "_spawn_item"):
            for position in (778.269, 778.79, 779.0):
                canvas.position = position
                canvas._spawn_due_items()
        self.frame.screen_reader.announce.assert_called_once_with("栾念：嗯？")


    def test_xml_track_os_survives_seek_and_ends_on_explicit_spoken_label(self):
        captions = [
            DanmakuItem(1, "甲：（OS）首段", 4, user_id="a"),
            DanmakuItem(2, "乙：正常说话", 4, user_id="b"),
            DanmakuItem(10, "同一段内心独白续行", 4, user_id="a"),
            DanmakuItem(20, "甲：现在开口说话", 4, user_id="a"),
            DanmakuItem(21, "这次是正常台词续行", 4, user_id="a"),
        ]
        self.frame.subtitle_filter_enabled = True
        self.frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="full", os_body=False)
        self.frame._set_danmaku_items(self.frame.load_generation, captions)
        canvas = self.frame.danmaku_canvas
        canvas.seek(10)
        with patch.object(canvas, "_spawn_item"):
            for position in (10, 20, 21):
                canvas.position = position
                canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["甲：现在开口说话", "这次是正常台词续行"])
        self.frame.subtitle_filter_enabled = False
        self.frame.screen_reader.reset_mock()
        for item in canvas.items:
            self.frame._on_subtitle_due(item)
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         [item.text for item in captions])
    def test_every_playback_notification_uses_native_channel_not_subtitles(self):
        self.frame.playback = PlaybackInfo(123, "声音", "https://example.com/audio.mp3", duration_ms=180000)
        self.frame.subtitle_filter_presets = default_filter_presets()
        cases = (
            (lambda: self.frame._announce_time(65, 180), "1分5秒/3分"),
            (lambda: self.frame._jump_to_time_done(0, 10, {"ok": True, "position": 10}), "已跳转到10秒"),
            (lambda: self.frame._jump_to_time_done(0, 10, None), "跳转失败，请等播放器加载完成后再试"),
            (lambda: self.frame._set_playback_rate_done(self.frame.rate_change_generation, 1.25, {"ok": True, "rate": 1.25}), "1.25倍速"),
            (lambda: self.frame._set_playback_rate_done(self.frame.rate_change_generation, 1.0, None), "当前播放器不支持倍速"),
            (self.frame._toggle_subtitle_reader, "字幕朗读已关闭"),
            (self.frame._toggle_danmaku_reader, "弹幕朗读已开启"),
            (self.frame._toggle_subtitle_filter_mode, "字幕过滤模式已开启"),
            (lambda: self.frame._select_subtitle_filter_slot(1), "有声书过滤方案"),
            (lambda: self.frame._output_device_changed(0, {"ok": True, "name": "耳机"}), "耳机"),
            (lambda: self.frame._output_device_changed(0, {"ok": False, "error": "设备离线"}), "切换输出设备失败：设备离线"),
        )
        for action, expected in cases:
            with self.subTest(message=expected):
                self.frame.read_subtitle_enabled = True
                self.frame.subtitle_filter_enabled = False
                self.frame.read_danmaku_enabled = False
                self.frame.screen_reader.reset_mock()
                self.frame.status_reader.reset_mock()
                action()
                self.frame.status_reader.announce.assert_called_once_with(expected)
                self.frame.screen_reader.announce.assert_not_called()

    def test_space_volume_and_five_second_seek_keys_are_silent(self):
        self.player.toggle_pause.side_effect = (True, False)
        self.player.volume_up.return_value = 70
        self.player.volume_down.return_value = 60
        with patch.object(self.frame, "_set_parent_status") as status, \
             patch.object(self.frame.danmaku_canvas, "set_paused") as paused, \
             patch.object(self.frame.danmaku_canvas, "seek") as seek:
            for key in (wx.WXK_SPACE, wx.WXK_SPACE, wx.WXK_UP, wx.WXK_DOWN, wx.WXK_LEFT, wx.WXK_RIGHT):
                event = Mock()
                event.GetKeyCode.return_value = key
                self.frame.on_char_hook(event)
                event.Skip.assert_not_called()
            status.assert_not_called()
        self.frame.status_reader.announce.assert_not_called()
        self.frame.screen_reader.announce.assert_not_called()
        self.player.volume_up.assert_called_once()
        self.player.volume_down.assert_called_once()
        self.assertEqual([call.args for call in paused.call_args_list], [(True,), (False,)])
        self.assertEqual([call.args for call in self.player.seek.call_args_list], [(-5,), (5,)])
        self.assertEqual([call.args for call in seek.call_args_list], [(-5,), (5,)])

    def test_native_failure_never_uses_subtitle_channel_and_subtitles_still_read(self):
        self.frame.status_reader.announce.return_value = False
        self.frame._announce_time(1, 60)
        self.frame.screen_reader.announce.assert_not_called()
        self.frame._on_subtitle_due(DanmakuItem(1, "旁白：故事开始", DANMAKU_MODE_SUBTITLE))
        self.frame.screen_reader.announce.assert_called_once_with("旁白：故事开始")

    def test_time_query_missing_duration_notifies_only_once(self):
        self.frame.playback = PlaybackInfo(123, "声音", "https://example.com/audio.mp3")
        generation = self.frame.time_announcement_generation
        self.frame._announce_playback_time_done(generation, None)
        self.frame._announce_playback_time_fallback(generation)
        self.frame.status_reader.announce.assert_called_once_with("没有获取到时长")
        self.frame.screen_reader.announce.assert_not_called()

    def test_constructor_separates_native_notifications_from_subtitle_bridge(self):
        with patch("app.ScreenReaderAnnouncer", side_effect=lambda *args, **kwargs: Mock()) as factory:
            frame = PlaybackFrame(None, Mock(), Mock(), Mock(), Mock())
        self.addCleanup(frame.Destroy)
        self.assertEqual(len(factory.call_args_list), 2)
        self.assertEqual(factory.call_args_list[0].kwargs, {})
        self.assertEqual(factory.call_args_list[1].kwargs, {"native_only": True})
        self.assertIsNot(frame.screen_reader, frame.status_reader)
        frame._close_readers()
        frame.screen_reader.close.assert_called_once()
        frame.status_reader.close.assert_called_once()

    def test_time_query_never_uses_subtitle_speech_channel(self):
        self.frame.status_reader = Mock()
        self.frame._announce_time(65, 180)
        self.frame.screen_reader.announce.assert_not_called()
        self.frame.status_reader.announce.assert_called_once_with("1分5秒/3分")

    @staticmethod
    def _presets_with_full_credits():
        # These regression cases exercise the optional unfiltered credits,
        # not the user's newer built-in label-only default.
        return tuple(replace(preset, rules=replace(preset.rules, info_labels=("系统", "提示音")))
                     for preset in default_filter_presets())

    def setUp(self):
        self.app = wx.GetApp() or wx.App(False)
        self.player = Mock()
        with patch("app.ScreenReaderAnnouncer", side_effect=lambda *args, **kwargs: Mock()):
            self.frame = PlaybackFrame(
                None, Mock(), self.player, Mock(), Mock(),
                read_danmaku_default=False, read_subtitle_default=True,
                subtitle_filter_presets=(SubtitleFilterPreset(
                    "旧规则", SubtitleFilterRules(dialogue_mode="mute", info_label_only=False)
                ),) * 10,
            )
        self.addCleanup(self.frame.Destroy)

    def choose(self, label):
        def popup(menu, _position):
            items = menu.GetMenuItems()
            self.assertEqual(
                [item.GetItemLabelText() for item in items if not item.IsSeparator()],
                ["快退 5 秒", "快进 5 秒", "跳转时间…", "播放倍速", "朗读字幕", "过滤模式（实验性功能）", "过滤方案", "朗读弹幕"],
            )
            self.assertEqual(items[5].IsChecked(), self.frame.read_subtitle_enabled)
            self.assertEqual(items[6].IsChecked(), self.frame.subtitle_filter_enabled)
            self.assertEqual(items[8].IsChecked(), self.frame.read_danmaku_enabled)
            speed_menu = items[3].GetSubMenu()
            presets_menu = items[7].GetSubMenu()
            self.assertEqual(
                [item.GetItemLabelText() for item in speed_menu.GetMenuItems()],
                ["0.5 倍", "1 倍", "1.25 倍", "1.5 倍", "1.75 倍", "2 倍"],
            )
            self.assertEqual(len(presets_menu.GetMenuItems()), len(self.frame.subtitle_filter_presets))
            self.assertTrue(presets_menu.GetMenuItems()[self.frame.subtitle_filter_slot].IsChecked())
            for item in list(items) + list(speed_menu.GetMenuItems()) + list(presets_menu.GetMenuItems()):
                if item.GetItemLabelText() == label:
                    return item.GetId()
            return wx.ID_NONE

        with patch.object(PlaybackFrame, "GetPopupMenuSelectionFromUser", side_effect=popup):
            self.frame._show_playback_menu(wx.DefaultPosition)

    def press_f(self, control=False, control_code=False):
        event = Mock()
        event.GetKeyCode.return_value = wx.WXK_CONTROL_F if control_code else ord("F")
        event.ControlDown.return_value = control
        self.frame.on_char_hook(event)
        event.Skip.assert_not_called()

    def play_overlapping_subtitle_sources(self, role="甲", content="你好", xml_text=None):
        subtitle_url = "https://static.example/subtitle.json"
        xml_caption = xml_text if xml_text is not None else f"{role}：{content}"
        xml = f'<i><d p="1.0,4,25,0">{xml_caption}</d></i>'
        json_subtitles = json.dumps([{"start_time": 1250, "role": role, "content": content}])
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else json_subtitles):
            subtitles = api.sound_danmaku(1, subtitle_url=subtitle_url)
        canvas = self.frame.danmaku_canvas
        canvas.set_items(subtitles)
        with patch.object(canvas, "_spawn_item"):
            for position in (1.0, 1.25):
                canvas.position = position
                canvas._spawn_due_items()
        return [call.args[0] for call in self.frame.screen_reader.announce.call_args_list]

    def test_overlapping_xml_and_json_subtitles_are_read_once(self):
        self.assertEqual(
            self.play_overlapping_subtitle_sources(),
            ["甲：你好"],
        )

    def test_ctrl_f_filters_dialogue_from_both_subtitle_sources(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        self.assertEqual(
            self.play_overlapping_subtitle_sources(),
            [],
        )

    def test_f_reads_role_and_dialogue_once_when_xml_has_content_only(self):
        self.press_f()
        self.press_f()
        self.frame.screen_reader.reset_mock()
        self.assertEqual(
            self.play_overlapping_subtitle_sources(xml_text="你好"),
            ["甲：你好"],
        )

    def test_full_subtitle_reading_says_speaker_once_when_json_content_repeats_it(self):
        self.assertEqual(
            self.play_overlapping_subtitle_sources(
                role="陆驿站", content="陆驿站：你这几天还好吗？",
                xml_text="陆驿站：你这几天还好吗？",
            ),
            ["陆驿站：你这几天还好吗？"],
        )

    def test_split_xml_and_full_json_dialogue_are_announced_once(self):
        xml = (
            '<i><d p="151.02,4,25,0,0,0,10283562,1">陆驿站：你这几天还好吗？</d>'
            '<d p="152.61,4,25,0,0,0,10283562,2">难过？生气？</d></i>'
        )
        json_subtitles = json.dumps([{
            "start_time": 150560, "end_time": 154770, "role": "陆驿站",
            "content": "你这几天还好吗？难过？生气？",
        }], ensure_ascii=False)
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else json_subtitles):
            items = api.sound_danmaku(7741548, subtitle_url="https://static.example/subtitle.json")
        canvas = self.frame.danmaku_canvas
        canvas.set_items(items)
        with patch.object(canvas, "_spawn_item"):
            for position in (150.56, 151.02, 152.61):
                canvas.position = position
                canvas._spawn_due_items()
        self.assertEqual(
            [call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
            ["陆驿站：你这几天还好吗？难过？生气？"],
        )

    def test_ctrl_f_silences_dialogue_when_xml_has_content_only(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        self.assertEqual(
            self.play_overlapping_subtitle_sources(xml_text="你好"),
            [],
        )

    def test_ctrl_f_keeps_overlapping_narration_and_reads_it_once(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        self.assertEqual(
            self.play_overlapping_subtitle_sources("旁白", "夜幕降临"),
            ["旁白：夜幕降临"],
        )

    def test_f_then_ctrl_f_with_overlapping_dialogue_and_narration(self):
        self.press_f()
        self.press_f()
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        xml = (
            '<i><d p="1.0,4,25,0">甲：你好</d>'
            '<d p="2.0,4,25,0">旁白：夜幕降临</d></i>'
        )
        subtitles = json.dumps([
            {"start_time": 1250, "role": "甲", "content": "你好"},
            {"start_time": 2250, "role": "旁白", "content": "夜幕降临"},
        ])
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", side_effect=lambda path, params=None: xml if path == "/sound/getdm" else subtitles):
            items = api.sound_danmaku(1, subtitle_url="https://static.example/subtitle.json")
        canvas = self.frame.danmaku_canvas
        canvas.set_items(items)
        with patch.object(canvas, "_spawn_item"):
            for position in (1.0, 1.25, 2.0, 2.25):
                canvas.position = position
                canvas._spawn_due_items()
        self.assertEqual(
            [call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
            ["旁白：夜幕降临"],
        )

    def test_ctrl_f_filters_xml_dialogue_without_json_subtitles(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        self.frame._on_subtitle_due(DanmakuItem(1.0, "甲：你好", DANMAKU_MODE_SUBTITLE))
        self.frame.screen_reader.announce.assert_not_called()

    def test_ctrl_f_filters_dialogue_and_keeps_other_subtitles(self):
        self.press_f(control=True)
        self.assertTrue(self.frame.read_subtitle_enabled)
        self.assertTrue(self.frame.subtitle_filter_enabled)
        self.frame.screen_reader.reset_mock()

        subtitles = [
            DanmakuItem(1.0, "甲：你好", DANMAKU_MODE_SUBTITLE, role="甲", content="你好"),
            DanmakuItem(1.0, "旁白：夜幕降临", DANMAKU_MODE_SUBTITLE, role="旁白", content="夜幕降临"),
        ]
        canvas = self.frame.danmaku_canvas
        canvas.set_items(subtitles)
        canvas.position = 1.0
        with patch.object(canvas, "_spawn_item"):
            canvas._spawn_due_items()
        self.frame.screen_reader.announce.assert_called_once_with("旁白：夜幕降临")

        self.press_f(control=True)
        self.assertFalse(self.frame.subtitle_filter_enabled)
        self.frame.screen_reader.reset_mock()
        self.frame._on_subtitle_due(subtitles[0])
        self.frame.screen_reader.announce.assert_called_once_with("甲：你好")

    def test_ctrl_f_reads_only_role_and_os_marker(self):
        self.press_f(control=True)
        cases = (
            # The first line was reported from 灯花笑 episode 16; the others
            # follow the public subtitle format seen in episodes 1 and 3.
            ("裴云暎", "（OS）万恩寺溺亡案，秋闱舞弊案", "裴云暎：（OS）"),
            ("陆曈", "（OS）一走就是七年", "陆曈：（OS）"),
            ("银筝", "（OS）我这个乌鸦嘴", "银筝：（OS）"),
            ("甲", "(os)你好", "甲：(os)"),
            ("甲", "先想到这里（ OS ）后面不读", "甲：先想到这里（ OS ）"),
        )
        for role, content, expected in cases:
            with self.subTest(role=role, content=content):
                self.frame.screen_reader.reset_mock()
                item = DanmakuItem(1.0, f"{role}：{content}", DANMAKU_MODE_SUBTITLE,
                                   role=role, content=content)
                self.assertTrue(self.frame._should_read_subtitle(item))
                self.frame._on_subtitle_due(item)
                self.frame.screen_reader.announce.assert_called_once_with(expected)

    def test_ctrl_f_reads_os_marker_from_legacy_xml_text(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        item = DanmakuItem(1.0, "裴云暎：（OS）万恩寺溺亡案", DANMAKU_MODE_SUBTITLE)
        self.frame._on_subtitle_due(item)
        self.frame.screen_reader.announce.assert_called_once_with("裴云暎：（OS）")

    def test_ctrl_f_reads_inferred_speaker_before_os_marker(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        item = DanmakuItem(1.0, "(os)还没说完", DANMAKU_MODE_SUBTITLE,
                           role="叶修", content="(os)还没说完")
        self.frame._on_subtitle_due(item)
        self.frame.screen_reader.announce.assert_called_once_with("叶修：(os)")

    def test_os_rule_only_applies_while_dialogue_filter_is_on(self):
        item = DanmakuItem(1.0, "裴云暎：（OS）万恩寺溺亡案", DANMAKU_MODE_SUBTITLE,
                           role="裴云暎", content="（OS）万恩寺溺亡案")
        self.frame._on_subtitle_due(item)
        self.frame.screen_reader.announce.assert_called_once_with(item.text)
        self.frame.screen_reader.reset_mock()
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        for text in ("甲：你好", "甲：（OST）片尾曲", "甲：close the door"):
            self.frame._on_subtitle_due(DanmakuItem(1.0, text, DANMAKU_MODE_SUBTITLE))
        self.frame.screen_reader.announce.assert_not_called()

    def test_canvas_selects_os_after_skipping_ordinary_dialogue(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        canvas = self.frame.danmaku_canvas
        canvas.set_items([
            DanmakuItem(1.0, "甲：你好", DANMAKU_MODE_SUBTITLE, role="甲", content="你好"),
            DanmakuItem(1.0, "裴云暎：（OS）万恩寺溺亡案", DANMAKU_MODE_SUBTITLE,
                        role="裴云暎", content="（OS）万恩寺溺亡案"),
        ])
        canvas.position = 1.0
        with patch.object(canvas, "_spawn_item"):
            canvas._spawn_due_items()
        self.frame.screen_reader.announce.assert_called_once_with("裴云暎：（OS）")

    def test_late_loaded_opening_scene_is_read_once_while_filtering_dialogue(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        canvas = self.frame.danmaku_canvas
        scene = DanmakuItem(
            0.0, "【梦境：永昌三十二年冬，整个常武县笼罩在一片萧条中】",
            DANMAKU_MODE_SUBTITLE, role="【梦境", content="永昌三十二年冬，整个常武县笼罩在一片萧条中】",
        )
        canvas.position = 2.0  # Playback has started before subtitles finish loading.
        canvas.set_items([scene, DanmakuItem(14.43, "【下一幕】", DANMAKU_MODE_SUBTITLE)])
        with patch.object(canvas, "_spawn_item"):
            canvas._spawn_due_items()
            canvas.position = 2.1
            canvas._spawn_due_items()
        self.frame.screen_reader.announce.assert_called_once_with(scene.text)

    def test_late_load_does_not_replay_stale_or_seeked_away_caption(self):
        canvas = self.frame.danmaku_canvas
        old_scene = DanmakuItem(0.0, "【旧场景】", DANMAKU_MODE_SUBTITLE)
        canvas.position = 40.0
        canvas.set_items([old_scene])
        with patch.object(canvas, "_spawn_item"):
            canvas._spawn_due_items()
        self.frame.screen_reader.announce.assert_not_called()

        canvas.position = 2.0
        canvas.set_items([old_scene])
        canvas.seek(15)
        with patch.object(canvas, "_spawn_item"):
            canvas._spawn_due_items()
        self.frame.screen_reader.announce.assert_not_called()

    def test_filter_skips_same_submitter_unlabelled_dialogue_continuations(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        xml = (
            '<i>'
            '<d p="11.49,4,25,0,0,0,4055034,1">主播3：叶秋更名叶修，下赛季正式回归联盟</d>'
            '<d p="14.49,4,25,0,0,0,4055034,2">荣耀教科书能否再创当年三冠王奇迹？</d>'
            '<d p="18.48,4,25,0,0,0,5434472,3">黄少天：哎呀那你们就多虑了</d>'
            '<d p="20.05,4,25,0,0,0,5434472,4">嘉世的水平不也要取决于他们的选手吗？</d>'
            '<d p="22.15,4,25,0,0,0,5434472,5">可是以现在嘉世战队的状况</d>'
            '<d p="26.43,4,25,0,0,0,5434472,6">【兴欣训练室】</d>'
            '<d p="30.43,4,25,0,0,0,5434472,7">报幕：本作品由阅文集团提供</d>'
            '<d p="33.47,4,25,0,0,0,5434472,8">猫耳FM出品，729声工场配音</d>'
            '<d p="35.47,4,25,0,0,0,9999999,9">无角色说明文字</d>'
            '<d p="38.47,4,25,0,0,0,12872929,10">韩文清/王杰希：本作品由阅文集团提供</d>'
            '<d p="41.47,4,25,0,0,0,12872929,11">猫耳FM出品 729声工场配音</d>'
            '</i>'
        )
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", return_value=xml):
            items = api.sound_danmaku(13639580, subtitle_url="")
        self.frame.load_generation = 1
        self.frame._set_danmaku_items(1, items)
        canvas = self.frame.danmaku_canvas
        with patch.object(canvas, "_spawn_item"):
            for position in (11.49, 14.49, 18.48, 20.05, 22.15, 26.43, 30.43, 33.47, 35.47, 38.47, 41.47):
                canvas.position = position
                canvas._spawn_due_items()
        self.assertEqual(
            [call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
            ["【兴欣训练室】", "报幕：本作品由阅文集团提供",
             "猫耳FM出品，729声工场配音", "无角色说明文字",
             "韩文清/王杰希：本作品由阅文集团提供", "猫耳FM出品 729声工场配音"],
        )

    def test_filter_tracks_interleaved_speakers_by_caption_submitter(self):
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        xml = (
            '<i>'
            '<d p="1,4,25,0,0,0,8471336,1">陈果：我先介绍一下兴欣网吧</d>'
            '<d p="2,4,25,0,0,0,8471336,2">我们兴欣网吧始建于25年前</d>'
            '<d p="3,4,25,0,0,0,4116945,3">叶修：（OS）老板这是存心要恶心他啊</d>'
            '<d p="4,4,25,0,0,0,8471336,4">那时候光我们那一条街</d>'
            '<d p="5,4,25,0,0,0,4116945,5">但这么搞是不是有点……</d>'
            '<d p="6,4,25,0,0,0,9876543,6">【场景切换】</d>'
            '<d p="7,4,25,0,0,0,8471336,7">新的场景说明</d>'
            '</i>'
        )
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", return_value=xml):
            items = api.sound_danmaku(13639580, subtitle_url="")
        self.frame.load_generation = 1
        self.frame._set_danmaku_items(1, items)
        canvas = self.frame.danmaku_canvas
        with patch.object(canvas, "_spawn_item"):
            for position in range(1, 8):
                canvas.position = float(position)
                canvas._spawn_due_items()
        self.assertEqual(
            [call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
            ["叶修：（OS）", "【场景切换】", "新的场景说明"],
        )

    def test_ctrl_f_accepts_wx_control_character_key_code(self):
        self.press_f(control=True, control_code=True)
        self.assertTrue(self.frame.subtitle_filter_enabled)

    def test_f_disables_filter_and_ctrl_f_starts_filter_mode_while_reading_is_off(self):
        self.press_f(control=True)
        self.press_f()
        self.assertFalse(self.frame.read_subtitle_enabled)
        self.assertFalse(self.frame.subtitle_filter_enabled)
        self.frame.status_reader.reset_mock()
        self.press_f(control=True)
        self.assertTrue(self.frame.read_subtitle_enabled)
        self.assertTrue(self.frame.subtitle_filter_enabled)
        self.frame.status_reader.announce.assert_called_once_with("字幕过滤模式已开启")

        self.press_f()
        self.assertFalse(self.frame.read_subtitle_enabled)
        self.assertFalse(self.frame.subtitle_filter_enabled)

    def test_filter_menu_switches_mode_and_keeps_full_reading_available(self):
        self.choose("过滤模式（实验性功能）")
        self.assertTrue(self.frame.subtitle_filter_enabled)
        self.assertTrue(self.frame.read_subtitle_enabled)
        self.choose("过滤模式（实验性功能）")
        self.assertFalse(self.frame.subtitle_filter_enabled)
        self.assertTrue(self.frame.read_subtitle_enabled)

    def test_ctrl_f_announcements_do_not_include_experimental_menu_label(self):
        self.frame.status_reader.reset_mock()
        self.press_f(control=True)
        self.press_f(control=True)
        self.assertEqual([call.args[0] for call in self.frame.status_reader.announce.call_args_list],
                         ["字幕过滤模式已开启", "已恢复朗读全部字幕"])

    def test_custom_filter_rules_apply_only_in_filter_mode(self):
        self.frame.subtitle_filter_rules = SubtitleFilterRules(
            dialogue_mode="full", story_barrage=False, os_body=False,
            info_label_only=False, keywords=("秘密",)
        )
        dialogue = DanmakuItem(1.0, "甲：你好", DANMAKU_MODE_SUBTITLE, role="甲", content="你好")
        barrage = DanmakuItem(2.0, "弹幕：太好了", DANMAKU_MODE_SUBTITLE,
                              role="弹幕", content="太好了")
        os_line = DanmakuItem(3.0, "甲：（OS）真相", DANMAKU_MODE_SUBTITLE,
                              role="甲", content="（OS）真相")
        keyword = DanmakuItem(4.0, "旁白：这个秘密", DANMAKU_MODE_SUBTITLE,
                              role="旁白", content="这个秘密")
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        for item in (dialogue, barrage, os_line, keyword):
            self.frame._on_subtitle_due(item)
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["甲：你好", "弹幕：太好了"])

        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        self.frame._on_subtitle_due(keyword)
        self.frame.screen_reader.announce.assert_called_once_with(keyword.text)

    def test_story_barrage_rule_is_independent_of_dialogue_rule(self):
        self.frame.subtitle_filter_rules = SubtitleFilterRules(
            dialogue_mode="mute", story_barrage=False, info_label_only=False,
        )
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        self.frame._on_subtitle_due(DanmakuItem(1.0, "弹幕：好看", DANMAKU_MODE_SUBTITLE))
        self.frame._on_subtitle_due(DanmakuItem(2.0, "甲：你好", DANMAKU_MODE_SUBTITLE))
        self.frame.screen_reader.announce.assert_called_once_with("弹幕：好看")

    def test_os_rule_can_keep_marker_even_when_dialogue_rule_is_off(self):
        self.frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="full")
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        self.frame._on_subtitle_due(DanmakuItem(1.0, "甲：（OS）心里话", DANMAKU_MODE_SUBTITLE))
        self.frame.screen_reader.announce.assert_called_once_with("甲：（OS）")

    def test_disabling_os_rule_mutes_os_even_when_dialogue_is_filtered(self):
        self.frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="mute", os_body=False)
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        self.frame._on_subtitle_due(DanmakuItem(1.0, "甲：（OS）心里话", DANMAKU_MODE_SUBTITLE))
        self.frame._on_subtitle_due(DanmakuItem(2.0, "甲：普通对话", DANMAKU_MODE_SUBTITLE))
        self.frame.screen_reader.announce.assert_not_called()

    def test_new_filter_rules_use_real_drama_caption_shapes(self):
        self.frame.subtitle_filter_rules = SubtitleFilterRules()
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        cases = (
            ("老师：这个题型我讲了多少次？", "老师"),
            ("陆驿站：你这几天还好吗？难过？生气？", "陆驿站"),
            ("系统：灵魂资料采集中……", "系统"),
            ("提示音：主播已隐藏弹幕", "提示音"),
            ("系统：你好 新人玩家 欢迎来到无限游戏", "系统"),
            ("旁白：夜幕降临", "旁白：夜幕降临"),
            ("报幕：广播剧第一集", "报幕：广播剧第一集"),
            ("【梦境：永昌三十二年冬】", "【梦境：永昌三十二年冬】"),
            ("甲：（OS）心里话", "甲：（OS）"),
        )
        for text, _expected in cases:
            self.frame._on_subtitle_due(DanmakuItem(1.0, text, DANMAKU_MODE_SUBTITLE))
        self.frame._on_subtitle_due(DanmakuItem(1.0, "弹幕：太好了", DANMAKU_MODE_SUBTITLE))
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         [expected for _, expected in cases])

    def test_yashe_book_filter_announces_only_speaker_transitions(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.screen_reader.reset_mock()
        xml = (
            '<i>'
            '<d p="38.18,4,25,0,0,0,6888845,1">旁白：何亦瑶着迷地看着</d>'
            '<d p="39.50,4,25,0,0,0,6888845,2">面前玻璃柜里那块圆形古镜</d>'
            '<d p="45.69,4,25,0,0,0,6888845,3">老板：喜欢的话可以拿出来看一下</d>'
            '<d p="48.55,4,25,0,0,0,6888845,4">何亦瑶：哦、哦</d>'
            '<d p="50.03,4,25,0,0,0,6888845,5">旁白：古董店老板轻笑道</d>'
            '<d p="52.10,4,25,0,0,0,6888845,6">语气温柔，令人心生好感</d>'
            '<d p="66.78,4,25,0,0,0,6888845,7">老板：这块是罕见的汉代鱼纹铜镜</d>'
            '<d p="69.73,4,25,0,0,0,6888845,8">汉代铜镜多以龙虎凤鸟四神为图案</d>'
            '<d p="86.57,4,25,0,0,0,6888845,9">旁白：何亦瑶小心翼翼地捧着铜镜</d>'
            '</i>'
        )
        api = MaoerApi(cookie="")
        with patch.object(api, "_get_text", return_value=xml):
            items = api.sound_danmaku(11104161, subtitle_url="")
        self.frame.load_generation = 1
        self.frame._set_danmaku_items(1, items)
        canvas = self.frame.danmaku_canvas
        with patch.object(canvas, "_spawn_item"):
            for position in (38.18, 39.50, 45.69, 48.55, 50.03, 52.10, 66.78, 69.73, 86.57):
                canvas.position = position
                canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["旁白", "老板", "何亦瑶", "旁白", "老板", "旁白"])

    def test_book_filter_activated_mid_narration_announces_current_speaker_not_body(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.screen_reader.reset_mock()
        items = [
            DanmakuItem(38.18, "旁白：何亦瑶着迷地看着", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(39.50, "面前玻璃柜里那块圆形古镜", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(43.08, "眼睛都不舍得眨一下", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(45.69, "老板：喜欢的话可以拿出来看一下", DANMAKU_MODE_SUBTITLE),
        ]
        self.frame.load_generation = 1
        self.frame._set_danmaku_items(1, items)
        canvas = self.frame.danmaku_canvas
        canvas.seek(39.50)
        with patch.object(canvas, "_spawn_item"):
            for position in (39.50, 43.08, 45.69):
                canvas.position = position
                canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["旁白", "老板"])

    def test_yashe_book_filter_keeps_credits_and_announcement_continuations(self):
        self.frame.subtitle_filter_presets = self._presets_with_full_credits()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.screen_reader.reset_mock()
        captions = [
            (1.30, "°•৹一枝字幕组施工৹•°"),
            (8.02, "报幕：玄色原著，儒意欣欣文化授权"),
            (12.78, "磨铁、19Hz声物局联合出品"),
            (17.68, "精品多人有声书《哑舍》"),
            (27.76, "第1集"),
            (38.18, "旁白：何亦瑶着迷地看着"),
            (39.50, "面前玻璃柜里那块圆形古镜"),
            (45.69, "老板：喜欢的话可以拿出来看一下"),
        ]
        self.frame._set_danmaku_items(self.frame.load_generation, [
            DanmakuItem(time, text, DANMAKU_MODE_SUBTITLE, user_id="6888845")
            for time, text in captions
        ])
        canvas = self.frame.danmaku_canvas
        for position, _text in captions:
            canvas.position = position
            canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         [text for _time, text in captions[:5]] + ["旁白", "老板"])

    def test_broadcast_preset_still_reads_narration_and_mutes_dialogue(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame._select_subtitle_filter_slot(0)
        self.frame.screen_reader.reset_mock()
        for item in (
            DanmakuItem(38.18, "旁白：何亦瑶着迷地看着", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(39.50, "面前玻璃柜里那块圆形古镜", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(45.69, "老板：喜欢的话可以拿出来看一下", DANMAKU_MODE_SUBTITLE),
        ):
            self.frame._on_subtitle_due(item)
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["旁白：何亦瑶着迷地看着", "面前玻璃柜里那块圆形古镜"])

    def test_book_filter_announcement_ends_the_previous_speaker_continuation(self):
        self.frame.subtitle_filter_presets = self._presets_with_full_credits()
        for announcement in ("报幕：玄色原著", "老板：（报幕）玄色原著"):
            with self.subTest(announcement=announcement):
                self.frame._select_subtitle_filter_slot(1)
                self.frame.screen_reader.reset_mock()
                captions = [
                    "旁白：故事结束", "夜色渐深", announcement,
                    "磨铁、19Hz声物局联合出品", "+-°•৹一枝字幕组施工৹•°",
                    "旁白：另一段故事开始", "仍然是旁白内容",
                ]
                canvas = self.frame.danmaku_canvas
                canvas.position = 0
                self.frame._set_danmaku_items(self.frame.load_generation, [
                    DanmakuItem(index + 1, text, DANMAKU_MODE_SUBTITLE, user_id="6888845")
                    for index, text in enumerate(captions)
                ])
                for position in range(1, len(captions) + 1):
                    canvas.position = position
                    canvas._spawn_due_items()
                self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                                 ["旁白", *captions[2:5], "旁白"])

    def test_book_filter_keeps_narration_and_character_bodies_muted_across_cues(self):
        self.frame.subtitle_filter_presets = self._presets_with_full_credits()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.screen_reader.reset_mock()
        captions = [
            "旁白：第一段旁白正文", "【闪回】", "仍然是旁白正文",
            "老板：（压低声音）", "这是老板的下一句台词", "老板：接着说话",
            "旁白：切换回旁白正文", "没有角色名的旁白续行",
            "报幕：第一集结束", "制作单位名单",
        ]
        self.frame._set_danmaku_items(self.frame.load_generation, [
            DanmakuItem(index + 1, text, DANMAKU_MODE_SUBTITLE, user_id="6888845")
            for index, text in enumerate(captions)
        ])
        canvas = self.frame.danmaku_canvas
        for position in range(1, len(captions) + 1):
            canvas.position = position
            canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["旁白", "老板", "旁白", "报幕：第一集结束", "制作单位名单"])

    def test_weigou_book_filter_reads_only_speakers_through_narration_and_scene_cues(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.screen_reader.reset_mock()
        captions = [
            (1.70, "【日，冬海海域，大海上】", ""),
            (9.95, "2015年10月9日，中州基地外的冬海海域", "旁白"),
            (15.31, "288海里处，一个普通的下午", "旁白"),
            (25.77, "【致远舰-航母甲板上，甲板摇晃】", ""),
            (32.21, "致远舰甲板上，站着十几位飞行员", "旁白"),
            (75.37, "准备着舰", "周其琛"),
            (77.89, "航速30节，甲板横摇2.4！", "白子聿"),
            (80.90, "收到", "周其琛"),
            (82.98, "舰载机降落时速有300多公里，跑道却仅有区区300米", "旁白"),
            (89.29, "不到陆基跑道的十分之一", "旁白"),
        ]
        self.frame._set_danmaku_items(self.frame.load_generation, [
            DanmakuItem(time, f"{role}：{content}" if role else content,
                        DANMAKU_MODE_SUBTITLE, role=role, content=content)
            for time, content, role in captions
        ])
        canvas = self.frame.danmaku_canvas
        for position, _content, _role in captions:
            canvas.position = position
            canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["旁白", "周其琛", "白子聿", "周其琛", "旁白"])

    def test_weigou_book_filter_recognizes_combined_announcement_labels(self):
        self.frame.subtitle_filter_presets = self._presets_with_full_credits()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.screen_reader.reset_mock()
        captions = [
            "旁白：这一集的故事结束", "报幕/周其琛：左岸原著",
            "报幕/周其琛：猫耳FM&野声文化联合出品",
            "报幕/周其琛：现代多人有声剧《尾钩》，欢迎收听",
        ]
        self.frame._set_danmaku_items(self.frame.load_generation, [
            DanmakuItem(index + 1, text, DANMAKU_MODE_SUBTITLE)
            for index, text in enumerate(captions)
        ])
        canvas = self.frame.danmaku_canvas
        for position in range(1, len(captions) + 1):
            canvas.position = position
            canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["旁白", *captions[1:]])

    def test_weigou_book_filter_announces_os_even_for_the_current_speaker(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.screen_reader.reset_mock()
        captions = [
            (1.0, "周其琛：收到", "周其琛", "收到"),
            (79.95, "周其琛：（OS）高度下降到600，减速", "周其琛", "（OS）高度下降到600，减速"),
            (80.5, "没有标注角色的内心独白续行", "", ""),
            (85.36, "周其琛：（OS）放起落架，打开减速板", "周其琛", "（OS）放起落架，打开减速板"),
            (86.0, "周其琛：普通台词", "周其琛", "普通台词"),
            (87.0, "旁白：切换到旁白正文", "旁白", "切换到旁白正文"),
        ]
        self.frame._set_danmaku_items(self.frame.load_generation, [
            DanmakuItem(time, text, DANMAKU_MODE_SUBTITLE, role=role, content=content)
            for time, text, role, content in captions
        ])
        canvas = self.frame.danmaku_canvas
        for position, *_ in captions:
            canvas.position = position
            canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["周其琛", "周其琛：（OS）", "周其琛：（OS）", "旁白"])

    def test_book_os_without_role_uses_context_and_keeps_story_barrage_filtered(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.screen_reader.reset_mock()
        captions = [
            "周其琛：普通对话", "(os)", "内心独白续行",
            "（OS）这里是内心独白正文", "周其琛：心里想着【OS】后面的正文",
            "弹幕：（OS）剧情弹幕", "（OS）", "旁白：这里是旁白正文",
        ]
        self.frame._set_danmaku_items(self.frame.load_generation, [
            DanmakuItem(index + 1, text, DANMAKU_MODE_SUBTITLE, user_id="6888845")
            for index, text in enumerate(captions)
        ])
        canvas = self.frame.danmaku_canvas
        for position in range(1, len(captions) + 1):
            canvas.position = position
            canvas._spawn_due_items()
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["周其琛", "周其琛：(os)", "周其琛：（OS）", "周其琛：【OS】", "旁白"])

    def test_book_os_checkbox_can_be_disabled_without_disabling_speaker_filter(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame._select_subtitle_filter_slot(1)
        self.frame.subtitle_filter_rules = SubtitleFilterRules(speaker_transitions_only=True, os_body=False)
        self.frame.screen_reader.reset_mock()
        self.frame._on_subtitle_due(DanmakuItem(1.0, "周其琛：（OS）心里话", DANMAKU_MODE_SUBTITLE))
        self.frame._on_subtitle_due(DanmakuItem(2.0, "周其琛：普通台词", DANMAKU_MODE_SUBTITLE))
        self.frame._on_subtitle_due(DanmakuItem(3.0, "旁白：普通旁白正文", DANMAKU_MODE_SUBTITLE))
        self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                         ["周其琛", "旁白"])

    def test_unchecked_os_mutes_continuations_and_resumes_at_an_explicit_normal_role(self):
        for book_mode in (False, True):
            for start_time in (0.0, 10.0):
                with self.subTest(book_mode=book_mode, start_time=start_time):
                    self.frame.subtitle_filter_enabled = True
                    self.frame.subtitle_filter_rules = SubtitleFilterRules(
                        dialogue_mode="role", speaker_transitions_only=book_mode, os_body=False,
                    )
                    self.frame.book_filter_last_role = None
                    self.frame.screen_reader.reset_mock()
                    captions = [
                        (1.0, "周其琛：（OS）高度下降到600，减速"),
                        (10.0, "没有角色名的下一段内心独白"),
                        (20.0, "较长间隔后仍是同一段内心独白"),
                        (21.0, "周其琛：收到，普通对话恢复"),
                        (22.0, "何亦瑶OS：虽然这里很破旧"),
                        (32.0, "这一段同样没有重复写角色名或OS"),
                        (33.0, "旁白：此处恢复正常旁白"),
                        (34.0, "报幕：第一集结束"),
                        (35.0, "制作单位名单"),
                    ]
                    canvas = self.frame.danmaku_canvas
                    canvas.position = 0
                    self.frame._set_danmaku_items(self.frame.load_generation, [
                        DanmakuItem(time, text, DANMAKU_MODE_SUBTITLE, user_id="subtitle-author")
                        for time, text in captions
                    ])
                    canvas.seek(start_time)
                    for position, _text in captions:
                        if position < start_time:
                            continue
                        canvas.position = position
                        canvas._spawn_due_items()
                    narration = "旁白" if book_mode else "旁白：此处恢复正常旁白"
                    self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list],
                                     ["周其琛", narration, "报幕：第一集结束", "制作单位名单"])

    def test_os_continuity_keeps_interleaved_xml_speakers_separate(self):
        for enabled in (False, True):
            with self.subTest(os_enabled=enabled):
                self.frame.subtitle_filter_enabled = True
                self.frame.subtitle_filter_rules = SubtitleFilterRules(dialogue_mode="full", os_body=enabled)
                self.frame.screen_reader.reset_mock()
                captions = [
                    DanmakuItem(1.0, "周其琛：（OS）内心独白首行", DANMAKU_MODE_SUBTITLE, user_id="a"),
                    DanmakuItem(2.0, "白子聿：另一人的正常台词", DANMAKU_MODE_SUBTITLE, user_id="b"),
                    DanmakuItem(3.0, "【镜头推近】", DANMAKU_MODE_SUBTITLE, user_id="a"),
                    DanmakuItem(15.0, "周其琛没有再次标注名字的内心独白", DANMAKU_MODE_SUBTITLE, user_id="a"),
                    DanmakuItem(16.0, "周其琛：现在是普通对话", DANMAKU_MODE_SUBTITLE, user_id="a"),
                    DanmakuItem(17.0, "Carlos：普通的英文人名", DANMAKU_MODE_SUBTITLE, user_id="a"),
                ]
                canvas = self.frame.danmaku_canvas
                canvas.position = 0
                self.frame._set_danmaku_items(self.frame.load_generation, captions)
                for item in captions:
                    canvas.position = item.time
                    canvas._spawn_due_items()
                expected = [captions[index].text for index in (1, 2, 4, 5)]
                if enabled:
                    expected.insert(0, "周其琛：（OS）")
                self.assertEqual([call.args[0] for call in self.frame.screen_reader.announce.call_args_list], expected)

    def test_seeking_in_book_filter_reannounces_the_current_speaker(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame._select_subtitle_filter_slot(1)
        self.frame._on_subtitle_due(DanmakuItem(1.0, "旁白：第一段", DANMAKU_MODE_SUBTITLE))
        self.assertEqual(self.frame.book_filter_last_role, "旁白")
        self.frame._seek_relative(15)
        self.assertIsNone(self.frame.book_filter_last_role)

    def test_role_only_does_not_repeat_name_for_inferred_dialogue_continuation(self):
        self.frame.subtitle_filter_rules = SubtitleFilterRules()
        self.press_f(control=True)
        self.frame.screen_reader.reset_mock()
        captions = mark_dialogue_continuations([
            DanmakuItem(1.0, "叶修：你们先走", DANMAKU_MODE_SUBTITLE, user_id="same"),
            DanmakuItem(2.0, "我随后就到", DANMAKU_MODE_SUBTITLE, user_id="same"),
        ])
        for caption in captions:
            self.frame._on_subtitle_due(caption)
        self.frame.screen_reader.announce.assert_called_once_with("叶修")

    def test_main_keyboard_digits_select_available_presets_up_to_ten(self):
        presets = list(default_filter_presets())
        presets.extend(SubtitleFilterPreset(f"自定义 {number}") for number in range(3, 11))
        presets[9] = SubtitleFilterPreset("只读信息", SubtitleFilterRules(dialogue_mode="full"))
        self.frame.subtitle_filter_presets = tuple(presets)
        self.frame.on_filter_slot_changed = Mock()
        self.frame.read_subtitle_enabled = True
        for digit, expected_slot in (("1", 0), ("0", 9)):
            event = Mock()
            event.GetKeyCode.return_value = ord(digit)
            event.ControlDown.return_value = False
            event.AltDown.return_value = False
            event.ShiftDown.return_value = False
            self.frame.on_char_hook(event)
            event.Skip.assert_not_called()
            self.assertEqual(self.frame.subtitle_filter_slot, expected_slot)
            self.assertTrue(self.frame.read_subtitle_enabled)
            self.assertTrue(self.frame.subtitle_filter_enabled)
            self.assertEqual(self.frame.subtitle_filter_rules, presets[expected_slot].rules)
        self.assertEqual([call.args[0] for call in self.frame.on_filter_slot_changed.call_args_list], [0, 9])
        numpad = Mock()
        numpad.GetKeyCode.return_value = wx.WXK_NUMPAD1
        self.frame.on_char_hook(numpad)
        numpad.Skip.assert_called_once()
        self.assertEqual(self.frame.subtitle_filter_slot, 9)

    def test_number_shortcut_does_not_start_subtitle_reading(self):
        self.frame.subtitle_filter_presets = self.frame.subtitle_filter_presets[:2]
        self.frame.read_subtitle_enabled = False
        self.frame.subtitle_filter_slot = 0
        self.frame.on_filter_slot_changed = Mock()
        event = Mock()
        event.GetKeyCode.return_value = ord("2")
        event.ControlDown.return_value = False
        event.AltDown.return_value = False
        event.ShiftDown.return_value = False
        self.frame.on_char_hook(event)
        self.assertFalse(self.frame.read_subtitle_enabled)
        self.assertFalse(self.frame.subtitle_filter_enabled)
        self.assertEqual(self.frame.subtitle_filter_slot, 0)
        self.frame.on_filter_slot_changed.assert_not_called()

    def test_number_shortcut_speaks_only_the_preset_name(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame.read_subtitle_enabled = True
        event = Mock()
        event.GetKeyCode.return_value = ord("2")
        event.ControlDown.return_value = False
        event.AltDown.return_value = False
        event.ShiftDown.return_value = False
        self.frame.on_char_hook(event)
        self.frame.status_reader.announce.assert_called_once_with("有声书过滤方案")

    def test_number_for_nonexistent_preset_does_nothing(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame.read_subtitle_enabled = True
        self.frame.on_filter_slot_changed = Mock()
        event = Mock()
        event.GetKeyCode.return_value = ord("3")
        event.ControlDown.return_value = False
        event.AltDown.return_value = False
        event.ShiftDown.return_value = False
        self.frame.on_char_hook(event)
        self.assertEqual(self.frame.subtitle_filter_slot, 0)
        self.frame.screen_reader.announce.assert_not_called()
        self.frame.on_filter_slot_changed.assert_not_called()

    def test_playback_menu_filter_preset_does_nothing_while_subtitles_are_off(self):
        self.frame.subtitle_filter_presets = default_filter_presets()
        self.frame.read_subtitle_enabled = False
        self.choose("2：有声书过滤方案")
        self.assertEqual(self.frame.subtitle_filter_slot, 0)
        self.assertFalse(self.frame.subtitle_filter_enabled)
        self.frame.screen_reader.announce.assert_not_called()

    def test_playback_menu_can_select_a_filter_preset(self):
        self.frame.read_subtitle_enabled = True
        self.choose("0：旧规则")
        self.assertEqual(self.frame.subtitle_filter_slot, 9)
        self.assertTrue(self.frame.read_subtitle_enabled)
        self.assertTrue(self.frame.subtitle_filter_enabled)

    def test_switch_warns_if_active_preset_cannot_be_saved(self):
        self.frame.on_filter_slot_changed = Mock(return_value=False)
        self.frame._select_subtitle_filter_slot(1)
        self.assertIn("未保存", self.frame.status_reader.announce.call_args.args[0])

    def test_menu_seeks_and_opens_jump_dialog_using_existing_operations(self):
        self.choose("快退 5 秒")
        self.player.seek.assert_called_once_with(-5)
        self.player.seek.reset_mock()
        self.choose("快进 5 秒")
        self.player.seek.assert_called_once_with(5)
        self.frame.status_reader.announce.assert_not_called()
        self.frame.screen_reader.announce.assert_not_called()
        with patch.object(self.frame, "_prompt_jump_to_time") as prompt:
            self.choose("跳转时间…")
        prompt.assert_called_once_with()

    def test_menu_reading_switches_are_local_and_reset_for_next_sound(self):
        self.choose("朗读弹幕")
        self.assertTrue(self.frame.read_danmaku_enabled)
        self.assertFalse(self.frame.read_danmaku_default)
        with patch.object(self.frame, "_load_danmaku"), patch("app.wx.CallLater"), patch("app.wx.CallAfter"):
            self.frame.play(PlaybackInfo(1, "第一集", "https://example.com/1.mp3"))
            self.frame.read_danmaku_enabled = True
            self.frame.read_subtitle_enabled = False
            self.frame.subtitle_filter_enabled = True
            self.frame.play(PlaybackInfo(2, "第二集", "https://example.com/2.mp3"))
        self.assertFalse(self.frame.read_danmaku_enabled)
        self.assertTrue(self.frame.read_subtitle_enabled)
        self.assertFalse(self.frame.subtitle_filter_enabled)

    def test_speed_menu_and_x_c_z_use_native_speed_levels(self):
        self.player.set_playback_rate.side_effect = lambda rate, callback: callback({"ok": True, "rate": rate})
        self.choose("1.25 倍")
        self.assertEqual(self.player.set_playback_rate.call_args.args[0], 1.25)
        self.assertEqual(self.frame.playback_rate, 1.25)
        self.frame._change_playback_rate(1)
        self.assertEqual(self.frame.playback_rate, 1.5)
        self.frame._change_playback_rate(-1)
        self.assertEqual(self.frame.playback_rate, 1.25)
        self.frame._set_playback_rate(1.0)
        self.assertEqual(self.frame.playback_rate, 1.0)

    def test_right_click_on_playback_image_and_keyboard_menu_open_actions(self):
        mouse_event = Mock()
        mouse_event.GetEventObject.return_value = self.frame.danmaku_canvas.bitmap_view
        mouse_event.GetPosition.return_value = wx.Point(30, 40)
        with patch.object(self.frame, "_show_playback_menu") as show_menu:
            self.frame.on_playback_right_up(mouse_event)
            show_menu.assert_called_once()
            context_event = Mock()
            context_event.GetPosition.return_value = wx.Point(30, 40)
            self.frame.on_playback_context_menu(context_event)
            show_menu.assert_called_once()

        key_event = Mock()
        key_event.GetKeyCode.return_value = wx.WXK_MENU
        with patch.object(self.frame, "_show_playback_menu") as show_menu:
            self.frame.on_char_hook(key_event)
            show_menu.assert_called_once_with(wx.DefaultPosition)
        key_event.Skip.assert_not_called()


    def test_r_opens_rules_even_when_reading_is_off_without_changing_toggles(self):
        self.frame.read_subtitle_enabled = False
        self.frame.on_filter_rules = Mock()
        event = Mock()
        event.GetKeyCode.return_value = ord("R")
        event.ControlDown.return_value = event.AltDown.return_value = event.ShiftDown.return_value = False
        with patch("app.wx.CallAfter"):
            self.frame.on_char_hook(event)
        self.frame.on_filter_rules.assert_called_once_with(self.frame)
        self.assertFalse(self.frame.read_subtitle_enabled)
        event.Skip.assert_not_called()
        event.ControlDown.return_value = True
        self.frame.on_char_hook(event)
        self.frame.on_filter_rules.assert_called_once()
        event.Skip.assert_called_once()

    def test_subtitle_jump_preserves_fractional_time_and_resumes_paused_audio(self):
        self.frame.playback = PlaybackInfo(123, "声音", "https://example.com/audio.mp3", duration_ms=120000)
        self.player.seek_to.side_effect = lambda seconds, callback, **kwargs: callback({"ok": True, "position": seconds, "paused": False})
        with patch.object(self.frame.danmaku_canvas, "sync_position") as sync:
            self.frame._jump_to_time_ready(self.frame.load_generation, 61.375,
                                          {"ok": True, "duration": 120, "paused": True})
        self.assertEqual(self.player.seek_to.call_args.args[0], 61.375)
        self.assertTrue(self.player.seek_to.call_args.kwargs["resume"])
        sync.assert_called_once_with(61.375, False, 1.0)

    def test_failed_resume_reports_seek_success_but_keeps_canvas_paused(self):
        self.frame.playback = PlaybackInfo(123, "声音", "https://example.com/audio.mp3", duration_ms=120000)
        with patch.object(self.frame.danmaku_canvas, "sync_position") as sync:
            self.frame._jump_to_time_done(self.frame.load_generation, 30,
                                         {"ok": True, "position": 30, "paused": True, "resume_error": True})
        sync.assert_called_once_with(30, True, 1.0)
        self.frame.status_reader.announce.assert_called_once_with("已跳转到30秒，但未能开始播放，请按空格重试")

    def test_f9_and_shift_f9_switch_output_and_only_announce_latest_result(self):
        self.frame.on_cycle_output = Mock()
        event = Mock()
        event.GetKeyCode.return_value = wx.WXK_F9
        event.ControlDown.return_value = event.AltDown.return_value = False
        event.ShiftDown.return_value = False
        self.frame.on_char_hook(event)
        forward, first_done = self.frame.on_cycle_output.call_args.args
        self.assertEqual(forward, 1)
        event.ShiftDown.return_value = True
        self.frame.on_char_hook(event)
        backward, last_done = self.frame.on_cycle_output.call_args.args
        self.assertEqual(backward, -1)
        first_done({"ok": True, "name": "旧声卡"})
        self.frame.screen_reader.announce.assert_not_called()
        last_done({"ok": True, "name": "跟随系统"})
        self.frame.status_reader.announce.assert_called_once_with("跟随系统")
        event.Skip.assert_not_called()

    def test_jump_dialog_cannot_seek_a_different_episode(self):
        self.frame.playback = PlaybackInfo(123, "声音", "https://example.com/audio.mp3", duration_ms=120000)
        dialog = Mock(seconds=15)
        def switch_episode():
            self.frame.load_generation += 1
            return wx.ID_OK
        dialog.ShowModal.side_effect = switch_episode
        with patch("app.JumpTimeDialog", return_value=dialog), patch("app.wx.CallAfter"):
            self.frame._prompt_jump_to_time()
        self.player.status.assert_not_called()
        self.player.seek_to.assert_not_called()


class JumpDialogTests(unittest.TestCase):
    def setUp(self):
        self.app = wx.GetApp() or wx.App(False)
        announcer_patch = patch("app.ScreenReaderAnnouncer")
        self.announcer_type = announcer_patch.start()
        self.addCleanup(announcer_patch.stop)

    def test_range_warning_uses_only_native_accessibility_notifications(self):
        dialog = JumpTimeDialog(None, "请输入跳转时间", 120, 0, lambda: [])
        self.addCleanup(dialog.Destroy)
        self.announcer_type.assert_called_once_with(dialog.range_message, native_only=True)
        dialog.time_input.SetValue("3")
        dialog.screen_reader.announce.assert_called_once_with("输入超出范围，当前音频总时长为2分00秒")

    def test_invalid_and_out_of_range_inputs_keep_dialog_open_for_correction(self):
        dialog = JumpTimeDialog(None, "请输入跳转时间", 120, 0, lambda: [])
        self.addCleanup(dialog.Destroy)
        with patch.object(dialog, "EndModal") as end, patch("app.wx.MessageBox") as message:
            for value in ("2", "3.70", "9" * 100):
                dialog.time_input.SetValue(value)
                dialog._accept_time(None)
                self.assertEqual(dialog.range_message.GetLabel(), "输入超出范围，当前音频总时长为2分00秒")
                message.assert_not_called()
                end.assert_not_called()
            dialog.time_input.SetValue("bad")
            dialog._accept_time(None)
            self.assertEqual(message.call_args.args[1], "时间格式错误")
            end.assert_not_called()
            dialog.time_input.SetValue("1.05")
            dialog._accept_time(None)
            self.assertEqual(dialog.seconds, 65)
            end.assert_called_once_with(wx.ID_OK)

    def test_range_is_announced_while_typing_without_enter_or_focus_change(self):
        dialog = JumpTimeDialog(None, "请输入跳转时间", 120, 0, lambda: [])
        self.addCleanup(dialog.Destroy)
        with patch("app.wx.MessageBox") as popup, patch.object(dialog, "EndModal") as end:
            dialog.time_input.SetValue("3")
            dialog.screen_reader.announce.assert_called_once_with("输入超出范围，当前音频总时长为2分00秒")
            self.assertFalse(dialog.FindWindow(wx.ID_OK).IsEnabled())
            for text in ("33", "33.", "33.5"):
                dialog.time_input.SetValue(text)
            dialog.screen_reader.announce.assert_called_once()
            self.assertTrue(dialog.subtitle_button.IsEnabled())
            dialog.time_input.SetValue("1.05")
            self.assertTrue(dialog.FindWindow(wx.ID_OK).IsEnabled())
            self.assertEqual(dialog.range_message.GetLabel(), "")
            dialog.time_input.SetValue("2")
            self.assertEqual(dialog.screen_reader.announce.call_count, 2)
            popup.assert_not_called()
            end.assert_not_called()

    def test_unknown_duration_revalidates_current_input_when_duration_arrives(self):
        dialog = JumpTimeDialog(None, "请输入跳转时间，共未知", None, 65, lambda: [])
        self.addCleanup(dialog.Destroy)
        dialog.time_input.SetValue("3.05")
        dialog.screen_reader.announce.assert_not_called()
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.load_generation = 2
        frame._jump_dialog_duration_ready(2, dialog, {"ok": True, "duration": 120})
        self.assertEqual(dialog.total_seconds, 120)
        dialog.screen_reader.announce.assert_called_once_with("输入超出范围，当前音频总时长为2分00秒")
        self.assertIn("共2分00秒", dialog.prompt_text.GetLabel())
        frame._jump_dialog_duration_ready(1, dialog, {"ok": True, "duration": 300})
        self.assertEqual(dialog.total_seconds, 120)

    def test_partial_input_and_second_overflow_are_validated_without_format_popups(self):
        dialog = JumpTimeDialog(None, "请输入跳转时间", 240, 0, lambda: [])
        self.addCleanup(dialog.Destroy)
        for value in ("", "3", "3.", "3.7", "3.05"):
            dialog.time_input.SetValue(value)
        dialog.screen_reader.announce.assert_not_called()
        dialog.time_input.SetValue("3.70")
        dialog.screen_reader.announce.assert_called_once_with("输入超出范围，当前音频总时长为4分00秒")

    def test_subtitle_list_contains_full_subtitles_only_in_time_order(self):
        dialog = SubtitleJumpDialog(None, [
            DanmakuItem(61.375, "陆驿站：你这几天还好吗？", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(3, "普通弹幕", 1),
            DanmakuItem(0, "片头", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(float("nan"), "无效时间", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(-1, "负数时间", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(2, " ", DANMAKU_MODE_SUBTITLE),
            DanmakuItem(100, "片头", DANMAKU_MODE_SUBTITLE),
        ], 70)
        self.addCleanup(dialog.Destroy)
        self.assertEqual([item.time for item in dialog.items], [0, 61.375, 100])
        self.assertEqual(dialog.subtitle_list.GetSelection(), 1)
        self.assertEqual(dialog.subtitle_list.GetString(1), "陆驿站：你这几天还好吗？，1分01.375秒")
        self.assertEqual(dialog.selected_seconds(), 61.375)
        event = Mock()
        event.GetKeyCode.return_value = wx.WXK_RETURN
        with patch.object(dialog, "FindFocus", return_value=dialog.subtitle_list), patch.object(dialog, "EndModal") as end:
            dialog._on_key(event)
            end.assert_called_once_with(wx.ID_OK)
        event.Skip.assert_not_called()

    def test_subtitle_choice_returns_precise_time_and_cancel_keeps_input_dialog(self):
        dialog = JumpTimeDialog(None, "跳转时间", 120, 0, lambda: [])
        self.addCleanup(dialog.Destroy)
        child = Mock(items=[Mock()])
        with patch("app.SubtitleJumpDialog", return_value=child), patch.object(dialog, "EndModal") as end:
            child.ShowModal.return_value = wx.ID_CANCEL
            dialog._choose_subtitle(None)
            end.assert_not_called()
            child.ShowModal.return_value = wx.ID_OK
            child.selected_seconds.return_value = 61.375
            dialog._choose_subtitle(None)
            self.assertEqual(dialog.seconds, 61.375)
            end.assert_called_once_with(wx.ID_OK)

    def test_no_loaded_subtitles_does_not_open_empty_modal_or_close_time_dialog(self):
        dialog = JumpTimeDialog(None, "跳转时间", None, 0, lambda: [])
        self.addCleanup(dialog.Destroy)
        with patch.object(dialog, "EndModal") as end, patch("app.wx.MessageBox") as message:
            dialog._choose_subtitle(None)
        end.assert_not_called()
        self.assertIn("当前暂无字幕", message.call_args.args[0])


class PlaybackJumpTests(unittest.TestCase):
    def test_minute_dot_second_inputs_are_not_decimal_minutes(self):
        for value, expected in (("3.45", 225), ("1.01", 61), ("3.5", 185),
                                ("3.05", 185), ("3.50", 230), ("3.70", 250), ("0.00", 0)):
            with self.subTest(value=value):
                self.assertEqual(PlaybackFrame._parse_jump_time(value), expected)
        for value in ("", "3.456", "-1.20", "3:45", "hello"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PlaybackFrame._parse_jump_time(value)

    def test_j_jumps_to_absolute_position_and_syncs_danmaku(self):
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.playback = PlaybackInfo(123, "声音", "https://example.com/audio.mp3", duration_ms=300000)
        frame.load_generation = 2
        frame.time_announcement_generation = 0
        frame.playback_rate = 1.0
        frame.player = Mock()
        frame.danmaku_canvas = Mock()
        frame.danmaku_canvas.current_position.return_value = 185
        frame.screen_reader = Mock()
        frame.status_reader = Mock()
        frame._set_parent_status = Mock()
        frame.player.status.side_effect = lambda callback: callback({"ok": True, "duration": 300, "paused": False})
        frame.player.seek_to.side_effect = lambda seconds, callback, **kwargs: callback({"ok": True, "position": seconds, "paused": False})
        event = Mock()
        event.GetKeyCode.return_value = ord("j")
        dialog = Mock()
        dialog.ShowModal.return_value = wx.ID_OK
        dialog.seconds = 250

        with patch("app.JumpTimeDialog", return_value=dialog) as create_dialog, patch("app.wx.CallAfter"):
            frame.on_char_hook(event)

        self.assertEqual(create_dialog.call_args.args[1],
                         "请输入跳转时间（分.秒，当前3分05秒，共5分00秒）")
        frame.player.seek_to.assert_called_once()
        self.assertEqual(frame.player.seek_to.call_args.args[0], 250)
        self.assertTrue(frame.player.seek_to.call_args.kwargs["resume"])
        frame.danmaku_canvas.sync_position.assert_called_once_with(250, False, 1.0)
        frame.status_reader.announce.assert_called_once_with("已跳转到4分10秒")
        frame.screen_reader.announce.assert_not_called()
        event.Skip.assert_not_called()

    def test_jump_prompt_reports_unknown_duration_without_example(self):
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.load_generation = 2
        frame.player = Mock()
        frame.playback = PlaybackInfo(123, "声音", "https://example.com/audio.mp3")
        frame.danmaku_canvas = Mock()
        frame.danmaku_canvas.current_position.return_value = 65
        dialog = Mock()
        dialog.ShowModal.return_value = wx.ID_CANCEL

        with patch("app.JumpTimeDialog", return_value=dialog) as create_dialog, patch("app.wx.CallAfter"):
            frame._prompt_jump_to_time()

        self.assertEqual(create_dialog.call_args.args[1],
                         "请输入跳转时间（分.秒，当前1分05秒，共未知）")
        self.assertNotIn("例如", create_dialog.call_args.args[1])

    def test_jump_rejects_time_past_duration_without_seeking(self):
        frame = PlaybackFrame.__new__(PlaybackFrame)
        frame.playback = PlaybackInfo(123, "声音", "https://example.com/audio.mp3", duration_ms=120000)
        frame.load_generation = 2
        frame.player = Mock()
        frame._set_parent_status = Mock()

        with patch("app.wx.MessageBox") as message_box:
            frame._jump_to_time_ready(2, 250, {"ok": True, "duration": 120, "paused": False})
        self.assertEqual(message_box.call_args.args[0], "输入超出范围，当前音频总时长为2分00秒")

        frame.player.seek_to.assert_not_called()
        self.assertIn("输入超出范围", frame._set_parent_status.call_args.args[0])


class SettingsMenuTests(unittest.TestCase):
    def setUp(self):
        self.app = wx.GetApp() or wx.App(False)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app_data_patch = patch("app_settings.app_data_dir", return_value=Path(self.directory.name))
        self.app_data_patch.start()
        self.addCleanup(self.app_data_patch.stop)
        api = Mock(cookie_header="")
        with patch("app.MaoerApi", return_value=api), patch("app.HiddenBrowserPlayer"), \
             patch("app.wx.CallAfter"), patch.object(MaoerFrame, "load_homepage"):
            self.frame = MaoerFrame()
        self.addCleanup(self.frame.Destroy)
        self.addCleanup(self.frame.audio_output_router.close)

    def menu_item(self, item_id):
        return self.frame.GetMenuBar().FindItemById(item_id)

    def change(self, item_id, enabled):
        item = self.menu_item(item_id)
        item.Check(enabled)
        event = wx.CommandEvent(wx.wxEVT_MENU, int(item_id))
        event.SetInt(int(enabled))
        self.frame.on_setting_changed(event)

    def test_defaults_and_menu_rebuild_keep_checkmarks(self):
        frame = self.frame
        self.assertEqual(frame.GetMenuBar().GetMenuLabelText(3), "设置")
        self.assertEqual(self.menu_item(frame.settings_startup_sound_menu_id).GetItemLabelText(), "播放启动音效")
        self.assertEqual(self.menu_item(frame.settings_subtitle_menu_id).GetItemLabelText(), "默认朗读字幕")
        self.assertEqual(self.menu_item(frame.settings_danmaku_menu_id).GetItemLabelText(), "默认朗读弹幕")
        self.assertEqual(self.menu_item(frame.settings_subtitle_filter_menu_id).GetItemLabelText(),
                         "字幕过滤规则（实验性功能）(R)…")
        self.assertTrue(self.menu_item(frame.settings_startup_sound_menu_id).IsChecked())
        self.assertFalse(self.menu_item(frame.settings_subtitle_menu_id).IsChecked())
        self.assertFalse(self.menu_item(frame.settings_danmaku_menu_id).IsChecked())
        self.change(frame.settings_startup_sound_menu_id, False)
        self.change(frame.settings_subtitle_menu_id, True)
        self.change(frame.settings_danmaku_menu_id, True)
        frame._update_account_menu()
        self.assertFalse(self.menu_item(frame.settings_startup_sound_menu_id).IsChecked())
        self.assertTrue(self.menu_item(frame.settings_subtitle_menu_id).IsChecked())
        self.assertTrue(self.menu_item(frame.settings_danmaku_menu_id).IsChecked())
        self.assertEqual(load_settings(), AppSettings(False, True, True))

    def test_filter_rules_dialog_edits_and_deduplicates_keywords(self):
        dialog = SubtitleFilterRulesDialog(self.frame, default_filter_presets(), 0)
        try:
            self.assertEqual(dialog.dialogue_mode.GetSelection(), 0)
            self.assertTrue(dialog.story_barrage.GetValue())
            self.assertTrue(dialog.os_body.GetValue())
            self.assertTrue(dialog.info_label_only.GetValue())
            dialog.dialogue_mode.SetSelection(1)
            dialog.preset_name.SetValue("只读提示")
            dialog.info_labels.SetValue("系统\n提示音\n系统")
            dialog.keywords.SetValue("广告\n \n广告\n剧透")
            dialog._add_preset(None)
            self.assertEqual(dialog.preset_name.GetValue(), "方案 3")
            dialog.preset_choice.SetSelection(0)
            dialog._on_slot_changed(None)
            presets, slot = dialog.get_configuration()
            self.assertEqual(slot, 0)
            self.assertEqual(presets[0], SubtitleFilterPreset("只读提示", SubtitleFilterRules(
                dialogue_mode="role", info_labels=("系统", "提示音"), keywords=("广告", "剧透")
            )))
            self.assertEqual(presets[2], SubtitleFilterPreset("方案 3"))
        finally:
            dialog.Destroy()

    def test_only_built_in_presets_offer_restore_defaults(self):
        dialog = SubtitleFilterRulesDialog(self.frame, default_filter_presets(), 0)
        try:
            self.assertTrue(dialog.restore_default_button.IsShown())
            dialog.dialogue_mode.SetSelection(1)
            dialog._restore_defaults(None)
            self.assertEqual(dialog.get_configuration()[0][0], default_filter_presets()[0])
            dialog._add_preset(None)
            self.assertEqual(len(dialog.get_configuration()[0]), 3)
            self.assertFalse(dialog.restore_default_button.IsShown())
            dialog.preset_choice.SetSelection(1)
            dialog._on_slot_changed(None)
            self.assertTrue(dialog.restore_default_button.IsShown())
            dialog.info_labels.SetValue("随意标签")
            dialog.os_body.SetValue(False)
            dialog._restore_defaults(None)
            self.assertEqual(dialog.get_configuration()[0][1], default_filter_presets()[1])
        finally:
            dialog.Destroy()

    def test_saving_book_preset_keeps_speaker_transition_rule(self):
        dialog = SubtitleFilterRulesDialog(self.frame, default_filter_presets(), 1)
        try:
            presets, selected = dialog.get_configuration()
            self.assertEqual(selected, 1)
            self.assertTrue(presets[1].rules.speaker_transitions_only)
            self.assertFalse(presets[0].rules.speaker_transitions_only)
        finally:
            dialog.Destroy()

    def test_filter_dialog_converts_retired_choice_only_in_its_draft(self):
        presets = (*default_filter_presets(), SubtitleFilterPreset(
            "旧方案", SubtitleFilterRules(dialogue_mode="full", keywords=("广告",)),
        ))
        dialog = SubtitleFilterRulesDialog(self.frame, presets, 2)
        try:
            self.assertEqual(dialog.dialogue_mode.GetCount(), 2)
            self.assertEqual(dialog.dialogue_mode.GetSelection(), 1)
            edited, slot = dialog.get_configuration()
            self.assertEqual(slot, 2)
            self.assertEqual(edited[2].rules.dialogue_mode, "role")
            self.assertEqual(edited[2].rules.keywords, ("广告",))
            self.assertEqual(presets[2].rules.dialogue_mode, "full")
        finally:
            dialog.Destroy()

    def test_filter_dialog_links_book_mode_and_preserves_disabled_label_text(self):
        dialog = SubtitleFilterRulesDialog(self.frame, default_filter_presets(), 0)
        try:
            dialog.info_labels.SetValue("系统\n提示音\n通知")
            dialog.info_label_only.SetValue(False)
            dialog.info_label_only.GetEventHandler().ProcessEvent(
                wx.CommandEvent(wx.EVT_CHECKBOX.typeId, dialog.info_label_only.GetId()))
            self.assertFalse(dialog.info_labels.IsEnabled())
            dialog.speaker_transitions_only.SetValue(True)
            dialog.speaker_transitions_only.GetEventHandler().ProcessEvent(
                wx.CommandEvent(wx.EVT_CHECKBOX.typeId, dialog.speaker_transitions_only.GetId()))
            rules = dialog.get_configuration()[0][0].rules
            self.assertEqual(rules.dialogue_mode, "role")
            self.assertTrue(rules.speaker_transitions_only)
            self.assertFalse(rules.info_label_only)
            self.assertEqual(rules.info_labels, ("系统", "提示音", "通知"))
            dialog.dialogue_mode.SetSelection(0)
            dialog.dialogue_mode.GetEventHandler().ProcessEvent(
                wx.CommandEvent(wx.EVT_RADIOBOX.typeId, dialog.dialogue_mode.GetId()))
            self.assertFalse(dialog.speaker_transitions_only.GetValue())
            self.assertFalse(dialog.get_configuration()[0][0].rules.speaker_transitions_only)
            dialog.info_label_only.SetValue(True)
            dialog.info_label_only.GetEventHandler().ProcessEvent(
                wx.CommandEvent(wx.EVT_CHECKBOX.typeId, dialog.info_label_only.GetId()))
            self.assertTrue(dialog.info_labels.IsEnabled())
            self.assertEqual(dialog.info_labels.GetValue(), "系统\n提示音\n通知")
        finally:
            dialog.Destroy()

    def test_add_preset_stops_at_ten(self):
        dialog = SubtitleFilterRulesDialog(self.frame, default_filter_presets(), 0)
        try:
            for _ in range(8):
                dialog._add_preset(None)
            self.assertEqual(len(dialog.get_configuration()[0]), 10)
            self.assertFalse(dialog.add_button.IsEnabled())
            dialog._add_preset(None)
            self.assertEqual(len(dialog.get_configuration()[0]), 10)
        finally:
            dialog.Destroy()

    def test_filter_rules_menu_saves_and_updates_open_player(self):
        frame = self.frame
        rules = SubtitleFilterRules(dialogue_mode="full", keywords=("广告",))
        presets = list(default_filter_presets())
        presets[1] = SubtitleFilterPreset("自选", rules)
        frame.player_frame = Mock(subtitle_filter_enabled=True, book_filter_last_role="旁白")
        with patch("app.SubtitleFilterRulesDialog") as dialog_type:
            dialog_type.return_value.ShowModal.return_value = wx.ID_OK
            dialog_type.return_value.get_configuration.return_value = (tuple(presets), 1)
            frame.on_subtitle_filter_rules(None)
        self.assertEqual(load_settings().subtitle_filter_presets[1].rules, rules)
        self.assertEqual(load_settings().active_subtitle_filter_slot, 1)
        self.assertEqual(frame.player_frame.subtitle_filter_rules, rules)
        self.assertEqual(frame.player_frame.subtitle_filter_slot, 1)
        self.assertTrue(frame.player_frame.subtitle_filter_enabled)
        self.assertIsNone(frame.player_frame.book_filter_last_role)

    def test_cancel_filter_rules_keeps_existing_settings(self):
        frame = self.frame
        with patch("app.SubtitleFilterRulesDialog") as dialog_type:
            dialog_type.return_value.ShowModal.return_value = wx.ID_CANCEL
            frame.on_subtitle_filter_rules(None)
        self.assertEqual(load_settings(), AppSettings())

    def test_rules_opened_from_playback_use_player_owner_and_current_slot(self):
        player = Mock(subtitle_filter_slot=1)
        self.frame.player_frame = player
        with patch("app.SubtitleFilterRulesDialog") as dialog_type:
            dialog_type.return_value.ShowModal.return_value = wx.ID_OK
            dialog_type.return_value.get_configuration.return_value = (default_filter_presets(), 1)
            self.frame._edit_subtitle_filter_rules(player)
        dialog_type.assert_called_once_with(player, default_filter_presets(), 1)
        self.assertEqual(load_settings().active_subtitle_filter_slot, 1)
        self.assertEqual(player.subtitle_filter_rules, default_filter_presets()[1].rules)

    def test_playback_number_shortcut_persists_selected_slot(self):
        self.assertTrue(self.frame._on_filter_slot_changed(1))
        self.assertEqual(self.frame.settings.active_subtitle_filter_slot, 1)
        self.assertEqual(load_settings().active_subtitle_filter_slot, 1)

    def test_output_submenu_checks_saved_device_and_enter_action_saves_it(self):
        self.frame.output_devices = (SYSTEM_OUTPUT, OutputDevice("card-a", "耳机 & 声卡"))
        self.frame._populate_output_device_menu()
        items = self.frame.output_device_menu.GetMenuItems()
        self.assertEqual([item.GetItemLabelText() for item in items], ["跟随系统", "耳机 & 声卡"])
        self.assertTrue(items[0].IsChecked())
        with patch.object(self.frame.audio_output_router, "select") as select:
            event = wx.CommandEvent(wx.wxEVT_MENU, items[1].GetId())
            self.frame.ProcessEvent(event)
            self.assertEqual(select.call_args.args[0], "card-a")
        self.assertEqual(load_settings().output_device_id, "card-a")
        self.frame._populate_output_device_menu()
        self.assertTrue(self.frame.output_device_menu.GetMenuItems()[1].IsChecked())

    def test_missing_default_is_visible_without_overwriting_saved_preference(self):
        self.frame.settings = replace(self.frame.settings, output_device_id="missing")
        self.frame._populate_output_device_menu()
        items = self.frame.output_device_menu.GetMenuItems()
        self.assertTrue(items[-1].IsChecked())
        self.assertFalse(items[-1].IsEnabled())
        self.assertIn("不可用", items[-1].GetItemLabelText())
        self.assertEqual(self.frame.settings.output_device_id, "missing")

    def test_output_shortcut_does_not_save_over_default(self):
        self.frame.settings = replace(self.frame.settings, output_device_id="default-card")
        with patch.object(self.frame.audio_output_router, "cycle") as cycle, patch("app.save_settings") as save:
            self.frame._cycle_output_device(-1, Mock())
        self.assertEqual(cycle.call_args.args[0], -1)
        save.assert_not_called()
        self.assertEqual(self.frame.settings.output_device_id, "default-card")

    def test_device_disconnect_background_notice_does_not_use_subtitle_speech(self):
        player = Mock()
        self.frame.player_frame = player
        with patch.object(self.frame.audio_output_router, "poll", side_effect=lambda done: done({"ok": True, "fallback": True})), \
             patch("app.wx.CallAfter", side_effect=lambda fn, *args: fn(*args)), patch("app.wx.CallLater"):
            self.frame._poll_output_device(self.frame._audio_poll_generation)
        player._announce_status.assert_called_once_with("原播放设备不可用，音频输出：跟随系统")
        player.screen_reader.announce.assert_not_called()

    def test_failed_slot_save_keeps_persisted_choice(self):
        with patch("app.save_settings", side_effect=OSError("disk full")):
            self.assertFalse(self.frame._on_filter_slot_changed(9))
        self.assertEqual(self.frame.settings.active_subtitle_filter_slot, 0)

    def test_account_subscriptions_are_named_my_followed_dramas(self):
        frame = self.frame
        frame.account_logged_in = True
        frame._update_account_menu()
        self.assertEqual(self.menu_item(frame.account_subscriptions_menu_id).GetItemLabelText(), "我的追剧")

        frame._run_background = Mock()
        frame._enter_items = Mock()
        frame.on_account_subscriptions(None)
        status, work, done = frame._run_background.call_args.args
        self.assertEqual(status, "正在加载我的追剧...")
        frame.api.subscribed_dramas.return_value = []
        done(work())
        self.assertEqual(frame._enter_items.call_args.args[1], "我的追剧")

    def test_account_history_prompts_login_when_signed_out(self):
        frame = self.frame
        self.assertEqual(self.menu_item(frame.account_history_menu_id).GetItemLabelText(), "我的播放历史")
        frame._run_background = Mock()
        with patch("app.wx.MessageBox") as message_box:
            frame.on_account_history(None)
        self.assertIn("请先登录", message_box.call_args.args[0])
        frame._run_background.assert_not_called()

    def test_account_history_uses_account_data_without_resume_prompt(self):
        frame = self.frame
        frame.api.cookie_header = "test-cookie"
        frame._run_background = Mock()
        frame._enter_items = Mock()
        items = [MediaItem(kind="sound", id=12, title="第一集", raw={"_history_date": "今天"})]
        frame.api.playback_history.return_value = items

        frame.on_account_history(None)

        status, work, done = frame._run_background.call_args.args
        self.assertEqual(status, "正在加载我的播放历史...")
        with patch("app.wx.MessageBox") as message_box:
            done(work())
        message_box.assert_not_called()
        self.assertEqual(frame._enter_items.call_args.args[0], items)
        self.assertEqual(frame._enter_items.call_args.args[1], "我的播放历史")
        self.assertTrue(frame._enter_items.call_args.kwargs["hide_detail_column"])
        frame.current_title = "我的播放历史"
        self.assertEqual(frame._display_item_title(items[0]), "今天 · 第一集")

    def test_my_following_requires_login_and_opens_account_list(self):
        frame = self.frame
        self.assertEqual(self.menu_item(frame.account_following_menu_id).GetItemLabelText(), "我的关注")
        frame._run_background = Mock()
        with patch("app.wx.MessageBox") as message_box:
            frame.on_account_following(None)
        self.assertIn("请先登录", message_box.call_args.args[0])
        frame._run_background.assert_not_called()

        frame.api.cookie_header = "test-cookie"
        frame.api.followed_accounts.return_value = [
            MediaItem(kind="publisher_account", id=10452520, title="三米造事务所")
        ]
        frame._enter_items = Mock()
        frame.on_account_following(None)
        _status, work, done = frame._run_background.call_args.args
        done(work())
        self.assertEqual(frame._enter_items.call_args.args[1], "我的关注")
        self.assertTrue(frame._enter_items.call_args.kwargs["hide_detail_column"])

        frame._run_background.reset_mock()
        frame.open_item(frame.api.followed_accounts.return_value[0])
        _status, work, done = frame._run_background.call_args.args
        profile = PublisherProfile(10452520, "三米造事务所", 5, 1, "简介", True)
        frame.api.publisher_profile.return_value = profile
        done(work())
        self.assertEqual(frame._enter_items.call_args.args[1], "发布者：三米造事务所")

    def test_changed_reading_default_also_updates_open_player(self):
        player_frame = Mock(read_subtitle_enabled=False, read_danmaku_enabled=False,
                            subtitle_filter_enabled=True)
        self.frame.player_frame = player_frame
        self.change(self.frame.settings_subtitle_menu_id, True)
        self.change(self.frame.settings_danmaku_menu_id, True)
        self.assertTrue(player_frame.read_subtitle_enabled)
        self.assertTrue(player_frame.read_danmaku_enabled)
        self.assertFalse(player_frame.subtitle_filter_enabled)

    def test_new_playback_window_receives_saved_reading_defaults(self):
        frame = self.frame
        self.change(frame.settings_subtitle_menu_id, True)
        self.change(frame.settings_danmaku_menu_id, True)
        playback = Mock(sound_id=34, title="声音")
        player_frame = Mock()
        with patch("app.PlaybackFrame", return_value=player_frame) as create_player, \
             patch("app.threading.Thread"):
            frame._play(playback)
        self.assertTrue(create_player.call_args.kwargs["read_subtitle_default"])
        self.assertTrue(create_player.call_args.kwargs["read_danmaku_default"])
        self.assertEqual(create_player.call_args.kwargs["subtitle_filter_presets"], default_filter_presets())
        self.assertEqual(create_player.call_args.kwargs["subtitle_filter_slot"], 0)
        self.assertEqual(create_player.call_args.kwargs["on_filter_slot_changed"],
                         frame._on_filter_slot_changed)

    def test_failed_save_restores_checkmark_and_current_settings(self):
        frame = self.frame
        with patch("app.save_settings", side_effect=OSError("disk full")):
            with patch.object(frame, "show_error") as show_error:
                self.change(frame.settings_startup_sound_menu_id, False)
        self.assertTrue(self.menu_item(frame.settings_startup_sound_menu_id).IsChecked())
        self.assertEqual(frame.settings, AppSettings())
        show_error.assert_called_once()

    def test_vip_menu_contains_catalogs_without_daily_claim(self):
        entries = self.frame.GetMenuBar().GetMenu(2).GetMenuItems()
        self.assertEqual([entry.GetItemLabelText() for entry in entries],
                         ["会员限免剧", "会员折扣剧"])

    def test_sound_and_drama_menus_both_show_publisher_action(self):
        frame = self.frame
        original_list = frame.list
        frame.list = Mock()
        frame.show_sound_drama = Mock()
        frame.show_item_publisher = Mock()
        try:
            sound = MediaItem(kind="sound", id=13042654, title="预告·归路")
            drama = MediaItem(kind="drama", id=94733, title="灯花笑 上季")
            seen = []

            def choose(menu, _position):
                labels = [entry.GetItemLabelText() for entry in menu.GetMenuItems()]
                seen.append(labels)
                target = "查看该剧集" if len(seen) == 1 else "查看发布者"
                return next(entry.GetId() for entry in menu.GetMenuItems()
                            if entry.GetItemLabelText() == target)

            frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
            frame._display_item_menu(wx.Point(1, 1), sound, None)
            frame._display_item_menu(wx.Point(1, 1), drama, None)

            self.assertIn("查看该剧集", seen[0])
            self.assertIn("查看发布者", seen[0])
            self.assertIn("查看发布者", seen[1])
            self.assertNotIn("查看该剧集", seen[1])
            frame.show_sound_drama.assert_called_once_with(sound)
            frame.show_item_publisher.assert_called_once_with(drama)
        finally:
            frame.list = original_list

    def test_publisher_profile_links_to_published_dramas_and_sounds(self):
        frame = self.frame
        profile = PublisherProfile(10452520, "三米造事务所", 194648, 1, "同名微博：三米造事务所")
        item = MediaItem(kind="sound", id=13042654, title="预告·归路")
        frame.api.publisher_profile_for_item.return_value = profile
        frame._run_background = Mock()
        frame._enter_items = Mock()
        frame._navigation_state_snapshot = Mock(return_value=Mock())

        frame.show_item_publisher(item)
        _status, work, done = frame._run_background.call_args.args
        done(work())

        profile_items = frame._enter_items.call_args.args[0]
        self.assertEqual([entry.kind for entry in profile_items],
                         ["publisher_profile", "publisher_dramas", "publisher_sounds"])
        self.assertEqual(profile_items[0].title,
                         "三米造事务所粉丝：194648关注：1简介：同名微博：三米造事务所")
        self.assertEqual([entry.title for entry in profile_items[1:]], ["Ta的剧集", "Ta的声音"])

        frame.api.publisher_dramas.return_value = [MediaItem(kind="drama", id=94733, title="灯花笑 上季")]
        frame.open_item(profile_items[1])
        _status, work, done = frame._run_background.call_args.args
        done(work())
        self.assertEqual(frame._enter_items.call_args.args[0][0].title, "灯花笑 上季")
        self.assertEqual(frame._enter_items.call_args.args[1], "三米造事务所 · Ta的剧集")

        frame.api.publisher_sounds.return_value = [MediaItem(kind="sound", id=13042654, title="预告·归路")]
        frame.open_item(profile_items[2])
        _status, work, done = frame._run_background.call_args.args
        done(work())
        self.assertEqual(frame._enter_items.call_args.args[0][0].title, "预告·归路")

    def test_publisher_summary_is_one_focus_stop_without_prefix(self):
        profile = PublisherProfile(7, "微糖工作室", 56564, 0, "微糖也是甜o(≧v≦)o")

        items = self.frame._publisher_profile_items(profile)

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].title,
                         "微糖工作室粉丝：56564关注：0简介：微糖也是甜o(≧v≦)o")
        self.assertEqual([item.kind for item in items],
                         ["publisher_profile", "publisher_dramas", "publisher_sounds"])
        self.frame._show_item_detail_dialog = Mock()
        self.frame.open_item(items[0])
        self.assertIn("粉丝：56564", self.frame._show_item_detail_dialog.call_args.args[1])

    def test_sound_drama_navigation_contains_one_drama(self):
        frame = self.frame
        sound = MediaItem(kind="sound", id=13042654, title="预告·归路")
        drama = MediaItem(kind="drama", id=94733, title="灯花笑 上季")
        frame.api.drama_for_sound.return_value = drama
        frame._run_background = Mock()
        frame._enter_items = Mock()
        frame._navigation_state_snapshot = Mock(return_value=Mock())

        frame.show_sound_drama(sound)
        _status, work, done = frame._run_background.call_args.args
        done(work())

        frame.api.drama_for_sound.assert_called_once_with(sound.id)
        self.assertEqual(frame._enter_items.call_args.args[0], [drama])

    def test_publisher_follow_menu_uses_prefetched_status(self):
        frame = self.frame
        frame.api.cookie_header = "test-cookie"
        profile = PublisherProfile(10452520, "三米造事务所", 5, 1, "简介", True, "test-cookie")
        frame.current_title = "发布者：三米造事务所"
        frame.items = frame._publisher_profile_items(profile)
        frame._run_background = Mock()
        frame._selected_index = Mock(return_value=0)
        original_list = frame.list
        frame.list = Mock()
        try:
            labels = []

            def choose(menu, _position):
                labels.append([entry.GetItemLabelText() for entry in menu.GetMenuItems()])
                return menu.GetMenuItems()[0].GetId()

            frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
            frame._show_selected_item_menu(wx.Point(1, 1))
            self.assertEqual(labels, [["取消关注"]])
            frame.api.publisher_profile.assert_not_called()
            _status, work, done = frame._run_background.call_args.args
            frame.api.set_publisher_follow.return_value = "取消关注成功"
            with patch("app.wx.MessageBox"):
                done(work())
            self.assertFalse(profile.followed)
            self.assertEqual(profile.followers, 4)
            self.assertEqual(frame.items[0].title, "三米造事务所粉丝：4关注：1简介：简介")
            frame.list.SetItem.assert_called_with(0, 0, frame.items[0].title)
        finally:
            frame.list = original_list

    def test_publisher_follow_status_is_not_reused_after_account_switch(self):
        frame = self.frame
        frame.api.cookie_header = "other-cookie"
        profile = PublisherProfile(10452520, "三米造事务所", 5, 1, "简介", True, "old-cookie")
        frame._run_background = Mock()
        original_list = frame.list
        frame.list = Mock()
        try:
            def choose(menu, _position):
                self.assertIn("状态未确认", menu.GetMenuItems()[0].GetItemLabelText())
                self.assertFalse(menu.GetMenuItems()[0].IsEnabled())
                return 0

            frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
            frame._display_publisher_follow_menu(wx.Point(1, 1), profile)
            frame._run_background.assert_not_called()
        finally:
            frame.list = original_list

    def test_publisher_follow_menu_says_login_when_signed_out(self):
        frame = self.frame
        profile = PublisherProfile(10452520, "三米造事务所", 5, 1, "简介", None)
        frame._run_background = Mock()
        original_list = frame.list
        frame.list = Mock()
        try:
            def choose(menu, _position):
                self.assertEqual(menu.GetMenuItems()[0].GetItemLabelText(), "请先登录")
                return menu.GetMenuItems()[0].GetId()

            frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
            with patch("app.wx.MessageBox") as message_box:
                frame._display_publisher_follow_menu(wx.Point(1, 1), profile)
            self.assertIn("请先登录", message_box.call_args.args[0])
            frame._run_background.assert_not_called()
        finally:
            frame.list = original_list


class PaidSoundRoutingTests(unittest.TestCase):
    def test_paid_sound_is_delegated_to_playback_resolution(self) -> None:
        frame = MaoerFrame.__new__(MaoerFrame)
        calls: list[str] = []
        frame._prompt_sound_purchase = lambda _item: calls.append("prompt")
        frame._play_sound_item = lambda _item: calls.append("play")

        frame.open_item(
            MediaItem(
                kind="sound",
                id=456,
                title="会员限免剧",
                need_pay=True,
                raw={"search_result": True},
            )
        )

        self.assertEqual(calls, ["play"])

    def test_paid_sound_reaches_playback_worker(self) -> None:
        frame = MaoerFrame.__new__(MaoerFrame)
        calls: list[str] = []
        frame.current_title = "搜索结果"
        frame._prompt_sound_purchase = lambda _item: calls.append("prompt")
        frame._run_background = lambda *_args, **_kwargs: calls.append("background")

        frame._play_sound_item(
            MediaItem(
                kind="sound",
                id=456,
                title="会员限免剧",
                need_pay=True,
                raw={"search_result": True},
            )
        )

        self.assertEqual(calls, ["background"])


class PublisherColumnTests(unittest.TestCase):
    def make_frame(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.current_title = "首页"
        frame.hide_list_detail_column = False
        frame.items = []
        frame._opened_drama_id = None
        frame._set_list_column_header = Mock()
        return frame

    def test_generic_column_is_publisher_not_author(self):
        frame = self.make_frame()
        frame._update_list_column_headers("首页")
        frame._set_list_column_header.assert_any_call(1, "发布")

    def test_never_uses_section_or_author_credit_as_publisher(self):
        frame = self.make_frame()
        item = MediaItem(kind="drama", id=12, title="测试剧", subtitle="首页 / 近三日热门声音",
                         raw={"author": "原著作者及出品说明"})
        self.assertEqual(frame._item_publisher(item), "")
        item.raw["_publisher_name"] = "发布账号"
        self.assertEqual(frame._item_publisher(item), "发布账号")

    def test_vip_catalog_shows_publisher_instead_of_benefit(self):
        for title, benefit in (("会员限免剧", "会员限免（会员畅听）"),
                               ("会员折扣剧", "会员折扣")):
            with self.subTest(title=title):
                frame = self.make_frame()
                frame.current_title = title
                item = MediaItem(kind="drama", id=12, title="剧", subtitle=benefit,
                                 raw={"_hide_author": True, "_publisher_name": "发布账号"})
                self.assertEqual(frame._item_publisher(item), "发布账号")
                frame._update_list_column_headers(title)
                frame._set_list_column_header.assert_any_call(1, "发布")

    def test_vip_catalog_resolves_missing_publisher_like_homepage(self):
        for title in ("会员限免剧", "会员折扣剧"):
            with self.subTest(title=title):
                frame = self.make_frame()
                frame.current_title = title
                item = MediaItem(kind="drama", id=12, title="剧", subtitle="会员权益",
                                 raw={"_hide_author": True})
                frame.items = [item]
                frame.list = Mock()
                frame.api = Mock()
                frame.api.publisher_name_for_item.return_value = "发布账号"
                frame._publisher_request = object()
                self.assertTrue(frame._uses_publisher_column())

                with patch("app.threading.Thread") as thread, \
                     patch("app.wx.CallAfter", side_effect=lambda callback, *args: callback(*args)):
                    frame._start_publisher_resolution([item])
                    thread.call_args.kwargs["target"]()

                frame.api.publisher_name_for_item.assert_called_once_with(item)
                frame.list.SetItem.assert_called_once_with(0, 1, "发布账号")

    def test_background_lookup_updates_only_the_current_item(self):
        frame = self.make_frame()
        item = MediaItem(kind="sound", id=34, title="声音", subtitle="首页热门声音", raw={})
        frame.items = [item]
        frame.list = Mock()
        frame.api = Mock()
        frame.api.publisher_name_for_item.return_value = "发布账号"
        frame._publisher_request = object()

        with patch("app.threading.Thread") as thread, \
             patch("app.wx.CallAfter", side_effect=lambda callback, *args: callback(*args)):
            frame._start_publisher_resolution([item])
            thread.call_args.kwargs["target"]()

        self.assertEqual(item.raw["_publisher_name"], "发布账号")
        frame.list.SetItem.assert_called_once_with(0, 1, "发布账号")
        frame._publisher_request = object()
        frame.list.SetItem.reset_mock()
        frame._apply_resolved_publisher(item, "过期结果", object())
        frame.list.SetItem.assert_not_called()


class DramaFollowButtonTests(unittest.TestCase):
    def test_episode_list_has_no_tab_focusable_follow_button(self):
        app = wx.App(False)
        api = Mock(cookie_header="test-cookie")
        with patch("app.MaoerApi", return_value=api), patch("app.HiddenBrowserPlayer"), \
             patch.object(MaoerFrame, "load_homepage"), \
             patch.object(MaoerFrame, "_refresh_account_title"):
            frame = MaoerFrame()
            try:
                frame.set_items([MediaItem(kind="drama", id=12, title="剧")], "搜索结果")
                self.assertFalse(hasattr(frame, "drama_follow_button"))

                previous = frame._navigation_state_snapshot()
                frame._enter_items(
                    [MediaItem(kind="sound", id=34, title="第一集")],
                    "剧",
                    previous,
                    opened_drama_id=12,
                )
                self.assertFalse(hasattr(frame, "drama_follow_button"))

                self.assertTrue(frame.go_back())
                self.assertFalse(hasattr(frame, "drama_follow_button"))
                api.set_drama_follow.assert_not_called()
            finally:
                frame.Destroy()
                app.Yield()


class DramaWorkMenuTests(unittest.TestCase):
    def test_context_menu_opens_immediately_when_follow_status_is_unknown(self):
        app = wx.App(False)
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame.api.cached_drama_follow_status.return_value = None
        frame._selected_index = Mock(return_value=0)
        frame.list = Mock()
        item = MediaItem(kind="drama", id=12, title="已追的剧", raw={})
        frame.items = [item]
        labels: list[str] = []

        def choose(menu, _position):
            labels.extend(entry.GetItemLabelText() for entry in menu.GetMenuItems())
            return wx.ID_NONE

        frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
        frame._run_background = Mock()
        frame._prefetch_follow_status_for_item = Mock()

        frame._show_selected_item_menu(wx.Point(1, 1))

        self.assertIn("管理追剧…", labels)
        frame._run_background.assert_not_called()
        frame._prefetch_follow_status_for_item.assert_called_once_with(item)
        app.Yield()

    def test_context_menu_uses_verified_cache_over_stale_list_flag(self):
        app = wx.App(False)
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame.api.cached_drama_follow_status.return_value = None
        frame._selected_index = Mock(return_value=0)
        frame.list = Mock()
        item = MediaItem(kind="drama", id=12, title="已追的剧", raw={"like": 0})
        frame.items = [item]
        labels = []
        frame.list.GetPopupMenuSelectionFromUser.side_effect = lambda menu, _: (
            labels.extend(entry.GetItemLabelText() for entry in menu.GetMenuItems()) or wx.ID_NONE
        )
        frame._run_background = Mock()
        frame._prefetch_follow_status_for_item = Mock()
        frame._follow_status_cookie = "test-cookie"
        frame._follow_status_cache = {12: True}
        frame._follow_status_pending = {}

        frame._show_selected_item_menu(wx.Point(1, 1))

        self.assertIn("取消追剧", labels)
        frame._run_background.assert_not_called()
        app.Yield()

    def test_unknown_follow_status_keeps_other_menu_actions_available(self):
        app = wx.App(False)
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame._selected_index = Mock(return_value=0)
        frame.SetStatusText = Mock()
        frame.list = Mock()
        frame.items = [MediaItem(kind="drama", id=12, title="剧")]
        entries = []

        def choose(menu, _position):
            entries.extend((entry.GetItemLabelText(), entry.IsEnabled()) for entry in menu.GetMenuItems())
            return wx.ID_NONE

        frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
        frame._run_background = Mock()
        frame._prefetch_follow_status_for_item = Mock()
        frame.api.cached_drama_follow_status.return_value = None

        frame._show_selected_item_menu(wx.Point(1, 1))

        self.assertIn("用网页打开", [label for label, _ in entries])
        follow = next(enabled for label, enabled in entries if label == "管理追剧…")
        self.assertTrue(follow)
        app.Yield()

    def test_unknown_follow_menu_uses_existing_verified_action(self):
        app = wx.App(False)
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame._selected_index = Mock(return_value=0)
        frame.list = Mock()
        frame._run_background = Mock()
        frame._prefetch_follow_status_for_item = Mock()
        frame._follow_drama_from_work_menu = Mock()
        frame.api.cached_drama_follow_status.return_value = None
        frame.items = [MediaItem(kind="drama", id=12, title="原来的剧")]
        frame.list.GetPopupMenuSelectionFromUser.side_effect = lambda menu, _position: next(
            entry.GetId() for entry in menu.GetMenuItems() if entry.GetItemLabelText() == "管理追剧…"
        )

        frame._show_selected_item_menu(wx.Point(1, 1))

        frame._follow_drama_from_work_menu.assert_called_once_with(frame.items[0])
        app.Yield()

    def test_known_follow_menu_preserves_the_action_user_saw(self):
        app = wx.App(False)
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame.api.cached_drama_follow_status.return_value = True
        frame._selected_index = Mock(return_value=0)
        frame._follow_drama_from_work_menu = Mock()
        frame.list = Mock()
        item = MediaItem(kind="drama", id=12, title="剧")
        frame.items = [item]
        frame.list.GetPopupMenuSelectionFromUser.side_effect = lambda menu, _position: next(
            entry.GetId() for entry in menu.GetMenuItems() if entry.GetItemLabelText() == "取消追剧"
        )

        frame._show_selected_item_menu(wx.Point(1, 1))

        frame._follow_drama_from_work_menu.assert_called_once_with(item, expected_followed=True)
        app.Yield()

    def test_selected_work_prefetches_once_and_caches_result(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame.api.cached_drama_follow_status.return_value = None
        frame.api.drama_follow_status.return_value = True
        item = MediaItem(kind="drama", id=12, title="剧")

        with patch("app.threading.Thread") as thread, \
             patch("app.wx.CallAfter", side_effect=lambda callback, *args: callback(*args)):
            frame._prefetch_follow_status_for_item(item)
            frame._prefetch_follow_status_for_item(item)
            thread.assert_called_once()
            thread.call_args.kwargs["target"]()

        frame.api.drama_follow_status.assert_called_once_with(12)
        self.assertTrue(frame._known_follow_status(item))

    def test_list_selection_starts_prefetch_and_following_list_seeds_cache(self):
        app = wx.App(False)
        api = Mock(cookie_header="test-cookie")
        api.cached_drama_follow_status.return_value = None
        with patch("app.MaoerApi", return_value=api), patch("app.HiddenBrowserPlayer"), patch("app.wx.CallAfter"), \
             patch.object(MaoerFrame, "load_homepage"), \
             patch.object(MaoerFrame, "_refresh_account_title"):
            frame = MaoerFrame()
        try:
            item = MediaItem(kind="drama", id=12, title="剧", raw={"username": "发布者"})
            frame._prefetch_follow_status_for_item = Mock()
            frame.set_items([item], "搜索结果")
            frame._prefetch_follow_status_for_item.assert_called_with(item)

            frame.set_items([item], "我的追剧")
            self.assertTrue(frame._known_follow_status(item))
        finally:
            frame.Destroy()
            app.Yield()

    def test_follow_cache_does_not_cross_accounts(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="first-cookie")
        frame.api.cached_drama_follow_status.return_value = None
        item = MediaItem(kind="drama", id=12, title="剧")

        with patch("app.threading.Thread"):
            frame._prefetch_follow_status_for_item(item)
        token = frame._follow_status_pending[12]
        frame.api.cookie_header = "second-cookie"
        frame._finish_follow_status_prefetch(12, "first-cookie", token, True)

        self.assertIsNone(frame._known_follow_status(item))
        self.assertEqual(frame._follow_status_pending, {})

    def test_my_following_list_seeds_positive_status_only(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame.api.cached_drama_follow_status.return_value = None
        followed = MediaItem(kind="drama", id=12, title="已追的剧")
        other = MediaItem(kind="drama", id=13, title="未知的剧")

        frame._remember_followed_items([followed], "我的追剧")

        self.assertTrue(frame._known_follow_status(followed))
        self.assertIsNone(frame._known_follow_status(other))
        frame._follow_status_cache[12] = False
        frame._remember_followed_items([followed], "我的追剧")
        self.assertFalse(frame._known_follow_status(followed))

    def test_changed_server_status_cannot_reverse_the_selected_menu_action(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame._run_background = Mock()
        frame.SetStatusText = Mock()
        item = MediaItem(kind="drama", id=12, title="剧")

        with patch("app.wx.MessageBox") as notify:
            frame._confirm_work_menu_follow(item, True, "test-cookie", expected_followed=False)

        frame._run_background.assert_not_called()
        self.assertTrue(frame._known_follow_status(item))
        notify.assert_called_once()

    def test_context_menu_routes_purchase_from_drama_not_episode(self):
        app = wx.App(False)
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame._selected_index = Mock(return_value=0)
        frame._prompt_drama_purchase = Mock()
        frame._run_background = Mock()
        frame._prefetch_follow_status_for_item = Mock()
        frame.api.cached_drama_follow_status.return_value = False
        frame.list = Mock()
        seen_labels: list[list[str]] = []

        def choose(menu, _position):
            entries = menu.GetMenuItems()
            seen_labels.append([entry.GetItemLabelText() for entry in entries])
            purchase = next((entry for entry in entries if entry.GetItemLabelText().startswith("购买本剧")), None)
            return purchase.GetId() if purchase is not None else wx.ID_NONE

        frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
        frame.items = [MediaItem(kind="drama", id=12, title="剧", pay_type=2, price=30)]
        frame._show_selected_item_menu(wx.Point(1, 1))
        frame.items = [MediaItem(kind="sound", id=34, title="第一集", drama_id=12, need_pay=True)]
        frame._show_selected_item_menu(wx.Point(1, 1))

        self.assertIn("购买本剧", seen_labels[0])
        self.assertIn("追剧", seen_labels[0])
        self.assertFalse(any(label.startswith("购买本剧") for label in seen_labels[1]))
        frame._prompt_drama_purchase.assert_called_once_with(12)
        app.Yield()

    def test_purchase_action_only_belongs_to_unowned_whole_drama(self):
        drama = MediaItem(kind="drama", id=12, title="剧", pay_type=2, price=30, raw={"pay_type": 2})
        self.assertEqual(MaoerFrame._drama_purchase_menu_label(drama), "购买本剧")
        self.assertIsNone(MaoerFrame._drama_purchase_menu_label(MediaItem(
            kind="sound", id=34, title="第一集", drama_id=12, pay_type=2, price=30,
        )))
        drama.raw["_purchased_full_drama"] = True
        self.assertIsNone(MaoerFrame._drama_purchase_menu_label(drama))

    def test_paid_episode_label_disappears_after_purchase(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.current_title = "剧"
        frame._opened_drama_id = 12
        frame.list = Mock()
        item = MediaItem(kind="sound", id=34, title="第一集", drama_id=12, need_pay=True)
        frame.items = [item]
        self.assertEqual(frame._display_item_title(item), "第一集（付费）")

        frame._mark_sound_purchased_in_items(34)

        self.assertEqual(frame._display_item_title(item), "第一集")
        self.assertTrue(item.raw["_purchased_sound"])
        frame.list.SetItem.assert_called_once_with(0, 0, "第一集")

    def test_member_vip_limited_episode_label_is_limited_free(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.current_title = "剧"
        frame._opened_drama_id = 12
        item = MediaItem(kind="sound", id=34, title="第一集", drama_id=12,
                         need_pay=True, raw={"_member_vip_limited_free": True})
        self.assertEqual(frame._display_item_title(item), "第一集（限免）")

    def test_purchase_confirmations_show_price_without_playback_claim(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame._confirm_purchase = Mock(return_value=False)
        info = DramaPurchaseInfo(drama_id=12, title="测试剧", pay_type=2, need_pay=True, price=30)
        item = MediaItem(kind="sound", id=34, title="第一集", drama_id=12, need_pay=True)

        frame._confirm_drama_purchase(info)
        self.assertEqual(frame._confirm_purchase.call_args.args[0], "《测试剧》价格为 30 钻石，是否购买？")
        frame._confirm_episode_purchase(item, info, 10)
        self.assertEqual(frame._confirm_purchase.call_args.args[0],
                         "广播剧：测试剧\n《第一集》价格为 10 钻石，是否购买这一集？")

    def test_buying_whole_drama_updates_work_when_navigating_back(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        work = MediaItem(kind="drama", id=12, title="剧", pay_type=2, price=30)
        episode = MediaItem(kind="sound", id=34, title="第一集", drama_id=12, need_pay=True)
        frame.items = [episode]
        frame.navigation_stack = [NavigationState([work], "搜索结果", 0, None, 0, False)]
        frame.homepage_state = None
        frame.current_title = "剧"
        frame._opened_drama_id = 12
        frame.hide_list_detail_column = False
        frame._selected_index = Mock(return_value=0)
        frame._top_index = Mock(return_value=0)
        frame.set_items = Mock()

        frame._mark_drama_purchased_in_items(12)

        self.assertFalse(episode.need_pay)
        self.assertTrue(episode.raw["_purchased_full_drama"])
        self.assertIsNone(frame._drama_purchase_menu_label(work))
        frame.set_items.assert_called_once()

    def test_work_menu_follow_checks_status_before_mutation(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame._run_background = Mock()
        frame.SetStatusText = Mock()
        frame._opened_drama_id = None
        item = MediaItem(kind="drama", id=12, title="剧")

        frame._follow_drama_from_work_menu(item)
        _, query, done = frame._run_background.call_args.args
        frame.api.drama_follow_status.return_value = False
        done(query())
        frame.api.set_drama_follow_result.assert_not_called()

        _, change, finished = frame._run_background.call_args.args
        frame.api.set_drama_follow_result.return_value = DramaFollowResult(
            True, "喵！自己追的剧，跪着也要看完哦！",
        )
        with patch("app.wx.MessageBox") as notify:
            finished(change())
        frame.api.set_drama_follow_result.assert_called_once_with(12, follow=True)
        self.assertEqual(item.raw["like"], 1)
        self.assertEqual(notify.call_args.args[0], "喵！自己追的剧，跪着也要看完哦！")


class ItemContextMenuTests(unittest.TestCase):
    def test_sound_and_drama_have_distinct_detail_actions(self):
        app = wx.App(False)
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="")
        frame._selected_index = Mock(return_value=0)
        frame.show_sound_intro = Mock()
        frame.show_drama_detail = Mock()
        frame.list = Mock()
        labels = []

        def choose(menu, _position):
            entries = menu.GetMenuItems()
            labels.append([entry.GetItemLabelText() for entry in entries])
            detail = next(entry for entry in entries if entry.GetItemLabelText().startswith("查看"))
            return detail.GetId()

        frame.list.GetPopupMenuSelectionFromUser.side_effect = choose
        sound = MediaItem(kind="sound", id=34, title="第一集", drama_id=12)
        drama = MediaItem(kind="drama", id=12, title="测试剧")
        frame.items = [sound]
        frame._show_selected_item_menu(wx.Point(1, 1))
        frame.items = [drama]
        frame._show_selected_item_menu(wx.Point(1, 1))

        self.assertIn("查看音频简介", labels[0])
        self.assertNotIn("查看广播剧详情", labels[0])
        self.assertIn("查看广播剧详情", labels[1])
        frame.show_sound_intro.assert_called_once_with(sound)
        frame.show_drama_detail.assert_called_once_with(drama)
        app.Yield()

    def test_web_action_uses_selected_item_url(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        sound = MediaItem(kind="sound", id=34, title="第一集", drama_id=12)
        standalone = MediaItem(kind="sound", id=35, title="单独音频")
        drama = MediaItem(kind="drama", id=12, title="测试剧")

        with patch("app.wx.LaunchDefaultBrowser", return_value=True) as browser:
            frame.open_item_in_browser(sound)
            frame.open_item_in_browser(standalone)
            frame.open_item_in_browser(drama)

        self.assertEqual([call.args[0] for call in browser.call_args_list], [
            "https://www.missevan.com/sound/player?id=34",
            "https://www.missevan.com/sound/player?id=35",
            "https://www.missevan.com/mdrama/12",
        ])

    def test_sound_intro_action_uses_sound_id_and_audio_dialog_title(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock()
        frame.api.sound_intro_text.return_value = "单集简介"
        frame._run_background = Mock()
        frame._show_item_detail_dialog = Mock()
        item = MediaItem(kind="sound", id=34, title="第一集", drama_id=12)

        frame.show_sound_intro(item)
        _, work, done = frame._run_background.call_args.args
        done(work())

        frame.api.sound_intro_text.assert_called_once_with(34)
        frame.api.drama_detail_text.assert_not_called()
        frame._show_item_detail_dialog.assert_called_once_with(item, "单集简介", "音频简介")

        app = wx.App(False)
        dialog = MediaDetailDialog(None, item.title, "单集简介", "音频简介")
        try:
            self.assertEqual(dialog.GetTitle(), "音频简介 - 第一集")
        finally:
            dialog.Destroy()
            app.Yield()


class ItemShortcutTests(unittest.TestCase):
    def make_frame(self, item):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.list = Mock()
        frame.items = [item]
        frame._selected_index = Mock(return_value=0)
        frame.open_item = Mock()
        frame.show_sound_intro = Mock()
        frame.show_drama_detail = Mock()
        frame.show_comments = Mock()
        frame.open_item_in_browser = Mock()
        return frame

    def press(self, frame, modifiers, key=wx.WXK_RETURN):
        event = Mock()
        event.GetKeyCode.return_value = key
        event.GetModifiers.return_value = modifiers
        with patch.object(MaoerFrame, "FindFocus", return_value=frame.list):
            frame.on_char_hook(event)
        return event

    def test_shift_enter_shows_selected_item_detail(self):
        sound = MediaItem(kind="sound", id=34, title="第一集", drama_id=12)
        frame = self.make_frame(sound)
        event = self.press(frame, wx.MOD_SHIFT)
        event.Skip.assert_called_once_with()
        frame.show_sound_intro.assert_not_called()
        with patch.object(MaoerFrame, "FindFocus", return_value=frame.list):
            frame.on_item_detail_shortcut(None)
        frame.show_sound_intro.assert_called_once_with(sound)
        frame.open_item.assert_not_called()

        drama = MediaItem(kind="drama", id=12, title="测试剧")
        frame = self.make_frame(drama)
        event = self.press(frame, wx.MOD_SHIFT, key=wx.WXK_NUMPAD_ENTER)
        event.Skip.assert_called_once_with()
        with patch.object(MaoerFrame, "FindFocus", return_value=frame.list):
            frame.on_item_detail_shortcut(None)
        frame.show_drama_detail.assert_called_once_with(drama)
        frame.open_item.assert_not_called()

    def test_alt_enter_shows_sound_comments_only(self):
        sound = MediaItem(kind="sound", id=34, title="第一集")
        frame = self.make_frame(sound)
        event = self.press(frame, wx.MOD_ALT)
        event.Skip.assert_called_once_with()
        frame.show_comments.assert_not_called()
        with patch.object(MaoerFrame, "FindFocus", return_value=frame.list):
            frame.on_item_comments_shortcut(None)
        frame.show_comments.assert_called_once_with(sound)
        frame.open_item.assert_not_called()

        drama = MediaItem(kind="drama", id=12, title="测试剧")
        frame = self.make_frame(drama)
        event = self.press(frame, wx.MOD_ALT)
        event.Skip.assert_called_once_with()
        with patch.object(MaoerFrame, "FindFocus", return_value=frame.list):
            frame.on_item_comments_shortcut(None)
        frame.show_comments.assert_not_called()
        frame.open_item.assert_not_called()

    def test_alt_shift_enter_opens_selected_web_page(self):
        for item in (
            MediaItem(kind="sound", id=34, title="第一集", drama_id=12),
            MediaItem(kind="drama", id=12, title="测试剧"),
        ):
            with self.subTest(kind=item.kind):
                frame = self.make_frame(item)
                event = self.press(frame, wx.MOD_ALT | wx.MOD_SHIFT)
                event.Skip.assert_called_once_with()
                frame.open_item_in_browser.assert_not_called()
                with patch.object(MaoerFrame, "FindFocus", return_value=frame.list):
                    frame.on_item_browser_shortcut(None)
                frame.open_item_in_browser.assert_called_once_with(item)
                frame.open_item.assert_not_called()

    def test_accelerator_action_does_not_use_background_selection(self):
        item = MediaItem(kind="sound", id=34, title="第一集")
        frame = self.make_frame(item)
        with patch.object(MaoerFrame, "FindFocus", return_value=Mock()):
            frame.on_item_detail_shortcut(None)
            frame.on_item_comments_shortcut(None)
            frame.on_item_browser_shortcut(None)
        frame.show_sound_intro.assert_not_called()
        frame.show_comments.assert_not_called()
        frame.open_item_in_browser.assert_not_called()

    def test_unmodified_enter_keeps_opening_item(self):
        item = MediaItem(kind="sound", id=34, title="第一集")
        frame = self.make_frame(item)
        self.press(frame, 0)
        frame.open_item.assert_called_once_with(item)

    def test_hotkey_help_opens_bundled_current_hotkey_table(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        with patch("app.program_dir", return_value=Path("C:/old-help")), \
             patch("app.os.startfile") as open_file:
            frame.on_help_hotkeys(None)

        self.assertTrue(open_file.call_args.args[0].endswith("热键表.txt"))
        self.assertNotIn("old-help", open_file.call_args.args[0])


class VipCatalogMenuTests(unittest.TestCase):
    def make_frame(self):
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame._run_background = Mock()
        return frame

    def test_vip_catalog_reuses_drama_navigation_and_pagination(self):
        frame = self.make_frame()
        frame.items = []
        frame._navigation_state_snapshot = Mock(return_value="previous-state")
        frame._enter_items = Mock()
        items = [MediaItem(kind="drama", id=12, title="限免剧")]
        frame.api.vip_dramas.return_value = items

        frame.on_vip_free_dramas(None)
        _, work, done = frame._run_background.call_args.args
        done(work())

        frame.api.vip_dramas.assert_called_once_with("free", 1)
        args = frame._enter_items.call_args
        self.assertEqual(args.args, (items, "会员限免剧", "previous-state"))
        args.kwargs["page_state"].loader(2)
        frame.api.vip_dramas.assert_called_with("free", 2)

    def test_old_catalog_request_cannot_replace_new_catalog(self):
        frame = self.make_frame()
        frame.items = []
        frame._navigation_state_snapshot = Mock()
        frame._enter_items = Mock()
        frame.on_vip_free_dramas(None)
        old_done = frame._run_background.call_args.args[2]
        frame.on_vip_discount_dramas(None)
        old_done([])
        frame._enter_items.assert_not_called()


if __name__ == "__main__":
    unittest.main()
