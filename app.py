from __future__ import annotations

from dataclasses import dataclass, replace
import os
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Callable

import requests
import wx

from app_paths import clear_webview2_profile
from audio_output import AudioOutputRouter, SYSTEM_OUTPUT
from app_settings import (
    AppSettings, SubtitleFilterPreset, SubtitleFilterRules, default_filter_presets,
    load_settings, save_settings,
)
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
    DRAMA_PAY_TYPE_EPISODES,
    DRAMA_PAY_TYPE_WHOLE,
    DanmakuItem,
    DramaFollowResult,
    DramaPurchaseInfo,
    DrmUnsupported,
    MaoerApi,
    CommentItem,
    CommentPage,
    MediaItem,
    PlaybackInfo,
    PublisherProfile,
    PurchaseRequired,
    SoundPurchaseInfo,
)
from startup_sound import play_startup_sound
from uia_live_region import ScreenReaderAnnouncer
from updater import handle_update_cli, run_startup_update_check
from _build_info import APP_VERSION


APP_TITLE = "猫耳FM"
APP_AUTHOR = "欢喜就好&谷雨"
HOTKEYS_TEXT_NAME = "热键表.txt"
UPDATE_TEXT_NAME = "update.txt"
NON_DIALOGUE_SUBTITLE_ROLES = frozenset({
    "旁白", "报幕", "音效", "音乐", "背景音乐", "环境音", "动作", "场景", "提示", "提示音", "系统", "说明", "解说", "画外音",
})
NON_DIALOGUE_SUBTITLE_CONTENT = re.compile(
    r"^(?:\s*(?:（[^（）]+）|\([^()]+\)|【[^【】]+】|\[[^\[\]]+\]|<[^<>]+>)\s*)+$"
)
SUBTITLE_ROLE_PREFIX = re.compile(r"^\s*([^：:\r\n]{1,30})\s*[：:]\s*(\S[\s\S]*)$")
STORY_BARRAGE_SUBTITLE_ROLE = re.compile(r"弹幕\d*")
SUBTITLE_OS_MARKER = re.compile(r"(?:[（(]\s*OS\s*[）)]|[【\[]\s*OS\s*[】\]])", re.IGNORECASE)
SUBTITLE_OS_ROLE_SUFFIX = re.compile(r"(?<=[\u3400-\u9fff])\s*OS$|(?<=\s)OS$", re.IGNORECASE)
NON_DIALOGUE_ANNOUNCEMENT_PREFIX = re.compile(
    r"^(?:本(?:作品|剧|广播剧|节目)\s*由|[（(【\[]\s*(?:报幕|旁白|解说)\s*[）)】\]])"
)
SUBTITLE_CONTINUATION_MAX_GAP = 8.0


def is_character_dialogue_subtitle(item: DanmakuItem) -> bool:
    # A whole bracketed caption is a scene/action cue, even if the JSON
    # provider split an internal colon into role and content.
    if NON_DIALOGUE_SUBTITLE_CONTENT.fullmatch(item.text.strip()):
        return False
    role = item.role.strip()
    content = item.content.strip()
    if not role or not content:
        match = SUBTITLE_ROLE_PREFIX.fullmatch(item.text.strip())
        if match:
            role, content = match.group(1).strip(), match.group(2).strip()
    return bool(
        role and content
        and role not in NON_DIALOGUE_SUBTITLE_ROLES
        and not NON_DIALOGUE_SUBTITLE_CONTENT.fullmatch(content)
        and not NON_DIALOGUE_ANNOUNCEMENT_PREFIX.match(content)
    )


def is_story_barrage_subtitle(item: DanmakuItem) -> bool:
    role = item.role.strip()
    if not role:
        match = SUBTITLE_ROLE_PREFIX.fullmatch(item.text.strip())
        role = match.group(1).strip() if match else ""
    return bool(STORY_BARRAGE_SUBTITLE_ROLE.fullmatch(role))


def mark_dialogue_continuations(items: list[DanmakuItem]) -> list[DanmakuItem]:
    """Classify unlabelled XML caption lines without altering their spoken text."""
    marked: list[DanmakuItem] = []
    # XML subtitle contributor IDs are not speaker IDs: one contributor may
    # caption several actors. Track the last *explicit* role for each
    # contributor so interleaved lines do not break a speaker's continuation.
    speaker_by_submitter: dict[str, tuple[str, float]] = {}
    for item in sorted(items, key=lambda entry: entry.time):
        if item.mode != DANMAKU_MODE_SUBTITLE:
            marked.append(item)
            continue

        text = item.text.strip()
        if NON_DIALOGUE_SUBTITLE_CONTENT.fullmatch(text):
            speaker_by_submitter.clear()
            marked.append(item)
            continue
        role = item.role.strip()
        content = item.content.strip()
        if not role or not content:
            explicit = SUBTITLE_ROLE_PREFIX.fullmatch(text)
            if explicit:
                role, content = explicit.group(1).strip(), explicit.group(2).strip()

        if role and content:
            if is_character_dialogue_subtitle(item):
                if item.user_id:
                    speaker_by_submitter[item.user_id] = (role, item.time)
            elif role in NON_DIALOGUE_SUBTITLE_ROLES or NON_DIALOGUE_ANNOUNCEMENT_PREFIX.match(content):
                speaker_by_submitter.clear()
            elif item.user_id:
                speaker_by_submitter.pop(item.user_id, None)
        elif item.user_id and item.user_id in speaker_by_submitter:
            speaker, last_time = speaker_by_submitter[item.user_id]
            if 0 <= item.time - last_time <= SUBTITLE_CONTINUATION_MAX_GAP:
                # Preserve the original text for full-reading mode.
                item = replace(item, role=speaker, content=text)
                speaker_by_submitter[item.user_id] = (speaker, item.time)
            else:
                speaker_by_submitter.pop(item.user_id, None)

        marked.append(item)
    return marked


def program_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


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
    hide_detail_column: bool
    opened_drama_id: int | None = None


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


class MediaDetailDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, title: str, content: str, detail_kind: str) -> None:
        super().__init__(parent, title=f"{detail_kind} - {title}", size=(660, 480))
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


