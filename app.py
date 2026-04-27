from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
import re
import sys
import threading
import time
from typing import Callable

import requests
import wx

from app_paths import clear_webview2_profile
from browser_player import (
    PLAYBACK_MODE_DEFAULT,
    PLAYBACK_MODE_REPEAT_ONE,
    PLAYBACK_MODE_SEQUENCE,
    HiddenBrowserPlayer,
    PlayerUnavailable,
)
from login_dialog import LoginDialog
from maoer_api import (
    AccountInfo,
    BASE_URL,
    ApiError,
    COMMENT_SORT_HOTTEST,
    COMMENT_SORT_NEWEST,
    DrmUnsupported,
    MaoerApi,
    CommentItem,
    CommentPage,
    MediaItem,
    PlaybackInfo,
    PurchaseRequired,
)


IS_WINDOWS = sys.platform.startswith("win")
HOTKEY_ID_BASE = 0x5100
WIN_MOD_CONTROL = 0x0002
WIN_MOD_SHIFT = 0x0004
WIN_MOD_NOREPEAT = 0x4000
WIN_VK_LEFT = 0x25
WIN_VK_UP = 0x26
WIN_VK_RIGHT = 0x27
WIN_VK_DOWN = 0x28
WIN_VK_HOME = 0x24
WIN_VK_END = 0x23
WM_HOTKEY = 0x0312


def debug_log(message: str) -> None:
    if os.environ.get("MAOER_DEBUG"):
        print(f"[app] {message}", flush=True)


@dataclass
class PageState:
    page: int
    loader: Callable[[int], list[MediaItem]]
    has_more: bool = True
    loading: bool = False


@dataclass
class NavigationState:
    items: list[MediaItem]
    title: str
    selected_index: int
    page_state: PageState | None
    top_index: int


@dataclass
class CommentWindowState:
    mode: str
    parent_comment: CommentItem | None
    items: list[CommentItem]
    page: int
    max_page: int
    has_more: bool
    total: int
    sort_index: int
    selected_index: int
    top_index: int


class DramaDetailDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, title: str, content: str) -> None:
        super().__init__(parent, title=f"广播剧详情 - {title}", size=(660, 480))
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        content_label = wx.StaticText(panel, label="内容")
        self.content_box = wx.TextCtrl(
            panel,
            value=content,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.BORDER_SUNKEN,
        )
        self.content_box.SetName("内容")

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        close_button = wx.Button(panel, wx.ID_CLOSE, label="关闭")
        close_button.SetName("关闭")
        close_button.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CLOSE))
        button_row.AddStretchSpacer(1)
        button_row.Add(close_button, 0)

        root.Add(content_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        root.Add(self.content_box, 1, wx.EXPAND | wx.ALL, 10)
        root.Add(button_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(root)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    def _on_char_hook(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
            return
        event.Skip()


class AccountInfoDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, account: AccountInfo) -> None:
        super().__init__(parent, title="我的信息", size=(520, 420))
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        self.content_box = wx.TextCtrl(
            panel,
            value=account.text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.BORDER_SUNKEN,
        )
        self.content_box.SetName("我的信息")

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        close_button = wx.Button(panel, wx.ID_CLOSE, label="关闭")
        close_button.SetName("关闭")
        close_button.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CLOSE))
        button_row.AddStretchSpacer(1)
        button_row.Add(close_button, 0)

        root.Add(self.content_box, 1, wx.EXPAND | wx.ALL, 10)
        root.Add(button_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(root)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    def _on_char_hook(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
            return
        event.Skip()


class CommentsFrame(wx.Frame):
    SORT_OPTIONS = (
        ("最热", COMMENT_SORT_HOTTEST),
        ("最新", COMMENT_SORT_NEWEST),
    )
    PAGE_SIZE = 20

    def __init__(self, parent: wx.Window, api: MaoerApi, sound_id: int, title: str) -> None:
        super().__init__(parent, title=f"评论 - {title}", size=(820, 560))
        self.api = api
        self.sound_id = sound_id
        self.source_title = title
        self.mode = "comments"
        self.parent_comment: CommentItem | None = None
        self.items: list[CommentItem] = []
        self.page = 0
        self.max_page = 0
        self.has_more = True
        self.total = 0
        self.loading = False
        self.load_generation = 0
        self.back_stack: list[CommentWindowState] = []

        self._build_ui()
        self._bind_events()
        wx.CallAfter(self.comment_list.SetFocus)
        wx.CallAfter(self._load_first_page)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        sort_row = wx.BoxSizer(wx.HORIZONTAL)
        self.sort_label = wx.StaticText(panel, label="排序")
        self.sort_box = wx.ComboBox(
            panel,
            choices=[label for label, _order in self.SORT_OPTIONS],
            style=wx.CB_READONLY,
        )
        self.sort_box.SetSelection(0)
        self.sort_box.SetName("排序")
        sort_row.Add(self.sort_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        sort_row.Add(self.sort_box, 0)

        self.comment_label = wx.StaticText(panel, label="评论")
        self.comment_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.comment_list.SetName("评论")
        self.comment_list.InsertColumn(0, "评论")
        self.comment_list.InsertColumn(1, "回复")
        self.comment_list.InsertColumn(2, "赞")
        self.comment_list.InsertColumn(3, "时间")

        root.Add(sort_row, 0, wx.EXPAND | wx.ALL, 10)
        root.Add(self.comment_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(self.comment_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(root)
        self.CreateStatusBar()
        self._resize_columns()

    def _bind_events(self) -> None:
        self.sort_box.Bind(wx.EVT_COMBOBOX, self.on_sort_changed)
        self.comment_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_comment_activated)
        self.comment_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_comment_selected)
        self.comment_list.Bind(wx.EVT_KEY_DOWN, self.on_comment_key_down)
        self.comment_list.Bind(wx.EVT_MOUSEWHEEL, self.on_comment_mouse_wheel)
        self.comment_list.Bind(wx.EVT_SCROLLWIN, self.on_comment_scroll)
        self.comment_list.Bind(wx.EVT_SIZE, self.on_comment_list_size)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def _resize_columns(self) -> None:
        width = self.comment_list.GetClientSize().width
        if width <= 0:
            return
        reply_width = 88
        like_width = 64
        time_width = 132
        comment_width = max(220, width - reply_width - like_width - time_width - 28)
        self.comment_list.SetColumnWidth(0, comment_width)
        self.comment_list.SetColumnWidth(1, reply_width)
        self.comment_list.SetColumnWidth(2, like_width)
        self.comment_list.SetColumnWidth(3, time_width)

    def on_comment_list_size(self, event: wx.SizeEvent) -> None:
        self._resize_columns()
        wx.CallAfter(self._load_next_page_if_near_bottom)
        event.Skip()

    def on_comment_mouse_wheel(self, event: wx.MouseEvent) -> None:
        event.Skip()
        if event.GetWheelRotation() < 0:
            wx.CallAfter(self._load_next_page_if_near_bottom)

    def on_comment_scroll(self, event: wx.ScrollWinEvent) -> None:
        event.Skip()
        wx.CallAfter(self._load_next_page_if_near_bottom)

    def on_comment_selected(self, event: wx.ListEvent) -> None:
        if event.GetIndex() >= max(0, len(self.items) - 3):
            wx.CallAfter(self._load_next_page)
        event.Skip()

    def on_sort_changed(self, _event: wx.CommandEvent) -> None:
        self.back_stack.clear()
        self.mode = "comments"
        self.parent_comment = None
        self.sort_box.Enable(True)
        self._load_first_page()

    def on_comment_activated(self, _event: wx.ListEvent) -> None:
        self._open_selected_replies()

    def on_comment_key_down(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_RETURN:
            self._open_selected_replies()
            return
        if key == wx.WXK_BACK:
            self._go_back()
            return
        event.Skip()

    def on_char_hook(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_BACK and self.FindFocus() is not self.sort_box:
            self._go_back()
            return
        event.Skip()

    def on_close(self, event: wx.CloseEvent) -> None:
        self.load_generation += 1
        event.Skip()

    def _load_first_page(self) -> None:
        self.items = []
        self.page = 0
        self.max_page = 0
        self.has_more = True
        self.total = 0
        self._replace_list_items([])
        self._load_page(1, replace=True)

    def _load_next_page_if_near_bottom(self) -> None:
        if not self.items:
            return
        row_height = max(self.comment_list.GetCharHeight() + 8, 20)
        visible_rows = max(1, self.comment_list.GetClientSize().height // row_height)
        threshold = max(3, visible_rows // 3)
        try:
            top_item = self.comment_list.GetTopItem()
        except Exception:
            top_item = 0
        if top_item + visible_rows + threshold >= len(self.items):
            self._load_next_page()

    def _load_next_page(self) -> None:
        if self.loading or not self.has_more:
            return
        self._load_page(max(1, self.page + 1), replace=False)

    def _load_page(self, page: int, replace: bool) -> None:
        if self.loading:
            return
        self.loading = True
        self.load_generation += 1
        generation = self.load_generation
        mode = self.mode
        parent_comment = self.parent_comment
        order = self._selected_sort_order()
        title = self._view_title()
        self.SetStatusText(f"正在加载{title}...")

        def runner() -> None:
            try:
                if mode == "replies" and parent_comment is not None:
                    result = self.api.comment_replies(parent_comment.id, page=page, page_size=self.PAGE_SIZE)
                else:
                    result = self.api.sound_comments(
                        self.sound_id,
                        order=order,
                        page=page,
                        page_size=self.PAGE_SIZE,
                    )
            except (ApiError, requests.RequestException, ValueError) as exc:
                wx.CallAfter(self._load_failed, generation, str(exc))
            except Exception as exc:
                wx.CallAfter(self._load_failed, generation, f"{type(exc).__name__}: {exc}")
            else:
                wx.CallAfter(self._load_done, generation, result, replace)

        threading.Thread(target=runner, daemon=True).start()

    def _load_done(self, generation: int, page: CommentPage, replace: bool) -> None:
        if generation != self.load_generation:
            return
        self.loading = False
        self.page = page.page
        self.max_page = page.max_page
        self.has_more = page.has_more
        self.total = page.total

        if replace:
            self.items = page.comments
            self._replace_list_items(self.items)
        else:
            existing = {item.id for item in self.items}
            new_items = [item for item in page.comments if item.id not in existing]
            if new_items:
                self.items.extend(new_items)
                self._append_list_items(new_items)
            else:
                self.has_more = False

        if self.items and self._selected_index() == -1:
            self._select_comment_row(0)
        self.comment_list.SetFocus()
        count_text = f"{len(self.items)}/{self.total}" if self.total else str(len(self.items))
        suffix = "，可按回车查看楼中楼" if self.mode == "comments" else ""
        self.SetStatusText(f"{self._view_title()}，已加载 {count_text} 条{suffix}")
        wx.CallAfter(self._load_next_page_if_near_bottom)

    def _load_failed(self, generation: int, message: str) -> None:
        if generation != self.load_generation:
            return
        self.loading = False
        self.SetStatusText("评论加载失败")
        wx.MessageBox(message or "评论加载失败", "错误", wx.OK | wx.ICON_ERROR, self)

    def _replace_list_items(self, items: list[CommentItem]) -> None:
        self.comment_list.Freeze()
        try:
            self.comment_list.DeleteAllItems()
            for item in items:
                self._append_comment_row(self.comment_list.GetItemCount(), item)
            self._resize_columns()
        finally:
            self.comment_list.Thaw()

    def _append_list_items(self, items: list[CommentItem]) -> None:
        self.comment_list.Freeze()
        try:
            for item in items:
                self._append_comment_row(self.comment_list.GetItemCount(), item)
            self._resize_columns()
        finally:
            self.comment_list.Thaw()

    def _append_comment_row(self, index: int, item: CommentItem) -> None:
        self.comment_list.InsertItem(index, self._comment_summary(item))
        self.comment_list.SetItem(index, 1, f"{item.reply_count} 条" if item.reply_count else "")
        self.comment_list.SetItem(index, 2, str(item.like_count) if item.like_count else "")
        self.comment_list.SetItem(index, 3, item.created_at)

    def _comment_summary(self, item: CommentItem) -> str:
        content = re.sub(r"\s+", " ", item.content).strip()
        return f"{item.username}说：{content}"

    def _open_selected_replies(self) -> None:
        index = self._selected_index()
        if index < 0 or index >= len(self.items):
            return
        item = self.items[index]
        if item.reply_count <= 0:
            self.SetStatusText("这条评论没有楼中楼回复")
            return

        self.back_stack.append(self._snapshot())
        self.mode = "replies"
        self.parent_comment = item
        self.sort_box.Enable(False)
        self._load_first_page()

    def _go_back(self) -> None:
        if not self.back_stack:
            return
        state = self.back_stack.pop()
        self.mode = state.mode
        self.parent_comment = state.parent_comment
        self.items = state.items
        self.page = state.page
        self.max_page = state.max_page
        self.has_more = state.has_more
        self.total = state.total
        self.sort_box.SetSelection(state.sort_index)
        self.sort_box.Enable(self.mode == "comments")
        self._replace_list_items(self.items)
        if self.items:
            self._select_comment_row(max(0, min(state.selected_index, len(self.items) - 1)))
            wx.CallAfter(self._restore_top_item, state.top_index)
        self.comment_list.SetFocus()
        self.SetStatusText(f"已返回{self._view_title()}")

    def _snapshot(self) -> CommentWindowState:
        return CommentWindowState(
            mode=self.mode,
            parent_comment=self.parent_comment,
            items=self.items.copy(),
            page=self.page,
            max_page=self.max_page,
            has_more=self.has_more,
            total=self.total,
            sort_index=max(0, self.sort_box.GetSelection()),
            selected_index=self._selected_index(),
            top_index=self._top_index(),
        )

    def _restore_top_item(self, top_index: int) -> None:
        if not self.items:
            return
        top_index = max(0, min(top_index, len(self.items) - 1))
        if top_index:
            self.comment_list.EnsureVisible(len(self.items) - 1)
            self.comment_list.EnsureVisible(top_index)

    def _selected_sort_order(self) -> int:
        index = self.sort_box.GetSelection()
        if index < 0 or index >= len(self.SORT_OPTIONS):
            return COMMENT_SORT_HOTTEST
        return self.SORT_OPTIONS[index][1]

    def _view_title(self) -> str:
        if self.mode == "replies" and self.parent_comment is not None:
            return f"{self.parent_comment.username}的楼中楼"
        return "评论"

    def _selected_index(self) -> int:
        return self.comment_list.GetFirstSelected()

    def _top_index(self) -> int:
        try:
            return max(0, self.comment_list.GetTopItem())
        except Exception:
            return 0

    def _select_comment_row(self, index: int) -> None:
        if index < 0 or index >= len(self.items):
            return
        previous = self.comment_list.GetFirstSelected()
        if previous != -1 and previous != index:
            self.comment_list.SetItemState(previous, 0, wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED)
        state = wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED
        self.comment_list.SetItemState(index, state, state)
        self.comment_list.EnsureVisible(index)


class MaoerFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title="猫耳FM", size=(940, 620))
        self.api = MaoerApi()
        self.browser_player = HiddenBrowserPlayer(
            self,
            cookie=self.api.cookie_header,
            on_sound_changed=self.on_player_sound_changed,
            on_sequence_advance=self.on_player_sequence_advance,
        )
        self.active_player: HiddenBrowserPlayer | None = None
        self.items: list[MediaItem] = []
        self.current_title = ""
        self.page_state: PageState | None = None
        self.navigation_stack: list[NavigationState] = []
        self.homepage_state: NavigationState | None = None
        self.hotkey_handlers: dict[int, Callable[[], None]] = {}
        self.native_hotkey_ids: list[int] = []
        self.wx_hotkey_ids: list[int] = []
        self.comment_windows: list[CommentsFrame] = []
        self.last_mouse_context_menu_at = 0.0
        self.account_logged_in = bool(self.api.cookie_header)
        self.playback_mode = PLAYBACK_MODE_DEFAULT
        self.playback_generation = 0
        self.sequence_advance_pending = False
        self.sequence_page_advance_generation: int | None = None

        self._build_ui()
        self._build_menu()
        self._bind_events()
        self.browser_player.set_playback_mode(self.playback_mode)
        self._register_hotkeys()
        if self.api.cookie_header:
            self._refresh_account_title()
        wx.CallAfter(lambda: self.load_homepage(focus_list=True))

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        self.panel = panel
        root = wx.BoxSizer(wx.VERTICAL)

        self.list_label = wx.StaticText(panel, label="项目")
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        self.list.InsertColumn(0, "名称")
        self.list.InsertColumn(1, "作者")

        search_row = wx.BoxSizer(wx.HORIZONTAL)
        self.search_label = wx.StaticText(panel, label="搜索")
        self.search_box = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_button = wx.Button(panel, label="搜索")
        search_row.Add(self.search_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        search_row.Add(self.search_box, 1, wx.EXPAND | wx.RIGHT, 8)
        search_row.Add(self.search_button, 0)

        self.search_box.SetName("搜索")
        self.search_button.SetName("搜索按钮")
        self.list.SetName("项目")
        self.list.MoveBeforeInTabOrder(self.search_box)

        root.Add(search_row, 0, wx.EXPAND | wx.ALL, 10)
        root.Add(self.list_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(root)
        self.CreateStatusBar()

    def _build_menu(self) -> None:
        self.account_login_menu_id = wx.NewIdRef()
        self.account_info_menu_id = wx.NewIdRef()
        self.account_subscriptions_menu_id = wx.NewIdRef()
        self.account_purchased_dramas_menu_id = wx.NewIdRef()
        self.account_logout_menu_id = wx.NewIdRef()
        self.account_exit_menu_id = wx.NewIdRef()
        self.playback_mode_default_menu_id = wx.NewIdRef()
        self.playback_mode_repeat_one_menu_id = wx.NewIdRef()
        self.playback_mode_sequence_menu_id = wx.NewIdRef()
        self._update_account_menu()

    def _update_account_menu(self) -> None:
        menu_bar = wx.MenuBar()
        account_menu = wx.Menu()
        if self.account_logged_in:
            account_menu.Append(self.account_info_menu_id, "我的信息(&I)")
            account_menu.Append(self.account_subscriptions_menu_id, "剧集订阅(&S)")
            account_menu.Append(self.account_purchased_dramas_menu_id, "已购广播剧(&P)")
            account_menu.AppendSeparator()
            account_menu.Append(self.account_logout_menu_id, "退出登录(&O)")
        else:
            account_menu.Append(self.account_login_menu_id, "账号登录(&L)")
        account_menu.AppendSeparator()
        account_menu.Append(self.account_exit_menu_id, "退出程序(&Q)")
        menu_bar.Append(account_menu, "账号(&A)")

        settings_menu = wx.Menu()
        playback_menu = wx.Menu()
        default_item = playback_menu.AppendRadioItem(self.playback_mode_default_menu_id, "默认(&D)")
        repeat_one_item = playback_menu.AppendRadioItem(self.playback_mode_repeat_one_menu_id, "单集循环(&R)")
        sequence_item = playback_menu.AppendRadioItem(self.playback_mode_sequence_menu_id, "顺序播放(&S)")
        if self.playback_mode == PLAYBACK_MODE_REPEAT_ONE:
            repeat_one_item.Check(True)
        elif self.playback_mode == PLAYBACK_MODE_SEQUENCE:
            sequence_item.Check(True)
        else:
            default_item.Check(True)
        settings_menu.AppendSubMenu(playback_menu, "播放设置(&P)")
        menu_bar.Append(settings_menu, "设置(&S)")
        self.SetMenuBar(menu_bar)

    def _bind_events(self) -> None:
        self.search_button.Bind(wx.EVT_BUTTON, self.on_search)
        self.search_box.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_item_activated)
        self.list.Bind(wx.EVT_RIGHT_DOWN, self.on_list_right_down)
        self.list.Bind(wx.EVT_RIGHT_UP, self.on_list_right_up)
        self.list.Bind(wx.EVT_MOUSEWHEEL, self.on_list_mouse_wheel)
        self.list.Bind(wx.EVT_SCROLLWIN, self.on_list_scroll)
        self.list.Bind(wx.EVT_KEY_DOWN, self.on_list_key_down)
        self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_list_context_menu)
        self.list.Bind(wx.EVT_SIZE, self.on_list_size)
        self.panel.Bind(wx.EVT_CONTEXT_MENU, self.on_list_context_menu)
        self.Bind(wx.EVT_CONTEXT_MENU, self.on_list_context_menu)
        self.Bind(wx.EVT_MENU, self.on_account_login, id=self.account_login_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_info, id=self.account_info_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_subscriptions, id=self.account_subscriptions_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_purchased_dramas, id=self.account_purchased_dramas_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_logout, id=self.account_logout_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_exit, id=self.account_exit_menu_id)
        self.Bind(wx.EVT_MENU, self.on_playback_mode_changed, id=self.playback_mode_default_menu_id)
        self.Bind(wx.EVT_MENU, self.on_playback_mode_changed, id=self.playback_mode_repeat_one_menu_id)
        self.Bind(wx.EVT_MENU, self.on_playback_mode_changed, id=self.playback_mode_sequence_menu_id)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def _register_hotkeys(self) -> None:
        specs = [
            ("快进", wx.WXK_RIGHT, WIN_VK_RIGHT, lambda: self._seek(15), True),
            ("快退", wx.WXK_LEFT, WIN_VK_LEFT, lambda: self._seek(-15), True),
            ("音量加", wx.WXK_UP, WIN_VK_UP, self._volume_up, True),
            ("音量减", wx.WXK_DOWN, WIN_VK_DOWN, self._volume_down, True),
            ("暂停/继续", wx.WXK_HOME, WIN_VK_HOME, self._toggle_pause, False),
            ("停止", wx.WXK_END, WIN_VK_END, self._stop, False),
        ]
        modifiers = wx.MOD_CONTROL | wx.MOD_SHIFT
        failures: list[str] = []
        for offset, (label, wx_key_code, win_key_code, handler, repeatable) in enumerate(specs):
            numeric_id = HOTKEY_ID_BASE + offset
            registered = False
            if self._register_native_hotkey(numeric_id, win_key_code, repeatable):
                self.native_hotkey_ids.append(numeric_id)
                registered = True
            elif self.RegisterHotKey(numeric_id, modifiers, wx_key_code):
                self.wx_hotkey_ids.append(numeric_id)
                registered = True

            if registered:
                self.hotkey_handlers[numeric_id] = handler
                self.Bind(wx.EVT_HOTKEY, self.on_hotkey, id=numeric_id)
                debug_log(f"hotkey registered label={label} id={numeric_id} native={numeric_id in self.native_hotkey_ids}")
            else:
                failures.append(label)
                debug_log(f"hotkey register failed label={label} id={numeric_id}")

        if failures:
            wx.CallAfter(self.SetStatusText, "以下全局快捷键注册失败: " + "、".join(failures))

    def _register_native_hotkey(self, hotkey_id: int, virtual_key: int, repeatable: bool) -> bool:
        if not IS_WINDOWS:
            return False
        modifiers = WIN_MOD_CONTROL | WIN_MOD_SHIFT
        if not repeatable:
            modifiers |= WIN_MOD_NOREPEAT
        hwnd = ctypes.c_void_p(self.GetHandle())
        return bool(
            ctypes.windll.user32.RegisterHotKey(
                hwnd,
                ctypes.c_int(hotkey_id),
                ctypes.c_uint(modifiers),
                ctypes.c_uint(virtual_key),
            )
        )

    def MSWWindowProc(self, message: int, w_param: int, l_param: int) -> int:
        if message == WM_HOTKEY:
            debug_log(f"WM_HOTKEY id={int(w_param)} l_param={int(l_param)}")
            handler = self.hotkey_handlers.get(int(w_param))
            if handler:
                wx.CallAfter(handler)
                return 0
        return super().MSWWindowProc(message, w_param, l_param)

    def _unregister_hotkeys(self) -> None:
        if IS_WINDOWS:
            hwnd = ctypes.c_void_p(self.GetHandle())
            for hotkey_id in self.native_hotkey_ids:
                ctypes.windll.user32.UnregisterHotKey(hwnd, ctypes.c_int(hotkey_id))
        self.native_hotkey_ids.clear()

        for hotkey_id in self.wx_hotkey_ids:
            self.UnregisterHotKey(hotkey_id)
        self.wx_hotkey_ids.clear()

    def load_homepage(self, focus_list: bool = False) -> None:
        self._run_background(
            "正在加载首页...",
            self.api.homepage,
            lambda items: self._set_root_items(items, "首页", focus_list=focus_list),
        )

    def on_search(self, event: wx.Event) -> None:
        keyword = self.search_box.GetValue().strip()
        focus_list = event.GetEventObject() is self.search_box
        if not keyword:
            self.load_homepage(focus_list=focus_list)
            return
        self._run_background(
            f"正在搜索: {keyword}",
            lambda: self.api.search(keyword),
            lambda items: self._set_root_items(
                items,
                f"搜索: {keyword}",
                focus_list=focus_list,
                page_state=PageState(1, lambda page: self.api.search(keyword, page)),
            ),
        )

    def on_account_login(self, _event: wx.Event) -> None:
        dialog = LoginDialog(self, self.api)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                cookie = dialog.cookie_header or self.api.cookie_header
                self.api.set_cookie(cookie)
                self.browser_player.cookie = cookie
                self.browser_player.shutdown()
                self.account_logged_in = True
                self._update_account_menu()
                self.SetStatusText("账号登录成功")
                self._refresh_account_title()
        finally:
            dialog.Destroy()

    def on_account_info(self, _event: wx.Event) -> None:
        self._run_background(
            "正在加载我的信息...",
            self.api.account_info,
            lambda account: self._show_account_info_dialog(account),
        )

    def _show_account_info_dialog(self, account: AccountInfo) -> None:
        dialog = AccountInfoDialog(self, account)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _account_list_previous_state(self) -> NavigationState:
        if self.current_title == "首页":
            return self._navigation_state_snapshot()
        elif self.homepage_state is not None:
            return self.homepage_state
        return self._navigation_state_snapshot()

    def on_account_subscriptions(self, _event: wx.Event) -> None:
        previous_state = self._account_list_previous_state()
        self._run_background(
            "正在加载剧集订阅...",
            lambda: self.api.subscribed_dramas(1),
            lambda items: self._enter_items(
                items,
                "剧集订阅",
                previous_state,
                focus_list=True,
                page_state=PageState(1, lambda page: self.api.subscribed_dramas(page)),
            ),
        )

    def on_account_purchased_dramas(self, _event: wx.Event) -> None:
        previous_state = self._account_list_previous_state()
        self._run_background(
            "正在加载已购广播剧...",
            lambda: self.api.purchased_dramas(1),
            lambda items: self._enter_items(
                items,
                "已购广播剧",
                previous_state,
                focus_list=True,
                page_state=PageState(1, lambda page: self.api.purchased_dramas(page)),
            ),
        )

    def on_account_logout(self, _event: wx.Event) -> None:
        self.api.set_cookie("")
        self.api.clear_saved_cookie()
        self.browser_player.cookie = ""
        self.browser_player.shutdown()
        self.account_logged_in = False
        self._update_account_menu()
        self.SetTitle("猫耳FM")
        self.SetStatusText("已退出登录")
        if self.current_title in {"剧集订阅", "已购广播剧"}:
            self.load_homepage(focus_list=True)

    def on_playback_mode_changed(self, event: wx.CommandEvent) -> None:
        mode_by_id = {
            int(self.playback_mode_default_menu_id): PLAYBACK_MODE_DEFAULT,
            int(self.playback_mode_repeat_one_menu_id): PLAYBACK_MODE_REPEAT_ONE,
            int(self.playback_mode_sequence_menu_id): PLAYBACK_MODE_SEQUENCE,
        }
        label_by_mode = {
            PLAYBACK_MODE_DEFAULT: "默认",
            PLAYBACK_MODE_REPEAT_ONE: "单集循环",
            PLAYBACK_MODE_SEQUENCE: "顺序播放",
        }
        self.playback_mode = mode_by_id.get(event.GetId(), PLAYBACK_MODE_DEFAULT)
        self.browser_player.set_playback_mode(self.playback_mode)
        if self.playback_mode != PLAYBACK_MODE_SEQUENCE:
            self.sequence_advance_pending = False
            self.sequence_page_advance_generation = None
        self.SetStatusText(f"播放模式: {label_by_mode[self.playback_mode]}")

    def _refresh_account_title(self) -> None:
        def runner() -> None:
            try:
                account = self.api.account_info()
            except ApiError as exc:
                debug_log(f"account title refresh failed: {exc}")
                wx.CallAfter(self._mark_account_logged_out, "登录状态失效，请重新登录")
                return
            except (requests.RequestException, ValueError) as exc:
                debug_log(f"account title refresh failed: {exc}")
                return
            except Exception as exc:
                debug_log(f"account title refresh failed: {type(exc).__name__}: {exc}")
                return
            wx.CallAfter(self._mark_account_logged_in, account.nickname)

        threading.Thread(target=runner, daemon=True).start()

    def _mark_account_logged_in(self, nickname: str) -> None:
        if not self.account_logged_in:
            self.account_logged_in = True
            self._update_account_menu()
        self.SetTitle(f"猫耳FM - 登录账号：{nickname}")

    def _mark_account_logged_out(self, status: str = "") -> None:
        self.api.set_cookie("")
        self.browser_player.cookie = ""
        if self.account_logged_in:
            self.account_logged_in = False
            self._update_account_menu()
        self.SetTitle("猫耳FM")
        if status:
            self.SetStatusText(status)

    def on_list_size(self, event: wx.SizeEvent) -> None:
        self._resize_list_columns()
        wx.CallAfter(self._load_next_page_if_near_bottom)
        event.Skip()

    def on_list_right_down(self, event: wx.MouseEvent) -> None:
        index = self._hit_test_list_index(event.GetPosition())
        if index != -1:
            self._select_list_row(index)
        event.Skip()

    def on_list_right_up(self, event: wx.MouseEvent) -> None:
        index = self._hit_test_list_index(event.GetPosition())
        if index != -1:
            self._select_list_row(index)
        self.last_mouse_context_menu_at = time.monotonic()
        self._show_selected_item_menu(event.GetPosition())
        self.last_mouse_context_menu_at = time.monotonic()

    def on_list_mouse_wheel(self, event: wx.MouseEvent) -> None:
        event.Skip()
        if event.GetWheelRotation() < 0:
            wx.CallAfter(self._load_next_page_if_near_bottom)

    def on_list_scroll(self, event: wx.ScrollWinEvent) -> None:
        event.Skip()
        wx.CallAfter(self._load_next_page_if_near_bottom)

    def on_list_key_down(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_UP:
            self._move_list_selection(-1)
            return
        if key == wx.WXK_DOWN:
            self._move_list_selection(1)
            return
        event.Skip()

    def on_list_context_menu(self, event: wx.ContextMenuEvent) -> None:
        if time.monotonic() - self.last_mouse_context_menu_at < 0.35:
            return
        position = self._list_menu_position_from_context_event(event)
        if position is None:
            event.Skip()
            return
        self._show_selected_item_menu(position)

    def _list_menu_position_from_context_event(self, event: wx.ContextMenuEvent) -> wx.Point | None:
        screen_position = event.GetPosition()
        if screen_position == wx.DefaultPosition:
            return self._default_list_menu_position()

        list_position = self.list.ScreenToClient(screen_position)
        if not self.list.GetClientRect().Contains(list_position):
            return None
        index = self._hit_test_list_index(list_position)
        if index != -1:
            self._select_list_row(index)
        return list_position

    def _default_list_menu_position(self) -> wx.Point:
        size = self.list.GetClientSize()
        selection = self._selected_index()
        y = 10
        if selection >= 0:
            try:
                rect = self.list.GetItemRect(selection)
                y = rect.y + max(1, rect.height // 2)
            except Exception:
                row_height = max(self.list.GetCharHeight() + 8, 20)
                y = 24 + max(0, selection - self.list.GetTopItem()) * row_height
        max_y = max(10, size.height - 10)
        return wx.Point(min(20, max(1, size.width - 10)), min(max(10, y), max_y))

    def _hit_test_list_index(self, position: wx.Point) -> int:
        hit = self.list.HitTest(position)
        index = hit[0] if isinstance(hit, tuple) else hit
        return index if index != wx.NOT_FOUND else -1

    def _select_list_row(self, index: int) -> None:
        self._select_list_row_at(index, ensure_visible=True)

    def _select_list_row_at(self, index: int, ensure_visible: bool) -> None:
        if index < 0 or index >= len(self.items):
            return

        previous = self.list.GetFirstSelected()
        if previous != -1 and previous != index:
            self.list.SetItemState(previous, 0, wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED)
        state = wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED
        self.list.SetItemState(index, state, state)
        if ensure_visible:
            self.list.EnsureVisible(index)

    def on_player_sound_changed(self, sound_id: int) -> None:
        if self.playback_mode != PLAYBACK_MODE_SEQUENCE:
            return
        for index, item in enumerate(self.items):
            if item.kind == "sound" and item.id == sound_id:
                self._select_list_row(index)
                self.SetStatusText(f"正在后台播放: {item.title}")
                return

    def on_player_sequence_advance(self) -> None:
        if self.playback_mode != PLAYBACK_MODE_SEQUENCE:
            return
        self._play_next_in_sequence(self.playback_generation)

    def _play_next_in_sequence(self, generation: int) -> None:
        if generation != self.playback_generation or self.playback_mode != PLAYBACK_MODE_SEQUENCE:
            return
        if self.sequence_advance_pending:
            return
        if self.active_player is not None and self.active_player.is_paused():
            wx.CallLater(1000, self._play_next_in_sequence, generation)
            return
        current_index = self._selected_index()
        index = self._next_playable_index(current_index)
        if index is None:
            if self.page_state is not None and self.page_state.has_more:
                self.sequence_advance_pending = True
                self.sequence_page_advance_generation = generation
            self._load_next_page()
            return
        self.sequence_advance_pending = True
        self._select_list_row(index)
        self.list.SetFocus()
        self.playback_generation += 1
        self._play_sequence_item(index, self.playback_generation)

    def _next_playable_index(self, current_index: int) -> int | None:
        start = max(current_index + 1, 0)
        for index in range(start, len(self.items)):
            item = self.items[index]
            if item.kind == "sound" and not item.need_pay:
                return index
        return None

    def _index_for_item(self, target: MediaItem) -> int | None:
        for index, item in enumerate(self.items):
            if item.kind == target.kind and item.id == target.id:
                return index
        return None

    def _play_sequence_item(self, index: int, generation: int) -> None:
        if index < 0 or index >= len(self.items):
            self.sequence_advance_pending = False
            return
        item = self.items[index]
        if item.kind != "sound" or item.need_pay:
            self.sequence_advance_pending = False
            return

        def done(playback: PlaybackInfo) -> None:
            if generation != self.playback_generation:
                return
            self._play(playback, item, index)

        def failed(exc: Exception) -> None:
            self.sequence_advance_pending = False
            self.show_error(str(exc) or type(exc).__name__)

        self._run_background_with_error(
            f"正在获取播放地址: {item.title}",
            lambda: self.api.playback_info(item),
            done,
            failed,
        )

    def _resize_list_columns(self) -> None:
        width = self.list.GetClientSize().width
        if width <= 0:
            return

        author_width = max(160, min(260, width // 3))
        name_width = max(120, width - author_width - 24)
        self.list.SetColumnWidth(0, name_width)
        self.list.SetColumnWidth(1, max(120, width - name_width - 24))

    def _append_list_item(self, index: int, item: MediaItem) -> None:
        self.list.InsertItem(index, item.title)
        self.list.SetItem(index, 1, self._item_author(item))

    def _item_author(self, item: MediaItem) -> str:
        raw = item.raw
        if isinstance(raw, dict):
            author = self._raw_text_value(
                raw,
                ("author", "author_name", "username", "user_name", "uname", "nickname", "nick_name"),
            )
            if author:
                return author

            for key in ("user", "member", "owner", "creator", "profile"):
                nested = raw.get(key)
                if isinstance(nested, dict):
                    author = self._raw_text_value(
                        nested,
                        ("name", "username", "user_name", "uname", "nickname", "nick_name"),
                    )
                    if author:
                        return author

        return item.subtitle

    @staticmethod
    def _raw_text_value(raw: dict[str, object], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = raw.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    def _show_selected_item_menu(self, position: wx.Point) -> None:
        index = self._selected_index()
        if index < 0 or index >= len(self.items):
            return

        item = self.items[index]
        can_show_drama_menu = self._can_show_drama_menu(item)
        can_show_comments_menu = self._can_show_comments_menu(item)
        if not can_show_drama_menu and not can_show_comments_menu:
            return

        menu = wx.Menu()
        open_id = wx.NewIdRef()
        detail_id = wx.NewIdRef()
        comments_id = wx.NewIdRef()
        if can_show_drama_menu:
            menu.Append(open_id, "用网页打开")
            menu.Append(detail_id, "查看广播剧详情")
        if can_show_comments_menu:
            if can_show_drama_menu:
                menu.AppendSeparator()
            menu.Append(comments_id, "查看评论")

        try:
            choice = self.list.GetPopupMenuSelectionFromUser(menu, position)
        finally:
            menu.Destroy()

        if can_show_drama_menu and choice == int(open_id):
            self.open_drama_in_browser(item)
        elif can_show_drama_menu and choice == int(detail_id):
            self.show_drama_detail(item)
        elif can_show_comments_menu and choice == int(comments_id):
            self.show_comments(item)

    def _can_show_drama_menu(self, item: MediaItem) -> bool:
        return item.kind in {"drama", "sound"} or item.drama_id is not None

    def _can_show_comments_menu(self, item: MediaItem) -> bool:
        return item.kind == "sound"

    def open_drama_in_browser(self, item: MediaItem) -> None:
        self._run_background(
            f"正在打开广播剧: {item.title}",
            lambda: self.api.item_drama_id(item),
            lambda drama_id: self._launch_drama_url(item, int(drama_id)),
        )

    def _launch_drama_url(self, item: MediaItem, drama_id: int) -> None:
        url = f"{BASE_URL}/mdrama/{drama_id}"
        if item.kind == "drama" and item.pay_type is not None:
            url += f"?pay_type={item.pay_type}"
        if not wx.LaunchDefaultBrowser(url):
            self.show_error("无法打开浏览器")

    def show_drama_detail(self, item: MediaItem) -> None:
        self._run_background(
            f"正在加载广播剧详情: {item.title}",
            lambda: self.api.drama_detail_text(self.api.item_drama_id(item)),
            lambda content: self._show_drama_detail_dialog(item, str(content)),
        )

    def _show_drama_detail_dialog(self, item: MediaItem, content: str) -> None:
        dialog = DramaDetailDialog(self, item.title, content)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def show_comments(self, item: MediaItem) -> None:
        if item.kind != "sound":
            return
        frame = CommentsFrame(self, self.api, item.id, item.title)
        self.comment_windows.append(frame)
        frame.Bind(wx.EVT_CLOSE, lambda event, frame=frame: self._on_comment_window_close(frame, event))
        frame.Show()

    def _on_comment_window_close(self, frame: CommentsFrame, event: wx.CloseEvent) -> None:
        try:
            self.comment_windows.remove(frame)
        except ValueError:
            pass
        event.Skip()

    def on_item_activated(self, event: wx.Event) -> None:
        index = event.GetIndex() if hasattr(event, "GetIndex") else self._selected_index()
        if index < 0 or index >= len(self.items):
            return
        self.open_item(self.items[index])

    def open_item(self, item: MediaItem) -> None:
        if item.is_collection:
            previous_state = self._navigation_state_snapshot()
            if item.kind == "drama":
                self._run_background(
                    f"正在加载: {item.title}",
                    lambda: self.api.drama_episodes_page(item.id, 1),
                    lambda items: self._enter_items(
                        items,
                        item.title,
                        previous_state,
                        page_state=PageState(1, lambda page: self.api.drama_episodes_page(item.id, page)),
                    ),
                )
                return

            self._run_background(
                f"正在加载: {item.title}",
                lambda: self.api.collection_items(item),
                lambda items: self._enter_items(items, item.title, previous_state),
            )
            return

        if item.need_pay:
            self.show_purchase_required(item.title)
            return

        self._run_background(
            f"正在获取播放地址: {item.title}",
            lambda: self.api.playback_info(item),
            lambda playback, item=item, index=self._index_for_item(item): self._play(playback, item, index),
        )

    def _set_root_items(
        self,
        items: list[MediaItem],
        title: str,
        focus_list: bool = False,
        page_state: PageState | None = None,
    ) -> None:
        if self.current_title == "首页" and title != "首页":
            self.homepage_state = self._navigation_state_snapshot()
        self.navigation_stack.clear()
        self.page_state = page_state
        self.set_items(items, title, focus_list=focus_list)

    def _navigation_state_snapshot(self) -> NavigationState:
        state = NavigationState(
            items=self.items.copy(),
            title=self.current_title,
            selected_index=self._selected_index(),
            page_state=self.page_state,
            top_index=self._top_index(),
        )
        if self.current_title == "首页":
            self.homepage_state = state
        return state

    def _enter_items(
        self,
        items: list[MediaItem],
        title: str,
        previous_state: NavigationState,
        focus_list: bool = False,
        page_state: PageState | None = None,
    ) -> None:
        self.navigation_stack.append(previous_state)
        self.page_state = page_state
        self.set_items(items, title, focus_list=focus_list)

    def set_items(
        self,
        items: list[MediaItem],
        title: str,
        selected_index: int = 0,
        focus_list: bool = False,
        top_index: int | None = None,
    ) -> None:
        self.current_title = title
        self.items = items
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            for index, item in enumerate(items):
                self._append_list_item(index, item)
            self._resize_list_columns()
        finally:
            self.list.Thaw()

        if items:
            selected_index = max(0, min(selected_index, len(items) - 1))
            self._select_list_row(selected_index)
            if top_index is not None:
                wx.CallAfter(self._restore_list_position, selected_index, top_index)
        if focus_list:
            wx.CallAfter(self._focus_list)
        self.SetStatusText(f"{title}，共 {len(items)} 项")

    def _top_index(self) -> int:
        try:
            return max(0, self.list.GetTopItem())
        except Exception:
            return 0

    def _restore_list_position(self, selected_index: int, top_index: int) -> None:
        if not self.items:
            return
        selected_index = max(0, min(selected_index, len(self.items) - 1))
        top_index = max(0, min(top_index, len(self.items) - 1))
        if top_index:
            self.list.EnsureVisible(len(self.items) - 1)
            self.list.EnsureVisible(top_index)
        self._select_list_row_at(selected_index, ensure_visible=False)

    def _focus_list(self) -> None:
        if self.items and self._selected_index() == -1:
            self._select_list_row(0)
        self.list.SetFocus()

    def _move_list_selection(self, delta: int) -> None:
        if not self.items:
            return

        index = self._selected_index()
        if index == -1:
            index = 0 if delta >= 0 else len(self.items) - 1
        else:
            index = max(0, min(len(self.items) - 1, index + delta))
        self._select_list_row(index)

        if delta > 0 and index >= len(self.items) - 1:
            self._load_next_page()

    def _load_next_page(self) -> None:
        state = self.page_state
        if state is None or state.loading or not state.has_more:
            return

        state.loading = True
        next_page = state.page + 1

        def work() -> list[MediaItem]:
            try:
                return state.loader(next_page)
            finally:
                state.loading = False

        self._run_background(
            f"正在加载下一页: {self.current_title}",
            work,
            lambda items: self._append_next_page(state, next_page, items),
        )

    def _load_next_page_if_near_bottom(self) -> None:
        if not self.items:
            return
        row_height = max(self.list.GetCharHeight() + 8, 20)
        visible_rows = max(1, self.list.GetClientSize().height // row_height)
        threshold = max(3, visible_rows // 3)
        if self.list.GetTopItem() + visible_rows + threshold >= len(self.items):
            self._load_next_page()

    def _append_next_page(self, state: PageState, page: int, items: list[MediaItem]) -> None:
        if self.page_state is not state:
            return

        existing = {(item.kind, item.id) for item in self.items}
        new_items = [item for item in items if (item.kind, item.id) not in existing]
        if not new_items:
            state.has_more = False
            self._clear_sequence_page_advance()
            self.SetStatusText("已经到最后一页")
            return

        state.page = page
        previous_selection = self._selected_index()
        self.items.extend(new_items)
        self.list.Freeze()
        try:
            for item in new_items:
                self._append_list_item(self.list.GetItemCount(), item)
            self._resize_list_columns()
        finally:
            self.list.Thaw()
        if previous_selection != -1:
            self._select_list_row(previous_selection)
        self.SetStatusText(f"{self.current_title}，共 {len(self.items)} 项")

        self._continue_sequence_after_page_load()

    def _continue_sequence_after_page_load(self) -> None:
        generation = self.sequence_page_advance_generation
        if generation is None:
            return

        self.sequence_page_advance_generation = None
        if generation != self.playback_generation or self.playback_mode != PLAYBACK_MODE_SEQUENCE:
            self.sequence_advance_pending = False
            return

        self.sequence_advance_pending = False
        self._play_next_in_sequence(generation)

    def _clear_sequence_page_advance(self) -> None:
        if self.sequence_page_advance_generation is not None:
            self.sequence_page_advance_generation = None
            self.sequence_advance_pending = False

    def _play(self, playback: PlaybackInfo, item: MediaItem | None = None, index: int | None = None) -> None:
        try:
            self.browser_player.cookie = self.api.cookie_header
            self.browser_player.set_playback_mode(self.playback_mode)
            self.browser_player.play(playback)
            self.active_player = self.browser_player
        except PlayerUnavailable as exc:
            self.show_error(str(exc))
            return

        self.playback_generation += 1
        self.sequence_advance_pending = False
        self.sequence_page_advance_generation = None
        self.SetStatusText(f"正在后台播放: {playback.title}")
        threading.Thread(target=self.api.add_play_times, args=(playback,), daemon=True).start()

    def _run_background(
        self,
        status: str,
        work: Callable[[], object],
        done: Callable[[object], None],
    ) -> None:
        self.SetStatusText(status)
        self.search_button.Enable(False)

        def runner() -> None:
            try:
                result = work()
            except PurchaseRequired as exc:
                wx.CallAfter(self.show_purchase_required, str(exc))
            except DrmUnsupported as exc:
                wx.CallAfter(self.show_error, str(exc))
            except ApiError as exc:
                if str(exc) == "需要登录":
                    wx.CallAfter(self._mark_account_logged_out, "需要登录")
                wx.CallAfter(self.show_error, str(exc))
            except (requests.RequestException, ValueError) as exc:
                wx.CallAfter(self.show_error, str(exc))
            except Exception as exc:
                wx.CallAfter(self.show_error, f"{type(exc).__name__}: {exc}")
            else:
                wx.CallAfter(done, result)
            finally:
                wx.CallAfter(self.search_button.Enable, True)

        threading.Thread(target=runner, daemon=True).start()

    def _run_background_with_error(
        self,
        status: str,
        work: Callable[[], object],
        done: Callable[[object], None],
        failed: Callable[[Exception], None],
    ) -> None:
        self.SetStatusText(status)
        self.search_button.Enable(False)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                wx.CallAfter(failed, exc)
            else:
                wx.CallAfter(done, result)
            finally:
                wx.CallAfter(self.search_button.Enable, True)

        threading.Thread(target=runner, daemon=True).start()

    def on_hotkey(self, event: wx.KeyEvent) -> None:
        debug_log(f"EVT_HOTKEY id={event.GetId()}")
        handler = self.hotkey_handlers.get(event.GetId())
        if handler:
            handler()

    def on_account_exit(self, _event: wx.CommandEvent) -> None:
        self.Close()

    def on_char_hook(self, event: wx.KeyEvent) -> None:
        if event.ControlDown() and event.ShiftDown():
            key = event.GetKeyCode()
            debug_log(f"EVT_CHAR_HOOK ctrl+shift key={key}")
            fallback = {
                wx.WXK_RIGHT: lambda: self._seek(15),
                wx.WXK_LEFT: lambda: self._seek(-15),
                wx.WXK_UP: self._volume_up,
                wx.WXK_DOWN: self._volume_down,
                wx.WXK_HOME: self._toggle_pause,
                wx.WXK_END: self._stop,
            }.get(key)
            if fallback:
                fallback()
                return

        if self.FindFocus() is self.list:
            key = event.GetKeyCode()
            if key == wx.WXK_MENU or (key == wx.WXK_F10 and event.ShiftDown()):
                self._show_selected_item_menu(wx.Point(10, 10))
                return

        if event.GetKeyCode() == wx.WXK_BACK and self.FindFocus() is not self.search_box:
            if self.current_title.startswith("搜索"):
                self.load_homepage(focus_list=self.FindFocus() is self.list)
                return
            if self.go_back():
                return

        if event.GetKeyCode() == wx.WXK_RETURN and self.FindFocus() is self.list:
            index = self._selected_index()
            if index != -1 and index < len(self.items):
                self.open_item(self.items[index])
                return

        event.Skip()

    def go_back(self) -> bool:
        if not self.navigation_stack:
            return False
        state = self.navigation_stack.pop()
        self.page_state = state.page_state
        self.set_items(state.items, state.title, state.selected_index, top_index=state.top_index)
        return True

    def _selected_index(self) -> int:
        return self.list.GetFirstSelected()

    def _seek(self, seconds: int) -> None:
        try:
            player = self._current_player()
            if player is None:
                self.SetStatusText("没有正在播放的内容")
                return
            player.seek(seconds)
            self.SetStatusText("快进 15 秒" if seconds > 0 else "快退 15 秒")
        except PlayerUnavailable as exc:
            self.show_error(str(exc))

    def _volume_up(self) -> None:
        try:
            player = self._current_player()
            debug_log(f"volume_up active_player={type(player).__name__ if player else None}")
            if player is None:
                self.SetStatusText("没有正在播放的内容")
                return
            volume = player.volume_up()
            debug_log(f"volume_up result volume={volume}")
            self.SetStatusText(f"音量: {volume}")
        except PlayerUnavailable as exc:
            self.show_error(str(exc))

    def _volume_down(self) -> None:
        try:
            player = self._current_player()
            debug_log(f"volume_down active_player={type(player).__name__ if player else None}")
            if player is None:
                self.SetStatusText("没有正在播放的内容")
                return
            volume = player.volume_down()
            debug_log(f"volume_down result volume={volume}")
            self.SetStatusText(f"音量: {volume}")
        except PlayerUnavailable as exc:
            self.show_error(str(exc))

    def _toggle_pause(self) -> None:
        try:
            player = self._current_player()
            if player is None:
                self.SetStatusText("没有正在播放的内容")
                return
            paused = player.toggle_pause()
            self.SetStatusText("已暂停" if paused else "继续播放")
        except PlayerUnavailable as exc:
            self.show_error(str(exc))

    def _stop(self) -> None:
        self.playback_generation += 1
        self.sequence_advance_pending = False
        self.sequence_page_advance_generation = None
        if self.active_player is not None:
            self.active_player.stop()
        self.active_player = None
        self.SetStatusText("已停止播放")

    def _current_player(self) -> HiddenBrowserPlayer | None:
        return self.active_player

    def show_purchase_required(self, message: str) -> None:
        if not message.startswith("《"):
            message = f"《{message}》需要购买后才能播放。"
        wx.MessageBox(message, "需要购买", wx.OK | wx.ICON_INFORMATION, self)

    def show_error(self, message: str) -> None:
        self.SetStatusText("操作失败")
        wx.MessageBox(message or "未知错误", "错误", wx.OK | wx.ICON_ERROR, self)

    def on_close(self, event: wx.CloseEvent) -> None:
        self._unregister_hotkeys()
        self.browser_player.shutdown()
        clear_webview2_profile()
        event.Skip()

    @staticmethod
    def _format_duration(duration_ms: int | None) -> str:
        if not duration_ms:
            return ""
        seconds = int(duration_ms) // 1000
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


class MaoerApp(wx.App):
    def OnInit(self) -> bool:
        frame = MaoerFrame()
        frame.Show()
        return True


if __name__ == "__main__":
    app = MaoerApp(False)
    app.MainLoop()
