import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import wx

from app import MaoerFrame, MediaDetailDialog, NavigationState
from app_settings import AppSettings, load_settings
from maoer_api import DramaFollowResult, DramaPurchaseInfo, MediaItem


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

    def test_changed_reading_default_also_updates_open_player(self):
        player_frame = Mock(read_subtitle_enabled=False, read_danmaku_enabled=False)
        self.frame.player_frame = player_frame
        self.change(self.frame.settings_subtitle_menu_id, True)
        self.change(self.frame.settings_danmaku_menu_id, True)
        self.assertTrue(player_frame.read_subtitle_enabled)
        self.assertTrue(player_frame.read_danmaku_enabled)

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

    def test_member_benefit_and_schedule_columns_remain_special(self):
        frame = self.make_frame()
        frame.current_title = "会员限免剧"
        item = MediaItem(kind="drama", id=12, title="剧", subtitle="会员限免（会员畅听）",
                         raw={"_hide_author": True})
        self.assertEqual(frame._item_publisher(item), "会员限免（会员畅听）")
        frame._update_list_column_headers("会员限免剧")
        frame._set_list_column_header.assert_any_call(1, "会员权益")

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
    def test_context_menu_routes_purchase_from_drama_not_episode(self):
        app = wx.App(False)
        frame = MaoerFrame.__new__(MaoerFrame)
        frame.api = Mock(cookie_header="test-cookie")
        frame._selected_index = Mock(return_value=0)
        frame._prompt_drama_purchase = Mock()
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