class SubtitleFilterRulesDialog(wx.Dialog):
    DIALOGUE_MODES = ("mute", "role")

    def __init__(self, parent: wx.Window, presets: tuple[SubtitleFilterPreset, ...], slot: int) -> None:
        super().__init__(parent, title="字幕过滤规则", size=(620, 680),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        # Convert the retired choice only in this dialog's draft. Cancel
        # leaves the saved settings and the caller's presets untouched.
        self.presets = [
            replace(preset, rules=replace(preset.rules, dialogue_mode="role"))
            if preset.rules.dialogue_mode == "full" or preset.rules.speaker_transitions_only
            else preset
            for preset in presets
        ]
        self.current_slot = slot
        panel = wx.Panel(self)
        self.panel = panel
        layout = wx.BoxSizer(wx.VERTICAL)

        hint = wx.StaticText(panel, label=(
            "仅在过滤模式生效；字幕朗读开启后，播放窗口按主键盘 1～0 切换方案。\n"
            "切换方案会保留本次编辑；点击保存后统一生效，取消则放弃本次编辑。"
        ))
        layout.Add(hint, 0, wx.ALL, 10)

        preset_row = wx.BoxSizer(wx.HORIZONTAL)
        preset_row.Add(wx.StaticText(panel, label="当前方案"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.preset_choice = wx.Choice(panel, choices=[
            f"{(index + 1) % 10}：{preset.name}" for index, preset in enumerate(self.presets)
        ])
        self.preset_choice.SetName("当前过滤方案")
        self.preset_choice.SetSelection(slot)
        preset_row.Add(self.preset_choice, 1, wx.EXPAND)
        self.add_button = wx.Button(panel, label="新增方案")
        preset_row.Add(self.add_button, 0, wx.LEFT, 8)
        self.restore_default_button = wx.Button(panel, label="恢复默认")
        preset_row.Add(self.restore_default_button, 0, wx.LEFT, 8)
        layout.Add(preset_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        layout.Add(wx.StaticText(panel, label="方案名称"), 0, wx.LEFT | wx.RIGHT, 10)
        self.preset_name = wx.TextCtrl(panel)
        self.preset_name.SetName("方案名称")
        layout.Add(self.preset_name, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.dialogue_mode = wx.RadioBox(
            panel, label="人物对话", choices=["人物对话：不朗读", "人物对话：只读角色名"],
            majorDimension=2, style=wx.RA_SPECIFY_COLS,
        )
        self.dialogue_mode.SetName("人物对话")
        layout.Add(self.dialogue_mode, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.speaker_transitions_only = wx.CheckBox(
            panel, label="有声书模式：旁白与角色只读名称，切换时播报",
        )
        self.story_barrage = wx.CheckBox(panel, label="过滤剧情弹幕字幕")
        self.os_body = wx.CheckBox(panel, label="朗读 OS 提示（只读角色名和 OS，不读正文）")
        self.info_label_only = wx.CheckBox(panel, label="下列信息标签只读名称，不读正文")
        for control in (self.speaker_transitions_only, self.story_barrage,
                        self.os_body, self.info_label_only):
            layout.Add(control, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.info_labels_label = wx.StaticText(panel, label="信息标签（每行一个；默认：系统、提示音）")
        layout.Add(self.info_labels_label, 0, wx.LEFT | wx.RIGHT, 10)
        self.info_labels = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.BORDER_SUNKEN)
        self.info_labels.SetName("信息标签，每行一个")
        layout.Add(self.info_labels, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        keyword_label = wx.StaticText(panel, label="自定义过滤词（每行一个；命中后整条字幕不朗读）")
        self.keywords = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.BORDER_SUNKEN)
        self.keywords.SetName("自定义过滤词，每行一个，命中后整条字幕不朗读")
        layout.Add(keyword_label, 0, wx.LEFT | wx.RIGHT, 10)
        layout.Add(self.keywords, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        self.help_button = wx.Button(panel, label="规则说明")
        button_row.Add(self.help_button, 0)
        button_row.AddStretchSpacer(1)
        buttons = wx.StdDialogButtonSizer()
        ok_button = wx.Button(panel, wx.ID_OK, label="保存")
        cancel_button = wx.Button(panel, wx.ID_CANCEL, label="取消")
        ok_button.SetDefault()
        buttons.AddButton(ok_button)
        buttons.AddButton(cancel_button)
        buttons.Realize()
        button_row.Add(buttons, 0)
        layout.Add(button_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(layout)
        self._load_slot(slot)
        self._update_preset_buttons()
        self.preset_choice.Bind(wx.EVT_CHOICE, self._on_slot_changed)
        self.add_button.Bind(wx.EVT_BUTTON, self._add_preset)
        self.restore_default_button.Bind(wx.EVT_BUTTON, self._restore_defaults)
        self.dialogue_mode.Bind(wx.EVT_RADIOBOX, self._on_dialogue_mode_changed)
        self.speaker_transitions_only.Bind(wx.EVT_CHECKBOX, self._on_book_mode_changed)
        self.info_label_only.Bind(wx.EVT_CHECKBOX, self._update_rule_controls)
        self.help_button.Bind(wx.EVT_BUTTON, self._show_rule_help)
        self.preset_choice.SetFocus()

    @staticmethod
    def _lines(value: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(word for line in value.splitlines() if (word := line.strip())))

    def _load_slot(self, slot: int) -> None:
        preset = self.presets[slot]
        rules = preset.rules
        self.preset_name.SetValue(preset.name)
        self.dialogue_mode.SetSelection(self.DIALOGUE_MODES.index(rules.dialogue_mode))
        self.speaker_transitions_only.SetValue(rules.speaker_transitions_only)
        self.story_barrage.SetValue(rules.story_barrage)
        self.os_body.SetValue(rules.os_body)
        self.info_label_only.SetValue(rules.info_label_only)
        self.info_labels.SetValue("\n".join(rules.info_labels))
        self.keywords.SetValue("\n".join(rules.keywords))
        self._update_rule_controls()

    def _store_current_slot(self) -> None:
        slot = self.current_slot
        name = self.preset_name.GetValue().strip() or self.presets[slot].name
        dialogue_mode = self.DIALOGUE_MODES[self.dialogue_mode.GetSelection()]
        rules = SubtitleFilterRules(
            dialogue_mode=dialogue_mode,
            speaker_transitions_only=self.speaker_transitions_only.GetValue() and dialogue_mode == "role",
            story_barrage=self.story_barrage.GetValue(),
            os_body=self.os_body.GetValue(),
            info_label_only=self.info_label_only.GetValue(),
            info_labels=self._lines(self.info_labels.GetValue()),
            keywords=self._lines(self.keywords.GetValue()),
        )
        self.presets[slot] = SubtitleFilterPreset(name, rules)
        self.preset_choice.SetString(slot, f"{(slot + 1) % 10}：{name}")

    def _on_dialogue_mode_changed(self, _event: wx.CommandEvent) -> None:
        if self.DIALOGUE_MODES[self.dialogue_mode.GetSelection()] == "mute":
            self.speaker_transitions_only.SetValue(False)

    def _on_book_mode_changed(self, _event: wx.CommandEvent) -> None:
        if self.speaker_transitions_only.GetValue():
            self.dialogue_mode.SetSelection(self.DIALOGUE_MODES.index("role"))

    def _update_rule_controls(self, _event: wx.CommandEvent | None = None) -> None:
        enabled = self.info_label_only.GetValue()
        self.info_labels.Enable(enabled)
        self.info_labels_label.Enable(enabled)

    def _show_rule_help(self, _event: wx.CommandEvent) -> None:
        content = (
            "人物对话：可选择不朗读，或只读角色名。需要完整朗读字幕时，请在播放窗口关闭过滤模式。\n\n"
            "有声书模式：自动选择只读角色名。旁白、角色仅在切换时播报名称，正文、场景和动作说明不读；"
            "报幕、制作信息仍保留。将人物对话改为不朗读时，会同时关闭有声书模式。\n\n"
            "OS：勾选后，每次出现 OS 标记会读角色名和 OS，不读正文；同一角色也会提示。"
            "取消勾选后，整段 OS 完全不读，包括没有重复标注 OS 或角色名的连续字幕。"
            "遇到明确的普通角色、旁白或报幕标签后，恢复该方案的正常规则。"
            "广播剧、有声书和自定义方案均适用；关闭过滤模式后仍完整朗读字幕。\n\n"
            "剧情弹幕：指字幕中写成弹幕、弹幕1等的剧情内容，与普通听众弹幕的朗读开关不同。\n\n"
            "信息标签：勾选后只读系统、提示音等标签，不读其正文。每行填写一个标签。"
            "取消勾选后，标签列表暂时停用，已填写内容会保留。\n\n"
            "自定义过滤词：每行填写一个词，命中的整条字幕都不朗读。重复词和空白行会在保存时整理。"
            "过滤词、剧情弹幕的过滤优先于 OS 提示。\n\n"
            "方案管理：最多保存十套方案；前两套可恢复默认。新增后可直接编辑方案名称。"
            "切换方案保留本次编辑；保存会保存全部方案，取消或按 Esc 不保存。"
            "旧配置的完整朗读选项会在保存后改为只读角色名。"
        )
        dialog = MediaDetailDialog(self, "字幕过滤规则", content, "规则说明")
        try:
            dialog.content_box.SetFocus()
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _on_slot_changed(self, _event: wx.CommandEvent) -> None:
        self._store_current_slot()
        self.current_slot = self.preset_choice.GetSelection()
        self._load_slot(self.current_slot)
        self._update_preset_buttons()

    def _update_preset_buttons(self) -> None:
        self.add_button.Enable(len(self.presets) < 10)
        self.restore_default_button.Show(self.current_slot < 2)
        self.panel.Layout()

    def _add_preset(self, _event: wx.CommandEvent) -> None:
        if len(self.presets) >= 10:
            return
        self._store_current_slot()
        slot = len(self.presets)
        preset = SubtitleFilterPreset(f"方案 {(slot + 1) % 10}")
        self.presets.append(preset)
        self.preset_choice.Append(f"{(slot + 1) % 10}：{preset.name}")
        self.preset_choice.SetSelection(slot)
        self.current_slot = slot
        self._load_slot(slot)
        self._update_preset_buttons()
        self.preset_name.SetFocus()
        self.preset_name.SelectAll()

    def _restore_defaults(self, _event: wx.CommandEvent) -> None:
        if self.current_slot >= 2:
            return
        preset = default_filter_presets()[self.current_slot]
        self.presets[self.current_slot] = preset
        self.preset_choice.SetString(self.current_slot,
                                     f"{self.current_slot + 1}：{preset.name}")
        self._load_slot(self.current_slot)
        self.preset_choice.SetFocus()

    def get_configuration(self) -> tuple[tuple[SubtitleFilterPreset, ...], int]:
        self._store_current_slot()
        return tuple(self.presets), self.current_slot


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
    INITIAL_SUBTITLE_CATCHUP_SECONDS = 30.0

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
        self.playback_rate = 1.0
        self.message = ""
        self.on_danmaku_due: Callable[[DanmakuItem], None] | None = None
        self.on_subtitle_due: Callable[[DanmakuItem], None] | None = None
        self.should_read_subtitle: Callable[[DanmakuItem], bool] | None = None
        self.pending_loaded_subtitle: DanmakuItem | None = None
        self.last_tick = time.monotonic()
        self.font = wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def reset(self, message: str = "") -> None:
        self.items = []
        self.pending_loaded_subtitle = None
        self.active = []
        self.next_index = 0
        self.next_lane = 0
        self.position = 0.0
        self.paused = True
        self.playback_rate = 1.0
        self.message = message
        self.last_tick = time.monotonic()
        self.timer.Start(self.FRAME_MS)
        self._render_frame()

    def set_items(self, items: list[DanmakuItem]) -> None:
        self.items = sorted(items, key=lambda item: item.time)
        self.active = []
        self.next_index = self._first_index_at_or_after(self.position)
        self.pending_loaded_subtitle = None
        # Subtitles may arrive after audio has started. Catch up to the most
        # recent caption once, but never replay a long-stale opening scene.
        if 0 < self.position <= self.INITIAL_SUBTITLE_CATCHUP_SECONDS:
            for index in range(self.next_index - 1, -1, -1):
                candidate = self.items[index]
                if candidate.mode == DANMAKU_MODE_SUBTITLE:
                    if self.position - candidate.time <= self.INITIAL_SUBTITLE_CATCHUP_SECONDS:
                        self.pending_loaded_subtitle = candidate
                    break
        self.next_lane = 0
        self.message = "" if self.items else "暂无弹幕"
        self._render_frame()

    def set_error(self, message: str) -> None:
        self.items = []
        self.pending_loaded_subtitle = None
        self.active = []
        self.message = f"弹幕加载失败: {message or '未知错误'}"
        self._render_frame()

    def set_paused(self, paused: bool) -> None:
        self._advance_position()
        self.paused = paused
        self.last_tick = time.monotonic()
        self._render_frame()

    def set_playback_rate(self, rate: float) -> None:
        self._advance_position()
        self.playback_rate = self._normalised_playback_rate(rate)
        self.last_tick = time.monotonic()

    def current_position(self) -> float:
        self._advance_position()
        return self.position

    def sync_position(self, seconds: float, paused: bool, playback_rate: float | None = None) -> None:
        target = max(0.0, float(seconds))
        self._advance_position()
        if playback_rate is not None:
            self.playback_rate = self._normalised_playback_rate(playback_rate)
        if abs(self.position - target) > 0.75:
            self.position = target
            self.active = []
            self.next_index = self._first_index_at_or_after(self.position)
            self.pending_loaded_subtitle = None
            self.next_lane = 0
        self.paused = paused
        self.last_tick = time.monotonic()
        self._render_frame()

    def seek(self, seconds: int) -> None:
        self.position = max(0.0, self.position + float(seconds))
        self.active = []
        self.next_index = self._first_index_at_or_after(self.position)
        self.pending_loaded_subtitle = None
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
            self.position += max(0.0, now - self.last_tick) * self.playback_rate
        self.last_tick = now

    @staticmethod
    def _normalised_playback_rate(rate: float) -> float:
        try:
            value = float(rate)
        except (TypeError, ValueError):
            return 1.0
        return value if value > 0 else 1.0

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
                if first_subtitle is None and (
                    self.should_read_subtitle is None or self.should_read_subtitle(item)
                ):
                    first_subtitle = item
            elif first_danmaku is None:
                first_danmaku = item
            self._spawn_item(item)
            self.next_index += 1
        pending = self.pending_loaded_subtitle
        self.pending_loaded_subtitle = None
        if first_subtitle is None and pending is not None and now - pending.time <= self.INITIAL_SUBTITLE_CATCHUP_SECONDS:
            if self.should_read_subtitle is None or self.should_read_subtitle(pending):
                first_subtitle = pending
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


class SubtitleJumpDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, items: list[DanmakuItem], current_seconds: float) -> None:
        super().__init__(parent, title="按字幕跳转", size=(760, 480),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        # Keep actual (including fractional) timestamps; audience comments
        # and filtering/reading preferences do not belong in this list.
        self.items = sorted(
            (item for item in items if item.mode == DANMAKU_MODE_SUBTITLE
             and item.text.strip() and math.isfinite(item.time) and item.time >= 0),
            key=lambda item: item.time,
        )
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(wx.StaticText(self, label="选择字幕后按回车跳转到对应时间"), 0, wx.ALL, 10)
        self.subtitle_list = wx.ListBox(self, choices=[
            f"{item.text}，{self._format_timestamp(item.time)}" for item in self.items
        ], name="字幕列表，选择后按回车跳转")
        root.Add(self.subtitle_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 10)
        self.FindWindow(wx.ID_OK).SetLabel("跳转")
        self.FindWindow(wx.ID_OK).Enable(bool(self.items))
        self.SetSizer(root)
        if self.items:
            selection = 0
            for index, item in enumerate(self.items):
                if item.time > current_seconds:
                    break
                selection = index
            self.subtitle_list.SetSelection(selection)
        self.subtitle_list.SetFocus()
        self.subtitle_list.Bind(wx.EVT_LISTBOX_DCLICK, self._accept)
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        minutes, milliseconds = divmod(round(seconds * 1000), 60000)
        return f"{minutes}分{milliseconds / 1000:06.3f}".rstrip("0").rstrip(".") + "秒"

    def _on_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self.FindFocus() is self.subtitle_list:
            self._accept(event)
            return
        event.Skip()

    def _accept(self, _event: wx.Event) -> None:
        if self.subtitle_list.GetSelection() != wx.NOT_FOUND:
            self.EndModal(wx.ID_OK)

    def selected_seconds(self) -> float:
        return self.items[self.subtitle_list.GetSelection()].time


class JumpTimeDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, prompt: str, total_seconds: float | None,
                 current_seconds: float, subtitle_items: Callable[[], list[DanmakuItem]]) -> None:
        super().__init__(parent, title="跳转进度", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.total_seconds = total_seconds
        self.current_seconds = current_seconds
        self.subtitle_items = subtitle_items
        self.seconds: float = 0
        self._range_warning = ""
        root = wx.BoxSizer(wx.VERTICAL)
        self.prompt_text = wx.StaticText(self, label=prompt)
        root.Add(self.prompt_text, 0, wx.ALL, 12)
        self.time_input = wx.TextCtrl(self, name="跳转时间，分.秒", style=wx.TE_PROCESS_ENTER)
        root.Add(self.time_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        self.range_message = wx.StaticText(self, label="", size=(450, 44))
        root.Add(self.range_message, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.screen_reader = ScreenReaderAnnouncer(self.range_message, native_only=True)
        self.subtitle_button = wx.Button(self, label="按字幕跳转")
        root.Add(self.subtitle_button, 0, wx.ALL, 12)
        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 12)
        self.FindWindow(wx.ID_OK).SetLabel("跳转")
        self.SetSizerAndFit(root)
        self.SetMinSize(self.GetSize())
        self.time_input.SetFocus()
        self.Bind(wx.EVT_BUTTON, self._accept_time, id=wx.ID_OK)
        self.time_input.Bind(wx.EVT_TEXT_ENTER, self._accept_time)
        self.time_input.Bind(wx.EVT_TEXT, self._on_time_changed)
        self.subtitle_button.Bind(wx.EVT_BUTTON, self._choose_subtitle)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        if event.GetEventObject() is self:
            self.screen_reader.close()
        event.Skip()

    def set_total_seconds(self, seconds: float) -> None:
        self.total_seconds = seconds
        current = PlaybackFrame._format_jump_time(min(self.current_seconds, seconds))
        total = PlaybackFrame._format_jump_time(seconds)
        self.prompt_text.SetLabel(f"请输入跳转时间（分.秒，当前{current}，共{total}）")
        self.GetSizer().Fit(self)
        self._update_range_warning()

    def _on_time_changed(self, _event: wx.Event) -> None:
        self._update_range_warning()

    def _update_range_warning(self) -> None:
        message = ""
        value = self.time_input.GetValue().strip()
        # A trailing dot is a normal intermediate input, not a new error.
        if value.endswith(".") and value.count(".") == 1:
            value = value[:-1]
        try:
            seconds = PlaybackFrame._parse_jump_time(value)
        except ValueError:
            pass  # Format errors are only reported on submission.
        else:
            if self.total_seconds is not None and seconds >= self.total_seconds:
                message = PlaybackFrame._jump_range_message(self.total_seconds)
        self.FindWindow(wx.ID_OK).Enable(not bool(message))
        if message == self._range_warning:
            return
        self._range_warning = message
        self.range_message.SetLabel(message)
        self.range_message.SetName(message)
        if message:
            self.screen_reader.announce(message)

    def _accept_time(self, _event: wx.Event) -> None:
        try:
            seconds = PlaybackFrame._parse_jump_time(self.time_input.GetValue())
        except ValueError:
            wx.MessageBox("请输入分.秒格式的时间", "时间格式错误", wx.OK | wx.ICON_WARNING, self)
            self.time_input.SetFocus()
            return
        if self.total_seconds is not None and seconds >= self.total_seconds:
            self._update_range_warning()
            return
        self.seconds = seconds
        self.EndModal(wx.ID_OK)

    def _choose_subtitle(self, _event: wx.Event) -> None:
        dialog = SubtitleJumpDialog(self, self.subtitle_items(), self.current_seconds)
        try:
            if not dialog.items:
                wx.MessageBox("当前暂无字幕可供跳转。如果字幕正在加载，请稍后重试。",
                              "按字幕跳转", wx.OK | wx.ICON_INFORMATION, self)
                return
            if dialog.ShowModal() != wx.ID_OK:
                return
            self.seconds = dialog.selected_seconds()
        finally:
            dialog.Destroy()
            self.subtitle_button.SetFocus()
        self.EndModal(wx.ID_OK)


class PlaybackFrame(wx.Frame):
    SEEK_SECONDS = 5
    PLAYBACK_RATES = (0.5, 1.0, 1.25, 1.5, 1.75, 2.0)
    MIN_PLAYBACK_RATE = 0.5
    MAX_PLAYBACK_RATE = 2.0
    FINISHED_EPSILON_SECONDS = 0.4

    def __init__(
        self,
        parent: wx.Window,
        api: MaoerApi,
        player: HiddenBrowserPlayer,
        on_closed: Callable[["PlaybackFrame"], None],
        on_finished: Callable[["PlaybackFrame", PlaybackInfo], None],
        read_danmaku_default: bool = False,
        read_subtitle_default: bool = False,
        subtitle_filter_presets: tuple[SubtitleFilterPreset, ...] | None = None,
        subtitle_filter_slot: int = 0,
        on_filter_slot_changed: Callable[[int], bool] | None = None,
        on_filter_rules: Callable[[wx.Window], None] | None = None,
        on_cycle_output: Callable[[int, Callable], None] | None = None,
    ) -> None:
        super().__init__(parent, title="", size=(760, 480))
        self.api = api
        self.player = player
        self.on_closed = on_closed
        self.on_finished = on_finished
        self.playback: PlaybackInfo | None = None
        self.load_generation = 0
        self.read_danmaku_default = read_danmaku_default
        self.read_subtitle_default = read_subtitle_default
        self.read_danmaku_enabled = read_danmaku_default
        self.read_subtitle_enabled = read_subtitle_default
        self.subtitle_filter_enabled = False
        self.subtitle_filter_presets = subtitle_filter_presets or default_filter_presets()
        self.subtitle_filter_slot = subtitle_filter_slot if 0 <= subtitle_filter_slot < len(self.subtitle_filter_presets) else 0
        self.subtitle_filter_rules = self.subtitle_filter_presets[self.subtitle_filter_slot].rules
        self.book_filter_last_role: str | None = None
        self.book_filter_context_roles: dict[int, str] = {}
        self.subtitle_os_items: set[int] = set()
        self.on_filter_slot_changed = on_filter_slot_changed
        self.on_filter_rules = on_filter_rules
        self.on_cycle_output = on_cycle_output
        self.output_change_generation = 0
        self.time_announcement_generation = 0
        self.rate_change_generation = 0
        self.playback_rate = 1.0
        self.requested_playback_rate = 1.0
        self.finish_notified = False
        self.last_mouse_context_menu_at = 0.0

        root = wx.BoxSizer(wx.VERTICAL)
        self.danmaku_canvas = DanmakuCanvas(self)
        self.danmaku_canvas.on_danmaku_due = self._on_danmaku_due
        self.danmaku_canvas.on_subtitle_due = self._on_subtitle_due
        self.danmaku_canvas.should_read_subtitle = self._should_read_subtitle
        root.Add(self.danmaku_canvas, 1, wx.EXPAND)
        self.SetSizer(root)
        self.live_region = wx.StaticText(self, label="", pos=(0, 0), size=(1, 1))
        self.live_region.SetForegroundColour(wx.BLACK)
        self.live_region.SetBackgroundColour(wx.BLACK)
        self.live_region.SetName("字幕朗读")
        # Subtitle and danmaku content share direct speech; controls use native notifications.
        self.screen_reader = ScreenReaderAnnouncer(self.live_region)
        self.status_live_region = wx.StaticText(self, label="", pos=(1, 0), size=(1, 1))
        self.status_live_region.SetName("播放提示")
        self.status_live_region.SetForegroundColour(wx.BLACK)
        self.status_live_region.SetBackgroundColour(wx.BLACK)
        self.status_reader = ScreenReaderAnnouncer(self.status_live_region, native_only=True)

        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_CONTEXT_MENU, self.on_playback_context_menu)
        self.danmaku_canvas.Bind(wx.EVT_CONTEXT_MENU, self.on_playback_context_menu)
        self.danmaku_canvas.bitmap_view.Bind(wx.EVT_CONTEXT_MENU, self.on_playback_context_menu)
        self.danmaku_canvas.Bind(wx.EVT_RIGHT_UP, self.on_playback_right_up)
        self.danmaku_canvas.bitmap_view.Bind(wx.EVT_RIGHT_UP, self.on_playback_right_up)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def play(self, playback: PlaybackInfo) -> None:
        if self.playback is None or self.playback.sound_id != playback.sound_id:
            self.read_danmaku_enabled = self.read_danmaku_default
            self.read_subtitle_enabled = self.read_subtitle_default
            self.subtitle_filter_enabled = False
            self.book_filter_last_role = None
            self.book_filter_context_roles = {}
            self.subtitle_os_items = set()
        self.playback = playback
        self.load_generation += 1
        self.rate_change_generation += 1
        generation = self.load_generation
        self.SetTitle(playback.title)
        self.playback_rate = 1.0
        self.requested_playback_rate = 1.0
        self.finish_notified = False
        self.danmaku_canvas.reset("正在加载弹幕...")
        self.danmaku_canvas.set_playback_rate(self.playback_rate)
        self.player.play(playback)
        self._load_danmaku(playback.sound_id, playback.subtitle_url, generation)
        wx.CallLater(500, self._sync_playback_status, generation)
        wx.CallAfter(self.SetFocus)

    def _load_danmaku(self, sound_id: int, subtitle_url: str, generation: int) -> None:
        def runner() -> None:
            try:
                items = self.api.sound_danmaku(sound_id, subtitle_url=subtitle_url)
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
        marked = mark_dialogue_continuations(items)
        active_role = ""
        context_roles: dict[int, str] = {}
        os_items: set[int] = set()
        os_by_submitter: dict[str, bool] = {}
        for item in sorted(marked, key=lambda entry: entry.time):
            if item.mode != DANMAKU_MODE_SUBTITLE:
                continue
            role = self._book_subtitle_context_role(item)
            if role is not None:
                active_role = role
            context_roles[id(item)] = active_role
            # Use only the selected track, including its role/OS boundaries.
            text = item.text.strip()
            explicit_os = SUBTITLE_OS_MARKER.search(text) or self._subtitle_os_role(item)
            if explicit_os:
                os_by_submitter[item.user_id] = True
                os_items.add(id(item))
            elif role is not None:
                # An explicit non-OS label ends the monologue, even when
                # the same actor resumes speaking. Narration/credits end
                # outstanding XML contributor streams as well.
                if role in NON_DIALOGUE_SUBTITLE_ROLES:
                    os_by_submitter.clear()
                os_by_submitter[item.user_id] = False
            elif not NON_DIALOGUE_SUBTITLE_CONTENT.fullmatch(text) and os_by_submitter.get(item.user_id, False):
                # Do not use the short dialogue-continuation timeout here:
                # OS prose can span multiple longer captions or a seek.
                os_items.add(id(item))
        self.book_filter_context_roles = context_roles
        self.subtitle_os_items = os_items
        self.danmaku_canvas.set_items(marked)

    def _set_danmaku_failed(self, generation: int, message: str) -> None:
        if generation != self.load_generation:
            return
        self.danmaku_canvas.set_error(message)

    def on_char_hook(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        try:
            if key == wx.WXK_MENU or (key == wx.WXK_F10 and event.ShiftDown()):
                self._show_playback_menu(wx.DefaultPosition)
                return
            if key == wx.WXK_F9 and not (event.ControlDown() or event.AltDown()):
                self._cycle_output_device(-1 if event.ShiftDown() else 1)
                return
            if ord("0") <= key <= ord("9") and not (
                event.ControlDown() or event.AltDown() or event.ShiftDown()
            ):
                self._select_subtitle_filter_slot(9 if key == ord("0") else key - ord("1"))
                return
            if key in (ord("D"), ord("d")):
                self._toggle_danmaku_reader()
                return
            if key == wx.WXK_CONTROL_F or (key in (ord("F"), ord("f")) and event.ControlDown()):
                self._toggle_subtitle_filter_mode()
                return
            if key in (ord("F"), ord("f")):
                self._toggle_subtitle_reader()
                return
            if key in (ord("T"), ord("t")):
                self._announce_playback_time()
                return
            if key in (ord("J"), ord("j")):
                self._prompt_jump_to_time()
                return
            if key in (ord("R"), ord("r")) and not (
                event.ControlDown() or event.AltDown() or event.ShiftDown()
            ):
                if self.on_filter_rules is not None:
                    self.on_filter_rules(self)
                    wx.CallAfter(self.SetFocus)
                return
            if key in (ord("C"), ord("c")):
                self._change_playback_rate(1)
                return
            if key in (ord("X"), ord("x")):
                self._change_playback_rate(-1)
                return
            if key in (ord("Z"), ord("z")):
                self._set_playback_rate(1.0)
                return
            if key == wx.WXK_SPACE:
                paused = self.player.toggle_pause()
                self.danmaku_canvas.set_paused(paused)
                return
            if key == wx.WXK_UP:
                self.player.volume_up()
                return
            if key == wx.WXK_DOWN:
                self.player.volume_down()
                return
            if key == wx.WXK_RIGHT:
                self._seek_relative(self.SEEK_SECONDS)
                return
            if key == wx.WXK_LEFT:
                self._seek_relative(-self.SEEK_SECONDS)
                return
        except PlayerUnavailable as exc:
            self._set_parent_status("操作失败")
            wx.MessageBox(str(exc), "错误", wx.OK | wx.ICON_ERROR, self)
            return
        event.Skip()

    def _cycle_output_device(self, direction: int) -> None:
        if self.on_cycle_output is None:
            return
        self.output_change_generation += 1
        generation = self.output_change_generation
        self.on_cycle_output(direction, lambda result: self._output_device_changed(generation, result))

    def _output_device_changed(self, generation: int, result: dict) -> None:
        if not self or generation != self.output_change_generation:
            return
        message = (result["name"] if result.get("ok")
                   else f"切换输出设备失败：{result.get('error', '未知错误')}")
        self._announce_status(message)

    def on_playback_right_up(self, event: wx.MouseEvent) -> None:
        screen_position = event.GetEventObject().ClientToScreen(event.GetPosition())
        self.last_mouse_context_menu_at = time.monotonic()
        self._show_playback_menu(self.ScreenToClient(screen_position))
        self.last_mouse_context_menu_at = time.monotonic()

    def on_playback_context_menu(self, event: wx.ContextMenuEvent) -> None:
        if time.monotonic() - self.last_mouse_context_menu_at < 0.35:
            return
        screen_position = event.GetPosition()
        position = wx.DefaultPosition if screen_position == wx.DefaultPosition else self.ScreenToClient(screen_position)
        self._show_playback_menu(position)

    def _show_playback_menu(self, position: wx.Point) -> None:
        menu = wx.Menu()
        actions: dict[int, Callable[[], None]] = {}

        def add_action(label: str, action: Callable[[], None]) -> None:
            item_id = wx.NewIdRef()
            menu.Append(item_id, label)
            actions[int(item_id)] = action

        add_action(f"快退 {self.SEEK_SECONDS} 秒", lambda: self._seek_relative(-self.SEEK_SECONDS))
        add_action(f"快进 {self.SEEK_SECONDS} 秒", lambda: self._seek_relative(self.SEEK_SECONDS))
        add_action("跳转时间…", self._prompt_jump_to_time)
        speed_menu = wx.Menu()
        selected_rate = self._clamp_playback_rate(self.requested_playback_rate)
        for rate in self.PLAYBACK_RATES:
            speed_id = wx.NewIdRef()
            entry = speed_menu.AppendRadioItem(speed_id, f"{rate:g} 倍")
            if rate == selected_rate:
                entry.Check(True)
            actions[int(speed_id)] = lambda value=rate: self._set_playback_rate(value)
        menu.AppendSubMenu(speed_menu, "播放倍速")
        menu.AppendSeparator()

        subtitle_id = wx.NewIdRef()
        menu.AppendCheckItem(subtitle_id, "朗读字幕").Check(self.read_subtitle_enabled)
        actions[int(subtitle_id)] = self._toggle_subtitle_reader
        filter_id = wx.NewIdRef()
        menu.AppendCheckItem(filter_id, "过滤模式（实验性功能）").Check(self.subtitle_filter_enabled)
        actions[int(filter_id)] = self._toggle_subtitle_filter_mode
        presets_menu = wx.Menu()
        for slot, preset in enumerate(self.subtitle_filter_presets):
            preset_id = wx.NewIdRef()
            entry = presets_menu.AppendRadioItem(preset_id, f"{(slot + 1) % 10}：{preset.name}")
            if slot == self.subtitle_filter_slot:
                entry.Check(True)
            actions[int(preset_id)] = lambda selected=slot: self._select_subtitle_filter_slot(selected)
        menu.AppendSubMenu(presets_menu, "过滤方案")
        danmaku_id = wx.NewIdRef()
        menu.AppendCheckItem(danmaku_id, "朗读弹幕").Check(self.read_danmaku_enabled)
        actions[int(danmaku_id)] = self._toggle_danmaku_reader

        try:
            choice = self.GetPopupMenuSelectionFromUser(menu, position)
        finally:
            menu.Destroy()
        action = actions.get(choice)
        if action is not None:
            try:
                action()
            except PlayerUnavailable as exc:
                self._set_parent_status("操作失败")
                wx.MessageBox(str(exc), "错误", wx.OK | wx.ICON_ERROR, self)

    def _seek_relative(self, seconds: int) -> None:
        self.player.seek(seconds)
        self.danmaku_canvas.seek(seconds)
        self.book_filter_last_role = None

    def _toggle_danmaku_reader(self) -> None:
        self.read_danmaku_enabled = not self.read_danmaku_enabled
        message = "弹幕朗读已开启" if self.read_danmaku_enabled else "弹幕朗读已关闭"
        self._announce_status(message)

    def _toggle_subtitle_reader(self) -> None:
        self.read_subtitle_enabled = not self.read_subtitle_enabled
        self.subtitle_filter_enabled = False
        self.book_filter_last_role = None
        message = "字幕朗读已开启" if self.read_subtitle_enabled else "字幕朗读已关闭"
        self._announce_status(message)

    def _toggle_subtitle_filter_mode(self) -> None:
        if not self.read_subtitle_enabled:
            self.read_subtitle_enabled = True
            self.subtitle_filter_enabled = True
        else:
            self.subtitle_filter_enabled = not self.subtitle_filter_enabled
        self.book_filter_last_role = None
        message = "字幕过滤模式已开启" if self.subtitle_filter_enabled else "已恢复朗读全部字幕"
        self._announce_status(message)

    def _select_subtitle_filter_slot(self, slot: int) -> None:
        if not self.read_subtitle_enabled or not 0 <= slot < len(self.subtitle_filter_presets):
            return
        self.subtitle_filter_slot = slot
        preset = self.subtitle_filter_presets[slot]
        self.subtitle_filter_rules = preset.rules
        self.subtitle_filter_enabled = True
        self.book_filter_last_role = None
        saved = self.on_filter_slot_changed(slot) if self.on_filter_slot_changed is not None else True
        message = preset.name
        if saved is False:
            message += "，但本次选择未保存"
        self._announce_status(message)

    def _should_read_subtitle(self, item: DanmakuItem) -> bool:
        return bool(self._subtitle_text_to_read(item))

    @staticmethod
    def _book_subtitle_context_role(item: DanmakuItem) -> str | None:
        """Return an explicit book role; inline cues retain the previous role."""
        text = item.text.strip()
        if NON_DIALOGUE_SUBTITLE_CONTENT.fullmatch(text):
            return None
        match = SUBTITLE_ROLE_PREFIX.fullmatch(text)
        if not match:
            return "报幕" if NON_DIALOGUE_ANNOUNCEMENT_PREFIX.match(text) else None
        role = item.role.strip() or match.group(1).strip()
        if re.match(r"^报幕\s*[/／]", role):
            return "报幕"
        # A character can read the credits. Otherwise even parenthesized
        # content after a speaker label belongs to that speaker in book mode.
        if role not in NON_DIALOGUE_SUBTITLE_ROLES and NON_DIALOGUE_ANNOUNCEMENT_PREFIX.match(match.group(2)):
            return "报幕"
        return role

    @staticmethod
    def _subtitle_os_role(item: DanmakuItem) -> str:
        role = PlaybackFrame._book_subtitle_context_role(item) or ""
        return role if SUBTITLE_OS_ROLE_SUFFIX.search(role) else ""

    @staticmethod
    def _book_filter_role(item: DanmakuItem, rules: SubtitleFilterRules) -> str:
        role = PlaybackFrame._book_subtitle_context_role(item)
        if not role:
            return ""
        if role == "旁白" or role not in NON_DIALOGUE_SUBTITLE_ROLES:
            return role
        if rules.info_label_only and role in rules.info_labels:
            return role
        return ""

    def _subtitle_text_to_read(self, item: DanmakuItem) -> str:
        if not self.read_subtitle_enabled:
            return ""
        text = item.text.strip()
        if not self.subtitle_filter_enabled:
            return text
        rules = self.subtitle_filter_rules
        folded_text = text.casefold()
        if any(keyword.casefold() in folded_text for keyword in rules.keywords):
            return ""
        story_barrage = is_story_barrage_subtitle(item)
        if rules.speaker_transitions_only:
            context_role = self.book_filter_context_roles.get(id(item), "")
            if STORY_BARRAGE_SUBTITLE_ROLE.fullmatch(context_role):
                story_barrage = True
        if story_barrage and rules.story_barrage:
            return ""
        os_marker = SUBTITLE_OS_MARKER.search(text)
        os_role = self._subtitle_os_role(item)
        if os_marker or os_role or id(item) in getattr(self, "subtitle_os_items", ()):
            if not rules.os_body or not (os_marker or os_role):
                return ""
            if not os_marker:
                return os_role
            if rules.speaker_transitions_only:
                role = (self._book_filter_role(item, rules) or item.role.strip()
                        or self.book_filter_context_roles.get(id(item), "")
                        or self.book_filter_last_role or "")
                return f"{role}：{os_marker.group(0)}" if role else os_marker.group(0)
            prefix = text[:os_marker.end()].rstrip()
            if item.role.strip() and text == item.content.strip():
                return f"{item.role.strip()}：{prefix}"
            return prefix
        if story_barrage:
            return text
        if rules.speaker_transitions_only:
            if NON_DIALOGUE_SUBTITLE_CONTENT.fullmatch(text):
                # Scene/action prose is not another speaker. Preserve the
                # current speaker across it; only an announcement block
                # continues reading its body in this mode.
                return text if self.book_filter_context_roles.get(id(item)) == "报幕" else ""
            role = self._book_filter_role(item, rules)
            if role:
                return role if role != self.book_filter_last_role else ""
            if self._book_subtitle_context_role(item) == "报幕":
                return text
            if not SUBTITLE_ROLE_PREFIX.fullmatch(text):
                context_role = self.book_filter_context_roles.get(id(item), self.book_filter_last_role or "")
                if not context_role:
                    return text
                if STORY_BARRAGE_SUBTITLE_ROLE.fullmatch(context_role):
                    return "" if rules.story_barrage else text
                if (context_role in NON_DIALOGUE_SUBTITLE_ROLES and context_role != "旁白"
                        and not (rules.info_label_only and context_role in rules.info_labels)):
                    return text
                return context_role if context_role != self.book_filter_last_role else ""
        explicit_role = SUBTITLE_ROLE_PREFIX.fullmatch(text)
        role = item.role.strip() or (explicit_role.group(1).strip() if explicit_role else "")
        if rules.info_label_only and role in rules.info_labels and not NON_DIALOGUE_SUBTITLE_CONTENT.fullmatch(text):
            return role
        if is_character_dialogue_subtitle(item):
            if rules.dialogue_mode == "mute":
                return ""
            if rules.dialogue_mode == "role":
                # Inferred XML continuation lines belong to the same utterance;
                # the explicit first line already announced its speaker.
                if item.role.strip() and item.content.strip() == text and explicit_role is None:
                    return ""
                return role
        return text

    def _change_playback_rate(self, direction: int) -> None:
        current_rate = self._clamp_playback_rate(self.requested_playback_rate)
        index = self.PLAYBACK_RATES.index(current_rate)
        next_index = max(0, min(len(self.PLAYBACK_RATES) - 1, index + direction))
        self._set_playback_rate(self.PLAYBACK_RATES[next_index])

    def _set_playback_rate(self, rate: float) -> None:
        target_rate = self._clamp_playback_rate(rate)
        self.requested_playback_rate = target_rate
        self.rate_change_generation += 1
        generation = self.rate_change_generation
        self.player.set_playback_rate(
            target_rate,
            lambda status: self._set_playback_rate_done(generation, target_rate, status),
        )

    def _set_playback_rate_done(
        self,
        generation: int,
        target_rate: float,
        status: dict[str, object] | None,
    ) -> None:
        if generation != self.rate_change_generation:
            return
        if not status or not status.get("ok"):
            self.requested_playback_rate = self.playback_rate
            message = "当前播放器不支持倍速"
            self._announce_status(message)
            return

        actual_rate = self._positive_float(status.get("rate")) or target_rate
        self.playback_rate = self._clamp_playback_rate(actual_rate)
        self.requested_playback_rate = self.playback_rate
        self.danmaku_canvas.set_playback_rate(self.playback_rate)
        message = self._format_playback_rate(self.playback_rate)
        self._announce_status(message)

    def _on_danmaku_due(self, item: DanmakuItem) -> None:
        if not self.read_danmaku_enabled:
            return
        text = item.text.strip()
        if text:
            self.screen_reader.announce(text)

    def _on_subtitle_due(self, item: DanmakuItem) -> None:
        text = self._subtitle_text_to_read(item)
        if text:
            if self.subtitle_filter_enabled and self.subtitle_filter_rules.speaker_transitions_only:
                role = (self._book_filter_role(item, self.subtitle_filter_rules)
                        or self.book_filter_context_roles.get(id(item), "")
                        or item.role.strip())
                if role and (text == role or SUBTITLE_OS_MARKER.search(item.text)):
                    self.book_filter_last_role = role
                elif self._book_subtitle_context_role(item) is not None:
                    self.book_filter_last_role = None
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
            self.time_announcement_generation += 1
            self._announce_status("没有获取到时长")
            return
        self.time_announcement_generation += 1
        self._announce_time(current_seconds, total_seconds)

    def _announce_playback_time_fallback(self, generation: int) -> None:
        if generation != self.time_announcement_generation:
            return
        current_seconds, total_seconds = self._playback_times_from_status(None)
        if total_seconds is None:
            self.time_announcement_generation += 1
            self._announce_status("没有获取到时长")
            return
        self.time_announcement_generation += 1
        self._announce_time(current_seconds, total_seconds)

    def _announce_time(self, current_seconds: float, total_seconds: float) -> None:
        current_seconds = min(current_seconds, total_seconds) if total_seconds else current_seconds
        message = f"{self._format_spoken_time(current_seconds)}/{self._format_spoken_time(total_seconds)}"
        self._announce_status(message)

    @staticmethod
    def _parse_jump_time(value: str) -> int:
        match = re.fullmatch(r"\s*(\d+)(?:\.(\d{1,2}))?\s*", value)
        if match is None:
            raise ValueError("请输入分.秒格式的时间")
        return int(match.group(1)) * 60 + int(match.group(2) or 0)

    @staticmethod
    def _format_jump_time(seconds: float) -> str:
        minutes, remainder = divmod(max(0, int(round(seconds))), 60)
        return f"{minutes}分{remainder:02d}秒"

    def _prompt_jump_to_time(self) -> None:
        if self.playback is None:
            self._announce_status("当前没有正在播放的音频")
            return
        current_seconds, total_seconds = self._playback_times_from_status(None)
        if total_seconds is not None:
            current_seconds = min(current_seconds, total_seconds)
        total_text = self._format_jump_time(total_seconds) if total_seconds is not None else "未知"
        prompt = f"请输入跳转时间（分.秒，当前{self._format_jump_time(current_seconds)}，共{total_text}）"
        generation = self.load_generation
        dialog = JumpTimeDialog(self, prompt, total_seconds, current_seconds,
                                lambda: self.danmaku_canvas.items)
        try:
            if total_seconds is None:
                self.player.status(lambda status: self._jump_dialog_duration_ready(generation, dialog, status))
            if dialog.ShowModal() != wx.ID_OK:
                return
            seconds = dialog.seconds
        finally:
            dialog.Destroy()
            wx.CallAfter(self.SetFocus)
        if generation != self.load_generation:
            return
        self.player.status(lambda status: self._jump_to_time_ready(generation, seconds, status))

    def _jump_dialog_duration_ready(self, generation: int, dialog: JumpTimeDialog,
                                    status: dict[str, object] | None) -> None:
        if generation != self.load_generation or not dialog or dialog.IsBeingDeleted():
            return
        if status and status.get("ok"):
            duration = self._positive_float(status.get("duration"))
            if duration and math.isfinite(duration):
                dialog.set_total_seconds(duration)

    @staticmethod
    def _jump_range_message(duration: float) -> str:
        return f"输入超出范围，当前音频总时长为{PlaybackFrame._format_jump_time(duration)}"

    def _jump_to_time_ready(
        self, generation: int, seconds: float, status: dict[str, object] | None,
    ) -> None:
        if generation != self.load_generation or self.playback is None:
            return
        if not status or not status.get("ok"):
            self._announce_status("播放器尚未就绪，无法跳转")
            return
        duration = self._positive_float(status.get("duration"))
        if duration is None and self.playback.duration_ms:
            duration = self.playback.duration_ms / 1000.0
        if duration and seconds >= duration:
            message = self._jump_range_message(duration)
            self._set_parent_status(message)
            wx.MessageBox(message, "输入超出范围", wx.OK | wx.ICON_WARNING, self)
            return
        self.player.seek_to(
            seconds,
            lambda result: self._jump_to_time_done(generation, seconds, result),
            resume=True,
        )

    def _jump_to_time_done(
        self, generation: int, seconds: float, result: dict[str, object] | None,
    ) -> None:
        if generation != self.load_generation or self.playback is None:
            return
        if not result or not result.get("ok"):
            self._announce_status("跳转失败，请等播放器加载完成后再试")
            return
        position = self._positive_float(result.get("position"))
        self.danmaku_canvas.sync_position(
            seconds if position is None else position,
            bool(result.get("paused", False)),
            self.playback_rate,
        )
        self.book_filter_last_role = None
        self.time_announcement_generation += 1
        message = f"已跳转到{self._format_spoken_time(seconds)}"
        if result.get("resume_error"):
            message += "，但未能开始播放，请按空格重试"
        self._announce_status(message)

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
        rate_generation = self.rate_change_generation
        self.player.status(lambda status: self._sync_playback_status_done(generation, rate_generation, status))

    def _sync_playback_status_done(
        self,
        generation: int,
        rate_generation: int,
        status: dict[str, object] | None,
    ) -> None:
        if generation != self.load_generation or self.playback is None:
            return
        if status and status.get("ok"):
            position = self._positive_float(status.get("position"))
            duration = self._positive_float(status.get("duration"))
            paused = bool(status.get("paused"))
            playback_rate = None
            if rate_generation == self.rate_change_generation:
                playback_rate = self._positive_float(status.get("rate"))
            if playback_rate is not None:
                self.playback_rate = self._clamp_playback_rate(playback_rate)
                self.requested_playback_rate = self.playback_rate
            if self._status_is_finished(status, position, duration):
                finish_position = duration
                if finish_position is None and self.playback and self.playback.duration_ms:
                    finish_position = self.playback.duration_ms / 1000.0
                if finish_position is not None:
                    self.danmaku_canvas.sync_position(finish_position, True, self.playback_rate)
                else:
                    self.danmaku_canvas.set_paused(True)
                self._notify_playback_finished()
                return
            if position is not None:
                if position > 0 or paused or self.danmaku_canvas.position < 0.75:
                    self.danmaku_canvas.sync_position(position, paused, self.playback_rate)
                else:
                    self.danmaku_canvas.set_paused(False)
                    self.danmaku_canvas.set_playback_rate(self.playback_rate)
            elif playback_rate is not None:
                self.danmaku_canvas.set_playback_rate(self.playback_rate)
        wx.CallLater(1000, self._sync_playback_status, generation)

    def on_close(self, event: wx.CloseEvent) -> None:
        self.load_generation += 1
        self.rate_change_generation += 1
        self.danmaku_canvas.stop()
        self._close_readers()
        self.player.stop()
        self.on_closed(self)
        event.Skip()

    def _status_is_finished(
        self,
        status: dict[str, object],
        position: float | None,
        duration: float | None,
    ) -> bool:
        if status.get("ended"):
            return True
        if position is None:
            return False
        if duration is None and self.playback and self.playback.duration_ms:
            duration = self.playback.duration_ms / 1000.0
        if duration is None or duration <= 0:
            return False
        return position >= max(0.0, duration - self.FINISHED_EPSILON_SECONDS)

    def _notify_playback_finished(self) -> None:
        if self.finish_notified or self.playback is None:
            return
        self.finish_notified = True
        self.on_finished(self, self.playback)

    def _close_readers(self) -> None:
        self.screen_reader.close()
        self.status_reader.close()

    def _announce_status(self, message: str) -> None:
        """Native reader notification, never fall back to subtitle speech."""
        self._set_parent_status(message)
        self.status_reader.announce(message)

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

    @classmethod
    def _clamp_playback_rate(cls, rate: float) -> float:
        try:
            value = float(rate)
        except (TypeError, ValueError):
            value = 1.0
        return min(cls.PLAYBACK_RATES, key=lambda candidate: abs(candidate - value))

    @staticmethod
    def _format_playback_rate(rate: float) -> str:
        if abs(rate - 1.0) < 0.05:
            return "正常速度"
        text = f"{rate:g}"
        return f"{text}倍速"

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
        super().__init__(None, title=APP_TITLE, size=(940, 620))
        self.api = MaoerApi()
        self.browser_player = HiddenBrowserPlayer(self, cookie=self.api.cookie_header)
        self.active_player: HiddenBrowserPlayer | None = None
        self.player_frame: PlaybackFrame | None = None
        self.settings: AppSettings = load_settings()
        self.audio_output_router = AudioOutputRouter(self.settings.output_device_id)
        self.output_devices = (SYSTEM_OUTPUT,)
        self._output_device_error = ""
        self._audio_poll_generation = 0
        self.items: list[MediaItem] = []
        self.current_title = ""
        self.page_state: PageState | None = None
        self.hide_list_detail_column = False
        self.navigation_stack: list[NavigationState] = []
        self.homepage_state: NavigationState | None = None
        self.comment_windows: list[CommentsFrame] = []
        self.last_mouse_context_menu_at = 0.0
        self.account_logged_in = bool(self.api.cookie_header)
        self.current_playback_key: tuple[str, int] | None = None
        self._vip_catalog_request: object | None = None
        self._content_feature_request: object | None = None
        self._opened_drama_id: int | None = None
        self._publisher_request: object = object()
        self._follow_status_cookie = self.api.cookie_header
        self._follow_status_cache: dict[int, bool] = {}
        self._follow_status_pending: dict[int, object] = {}

        self._build_ui()
        self._build_menu()
        self._bind_events()
        wx.CallAfter(self._refresh_output_devices)
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
        self.list.InsertColumn(1, "发布")

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

        list_header = wx.BoxSizer(wx.HORIZONTAL)
        list_header.Add(self.list_label, 0, wx.ALIGN_CENTER_VERTICAL)

        root.Add(search_row, 0, wx.EXPAND | wx.ALL, 10)
        root.Add(list_header, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(root)
        self.CreateStatusBar()

    def _build_menu(self) -> None:
        self.account_login_menu_id = wx.NewIdRef()
        self.account_info_menu_id = wx.NewIdRef()
        self.account_favorites_menu_id = wx.NewIdRef()
        self.account_subscriptions_menu_id = wx.NewIdRef()
        self.account_history_menu_id = wx.NewIdRef()
        self.account_following_menu_id = wx.NewIdRef()
        self.account_purchased_dramas_menu_id = wx.NewIdRef()
        self.account_logout_menu_id = wx.NewIdRef()
        self.account_exit_menu_id = wx.NewIdRef()
        self.content_categories_menu_id = wx.NewIdRef()
        self.content_books_menu_id = wx.NewIdRef()
        self.content_drama_index_menu_id = wx.NewIdRef()
        self.content_drama_timeline_menu_id = wx.NewIdRef()
        self.content_drama_following_menu_id = wx.NewIdRef()
        self.content_drama_all_menu_id = wx.NewIdRef()
        self.content_drama_finished_menu_id = wx.NewIdRef()
        self.content_drama_ongoing_menu_id = wx.NewIdRef()
        self.content_weekly_menu_id = wx.NewIdRef()
        self.vip_free_dramas_menu_id = wx.NewIdRef()
        self.vip_discount_dramas_menu_id = wx.NewIdRef()
        self.settings_startup_sound_menu_id = wx.NewIdRef()
        self.settings_subtitle_menu_id = wx.NewIdRef()
        self.settings_danmaku_menu_id = wx.NewIdRef()
        self.settings_subtitle_filter_menu_id = wx.NewIdRef()
        self.help_hotkeys_menu_id = wx.NewIdRef()
        self.help_update_log_menu_id = wx.NewIdRef()
        self.help_about_menu_id = wx.NewIdRef()
        self.item_detail_shortcut_id = wx.NewIdRef()
        self.item_comments_shortcut_id = wx.NewIdRef()
        self.item_browser_shortcut_id = wx.NewIdRef()
        self._update_account_menu()

    def _update_account_menu(self) -> None:
        menu_bar = wx.MenuBar()

        account_menu = wx.Menu()
        if self.account_logged_in:
            account_menu.Append(self.account_info_menu_id, "我的信息(&I)")
            account_menu.Append(self.account_favorites_menu_id, "我的收藏(&F)")
            account_menu.Append(self.account_subscriptions_menu_id, "我的追剧(&S)")
            account_menu.Append(self.account_history_menu_id, "我的播放历史(&H)")
            account_menu.Append(self.account_following_menu_id, "我的关注(&G)")
            account_menu.Append(self.account_purchased_dramas_menu_id, "已购广播剧(&P)")
            account_menu.AppendSeparator()
            account_menu.Append(self.account_logout_menu_id, "退出登录(&O)")
        else:
            account_menu.Append(self.account_login_menu_id, "账号登录(&L)")
            account_menu.Append(self.account_history_menu_id, "我的播放历史(&H)")
            account_menu.Append(self.account_following_menu_id, "我的关注(&G)")
        account_menu.AppendSeparator()
        account_menu.Append(self.account_exit_menu_id, "退出程序(&Q)")
        menu_bar.Append(account_menu, "账号(&A)")

        content_menu = wx.Menu()
        content_menu.Append(self.content_categories_menu_id, "分类(&C)")
        content_menu.AppendSeparator()
        content_menu.Append(self.content_books_menu_id, "听书(&B)")
        drama_menu = wx.Menu()
        drama_menu.Append(self.content_drama_index_menu_id, "索引(&I)")
        drama_menu.Append(self.content_drama_timeline_menu_id, "时间表(&T)")
        drama_menu.Append(self.content_drama_following_menu_id, "我的追剧(&S)")
        drama_menu.AppendSeparator()
        drama_menu.Append(self.content_drama_all_menu_id, "查看全部(&A)")
        drama_menu.Append(self.content_drama_finished_menu_id, "完结(&F)")
        drama_menu.Append(self.content_drama_ongoing_menu_id, "未完结(&O)")
        content_menu.AppendSubMenu(drama_menu, "广播剧(&D)")
        content_menu.Append(self.content_weekly_menu_id, "精品周更(&W)")
        menu_bar.Append(content_menu, "内容(&C)")

        vip_menu = wx.Menu()
        vip_menu.Append(self.vip_free_dramas_menu_id, "会员限免剧(&F)")
        vip_menu.Append(self.vip_discount_dramas_menu_id, "会员折扣剧(&D)")
        menu_bar.Append(vip_menu, "会员(&V)")

        settings_menu = wx.Menu()
        settings_menu.AppendCheckItem(self.settings_startup_sound_menu_id, "播放启动音效(&M)").Check(
            self.settings.startup_sound
        )
        settings_menu.AppendSeparator()
        settings_menu.AppendCheckItem(self.settings_subtitle_menu_id, "默认朗读字幕(&F)").Check(
            self.settings.read_subtitle
        )
        settings_menu.AppendCheckItem(self.settings_danmaku_menu_id, "默认朗读弹幕(&D)").Check(
            self.settings.read_danmaku
        )
        settings_menu.AppendSeparator()
        settings_menu.Append(self.settings_subtitle_filter_menu_id, "字幕过滤规则（实验性功能）(&R)…")
        self.output_device_menu = wx.Menu()
        self._populate_output_device_menu()
        settings_menu.AppendSubMenu(self.output_device_menu, "默认播放设备(&O)")
        self.settings_menu = settings_menu
        menu_bar.Append(settings_menu, "设置(&S)")

        help_menu = wx.Menu()
        help_menu.Append(self.help_hotkeys_menu_id, "热键表(&H)")
        help_menu.Append(self.help_update_log_menu_id, "更新日志(&U)")
        help_menu.AppendSeparator()
        help_menu.Append(self.help_about_menu_id, "关于本程序(&A)")
        menu_bar.Append(help_menu, "帮助(&H)")

        self.SetMenuBar(menu_bar)

    def _bind_events(self) -> None:
        self.search_button.Bind(wx.EVT_BUTTON, self.on_search)
        self.search_box.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_item_activated)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_list_item_selected)
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
        self.Bind(wx.EVT_MENU, self.on_account_history, id=self.account_history_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_following, id=self.account_following_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_purchased_dramas, id=self.account_purchased_dramas_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_logout, id=self.account_logout_menu_id)
        self.Bind(wx.EVT_MENU, self.on_account_exit, id=self.account_exit_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_categories, id=self.content_categories_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_books, id=self.content_books_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_drama_index, id=self.content_drama_index_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_drama_timeline, id=self.content_drama_timeline_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_drama_following, id=self.content_drama_following_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_drama_all, id=self.content_drama_all_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_drama_finished, id=self.content_drama_finished_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_drama_ongoing, id=self.content_drama_ongoing_menu_id)
        self.Bind(wx.EVT_MENU, self.on_content_weekly, id=self.content_weekly_menu_id)
        self.Bind(wx.EVT_MENU, self.on_vip_free_dramas, id=self.vip_free_dramas_menu_id)
        self.Bind(wx.EVT_MENU, self.on_vip_discount_dramas, id=self.vip_discount_dramas_menu_id)
        self.Bind(wx.EVT_MENU, self.on_setting_changed, id=self.settings_startup_sound_menu_id)
        self.Bind(wx.EVT_MENU, self.on_setting_changed, id=self.settings_subtitle_menu_id)
        self.Bind(wx.EVT_MENU, self.on_setting_changed, id=self.settings_danmaku_menu_id)
        self.Bind(wx.EVT_MENU, self.on_subtitle_filter_rules, id=self.settings_subtitle_filter_menu_id)
        self.Bind(wx.EVT_MENU_OPEN, self._on_output_menu_open)
        self.Bind(wx.EVT_MENU, self.on_help_hotkeys, id=self.help_hotkeys_menu_id)
        self.Bind(wx.EVT_MENU, self.on_help_update_log, id=self.help_update_log_menu_id)
        self.Bind(wx.EVT_MENU, self.on_help_about, id=self.help_about_menu_id)
        self.Bind(wx.EVT_MENU, self.on_item_detail_shortcut, id=self.item_detail_shortcut_id)
        self.Bind(wx.EVT_MENU, self.on_item_comments_shortcut, id=self.item_comments_shortcut_id)
        self.Bind(wx.EVT_MENU, self.on_item_browser_shortcut, id=self.item_browser_shortcut_id)
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (modifiers, key, item_id)
            for key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
            for modifiers, item_id in (
                (wx.ACCEL_SHIFT, self.item_detail_shortcut_id),
                (wx.ACCEL_ALT, self.item_comments_shortcut_id),
                (wx.ACCEL_ALT | wx.ACCEL_SHIFT, self.item_browser_shortcut_id),
            )
        ]))
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

    def on_content_categories(self, _event: wx.Event) -> None:
        previous_state = self._account_list_previous_state()
        self._clear_items_for_loading("分类", focus_list=True, hide_detail_column=True)
        self._run_background(
            "正在加载分类...",
            self.api.content_categories,
            lambda items: self._enter_items(
                items,
                "分类",
                previous_state,
                focus_list=True,
                hide_detail_column=True,
            ),
        )

    def on_content_books(self, _event: wx.Event) -> None:
        self._open_content_feature("books", "听书")

    def on_content_weekly(self, _event: wx.Event) -> None:
        self._open_content_feature("weekly", "精品周更")

    def on_content_drama_index(self, _event: wx.Event) -> None:
        self._run_background(
            "正在加载广播剧索引...",
            self.api.drama_index_facets,
            self._show_drama_index_dialog,
        )

    def _show_drama_index_dialog(self, facets: list[tuple[str, list[tuple[int, str]]]]) -> None:
        dialog = wx.Dialog(self, title="广播剧索引", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(wx.StaticText(dialog, label="选择筛选条件，可组合使用："), 0, wx.ALL, 12)
        selectors: list[tuple[wx.Choice, list[tuple[int, str]]]] = []
        for label, options in facets:
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
            choice = wx.Choice(dialog, choices=[name for _, name in options])
            choice.SetName(label)
            choice.SetSelection(next((index for index, (option_id, _) in enumerate(options) if option_id == 0), 0))
            row.Add(choice, 1, wx.EXPAND)
            root.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            selectors.append((choice, options))
        buttons = dialog.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons is not None:
            root.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        dialog.SetSizerAndFit(root)
        dialog.SetMinSize((420, dialog.GetSize().height))
        if selectors:
            selectors[0][0].SetFocus()
        selected: list[tuple[int, str]] | None = None
        try:
            if dialog.ShowModal() == wx.ID_OK:
                selected = [options[choice.GetSelection()] for choice, options in selectors]
        finally:
            dialog.Destroy()
        if selected is None:
            return
        filters = "_".join(str(option_id) for option_id, _ in selected)
        labels = [name for option_id, name in selected if option_id != 0]
        title = "广播剧 · 索引" + ("：" + " / ".join(labels) if labels else "")
        self._open_content_feature(
            "drama_index_results",
            title,
            loader=lambda page: self.api.drama_filter_items(filters, page),
        )

    def on_content_drama_timeline(self, _event: wx.Event) -> None:
        self._open_content_feature("drama_timeline", "广播剧 · 时间表")

    def on_content_drama_following(self, _event: wx.Event) -> None:
        self._open_content_feature("drama_following", "广播剧 · 我的追剧")

    def on_content_drama_all(self, _event: wx.Event) -> None:
        self._open_content_feature("drama_all", "广播剧 · 查看全部")

    def on_content_drama_finished(self, _event: wx.Event) -> None:
        self._open_content_feature("drama_finished", "广播剧 · 完结")

    def on_content_drama_ongoing(self, _event: wx.Event) -> None:
        self._open_content_feature("drama_ongoing", "广播剧 · 未完结")

    def _open_content_feature(
        self,
        kind: str,
        title: str,
        loader: Callable[[int], list[MediaItem]] | None = None,
    ) -> None:
        previous_state = self._navigation_state_snapshot()
        previous_items = self.items
        request = self._content_feature_request = object()
        load_page = loader or (lambda page: self.api.content_feature_items(kind, page))

        def loaded(items: list[MediaItem]) -> None:
            if self._content_feature_request is not request or self.items is not previous_items:
                return
            self._enter_items(
                items,
                title,
                previous_state,
                focus_list=True,
                page_state=PageState(
                    1,
                    load_page,
                    has_more=bool(items) and kind not in {"weekly", "drama_timeline"},
                ),
            )

        self._run_background(
            f"正在加载{title}...",
            lambda: load_page(1),
            loaded,
        )

    def on_vip_free_dramas(self, _event: wx.Event) -> None:
        self._open_vip_dramas("free", "会员限免剧")

    def on_vip_discount_dramas(self, _event: wx.Event) -> None:
        self._open_vip_dramas("discount", "会员折扣剧")

    def _open_vip_dramas(self, kind: str, title: str) -> None:
        previous_state = self._navigation_state_snapshot()
        previous_items = self.items
        request = self._vip_catalog_request = object()

        def loaded(items: list[MediaItem]) -> None:
            if self._vip_catalog_request is not request or self.items is not previous_items:
                return
            self._enter_items(
                items,
                title,
                previous_state,
                focus_list=True,
                page_state=PageState(1, lambda page: self.api.vip_dramas(kind, page), has_more=bool(items)),
            )

        self._run_background(
            f"正在加载{title}...",
            lambda: self.api.vip_dramas(kind, 1),
            loaded,
        )

    def on_setting_changed(self, event: wx.CommandEvent) -> None:
        setting = {
            int(self.settings_startup_sound_menu_id): ("startup_sound", "启动音效"),
            int(self.settings_subtitle_menu_id): ("read_subtitle", "字幕朗读"),
            int(self.settings_danmaku_menu_id): ("read_danmaku", "弹幕朗读"),
        }.get(event.GetId())
        if setting is None:
            return
        name, label = setting
        enabled = event.IsChecked()
        updated = replace(self.settings, **{name: enabled})
        try:
            save_settings(updated)
        except OSError as exc:
            self.GetMenuBar().FindItemById(event.GetId()).Check(getattr(self.settings, name))
            self.show_error(f"保存设置失败：{exc}")
            return

        self.settings = updated
        if self.player_frame is not None:
            if name == "read_subtitle":
                self.player_frame.read_subtitle_default = enabled
                self.player_frame.read_subtitle_enabled = enabled
                self.player_frame.subtitle_filter_enabled = False
            elif name == "read_danmaku":
                self.player_frame.read_danmaku_default = enabled
                self.player_frame.read_danmaku_enabled = enabled
        self.SetStatusText(f"{label}已{'开启' if enabled else '关闭'}，设置已保存")

    def _refresh_output_devices(self) -> None:
        def done(result):
            if not self:
                return
            if result.get("ok"):
                self.output_devices = result["devices"]
                self._output_device_error = ""
            else:
                self._output_device_error = "播放设备获取失败，请重新打开菜单重试"
        self.audio_output_router.refresh(lambda result: wx.CallAfter(done, result))

    def _populate_output_device_menu(self) -> None:
        menu = self.output_device_menu
        for item_id in getattr(self, "_output_menu_ids", ()):
            self.Unbind(wx.EVT_MENU, id=item_id)
        self._output_menu_ids = []
        for item in menu.GetMenuItems():
            menu.DestroyItem(item)
        for device in self.output_devices:
            item = menu.AppendRadioItem(wx.ID_ANY, device.name.replace("&", "&&"))
            item.Check(device.id == self.settings.output_device_id)
            self._output_menu_ids.append(item.GetId())
            self.Bind(wx.EVT_MENU, lambda event, value=device.id: self._set_default_output_device(value), id=item.GetId())
        if self.settings.output_device_id and all(device.id != self.settings.output_device_id for device in self.output_devices):
            item = menu.AppendRadioItem(wx.ID_ANY, "已保存的设备不可用（播放时跟随系统）")
            item.Check(True)
            item.Enable(False)
        if self._output_device_error:
            menu.Append(wx.ID_ANY, self._output_device_error).Enable(False)

    def _on_output_menu_open(self, event: wx.MenuEvent) -> None:
        if event.GetMenu() is self.settings_menu:
            self._refresh_output_devices()
        elif event.GetMenu() is self.output_device_menu:
            self._populate_output_device_menu()
        event.Skip()

    def _set_default_output_device(self, device_id: str) -> None:
        updated = replace(self.settings, output_device_id=device_id)
        try:
            save_settings(updated)
        except OSError as exc:
            self.show_error(f"保存默认播放设备失败：{exc}")
            return
        self.settings = updated
        def done(result):
            if not self:
                return
            if result.get("ok"):
                self.SetStatusText(f"默认播放设备：{result['name']}，已保存")
            else:
                self.show_error(f"默认设备已保存，但暂时无法应用：{result.get('error', '未知错误')}")
        self.audio_output_router.select(device_id, lambda result: wx.CallAfter(done, result))

    def _cycle_output_device(self, direction: int, callback: Callable) -> None:
        self.audio_output_router.cycle(direction, lambda result: wx.CallAfter(callback, result))

    def _poll_output_device(self, generation: int) -> None:
        if not self or generation != self._audio_poll_generation or self.player_frame is None:
            return
        def done(result):
            if not self or generation != self._audio_poll_generation or self.player_frame is None:
                return
            if result.get("fallback"):
                message = "原播放设备不可用，音频输出：跟随系统"
                self.player_frame._announce_status(message)
            elif not result.get("ok"):
                message = f"应用播放设备失败：{result.get('error', '未知错误')}"
                if message != self.GetStatusBar().GetStatusText():
                    self.SetStatusText(message)
        self.audio_output_router.poll(lambda result: wx.CallAfter(done, result))
        wx.CallLater(2000, self._poll_output_device, generation)

    def on_subtitle_filter_rules(self, _event: wx.Event) -> None:
        self._edit_subtitle_filter_rules(self)

    def _edit_subtitle_filter_rules(self, parent: wx.Window) -> None:
        dialog = SubtitleFilterRulesDialog(
            parent, self.settings.subtitle_filter_presets,
            self.player_frame.subtitle_filter_slot if parent is self.player_frame
            else self.settings.active_subtitle_filter_slot,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            presets, slot = dialog.get_configuration()
        finally:
            dialog.Destroy()

        updated = replace(self.settings, subtitle_filter_presets=presets, active_subtitle_filter_slot=slot)
        try:
            save_settings(updated)
        except OSError as exc:
            self.show_error(f"保存字幕过滤规则失败：{exc}")
            return
        self.settings = updated
        if self.player_frame is not None:
            self.player_frame.subtitle_filter_presets = presets
            self.player_frame.subtitle_filter_slot = slot
            self.player_frame.subtitle_filter_rules = presets[slot].rules
            self.player_frame.book_filter_last_role = None
        self.SetStatusText(f"过滤方案{(slot + 1) % 10}已保存")

    def _on_filter_slot_changed(self, slot: int) -> bool:
        updated = replace(self.settings, active_subtitle_filter_slot=slot)
        try:
            save_settings(updated)
        except OSError as exc:
            self.SetStatusText(f"过滤方案已切换，但保存选择失败：{exc}")
            return False
        self.settings = updated
        return True

    def on_help_hotkeys(self, _event: wx.Event) -> None:
        self._open_text_file("热键表", HOTKEYS_TEXT_NAME, bundled=True)

    def on_help_update_log(self, _event: wx.Event) -> None:
        self._open_text_file("更新日志", UPDATE_TEXT_NAME)

    def on_help_about(self, _event: wx.Event) -> None:
        wx.MessageBox(
            f"{APP_TITLE}版本：{APP_VERSION}\n作者：{APP_AUTHOR}",
            "关于本程序",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    def _open_text_file(self, title: str, filename: str, bundled: bool = False) -> None:
        directory = Path(__file__).resolve().parent if bundled else program_dir()
        path = directory / filename
        if not path.exists():
            wx.MessageBox(f"未找到文件：{filename}", title, wx.OK | wx.ICON_ERROR, self)
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif not wx.LaunchDefaultApplication(str(path)):
                raise OSError("系统没有可用的默认打开方式")
        except OSError:
            wx.MessageBox(f"无法打开文件：{filename}", title, wx.OK | wx.ICON_ERROR, self)

    def on_account_login(self, _event: wx.Event) -> None:
        dialog = LoginDialog(self, self.api)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                cookie = dialog.cookie_header or self.api.cookie_header
                self.api.set_cookie(cookie)
                self._sync_follow_status_account()
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
        dialog = button.GetTopLevelParent()
        self._focus_account_dialog_content(dialog)
        button.Enable(False)
        self.SetStatusText("正在签到...")

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
                wx.CallAfter(self._finish_check_in_from_account_dialog, button, dialog)

        threading.Thread(target=runner, daemon=True).start()

    @staticmethod
    def _safe_enable_window(window: wx.Window, enabled: bool) -> None:
        try:
            if not window.IsBeingDeleted():
                window.Enable(enabled)
        except RuntimeError:
            pass

    def _finish_check_in_from_account_dialog(self, button: wx.Button, dialog: wx.Window | None) -> None:
        self._safe_enable_window(button, True)
        self._focus_account_dialog_content(dialog)

    @staticmethod
    def _focus_account_dialog_content(window: wx.Window | None) -> None:
        if not isinstance(window, AccountInfoDialog):
            return
        try:
            if not window.IsBeingDeleted():
                window.content_box.SetFocus()
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
        if isinstance(parent, AccountInfoDialog):
            try:
                if parent.IsBeingDeleted():
                    parent = None
                elif updated_account is not None:
                    parent.content_box.SetValue(updated_account.text)
            except RuntimeError:
                parent = None
        self.SetStatusText(message)
        message_parent = parent or self
        wx.MessageBox(message, title, wx.OK | wx.ICON_INFORMATION, message_parent)
        try:
            message_parent.Raise()
            if isinstance(message_parent, AccountInfoDialog):
                message_parent.content_box.SetFocus()
            else:
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
            "正在加载我的追剧...",
            lambda: self.api.subscribed_dramas(1),
            lambda items: self._enter_items(
                items,
                "我的追剧",
                previous_state,
                focus_list=True,
                page_state=PageState(1, lambda page: self.api.subscribed_dramas(page)),
            ),
        )

    def on_account_history(self, _event: wx.Event) -> None:
        cookie = self.api.cookie_header
        if not cookie:
            wx.MessageBox("请先登录账号", "提示", wx.OK | wx.ICON_INFORMATION, self)
            return
        previous_state = self._account_list_previous_state()

        def show_history(items: list[MediaItem]) -> None:
            if self.api.cookie_header != cookie:
                self.SetStatusText("登录账号已变化，请重新打开播放历史")
                return
            self._enter_items(
                items,
                "我的播放历史",
                previous_state,
                focus_list=True,
                hide_detail_column=True,
            )

        self._run_background("正在加载我的播放历史...", self.api.playback_history, show_history)

    def on_account_following(self, _event: wx.Event) -> None:
        cookie = self.api.cookie_header
        if not cookie:
            wx.MessageBox("请先登录账号", "提示", wx.OK | wx.ICON_INFORMATION, self)
            return
        previous_state = self._account_list_previous_state()

        def load_page(page: int) -> list[MediaItem]:
            if self.api.cookie_header != cookie:
                raise ApiError("登录账号已变化，请重新打开我的关注")
            return self.api.followed_accounts(page)

        def show_following(items: list[MediaItem]) -> None:
            if self.api.cookie_header != cookie:
                self.SetStatusText("登录账号已变化，请重新打开我的关注")
                return
            self._enter_items(
                items,
                "我的关注",
                previous_state,
                focus_list=True,
                page_state=PageState(1, load_page),
                hide_detail_column=True,
            )

        self._run_background(
            "正在加载我的关注...",
            lambda: load_page(1),
            show_following,
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
        self._sync_follow_status_account()
        self.api.clear_saved_cookie()
        self.browser_player.cookie = ""
        self.browser_player.shutdown()
        self.account_logged_in = False
        self._update_account_menu()
        self.SetTitle(APP_TITLE)
        self.SetStatusText("已退出登录")
        if self.current_title in {"我的收藏", "我的追剧", "我的播放历史", "我的关注", "剧集订阅", "已购广播剧"}:
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
        self.SetTitle(f"{APP_TITLE} - 登录账号：{nickname}")

    def _mark_account_logged_out(self, status: str = "") -> None:
        self.api.set_cookie("")
        self._sync_follow_status_account()
        self.browser_player.cookie = ""
        if self.account_logged_in:
            self.account_logged_in = False
            self._update_account_menu()
        self.SetTitle(APP_TITLE)
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

    def on_list_item_selected(self, event: wx.ListEvent) -> None:
        index = event.GetIndex()
        if 0 <= index < len(self.items):
            self._prefetch_follow_status_for_item(self.items[index])
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
        self._prefetch_follow_status_for_item(self.items[index])

    def _resize_list_columns(self) -> None:
        width = self.list.GetClientSize().width
        if width <= 0:
            return

        if self._hide_detail_column():
            self.list.SetColumnWidth(0, max(120, width - 24))
            self.list.SetColumnWidth(1, 0)
            return

        author_width = max(160, min(260, width // 3))
        name_width = max(120, width - author_width - 24)
        self.list.SetColumnWidth(0, name_width)
        self.list.SetColumnWidth(1, max(120, width - name_width - 24))

    def _update_list_column_headers(self, title: str) -> None:
        name_label = "名称"
        detail_label = "声音数" if title == "我的收藏" else "发布"
        if title in {"精品周更", "广播剧 · 时间表"}:
            name_label = "更新日 · 剧名"
        if title == "广播剧 · 时间表":
            detail_label = "最新更新"
        if self._items_are_categories():
            name_label = "分类"
            detail_label = ""
        elif self._hide_detail_column():
            detail_label = ""
        self._set_list_column_header(0, name_label)
        self._set_list_column_header(1, detail_label)

    def _set_list_column_header(self, column_index: int, label: str) -> None:
        column = wx.ListItem()
        column.SetText(label)
        self.list.SetColumn(column_index, column)

    def _items_are_categories(self) -> bool:
        return bool(self.items) and all(item.kind == "category" for item in self.items)

    def _hide_detail_column(self) -> bool:
        return self.hide_list_detail_column or self._items_are_categories()

    def _clear_items_for_loading(
        self,
        title: str,
        focus_list: bool = False,
        hide_detail_column: bool = False,
    ) -> None:
        self._opened_drama_id = None
        self._publisher_request = object()
        self.current_title = title
        self.items = []
        self.page_state = None
        self.hide_list_detail_column = hide_detail_column
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            self._set_list_column_header(0, "")
            self._set_list_column_header(1, "")
        finally:
            self.list.Thaw()
        if focus_list:
            wx.CallAfter(self._focus_list)
        self.SetStatusText(f"正在加载{title}...")

    def _append_list_item(self, index: int, item: MediaItem) -> None:
        self.list.InsertItem(index, self._display_item_title(item))
        self.list.SetItem(index, 1, self._item_publisher(item))

    def _display_item_title(self, item: MediaItem) -> str:
        title = item.title
        if self.current_title == "我的播放历史" and isinstance(item.raw, dict):
            date = item.raw.get("_history_date")
            if isinstance(date, str) and date:
                title = f"{date} · {title}"
        if self.current_title in {"精品周更", "广播剧 · 时间表"} and isinstance(item.raw, dict):
            day_key = "_weekly_day_label" if self.current_title == "精品周更" else "_timeline_day_label"
            day = item.raw.get(day_key)
            if isinstance(day, str) and day:
                title = f"{day} · {title}"
        if self._opened_drama_id is not None and item.kind == "sound" and item.need_pay:
            raw = item.raw if isinstance(item.raw, dict) else {}
            title += "（限免）" if raw.get("_member_vip_limited_free") else "（付费）"
        return title

    def _item_publisher(self, item: MediaItem) -> str:
        if self._hide_detail_column():
            return ""
        if item.kind == "drama_purchase":
            return ""

        if self.current_title == "广播剧 · 时间表" and isinstance(item.raw, dict):
            newest = self._raw_text_value(item.raw, ("newest",))
            return newest or "更新内容未标注"

        raw = item.raw
        if isinstance(raw, dict):
            if raw.get("_hide_author") and self.current_title == "我的收藏":
                return item.subtitle
            return self._raw_text_value(raw, ("_publisher_name", "username", "user_name"))
        return ""

    def _uses_publisher_column(self) -> bool:
        return (
            not self._hide_detail_column()
            and self.current_title not in {"我的收藏", "广播剧 · 时间表"}
        )

    def _start_publisher_resolution(self, items: list[MediaItem]) -> None:
        if not self._uses_publisher_column():
            return
        pending = [item for item in items if item.kind in {"sound", "drama", "album"} and not self._item_publisher(item)]
        if not pending:
            return
        token = self._publisher_request

        def runner() -> None:
            for item in pending:
                if self._publisher_request is not token:
                    return
                try:
                    name = self.api.publisher_name_for_item(item)
                except (ApiError, requests.RequestException, ValueError):
                    continue
                if isinstance(name, str) and name.strip():
                    wx.CallAfter(self._apply_resolved_publisher, item, name.strip(), token)

        threading.Thread(target=runner, daemon=True).start()

    def _apply_resolved_publisher(self, item: MediaItem, name: str, token: object) -> None:
        if self._publisher_request is not token:
            return
        for index, current in enumerate(self.items):
            if current is item:
                item.raw = {**item.raw, "_publisher_name": name}
                self.list.SetItem(index, 1, name)
                return

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
        profile = item.raw.get("_publisher_profile") if isinstance(item.raw, dict) else None
        if isinstance(profile, PublisherProfile) and self.current_title == f"发布者：{profile.name}":
            self._display_publisher_follow_menu(position, profile)
            return
        followed = self._known_follow_status(item) if item.kind == "drama" else None
        if item.kind == "drama" and self.api.cookie_header and followed is None:
            self._prefetch_follow_status_for_item(item)
        self._display_item_menu(position, item, followed)

    def _display_publisher_follow_menu(self, position: wx.Point, profile: PublisherProfile) -> None:
        menu = wx.Menu()
        action_id = wx.NewIdRef()
        cookie = self.api.cookie_header
        if not cookie:
            label = "请先登录"
        elif profile.followed is None or profile.followed_cookie != cookie:
            label = "关注状态未确认，请重新打开发布者资料"
        else:
            label = "取消关注" if profile.followed else "关注"
        entry = menu.Append(action_id, label)
        entry.Enable(not cookie or (profile.followed is not None and profile.followed_cookie == cookie))
        try:
            choice = self.list.GetPopupMenuSelectionFromUser(menu, position)
        finally:
            menu.Destroy()
        if choice != int(action_id):
            return
        if not cookie:
            wx.MessageBox("请先登录账号", "提示", wx.OK | wx.ICON_INFORMATION, self)
            return
        follow = not profile.followed
        self._run_background(
            f"正在{'关注' if follow else '取消关注'}：{profile.name}",
            lambda: self.api.set_publisher_follow(profile.user_id, follow),
            lambda message: self._publisher_follow_done(profile, follow, cookie, str(message)),
        )

    def _publisher_follow_done(
        self, profile: PublisherProfile, follow: bool, cookie: str, message: str,
    ) -> None:
        if self.api.cookie_header != cookie:
            self.SetStatusText("登录账号已变化，请重新打开发布者资料")
            return
        profile.followed = follow
        profile.followed_cookie = cookie
        if profile.followers is not None:
            profile.followers = max(0, profile.followers + (1 if follow else -1))
        if self.current_title == f"发布者：{profile.name}":
            updated_profile_item = self._publisher_profile_items(profile)[0]
            for index, item in enumerate(self.items):
                if item.kind != "publisher_profile":
                    continue
                item.title = updated_profile_item.title
                item.raw["_profile_text"] = updated_profile_item.raw["_profile_text"]
                self.list.SetItem(index, 0, item.title)
                break
        if not follow:
            for state in self.navigation_stack:
                if state.title == "我的关注":
                    state.items = [item for item in state.items if item.id != profile.user_id]
                    state.selected_index = min(state.selected_index, len(state.items) - 1)
        self.SetStatusText(message)
        wx.MessageBox(message, "提示", wx.OK | wx.ICON_INFORMATION, self)

    def _display_item_menu(self, position: wx.Point, item: MediaItem, followed: bool | None) -> None:
        if item.kind == "category":
            return

        can_show_item_menu = self._can_show_item_menu(item)
        can_show_comments_menu = self._can_show_comments_menu(item)
        if not can_show_item_menu and not can_show_comments_menu:
            return

        menu = wx.Menu()
        open_id = wx.NewIdRef()
        detail_id = wx.NewIdRef()
        comments_id = wx.NewIdRef()
        drama_id = wx.NewIdRef()
        publisher_id = wx.NewIdRef()
        purchase_id = wx.NewIdRef()
        follow_id = wx.NewIdRef()
        purchase_label = self._drama_purchase_menu_label(item)
        if can_show_item_menu:
            menu.Append(open_id, "用网页打开")
            menu.Append(detail_id, "查看音频简介" if item.kind == "sound" else "查看广播剧详情")
            if item.kind == "sound":
                menu.Append(drama_id, "查看该剧集")
            menu.Append(publisher_id, "查看发布者")
        if item.kind == "drama":
            menu.AppendSeparator()
            if purchase_label is not None:
                menu.Append(purchase_id, purchase_label)
            if not self.api.cookie_header:
                follow_label = "登录后追剧"
            elif followed is None:
                follow_label = "管理追剧…"
            else:
                follow_label = "取消追剧" if followed else "追剧"
            follow_entry = menu.Append(follow_id, follow_label)
            follow_entry.Enable(bool(self.api.cookie_header))
        if can_show_comments_menu:
            if can_show_item_menu:
                menu.AppendSeparator()
            menu.Append(comments_id, "查看评论")

        try:
            choice = self.list.GetPopupMenuSelectionFromUser(menu, position)
        finally:
            menu.Destroy()

        if can_show_item_menu and choice == int(open_id):
            self.open_item_in_browser(item)
        elif can_show_item_menu and choice == int(detail_id):
            if item.kind == "sound":
                self.show_sound_intro(item)
            else:
                self.show_drama_detail(item)
        elif item.kind == "sound" and choice == int(drama_id):
            self.show_sound_drama(item)
        elif can_show_item_menu and choice == int(publisher_id):
            self.show_item_publisher(item)
        elif item.kind == "drama" and purchase_label is not None and choice == int(purchase_id):
            self._prompt_drama_purchase(item.id)
        elif item.kind == "drama" and choice == int(follow_id):
            if followed is None:
                self._follow_drama_from_work_menu(item)
            else:
                self._follow_drama_from_work_menu(item, expected_followed=followed)
        elif can_show_comments_menu and choice == int(comments_id):
            self.show_comments(item)

    @staticmethod
    def _drama_purchase_menu_label(item: MediaItem) -> str | None:
        if item.kind != "drama" or item.pay_type != DRAMA_PAY_TYPE_WHOLE:
            return None
        raw = item.raw if isinstance(item.raw, dict) else {}
        if raw.get("_purchased_full_drama") or ("need_pay" in raw and not item.need_pay):
            return None
        return "购买本剧"

    def _sync_follow_status_account(self) -> str:
        cookie = self.api.cookie_header
        if getattr(self, "_follow_status_cookie", None) != cookie:
            self._follow_status_cookie = cookie
            self._follow_status_cache = {}
            self._follow_status_pending = {}
        return cookie

    def _known_follow_status(self, item: MediaItem) -> bool | None:
        if not self._sync_follow_status_account():
            return None
        if item.id in self._follow_status_cache:
            return self._follow_status_cache[item.id]
        cached = self.api.cached_drama_follow_status(item.id)
        if isinstance(cached, bool):
            self._follow_status_cache[item.id] = cached
            return cached
        return None

    def _remember_followed_items(self, items: list[MediaItem], title: str) -> None:
        if title not in {"我的追剧", "广播剧 · 我的追剧"} or not self._sync_follow_status_account():
            return
        for item in items:
            if item.kind == "drama":
                self._follow_status_cache.setdefault(item.id, True)

    def _prefetch_follow_status_for_item(self, item: MediaItem) -> None:
        if item.kind != "drama":
            return
        cookie = self._sync_follow_status_account()
        if not cookie or self._known_follow_status(item) is not None:
            return
        if item.id in self._follow_status_pending:
            return
        token = object()
        self._follow_status_pending[item.id] = token
        drama_id = item.id

        def runner() -> None:
            try:
                followed = self.api.drama_follow_status(drama_id)
            except (ApiError, requests.RequestException, ValueError):
                followed = None
            except Exception as exc:
                debug_log(f"follow status prefetch failed: {type(exc).__name__}: {exc}")
                followed = None
            wx.CallAfter(self._finish_follow_status_prefetch, drama_id, cookie, token, followed)

        threading.Thread(target=runner, daemon=True).start()

    def _finish_follow_status_prefetch(
        self, drama_id: int, cookie: str, token: object, followed: bool | None,
    ) -> None:
        if self._sync_follow_status_account() != cookie:
            return
        if self._follow_status_pending.get(drama_id) is not token:
            return
        del self._follow_status_pending[drama_id]
        if isinstance(followed, bool):
            self._follow_status_cache[drama_id] = followed

    def _can_show_item_menu(self, item: MediaItem) -> bool:
        return item.kind in {"drama", "sound"}

    def _can_show_comments_menu(self, item: MediaItem) -> bool:
        return item.kind == "sound"

    def open_item_in_browser(self, item: MediaItem) -> None:
        if item.kind == "sound":
            url = f"{BASE_URL}/sound/player?id={item.id}"
        elif item.kind == "drama":
            url = f"{BASE_URL}/mdrama/{item.id}"
            if item.pay_type is not None:
                url += f"?pay_type={item.pay_type}"
        else:
            return
        if not wx.LaunchDefaultBrowser(url):
            self.show_error("无法打开浏览器")

    def show_sound_intro(self, item: MediaItem) -> None:
        self._run_background(
            f"正在加载音频简介: {item.title}",
            lambda: self.api.sound_intro_text(item.id),
            lambda content: self._show_item_detail_dialog(item, str(content), "音频简介"),
        )

    def show_drama_detail(self, item: MediaItem) -> None:
        self._run_background(
            f"正在加载广播剧详情: {item.title}",
            lambda: self.api.drama_detail_text(item.id),
            lambda content: self._show_item_detail_dialog(item, str(content), "广播剧详情"),
        )

    def show_sound_drama(self, item: MediaItem) -> None:
        previous_state = self._navigation_state_snapshot()
        self._run_background(
            f"正在查找该音频所属剧集: {item.title}",
            lambda: self.api.drama_for_sound(item.id),
            lambda drama: self._enter_items(
                [drama], "该音频所属剧集", previous_state, focus_list=True,
            ),
        )

    def show_item_publisher(self, item: MediaItem) -> None:
        previous_state = self._navigation_state_snapshot()
        self._run_background(
            f"正在加载发布者: {item.title}",
            lambda: self.api.publisher_profile_for_item(item),
            lambda profile: self._enter_publisher_profile(profile, previous_state),
        )

    def show_followed_account(self, item: MediaItem) -> None:
        previous_state = self._navigation_state_snapshot()
        self._run_background(
            f"正在加载账号: {item.title}",
            lambda: self.api.publisher_profile(item.id),
            lambda profile: self._enter_publisher_profile(profile, previous_state),
        )

    def _enter_publisher_profile(self, profile: PublisherProfile, previous_state: NavigationState) -> None:
        self._enter_items(
            self._publisher_profile_items(profile),
            f"发布者：{profile.name}",
            previous_state,
            focus_list=True,
            hide_detail_column=True,
        )

    @staticmethod
    def _publisher_profile_items(profile: PublisherProfile) -> list[MediaItem]:
        followers = str(profile.followers) if profile.followers is not None else "未知"
        following = str(profile.following) if profile.following is not None else "未知"
        bio = profile.bio or "暂无简介"
        text = f"发布者：{profile.name}\n粉丝：{followers}\n关注：{following}\n简介：{bio}"
        return [
            MediaItem(
                kind="publisher_profile",
                id=profile.user_id,
                title=f"{profile.name}粉丝：{followers}关注：{following}简介：{bio}",
                raw={"_publisher_profile": profile, "_profile_text": text},
            ),
            MediaItem(kind="publisher_dramas", id=profile.user_id, title="Ta的剧集", raw={"_publisher_profile": profile}),
            MediaItem(kind="publisher_sounds", id=profile.user_id, title="Ta的声音", raw={"_publisher_profile": profile}),
        ]

    def _show_item_detail_dialog(self, item: MediaItem, content: str, detail_kind: str) -> None:
        dialog = MediaDetailDialog(self, item.title, content, detail_kind)
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
        if item.kind == "publisher_account":
            self.show_followed_account(item)
            return

        if item.kind == "publisher_profile":
            profile = item.raw.get("_publisher_profile") if isinstance(item.raw, dict) else None
            content = item.raw.get("_profile_text", "") if isinstance(item.raw, dict) else ""
            if isinstance(profile, PublisherProfile):
                self._show_item_detail_dialog(
                    MediaItem(kind="publisher_profile", id=profile.user_id, title=profile.name),
                    str(content),
                    "发布者资料",
                )
            return

        if item.kind in {"publisher_dramas", "publisher_sounds"}:
            profile = item.raw.get("_publisher_profile") if isinstance(item.raw, dict) else None
            if not isinstance(profile, PublisherProfile):
                self.show_error("发布者资料已失效，请重新打开")
                return
            previous_state = self._navigation_state_snapshot()
            loader = (
                (lambda page: self.api.publisher_dramas(profile, page))
                if item.kind == "publisher_dramas"
                else (lambda page: self.api.publisher_sounds(profile, page))
            )
            self._run_background(
                f"正在加载{profile.name}的{'剧集' if item.kind == 'publisher_dramas' else '声音'}...",
                lambda: loader(1),
                lambda items: self._enter_items(
                    items,
                    f"{profile.name} · {item.title}",
                    previous_state,
                    focus_list=True,
                    page_state=PageState(1, loader),
                ),
            )
            return

        if item.kind == "drama_purchase":
            self._prompt_drama_purchase(item.drama_id or item.id)
            return

        if item.kind == "category":
            self.open_category(item)
            return

        if item.is_collection:
            previous_state = self._navigation_state_snapshot()
            if item.kind == "drama":
                force_owned = isinstance(item.raw, dict) and bool(item.raw.get("_purchased_full_drama"))
                self._run_background(
                    f"正在加载: {item.title}",
                    lambda: self.api.drama_episodes_page(item.id, 1, force_owned=force_owned),
                    lambda items: self._enter_items(
                        items,
                        item.title,
                        previous_state,
                        page_state=PageState(
                            1,
                            lambda page: self.api.drama_episodes_page(item.id, page, force_owned=force_owned),
                        ),
                        hide_detail_column=self.hide_list_detail_column,
                        opened_drama_id=item.id,
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
                        hide_detail_column=self.hide_list_detail_column,
                    ),
                )
                return

            self._run_background(
                f"正在加载: {item.title}",
                lambda: self.api.collection_items(item),
                lambda items: self._enter_items(
                    items,
                    item.title,
                    previous_state,
                    hide_detail_column=self.hide_list_detail_column,
                ),
            )
            return

        if item.need_pay and item.kind != "sound":
            self._prompt_sound_purchase(item)
            return

        self._play_sound_item(item)

    def open_category(self, item: MediaItem) -> None:
        previous_state = self._navigation_state_snapshot()
        children = self.api.category_children(item)
        if children:
            self.page_state = None
            self._enter_items(
                children,
                f"分类: {item.title}",
                previous_state,
                focus_list=True,
                hide_detail_column=True,
            )
            return

        self._run_background(
            f"正在加载分类内容: {item.title}",
            lambda: self.api.category_contents(item, 1),
            lambda items: self._enter_items(
                items,
                item.title,
                previous_state,
                focus_list=True,
                page_state=PageState(1, lambda page: self.api.category_contents(item, page)),
                hide_detail_column=True,
            ),
        )

    def _prompt_drama_purchase(
        self,
        drama_id: int,
        play_after: MediaItem | None = None,
    ) -> None:
        self._run_background(
            "正在获取购买信息...",
            lambda: self.api.drama_purchase_info(drama_id, refresh=True),
            lambda info: self._show_drama_purchase_prompt(info, play_after=play_after),
        )

    def _prompt_sound_purchase(self, item: MediaItem) -> None:
        if item.kind != "sound":
            self.show_purchase_required(item.title)
            return

        self._run_background(
            "正在获取购买信息...",
            lambda: self.api.sound_purchase_info(item),
            lambda info: self._show_sound_purchase_prompt(item, info),
        )

    def _show_sound_purchase_prompt(self, item: MediaItem, info: object) -> None:
        if not isinstance(info, SoundPurchaseInfo):
            self.show_purchase_required(item.title)
            return

        item.pay_type = info.pay_type
        item.price = info.price if info.price is not None else item.price
        item.drama_id = info.drama.drama_id
        if info.title:
            item.title = info.title

        if not info.need_pay and (
            info.drama.pay_type != DRAMA_PAY_TYPE_WHOLE or not info.drama.need_pay
        ):
            item.need_pay = False
            self._mark_sound_purchased_in_items(item.id)
            if info.drama.pay_type == DRAMA_PAY_TYPE_WHOLE:
                self._mark_drama_purchased_in_items(info.drama.drama_id)
            self.SetStatusText("已购买，正在播放")
            self._play_sound_item(item)
            return

        if info.pay_type == DRAMA_PAY_TYPE_EPISODES:
            price = item.price if item.price is not None else info.drama.price
            if not self._confirm_episode_purchase(item, info.drama, price):
                self.SetStatusText("已取消购买")
                return

            self._run_background(
                f"正在购买单集: {item.title}",
                lambda: self.api.buy_drama_episode(info.drama.drama_id, item.id),
                lambda _payload: self._finish_episode_purchase(item, info.drama.drama_id),
                on_error=self._show_purchase_failure,
            )
            return

        if info.pay_type == DRAMA_PAY_TYPE_WHOLE or info.drama.pay_type == DRAMA_PAY_TYPE_WHOLE:
            self._show_drama_purchase_prompt(info.drama, play_after=item)
            return

        if info.drama.pay_type != DRAMA_PAY_TYPE_EPISODES:
            self.show_purchase_required(item.title)
            return

        price = item.price if item.price is not None else info.drama.price
        if not self._confirm_episode_purchase(item, info.drama, price):
            self.SetStatusText("已取消购买")
            return

        self._run_background(
            f"正在购买单集: {item.title}",
            lambda: self.api.buy_drama_episode(info.drama.drama_id, item.id),
            lambda _payload: self._finish_episode_purchase(item, info.drama.drama_id),
            on_error=self._show_purchase_failure,
        )

    def _show_drama_purchase_prompt(
        self,
        info: object,
        play_after: MediaItem | None = None,
    ) -> None:
        if not isinstance(info, DramaPurchaseInfo):
            self.show_purchase_required(play_after.title if play_after else "广播剧")
            return

        if not info.need_pay:
            self._finish_drama_purchase(info.drama_id, play_after=play_after, already_owned=True)
            return

        if not self._confirm_drama_purchase(info):
            self.SetStatusText("已取消购买")
            return

        self._run_background(
            f"正在购买广播剧: {info.title}",
            lambda: self.api.buy_drama(info.drama_id),
            lambda _payload: self._finish_drama_purchase(info.drama_id, play_after=play_after),
            on_error=self._show_purchase_failure,
        )

    def _confirm_drama_purchase(self, info: DramaPurchaseInfo) -> bool:
        if info.price is None:
            message = f"《{info.title}》未获取到价格，是否购买本剧？"
        else:
            message = f"《{info.title}》价格为 {info.price} 钻石，是否购买？"
        return self._confirm_purchase(message, "购买广播剧")

    def _confirm_episode_purchase(
        self,
        item: MediaItem,
        info: DramaPurchaseInfo,
        price: int | None,
    ) -> bool:
        if price is None:
            message = f"《{item.title}》未获取到价格，是否购买这一集？"
        else:
            message = f"《{item.title}》价格为 {price} 钻石，是否购买这一集？"
        if info.title:
            message = f"广播剧：{info.title}\n{message}"
        return self._confirm_purchase(message, "购买单集")

    def _confirm_purchase(self, message: str, title: str) -> bool:
        dialog = wx.MessageDialog(
            self,
            message,
            title,
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        try:
            no_button = dialog.FindWindow(wx.ID_NO)
            if no_button is not None:
                no_button.SetFocus()
            return dialog.ShowModal() == wx.ID_YES
        finally:
            dialog.Destroy()

    def _finish_drama_purchase(
        self,
        drama_id: int,
        play_after: MediaItem | None = None,
        already_owned: bool = False,
    ) -> None:
        self._mark_drama_purchased_in_items(drama_id)
        message = "本剧已购买" if already_owned else "购买成功，已解锁本剧"
        wx.MessageBox(message, "购买成功", wx.OK | wx.ICON_INFORMATION, self)
        if play_after is not None:
            play_after.need_pay = False
            self._play_sound_item(play_after)
            return
        self.SetStatusText(message)

    def _finish_episode_purchase(self, item: MediaItem, drama_id: int) -> None:
        item.need_pay = False
        self._mark_sound_purchased_in_items(item.id)
        wx.MessageBox("购买成功，正在播放。", "购买成功", wx.OK | wx.ICON_INFORMATION, self)
        self.SetStatusText("购买成功，正在播放")
        self._play_sound_item(item)

    def _show_purchase_failure(self, message: str) -> None:
        self.SetStatusText("购买失败")
        text = message.strip()
        if text == "需要登录":
            text = "需要登录后才能购买。"
        elif "余额" in text and "不足" in text:
            text = "钻石余额不足，无法完成购买。"
        elif not text:
            text = "购买失败。"
        else:
            text = f"购买失败：{text}"
        wx.MessageBox(text, "购买失败", wx.OK | wx.ICON_INFORMATION, self)

    def _mark_drama_purchased_in_items(self, drama_id: int) -> None:
        selected_index = self._selected_index()
        top_index = self._top_index()
        changed = False
        new_items: list[MediaItem] = []
        for item in self.items:
            item_drama_id = item.drama_id or (item.id if item.kind == "drama" else None)
            if item.kind == "drama_purchase" and item_drama_id == drama_id:
                changed = True
                continue
            if item_drama_id == drama_id:
                if item.need_pay or not item.raw.get("_purchased_full_drama"):
                    changed = True
                item.need_pay = False
                item.raw = {**item.raw, "_purchased_full_drama": True}
            new_items.append(item)

        for state in self.navigation_stack:
            for item in state.items:
                item_drama_id = item.drama_id or (item.id if item.kind == "drama" else None)
                if item_drama_id == drama_id:
                    item.need_pay = False
                    item.raw = {**item.raw, "_purchased_full_drama": True}
        if self.homepage_state is not None:
            for item in self.homepage_state.items:
                item_drama_id = item.drama_id or (item.id if item.kind == "drama" else None)
                if item_drama_id == drama_id:
                    item.need_pay = False
                    item.raw = {**item.raw, "_purchased_full_drama": True}

        if not changed:
            return

        selected_index = max(0, min(selected_index, len(new_items) - 1)) if new_items else 0
        self.set_items(
            new_items,
            self.current_title,
            selected_index=selected_index,
            top_index=top_index,
            hide_detail_column=self.hide_list_detail_column,
            opened_drama_id=self._opened_drama_id,
        )

    def _mark_sound_purchased_in_items(self, sound_id: int) -> None:
        for index, item in enumerate(self.items):
            if item.kind == "sound" and item.id == sound_id:
                item.need_pay = False
                item.raw = {**item.raw, "_purchased_sound": True}
                self.list.SetItem(index, 0, self._display_item_title(item))

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
            hide_detail_column=self.hide_list_detail_column,
            opened_drama_id=self._opened_drama_id,
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
        hide_detail_column: bool = False,
        opened_drama_id: int | None = None,
    ) -> None:
        self.navigation_stack.append(previous_state)
        self.page_state = page_state
        self.set_items(
            items,
            title,
            focus_list=focus_list,
            hide_detail_column=hide_detail_column,
            opened_drama_id=opened_drama_id,
        )

    def set_items(
        self,
        items: list[MediaItem],
        title: str,
        selected_index: int = 0,
        focus_list: bool = False,
        top_index: int | None = None,
        hide_detail_column: bool = False,
        opened_drama_id: int | None = None,
    ) -> None:
        self.current_title = title
        self.items = items
        self._remember_followed_items(items, title)
        self.hide_list_detail_column = hide_detail_column
        self._opened_drama_id = opened_drama_id
        self._publisher_request = object()
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
        self._start_publisher_resolution(items)
        if self.page_state is not None:
            wx.CallAfter(self._load_next_page_if_near_bottom)

    def _follow_drama_from_work_menu(
        self, item: MediaItem, expected_followed: bool | None = None,
    ) -> None:
        cookie = self.api.cookie_header
        if not cookie:
            self.show_error("需要登录后才能追剧")
            return
        drama_id = item.id
        self._run_background(
            f"正在查询追剧状态: {item.title}",
            lambda: self.api.drama_follow_status(drama_id),
            lambda followed: self._confirm_work_menu_follow(
                item, bool(followed), cookie, expected_followed=expected_followed,
            ),
        )

    def _confirm_work_menu_follow(
        self, item: MediaItem, followed: bool, cookie: str, expected_followed: bool | None = None,
    ) -> None:
        if self.api.cookie_header != cookie:
            self.show_error("登录账号已变化，请重新追剧")
            return
        self._sync_follow_status_account()
        self._follow_status_cache[item.id] = followed
        self._follow_status_pending.pop(item.id, None)
        if expected_followed is not None and followed != expected_followed:
            message = "追剧状态已变化，请重新打开右键菜单。"
            self.SetStatusText(message)
            wx.MessageBox(message, "追剧状态已变化", wx.OK | wx.ICON_INFORMATION, self)
            return
        if followed and wx.MessageBox(
            "确定取消追剧吗？",
            "取消追剧",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
            self,
        ) != wx.YES:
            return

        target = not followed

        def work() -> DramaFollowResult:
            if self.api.cookie_header != cookie:
                raise ApiError("登录账号已变化，请重新追剧")
            return self.api.set_drama_follow_result(item.id, follow=target)

        self._run_background(
            "正在追剧..." if target else "正在取消追剧...",
            work,
            lambda result: self._finish_work_menu_follow(item, result, target, cookie),
        )

    def _finish_work_menu_follow(
        self, item: MediaItem, result: DramaFollowResult, target: bool, cookie: str,
    ) -> None:
        if self.api.cookie_header != cookie:
            return
        if result.followed != target:
            self.SetStatusText("服务端追剧状态未改变，请稍后重试")
            return
        item.raw = {**item.raw, "like": int(result.followed)}
        self._sync_follow_status_account()
        self._follow_status_cache[item.id] = result.followed
        self._follow_status_pending.pop(item.id, None)
        message = result.message.strip() or ("已加入追剧列表" if target else "已移出追剧列表")
        self.SetStatusText(message)
        wx.MessageBox(
            message,
            "追剧成功" if target else "取消追剧",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

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
        self._remember_followed_items(new_items, self.current_title)
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
        self._start_publisher_resolution(new_items)
        wx.CallAfter(self._load_next_page_if_near_bottom)

    def _play(
        self,
        playback: PlaybackInfo,
        source_key: tuple[str, int] | None = None,
        source_title: str = "",
    ) -> None:
        created = False
        if self.player_frame is None:
            self.player_frame = PlaybackFrame(
                self,
                self.api,
                self.browser_player,
                self._on_player_window_close,
                self._on_playback_finished,
                read_danmaku_default=self.settings.read_danmaku,
                read_subtitle_default=self.settings.read_subtitle,
                subtitle_filter_presets=self.settings.subtitle_filter_presets,
                subtitle_filter_slot=self.settings.active_subtitle_filter_slot,
                on_filter_slot_changed=self._on_filter_slot_changed,
                on_filter_rules=self._edit_subtitle_filter_rules,
                on_cycle_output=self._cycle_output_device,
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

        self.current_playback_key = source_key or ("sound", playback.sound_id)
        self._audio_poll_generation += 1
        if created:
            wx.CallAfter(self.audio_output_router.select, self.settings.output_device_id)
        wx.CallAfter(self._poll_output_device, self._audio_poll_generation)
        self.player_frame.Show()
        self.player_frame.Raise()
        prefix = "正在播放"
        if source_title and source_title != self.current_title:
            prefix = f"{prefix}({source_title})"
        self.SetStatusText(f"{prefix}: {playback.title}")
        threading.Thread(target=self.api.add_play_times, args=(playback,), daemon=True).start()

    def _on_player_window_close(self, frame: PlaybackFrame) -> None:
        if self.player_frame is frame:
            self.player_frame = None
        if self.active_player is self.browser_player:
            self.active_player = None
        self.current_playback_key = None
        self.SetStatusText("已停止播放")

    def _on_playback_finished(self, frame: PlaybackFrame, playback: PlaybackInfo) -> None:
        if frame is not self.player_frame:
            return
        playback_key = ("sound", playback.sound_id)
        if self.current_playback_key not in (None, playback_key):
            return
        self.current_playback_key = playback_key
        self._play_next_from_current_list(playback_key)

    def _play_next_from_current_list(self, current_key: tuple[str, int]) -> None:
        if self.current_playback_key != current_key:
            return
        if self._index_for_item_key(current_key) is None:
            self.SetStatusText("播放结束")
            return
        next_index = self._next_playable_index(current_key)
        if next_index is None:
            self._load_next_page_for_auto_play(current_key)
            return

        next_item = self.items[next_index]
        self._select_list_row(next_index)
        self._play_sound_item(next_item, status_prefix="正在自动播放", auto_current_key=current_key)

    def _load_next_page_for_auto_play(self, current_key: tuple[str, int]) -> None:
        state = self.page_state
        if state is None or not state.has_more:
            self.SetStatusText("已播放到最后一个音频")
            return
        if state.loading:
            self.SetStatusText("正在等待下一页加载")
            wx.CallLater(1200, self._play_next_from_current_list, current_key)
            return

        state.loading = True
        next_page = state.page + 1

        def work() -> list[MediaItem]:
            try:
                return state.loader(next_page)
            finally:
                state.loading = False

        def done(items: object) -> None:
            if self.page_state is state:
                self._append_next_page(state, next_page, items if isinstance(items, list) else [])
            if self.current_playback_key == current_key:
                self._play_next_from_current_list(current_key)

        self._run_background(
            f"正在加载下一页: {self.current_title}",
            work,
            done,
        )

    def _play_sound_item(
        self,
        item: MediaItem,
        status_prefix: str = "正在获取播放地址",
        auto_current_key: tuple[str, int] | None = None,
    ) -> None:
        if item.kind != "sound":
            if auto_current_key is not None:
                wx.CallAfter(self._play_next_from_current_list, auto_current_key)
            return
        source_key = self._item_key(item)
        source_title = self.current_title
        self._run_background(
            f"{status_prefix}: {item.title}",
            lambda: self.api.playback_info(item),
            lambda playback: self._play(playback, source_key=source_key, source_title=source_title),
            on_purchase_required=(
                (lambda _exc: self._skip_auto_purchase_required(auto_current_key, item))
                if auto_current_key is not None
                else (lambda _exc: self._on_manual_purchase_required(item))
            ),
        )

    def _on_manual_purchase_required(self, item: MediaItem) -> None:
        item.need_pay = True
        index = self._index_for_item_key(self._item_key(item))
        if index is not None:
            self.list.SetItem(index, 0, self._display_item_title(item))
        self._prompt_sound_purchase(item)

    def _skip_auto_purchase_required(self, current_key: tuple[str, int], item: MediaItem) -> None:
        if self.current_playback_key != current_key:
            return
        item.need_pay = True
        index = self._index_for_item_key(self._item_key(item))
        if index is not None:
            self.list.SetItem(index, 0, self._display_item_title(item))
        self.SetStatusText(f"跳过需要购买的音频: {item.title}")
        wx.CallAfter(self._play_next_from_current_list, current_key)

    def _next_playable_index(self, current_key: tuple[str, int]) -> int | None:
        current_index = self._index_for_item_key(current_key)
        if current_index is None:
            return None
        for index in range(current_index + 1, len(self.items)):
            if self._is_auto_playable_item(self.items[index]):
                return index
        return None

    @staticmethod
    def _is_auto_playable_item(item: MediaItem) -> bool:
        return item.kind == "sound" and not item.is_collection and not item.need_pay

    def _index_for_item_key(self, key: tuple[str, int]) -> int | None:
        for index, item in enumerate(self.items):
            if self._item_key(item) == key:
                return index
        return None

    @staticmethod
    def _item_key(item: MediaItem) -> tuple[str, int]:
        return (item.kind, item.id)

    def _run_background(
        self,
        status: str,
        work: Callable[[], object],
        done: Callable[[object], None],
        on_purchase_required: Callable[[PurchaseRequired], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.SetStatusText(status)
        self.search_button.Enable(False)

        def runner() -> None:
            try:
                result = work()
            except PurchaseRequired as exc:
                if on_purchase_required is not None:
                    wx.CallAfter(on_purchase_required, exc)
                else:
                    wx.CallAfter(self.show_purchase_required, str(exc))
            except DrmUnsupported as exc:
                wx.CallAfter(self.show_error, str(exc))
            except ApiError as exc:
                if str(exc) == "需要登录":
                    wx.CallAfter(self._mark_account_logged_out, "需要登录")
                if on_error is not None:
                    wx.CallAfter(on_error, str(exc))
                else:
                    wx.CallAfter(self.show_error, str(exc))
            except (requests.RequestException, ValueError) as exc:
                if on_error is not None:
                    wx.CallAfter(on_error, str(exc))
                else:
                    wx.CallAfter(self.show_error, str(exc))
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if on_error is not None:
                    wx.CallAfter(on_error, message)
                else:
                    wx.CallAfter(self.show_error, message)
            else:
                wx.CallAfter(done, result)
            finally:
                wx.CallAfter(self.search_button.Enable, True)

        threading.Thread(target=runner, daemon=True).start()

    def on_account_exit(self, _event: wx.CommandEvent) -> None:
        self.Close()

    def _selected_shortcut_item(self) -> MediaItem | None:
        if self.FindFocus() is not self.list:
            return None
        index = self._selected_index()
        if 0 <= index < len(self.items):
            return self.items[index]
        return None

    def on_item_detail_shortcut(self, _event: wx.CommandEvent) -> None:
        item = self._selected_shortcut_item()
        if item is None or not self._can_show_item_menu(item):
            return
        if item.kind == "sound":
            self.show_sound_intro(item)
        else:
            self.show_drama_detail(item)

    def on_item_comments_shortcut(self, _event: wx.CommandEvent) -> None:
        item = self._selected_shortcut_item()
        if item is not None and self._can_show_comments_menu(item):
            self.show_comments(item)

    def on_item_browser_shortcut(self, _event: wx.CommandEvent) -> None:
        item = self._selected_shortcut_item()
        if item is not None and self._can_show_item_menu(item):
            self.open_item_in_browser(item)

    def on_char_hook(self, event: wx.KeyEvent) -> None:
        if self.FindFocus() is self.list:
            key = event.GetKeyCode()
            if key == wx.WXK_MENU or (key == wx.WXK_F10 and event.ShiftDown()):
                self._show_selected_item_menu(wx.Point(10, 10))
                return
            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                if event.GetModifiers() in (wx.MOD_SHIFT, wx.MOD_ALT, wx.MOD_ALT | wx.MOD_SHIFT):
                    # Let the frame accelerator consume the key before the native list control.
                    event.Skip()
                    return
                index = self._selected_index()
                if 0 <= index < len(self.items):
                    item = self.items[index]
                    if event.GetModifiers() == 0:
                        self.open_item(item)
                        return

        if event.GetKeyCode() == wx.WXK_BACK and self.FindFocus() is not self.search_box:
            if self.current_title.startswith("搜索"):
                self.load_homepage(focus_list=self.FindFocus() is self.list)
                return
            if self.go_back():
                return

        event.Skip()

    def go_back(self) -> bool:
        if not self.navigation_stack:
            return False
        state = self.navigation_stack.pop()
        self.page_state = state.page_state
        self.set_items(
            state.items,
            state.title,
            state.selected_index,
            top_index=state.top_index,
            hide_detail_column=state.hide_detail_column,
            opened_drama_id=state.opened_drama_id,
        )
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
        except PlayerUnavailable as exc:
            self.show_error(str(exc))

    def _toggle_pause(self) -> None:
        try:
            player = self._current_player()
            if player is None:
                self.SetStatusText("没有正在播放的内容")
                return
            player.toggle_pause()
        except PlayerUnavailable as exc:
            self.show_error(str(exc))

    def _stop(self) -> None:
        if self.player_frame is not None:
            self.player_frame.Close()
            return
        if self.active_player is not None:
            self.active_player.stop()
        self.active_player = None
        self.current_playback_key = None
        self.SetStatusText("已停止播放")

    def _current_player(self) -> HiddenBrowserPlayer | None:
        return self.active_player

    def show_purchase_required(self, message: str) -> None:
        if not message.startswith("《"):
            message = f"《{message}》为付费内容。"
        wx.MessageBox(message, "需要购买", wx.OK | wx.ICON_INFORMATION, self)

    def show_error(self, message: str) -> None:
        self.SetStatusText("操作失败")
        wx.MessageBox(message or "未知错误", "错误", wx.OK | wx.ICON_ERROR, self)

    def on_close(self, event: wx.CloseEvent) -> None:
        self._publisher_request = object()
        self._audio_poll_generation += 1
        self.audio_output_router.close()
        if self.player_frame is not None:
            self.player_frame._close_readers()
            self.player_frame.Destroy()
            self.player_frame = None
        self.active_player = None
        self.current_playback_key = None
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
        if not run_startup_update_check():
            return False
        frame = MaoerFrame()
        frame.Show()
        if frame.settings.startup_sound:
            play_startup_sound()
        return True


def main() -> int:
    update_result = handle_update_cli(sys.argv)
    if update_result is not None:
        return update_result
    app = MaoerApp(False)
    app.MainLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
