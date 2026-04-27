from __future__ import annotations

from dataclasses import dataclass
import os
import re
import threading
import time
from typing import Callable

import requests
import wx

from app_paths import clear_webview2_profile
from browser_player import (
    HiddenBrowserPlayer,
    PlayerUnavailable,
)
from login_dialog import LoginDialog
from maoer_api import (
    AccountInfo,
    BASE_URL,
    ApiError,
    CheckInResult,
    COMMENT_SORT_HOTTEST,
    COMMENT_SORT_NEWEST,
    DANMAKU_MODE_SUBTITLE,
    DanmakuItem,
    DrmUnsupported,
    MaoerApi,
    CommentItem,
    CommentPage,
    MediaItem,
    PlaybackInfo,
    PurchaseRequired,
)
from uia_live_region import ScreenReaderAnnouncer


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
    def __init__(
        self,
        parent: wx.Window,
        account: AccountInfo,
        on_check_in: Callable[[wx.Button], None] | None = None,
    ) -> None:
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
        if on_check_in is not None:
            check_in_button = wx.Button(panel, label="签到")
            check_in_button.SetName("签到")
            check_in_button.Bind(wx.EVT_BUTTON, lambda _event: on_check_in(check_in_button))
            button_row.Add(check_in_button, 0)
            button_row.AddStretchSpacer(1)
        else:
            button_row.AddStretchSpacer(1)

        close_button = wx.Button(panel, wx.ID_CLOSE, label="关闭")
        close_button.SetName("关闭")
        close_button.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CLOSE))
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


class CommentDetailDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, comment: CommentItem) -> None:
        super().__init__(parent, title="评论详情", size=(620, 420))
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        content_label = wx.StaticText(panel, label="内容")
        self.content_box = wx.TextCtrl(
            panel,
            value=comment.content,
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
        self.content_box.SetFocus()
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
        self.last_comment_mouse_context_menu_at = 0.0

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
        self.comment_list.Bind(wx.EVT_RIGHT_DOWN, self.on_comment_right_down)
        self.comment_list.Bind(wx.EVT_RIGHT_UP, self.on_comment_right_up)
        self.comment_list.Bind(wx.EVT_CONTEXT_MENU, self.on_comment_context_menu)
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
        if wx.GetKeyState(wx.WXK_SHIFT):
            self._open_selected_comment_detail()
            return
        self._open_selected_replies()

    def on_comment_key_down(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if event.ShiftDown():
                self._open_selected_comment_detail()
                return
            self._open_selected_replies()
            return
        if key == wx.WXK_BACK:
            self._go_back()
            return
        event.Skip()

    def on_comment_right_down(self, event: wx.MouseEvent) -> None:
        index = self._hit_test_comment_index(event.GetPosition())
        if index != -1:
            self._select_comment_row(index)
        event.Skip()

    def on_comment_right_up(self, event: wx.MouseEvent) -> None:
        index = self._hit_test_comment_index(event.GetPosition())
        if index != -1:
            self._select_comment_row(index)
        self.last_comment_mouse_context_menu_at = time.monotonic()
        self._show_selected_comment_menu(event.GetPosition())
        self.last_comment_mouse_context_menu_at = time.monotonic()

    def on_comment_context_menu(self, event: wx.ContextMenuEvent) -> None:
        if time.monotonic() - self.last_comment_mouse_context_menu_at < 0.35:
            return
        position = self._comment_menu_position_from_context_event(event)
        if position is None:
            return
        self._show_selected_comment_menu(position)

    def on_char_hook(self, event: wx.KeyEvent) -> None:
        focus = self.FindFocus()
        key = event.GetKeyCode()
        if focus is self.comment_list:
            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and event.ShiftDown():
                self._open_selected_comment_detail()
                return
            if key == wx.WXK_MENU or (key == wx.WXK_F10 and event.ShiftDown()):
                self._show_selected_comment_menu(self._default_comment_menu_position())
                return

        if key == wx.WXK_BACK and focus is not self.sort_box:
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

    def _show_selected_comment_menu(self, position: wx.Point) -> None:
        index = self._selected_index()
        if index < 0 or index >= len(self.items):
            return

        menu = wx.Menu()
        detail_id = wx.NewIdRef()
        menu.Append(detail_id, "查看评论详情")
        try:
            choice = self.comment_list.GetPopupMenuSelectionFromUser(menu, position)
        finally:
            menu.Destroy()

        if choice == int(detail_id):
            self._open_selected_comment_detail()

    def _comment_menu_position_from_context_event(self, event: wx.ContextMenuEvent) -> wx.Point | None:
        screen_position = event.GetPosition()
        if screen_position == wx.DefaultPosition:
            return self._default_comment_menu_position()

        list_position = self.comment_list.ScreenToClient(screen_position)
        if not self.comment_list.GetClientRect().Contains(list_position):
            return None
        index = self._hit_test_comment_index(list_position)
        if index != -1:
            self._select_comment_row(index)
        return list_position

    def _default_comment_menu_position(self) -> wx.Point:
        size = self.comment_list.GetClientSize()
        selection = self._selected_index()
        y = 10
        if selection >= 0:
            try:
                rect = self.comment_list.GetItemRect(selection)
                y = rect.y + max(1, rect.height // 2)
            except Exception:
                row_height = max(self.comment_list.GetCharHeight() + 8, 20)
                y = 24 + max(0, selection - self._top_index()) * row_height
        max_y = max(10, size.height - 10)
        return wx.Point(min(20, max(1, size.width - 10)), min(max(10, y), max_y))

    def _hit_test_comment_index(self, position: wx.Point) -> int:
        hit = self.comment_list.HitTest(position)
        index = hit[0] if isinstance(hit, tuple) else hit
        return index if index != wx.NOT_FOUND else -1

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

    def _open_selected_comment_detail(self) -> None:
        index = self._selected_index()
        if index < 0 or index >= len(self.items):
            return
        dialog = CommentDetailDialog(self, self.items[index])
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        self.comment_list.SetFocus()

    def _go_back(self) -> None:
        if not self.back_stack:
            self.Close()
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


@dataclass
class DanmakuSprite:
    item: DanmakuItem
    lane: int
    start_position: float
    text_width: int


class DanmakuCanvas(wx.Panel):
    FRAME_MS = 33
    TRAVEL_SECONDS = 8.0

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.BORDER_NONE)
        self.SetBackgroundColour(wx.BLACK)
        self.SetName("")
        self.bitmap_view = wx.StaticBitmap(self, bitmap=wx.Bitmap(1, 1))
        self.bitmap_view.SetName("")
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self.bitmap_view, 1, wx.EXPAND)
        self.SetSizer(root)
        self.items: list[DanmakuItem] = []
        self.active: list[DanmakuSprite] = []
        self.next_index = 0
        self.next_lane = 0
        self.position = 0.0
        self.paused = True
        self.message = ""
        self.on_danmaku_due: Callable[[DanmakuItem], None] | None = None
        self.on_subtitle_due: Callable[[DanmakuItem], None] | None = None
        self.last_tick = time.monotonic()
        self.font = wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def reset(self, message: str = "") -> None:
        self.items = []
        self.active = []
        self.next_index = 0
        self.next_lane = 0
        self.position = 0.0
        self.paused = True
        self.message = message
        self.last_tick = time.monotonic()
        self.timer.Start(self.FRAME_MS)
        self._render_frame()

    def set_items(self, items: list[DanmakuItem]) -> None:
        self.items = sorted(items, key=lambda item: item.time)
        self.active = []
        self.next_index = self._first_index_at_or_after(self.position)
        self.next_lane = 0
        self.message = "" if self.items else "暂无弹幕"
        self._render_frame()

    def set_error(self, message: str) -> None:
        self.items = []
        self.active = []
        self.message = f"弹幕加载失败: {message or '未知错误'}"
        self._render_frame()

    def set_paused(self, paused: bool) -> None:
        self._advance_position()
        self.paused = paused
        self.last_tick = time.monotonic()
        self._render_frame()

    def current_position(self) -> float:
        self._advance_position()
        return self.position

    def sync_position(self, seconds: float, paused: bool) -> None:
        target = max(0.0, float(seconds))
        self._advance_position()
        if abs(self.position - target) > 0.75:
            self.position = target
            self.active = []
            self.next_index = self._first_index_at_or_after(self.position)
            self.next_lane = 0
        self.paused = paused
        self.last_tick = time.monotonic()
        self._render_frame()

    def seek(self, seconds: int) -> None:
        self.position = max(0.0, self.position + float(seconds))
        self.active = []
        self.next_index = self._first_index_at_or_after(self.position)
        self.next_lane = 0
        self.last_tick = time.monotonic()
        self._render_frame()

    def stop(self) -> None:
        self.timer.Stop()

    def on_size(self, event: wx.SizeEvent) -> None:
        self.active = []
        self.next_lane = 0
        self._render_frame()
        event.Skip()

    def on_timer(self, _event: wx.TimerEvent) -> None:
        if not self.paused:
            self._advance_position()
            self._spawn_due_items()
            self._drop_finished_items()
        self._render_frame()

    def _render_frame(self) -> None:
        size = self.GetClientSize()
        width = max(1, size.width)
        height = max(1, size.height)
        bitmap = wx.Bitmap(width, height)
        dc = wx.MemoryDC(bitmap)
        dc.SetBackground(wx.Brush(wx.BLACK))
        dc.Clear()
        dc.SetFont(self.font)

        if self.message:
            dc.SetTextForeground(wx.Colour(210, 210, 210))
            text_width, text_height = dc.GetTextExtent(self.message)
            x = max(0, (width - text_width) // 2)
            y = max(0, (height - text_height) // 2)
            dc.DrawText(self.message, x, y)
            dc.SelectObject(wx.NullBitmap)
            self.bitmap_view.SetBitmap(bitmap)
            return

        for sprite in self.active:
            elapsed = max(0.0, self.position - sprite.start_position)
            speed = (width + sprite.text_width) / self.TRAVEL_SECONDS
            x = int(width - elapsed * speed)
            y = self._lane_y(sprite.lane)
            dc.SetTextForeground(self._item_colour(sprite.item))
            dc.DrawText(sprite.item.text, x, y)
        dc.SelectObject(wx.NullBitmap)
        self.bitmap_view.SetBitmap(bitmap)

    def _advance_position(self) -> None:
        now = time.monotonic()
        if not self.paused:
            self.position += max(0.0, now - self.last_tick)
        self.last_tick = now

    def _spawn_due_items(self) -> None:
        if not self.items:
            return
        now = self.position
        stale_before = max(0.0, now - 0.4)
        while self.next_index < len(self.items) and self.items[self.next_index].time < stale_before:
            self.next_index += 1
        first_danmaku: DanmakuItem | None = None
        first_subtitle: DanmakuItem | None = None
        while self.next_index < len(self.items) and self.items[self.next_index].time <= now:
            item = self.items[self.next_index]
            if item.mode == DANMAKU_MODE_SUBTITLE:
                if first_subtitle is None:
                    first_subtitle = item
            elif first_danmaku is None:
                first_danmaku = item
            self._spawn_item(item)
            self.next_index += 1
        if first_danmaku is not None and self.on_danmaku_due is not None:
            self.on_danmaku_due(first_danmaku)
        if first_subtitle is not None and self.on_subtitle_due is not None:
            self.on_subtitle_due(first_subtitle)

    def _spawn_item(self, item: DanmakuItem) -> None:
        lane_count = self._lane_count()
        lane = self.next_lane % lane_count
        self.next_lane = (lane + 1) % lane_count
        bitmap = wx.Bitmap(1, 1)
        dc = wx.MemoryDC(bitmap)
        dc.SetFont(self.font)
        text_width = dc.GetTextExtent(item.text)[0]
        dc.SelectObject(wx.NullBitmap)
        self.active.append(DanmakuSprite(item, lane, self.position, text_width))

    def _drop_finished_items(self) -> None:
        width = self.GetClientSize().width
        kept: list[DanmakuSprite] = []
        for sprite in self.active:
            elapsed = max(0.0, self.position - sprite.start_position)
            speed = (width + sprite.text_width) / self.TRAVEL_SECONDS
            if width - elapsed * speed + sprite.text_width >= 0:
                kept.append(sprite)
        self.active = kept

    def _lane_count(self) -> int:
        height = max(1, self.GetClientSize().height)
        return max(1, height // self._lane_height())

    def _lane_height(self) -> int:
        return max(self.GetCharHeight() + 8, 28)

    def _lane_y(self, lane: int) -> int:
        return 4 + (lane % self._lane_count()) * self._lane_height()

    def _first_index_at_or_after(self, seconds: float) -> int:
        for index, item in enumerate(self.items):
            if item.time >= seconds:
                return index
        return len(self.items)

    @staticmethod
    def _item_colour(item: DanmakuItem) -> wx.Colour:
        if item.color <= 0:
            return wx.Colour(255, 255, 255)
        return wx.Colour((item.color >> 16) & 0xFF, (item.color >> 8) & 0xFF, item.color & 0xFF)


class PlaybackFrame(wx.Frame):
    SEEK_SECONDS = 15

    def __init__(
        self,
        parent: wx.Window,
        api: MaoerApi,
        player: HiddenBrowserPlayer,
        on_closed: Callable[["PlaybackFrame"], None],
    ) -> None:
        super().__init__(parent, title="", size=(760, 480))
        self.api = api
        self.player = player
        self.on_closed = on_closed
        self.playback: PlaybackInfo | None = None
        self.load_generation = 0
        self.read_danmaku_enabled = False
        self.read_subtitle_enabled = False
        self.time_announcement_generation = 0

        root = wx.BoxSizer(wx.VERTICAL)
        self.danmaku_canvas = DanmakuCanvas(self)
        self.danmaku_canvas.on_danmaku_due = self._on_danmaku_due
        self.danmaku_canvas.on_subtitle_due = self._on_subtitle_due
        root.Add(self.danmaku_canvas, 1, wx.EXPAND)
        self.SetSizer(root)
        self.live_region = wx.StaticText(self, label="", pos=(0, 0), size=(1, 1))
        self.live_region.SetForegroundColour(wx.BLACK)
        self.live_region.SetBackgroundColour(wx.BLACK)
        self.live_region.SetName("弹幕朗读")
        self.screen_reader = ScreenReaderAnnouncer(self.live_region)

        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def play(self, playback: PlaybackInfo) -> None:
        self.playback = playback
        self.load_generation += 1
        generation = self.load_generation
        self.SetTitle(playback.title)
        self.danmaku_canvas.reset("正在加载弹幕...")
        self.player.play(playback)
        self._load_danmaku(playback.sound_id, generation)
        wx.CallLater(500, self._sync_playback_status, generation)
        wx.CallAfter(self.SetFocus)

    def _load_danmaku(self, sound_id: int, generation: int) -> None:
        def runner() -> None:
            try:
                items = self.api.sound_danmaku(sound_id)
            except (ApiError, requests.RequestException, ValueError) as exc:
                wx.CallAfter(self._set_danmaku_failed, generation, str(exc))
            except Exception as exc:
                wx.CallAfter(self._set_danmaku_failed, generation, f"{type(exc).__name__}: {exc}")
            else:
                wx.CallAfter(self._set_danmaku_items, generation, items)

        threading.Thread(target=runner, daemon=True).start()

    def _set_danmaku_items(self, generation: int, items: list[DanmakuItem]) -> None:
        if generation != self.load_generation:
            return
        self.danmaku_canvas.set_items(items)

    def _set_danmaku_failed(self, generation: int, message: str) -> None:
        if generation != self.load_generation:
            return
        self.danmaku_canvas.set_error(message)

    def on_char_hook(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        try:
            if key in (ord("D"), ord("d")):
                self._toggle_danmaku_reader()
                return
            if key in (ord("F"), ord("f")):
                self._toggle_subtitle_reader()
                return
            if key in (ord("T"), ord("t")):
                self._announce_playback_time()
                return
            if key == wx.WXK_SPACE:
                paused = self.player.toggle_pause()
                self.danmaku_canvas.set_paused(paused)
                self._set_parent_status("已暂停" if paused else "继续播放")
                return
            if key == wx.WXK_UP:
                volume = self.player.volume_up()
                self._set_parent_status(f"音量: {volume}")
                return
            if key == wx.WXK_DOWN:
                volume = self.player.volume_down()
                self._set_parent_status(f"音量: {volume}")
                return
            if key == wx.WXK_RIGHT:
                self.player.seek(self.SEEK_SECONDS)
                self.danmaku_canvas.seek(self.SEEK_SECONDS)
                self._set_parent_status(f"快进 {self.SEEK_SECONDS} 秒")
                return
            if key == wx.WXK_LEFT:
                self.player.seek(-self.SEEK_SECONDS)
                self.danmaku_canvas.seek(-self.SEEK_SECONDS)
                self._set_parent_status(f"快退 {self.SEEK_SECONDS} 秒")
                return
        except PlayerUnavailable as exc:
            self._set_parent_status("操作失败")
            wx.MessageBox(str(exc), "错误", wx.OK | wx.ICON_ERROR, self)
            return
        event.Skip()

    def _toggle_danmaku_reader(self) -> None:
        self.read_danmaku_enabled = not self.read_danmaku_enabled
        message = "弹幕朗读已开启" if self.read_danmaku_enabled else "弹幕朗读已关闭"
        self.screen_reader.announce(message)
        self._set_parent_status(message)

    def _toggle_subtitle_reader(self) -> None:
        self.read_subtitle_enabled = not self.read_subtitle_enabled
        message = "字幕朗读已开启" if self.read_subtitle_enabled else "字幕朗读已关闭"
        self.screen_reader.announce(message)
        self._set_parent_status(message)

    def _on_danmaku_due(self, item: DanmakuItem) -> None:
        if not self.read_danmaku_enabled:
            return
        text = item.text.strip()
        if text:
            self.screen_reader.announce(text)

    def _on_subtitle_due(self, item: DanmakuItem) -> None:
        if not self.read_subtitle_enabled:
            return
        text = item.text.strip()
        if text:
            self.screen_reader.announce(text)

    def _announce_playback_time(self) -> None:
        self.time_announcement_generation += 1
        generation = self.time_announcement_generation
        self.player.status(lambda status: self._announce_playback_time_done(generation, status))
        wx.CallLater(700, self._announce_playback_time_fallback, generation)

    def _announce_playback_time_done(self, generation: int, status: dict[str, object] | None) -> None:
        if generation != self.time_announcement_generation:
            return
        current_seconds, total_seconds = self._playback_times_from_status(status)
        if total_seconds is None:
            self._set_parent_status("没有获取到时长")
            return
        self.time_announcement_generation += 1
        self._announce_time(current_seconds, total_seconds)

    def _announce_playback_time_fallback(self, generation: int) -> None:
        if generation != self.time_announcement_generation:
            return
        current_seconds, total_seconds = self._playback_times_from_status(None)
        if total_seconds is None:
            self._set_parent_status("没有获取到时长")
            return
        self.time_announcement_generation += 1
        self._announce_time(current_seconds, total_seconds)

    def _announce_time(self, current_seconds: float, total_seconds: float) -> None:
        current_seconds = min(current_seconds, total_seconds) if total_seconds else current_seconds
        message = f"{self._format_spoken_time(current_seconds)}/{self._format_spoken_time(total_seconds)}"
        self.screen_reader.announce(message)
        self._set_parent_status(message)

    def _playback_times_from_status(self, status: dict[str, object] | None) -> tuple[float, float | None]:
        current_seconds = self.danmaku_canvas.current_position()
        total_seconds = self.playback.duration_ms / 1000.0 if self.playback and self.playback.duration_ms else None
        if status and status.get("ok"):
            status_position = self._positive_float(status.get("position"))
            status_duration = self._positive_float(status.get("duration"))
            if status_position is not None:
                if status_position > 0 or status.get("paused") or current_seconds < 0.75:
                    current_seconds = status_position
            if status_duration is not None:
                total_seconds = status_duration
        return current_seconds, total_seconds

    def _sync_playback_status(self, generation: int) -> None:
        if generation != self.load_generation or self.playback is None:
            return
        self.player.status(lambda status: self._sync_playback_status_done(generation, status))

    def _sync_playback_status_done(self, generation: int, status: dict[str, object] | None) -> None:
        if generation != self.load_generation or self.playback is None:
            return
        if status and status.get("ok"):
            position = self._positive_float(status.get("position"))
            paused = bool(status.get("paused"))
            if position is not None:
                if position > 0 or paused or self.danmaku_canvas.position < 0.75:
                    self.danmaku_canvas.sync_position(position, paused)
                else:
                    self.danmaku_canvas.set_paused(False)
        wx.CallLater(1000, self._sync_playback_status, generation)

    def on_close(self, event: wx.CloseEvent) -> None:
        self.load_generation += 1
        self.danmaku_canvas.stop()
        self.player.stop()
        self.on_closed(self)
        event.Skip()

    def _set_parent_status(self, message: str) -> None:
        parent = self.GetParent()
        if parent is not None and hasattr(parent, "SetStatusText"):
            parent.SetStatusText(message)

    @staticmethod
    def _positive_float(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    @staticmethod
    def _format_spoken_time(seconds: float) -> str:
        total_seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours}小时")
        if minutes:
            parts.append(f"{minutes}分")
        if seconds or not parts:
            parts.append(f"{seconds}秒")
        return "".join(parts)


class MaoerFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title="猫耳FM", size=(940, 620))
        self.api = MaoerApi()
        self.browser_player = HiddenBrowserPlayer(self, cookie=self.api.cookie_header)
        self.active_player: HiddenBrowserPlayer | None = None
        self.player_frame: PlaybackFrame | None = None
        self.items: list[MediaItem] = []
        self.current_title = ""
        self.page_state: PageState | None = None
        self.navigation_stack: list[NavigationState] = []
        self.homepage_state: NavigationState | None = None
        self.comment_windows: list[CommentsFrame] = []
        self.last_mouse_context_menu_at = 0.0
        self.account_logged_in = bool(self.api.cookie_header)

        self._build_ui()
        self._build_menu()
        self._bind_events()
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
        self.account_favorites_menu_id = wx.NewIdRef()
        self.account_subscriptions_menu_id = wx.NewIdRef()
        self.account_purchased_dramas_menu_id = wx.NewIdRef()
        self.account_logout_menu_id = wx.NewIdRef()
        self.account_exit_menu_id = wx.NewIdRef()
        self._update_account_menu()

    def _update_account_menu(self) -> None:
        menu_bar = wx.MenuBar()

        account_menu = wx.Menu()
        if self.account_logged_in:
            account_menu.Append(self.account_info_menu_id, "我的信息(&I)")
            account_menu.Append(self.account_favorites_menu_id, "我的收藏(&F)")
            account_menu.Append(self.account_subscriptions_menu_id, "剧集订阅(&S)")
            account_menu.Append(self.account_purchased_dramas_menu_id, "已购广播剧(&P)")
            account_menu.AppendSeparator()
            account_menu.Append(self.account_logout_menu_id, "退出登录(&O)")
        else:
            account_menu.Append(self.account_login_menu_id, "账号登录(&L)")
        account_menu.AppendSeparator()
        account_menu.Append(self.account_exit_menu_id, "退出程序(&Q)")
        menu_bar.Append(account_menu, "账号(&A)")

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
        self.Bind(wx.EVT_MENU, self.on_account_favorites, id=self.account_favorites_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_subscriptions, id=self.account_subscriptions_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_purchased_dramas, id=self.account_purchased_dramas_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_logout, id=self.account_logout_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_exit, id=self.account_exit_menu_id)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_CLOSE, self.on_close)

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

    def on_account_favorites(self, _event: wx.Event) -> None:
        previous_state = self._account_list_previous_state()
        self.page_state = None
        self.set_items([], "我的收藏", focus_list=True)
        self._run_background(
            "正在加载我的收藏...",
            lambda: self.api.favorite_folders(1),
            lambda items: self._enter_items(
                items,
                "我的收藏",
                previous_state,
                focus_list=True,
                page_state=PageState(1, lambda page: self.api.favorite_folders(page)),
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
        dialog = AccountInfoDialog(self, account, self._check_in_from_account_dialog)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _check_in_from_account_dialog(self, button: wx.Button) -> None:
        button.Enable(False)
        self.SetStatusText("正在签到...")
        dialog = button.GetTopLevelParent()

        def runner() -> None:
            try:
                result = self.api.check_in()
                updated_account = None
                if result.success:
                    try:
                        updated_account = self.api.account_info()
                    except (ApiError, requests.RequestException, ValueError) as exc:
                        debug_log(f"account refresh after check-in failed: {exc}")
                    except Exception as exc:
                        debug_log(f"account refresh after check-in failed: {type(exc).__name__}: {exc}")
            except ApiError as exc:
                if str(exc) == "需要登录":
                    wx.CallAfter(self._mark_account_logged_out, "需要登录")
                wx.CallAfter(self.show_error, str(exc))
            except (requests.RequestException, ValueError) as exc:
                wx.CallAfter(self.show_error, str(exc))
            except Exception as exc:
                wx.CallAfter(self.show_error, f"{type(exc).__name__}: {exc}")
            else:
                wx.CallAfter(self._show_check_in_result, result, dialog, updated_account)
            finally:
                wx.CallAfter(self._safe_enable_window, button, True)

        threading.Thread(target=runner, daemon=True).start()

    @staticmethod
    def _safe_enable_window(window: wx.Window, enabled: bool) -> None:
        try:
            if not window.IsBeingDeleted():
                window.Enable(enabled)
        except RuntimeError:
            pass

    def _show_check_in_result(
        self,
        result: CheckInResult,
        parent: wx.Window | None = None,
        updated_account: AccountInfo | None = None,
    ) -> None:
        title = "签到成功" if result.success else "签到提示"
        message = result.message or ("签到成功" if result.success else "签到失败")
        if "请明天再来" in message:
            message = "您今天已经签到过了，请明天再来哟。"
        if result.fish_count is not None and str(result.fish_count) not in message:
            message = f"{message}\n获得小鱼干：{result.fish_count}"
        if updated_account is not None and isinstance(parent, AccountInfoDialog):
            try:
                if not parent.IsBeingDeleted():
                    parent.content_box.SetValue(updated_account.text)
            except RuntimeError:
                parent = None
        self.SetStatusText(message)
        message_parent = parent or self
        wx.MessageBox(message, title, wx.OK | wx.ICON_INFORMATION, message_parent)
        try:
            message_parent.Raise()
            message_parent.SetFocus()
        except RuntimeError:
            pass

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
        if self.current_title in {"我的收藏", "剧集订阅", "已购广播剧"}:
            self.load_homepage(focus_list=True)

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

    def _resize_list_columns(self) -> None:
        width = self.list.GetClientSize().width
        if width <= 0:
            return

        author_width = max(160, min(260, width // 3))
        name_width = max(120, width - author_width - 24)
        self.list.SetColumnWidth(0, name_width)
        self.list.SetColumnWidth(1, max(120, width - name_width - 24))

    def _update_list_column_headers(self, title: str) -> None:
        label = "声音数" if title == "我的收藏" else "作者"
        column = wx.ListItem()
        column.SetText(label)
        self.list.SetColumn(1, column)

    def _append_list_item(self, index: int, item: MediaItem) -> None:
        self.list.InsertItem(index, item.title)
        self.list.SetItem(index, 1, self._item_author(item))

    def _item_author(self, item: MediaItem) -> str:
        raw = item.raw
        if isinstance(raw, dict):
            if raw.get("_hide_author"):
                return item.subtitle

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

            if item.kind == "album":
                self._run_background(
                    f"正在加载: {item.title}",
                    lambda: self.api.album_sounds_page(item.id, 1),
                    lambda items: self._enter_items(
                        items,
                        item.title,
                        previous_state,
                        page_state=PageState(1, lambda page: self.api.album_sounds_page(item.id, page)),
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
            lambda playback: self._play(playback),
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
        self._update_list_column_headers(title)
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
        if self.page_state is not None:
            wx.CallAfter(self._load_next_page_if_near_bottom)

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
        wx.CallAfter(self._load_next_page_if_near_bottom)

    def _play(self, playback: PlaybackInfo) -> None:
        created = False
        if self.player_frame is None:
            self.player_frame = PlaybackFrame(
                self,
                self.api,
                self.browser_player,
                self._on_player_window_close,
            )
            created = True

        try:
            self.browser_player.cookie = self.api.cookie_header
            self.player_frame.play(playback)
            self.active_player = self.browser_player
        except PlayerUnavailable as exc:
            if created and self.player_frame is not None:
                self.player_frame.Destroy()
                self.player_frame = None
            self.show_error(str(exc))
            return

        self.player_frame.Show()
        self.player_frame.Raise()
        self.SetStatusText(f"正在播放: {playback.title}")
        threading.Thread(target=self.api.add_play_times, args=(playback,), daemon=True).start()

    def _on_player_window_close(self, frame: PlaybackFrame) -> None:
        if self.player_frame is frame:
            self.player_frame = None
        if self.active_player is self.browser_player:
            self.active_player = None
        self.SetStatusText("已停止播放")

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

    def on_account_exit(self, _event: wx.CommandEvent) -> None:
        self.Close()

    def on_char_hook(self, event: wx.KeyEvent) -> None:
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
        if self.player_frame is not None:
            self.player_frame.Close()
            return
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
        if self.player_frame is not None:
            self.player_frame.Destroy()
            self.player_frame = None
        self.active_player = None
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
