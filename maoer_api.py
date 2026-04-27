from __future__ import annotations

import json
import html
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse

import requests

from app_paths import cookie_path


BASE_URL = "https://www.missevan.com"
COMMENT_TARGET_SOUND = 1
COMMENT_SORT_NEWEST = 1
COMMENT_SORT_HOTTEST = 3
DANMAKU_MODE_SUBTITLE = 4
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) "
    "Gecko/20100101 Firefox/149.0"
)


class MaoerError(Exception):
    pass


class ApiError(MaoerError):
    pass


class PurchaseRequired(MaoerError):
    pass


class DrmUnsupported(MaoerError):
    pass


@dataclass(slots=True)
class MediaItem:
    kind: str
    id: int
    title: str
    subtitle: str = ""
    duration_ms: int | None = None
    need_pay: bool = False
    pay_type: int | None = None
    drama_id: int | None = None
    album_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_collection(self) -> bool:
        return self.kind in {"drama", "album"}


@dataclass(slots=True)
class PlaybackInfo:
    sound_id: int
    title: str
    url: str
    drama_id: int | None = None
    page_url: str | None = None
    drm: bool = False
    duration_ms: int | None = None


@dataclass(slots=True)
class DanmakuItem:
    time: float
    text: str
    mode: int = 0
    size: int = 0
    color: int = 0
    created_at: str = ""
    user_id: str = ""
    danmaku_id: str = ""


@dataclass(slots=True)
class LoginCaptcha:
    gt: str
    challenge: str
    voice_url: str


@dataclass(slots=True)
class AccountInfo:
    user_id: int | None
    nickname: str
    text: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CheckInResult:
    success: bool
    message: str
    fish_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommentItem:
    id: int
    username: str
    content: str
    created_at: str = ""
    like_count: int = 0
    reply_count: int = 0
    preview_replies: list["CommentItem"] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommentPage:
    comments: list[CommentItem]
    page: int
    max_page: int
    has_more: bool
    total: int = 0


def _to_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip() not in {"", "0", "false", "False", "none", "None"}
    return bool(value)


def _duration_ms(value: Any) -> int | None:
    duration = _to_int(value)
    if duration is None:
        return None
    if 0 < duration < 10000:
        return duration * 1000
    return duration


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class MaoerApi:
    """Small wrapper around the Maoer FM endpoints found in the packet capture."""

    def __init__(self, cookie: str | None = None, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self._account_info_cache: AccountInfo | None = None
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/",
            }
        )
        cookie = cookie if cookie is not None else self._load_cookie()
        if cookie:
            self.session.headers["Cookie"] = cookie
        self.cookie_header = cookie

    def _load_cookie(self) -> str:
        env_cookie = os.environ.get("MAOER_COOKIE", "").strip()
        if env_cookie:
            return env_cookie

        for cookie_file in (cookie_path("cookey", create_parent=False), cookie_path("cookies.txt", create_parent=False)):
            if cookie_file.exists():
                return cookie_file.read_text(encoding="utf-8").strip()

        for cookie_file in self._legacy_cookie_paths("cookey", "cookies.txt"):
            if cookie_file.exists():
                cookie = cookie_file.read_text(encoding="utf-8").strip()
                if cookie:
                    self.save_cookie(cookie)
                return cookie
        return ""

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else BASE_URL + path
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise ApiError(self._payload_message(data))
        return data

    def _get_json_allow_failure(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else BASE_URL + path
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ApiError("接口返回格式不正确")
        return data

    def _get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = path if path.startswith("http") else BASE_URL + path
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.text

    def _post(self, path: str, params: dict[str, Any] | None = None) -> None:
        url = path if path.startswith("http") else BASE_URL + path
        self.session.post(url, params=params, timeout=self.timeout)

    def start_login_captcha(self) -> LoginCaptcha:
        self.session.get(
            BASE_URL + "/member/login",
            params={"backurl": BASE_URL + "/"},
            timeout=self.timeout,
        ).raise_for_status()
        login_cookie = self._session_cookie_header()
        if login_cookie:
            self.session.headers["Cookie"] = login_cookie

        data = self._get("/x/captcha/challenge", {"scene": "login"})
        info = data.get("info") or {}
        params = info.get("params") or {}
        gt = _text(params.get("gt"))
        challenge = _text(params.get("challenge"))
        if not gt or not challenge:
            raise ApiError("没有拿到验证码参数")

        self._geetest_gettype(gt)
        callback = self._jsonp_callback()
        response = self.session.get(
            "https://api.geetest.com/get.php",
            params={
                "gt": gt,
                "challenge": challenge,
                "type": "voice",
                "lang": "zh-cn",
                "callback": callback,
            },
            headers=self._geetest_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = self._parse_jsonp(response.text)
        voice_url = self._extract_geetest_voice_url(payload)
        if not voice_url:
            self._geetest_unlock_fullpage(gt, challenge)
            payload = self._geetest_voice(gt, challenge)
            voice_url = self._extract_geetest_voice_url(payload)
        if not voice_url:
            payload = self._geetest_voice_fallback(gt, challenge)
            voice_url = self._extract_geetest_voice_url(payload)
        if not voice_url:
            raise ApiError("没有拿到语音验证码地址，请重新点击获取验证码")
        return LoginCaptcha(gt=gt, challenge=challenge, voice_url=voice_url)

    def send_login_sms_code(self, phone: str, captcha: LoginCaptcha, voice_answer: str) -> None:
        voice_answer = voice_answer.strip()
        if not voice_answer:
            raise ApiError("请输入语音验证码")

        callback = self._jsonp_callback()
        response = self.session.get(
            "https://api.geetest.com/ajax.php",
            params={
                "gt": captcha.gt,
                "challenge": captcha.challenge,
                "a": voice_answer,
                "lang": "zh-cn",
                "callback": callback,
            },
            headers=self._geetest_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = self._parse_jsonp(response.text)
        data = payload.get("data") or {}
        if payload.get("status") != "success" or data.get("result") != "success":
            raise ApiError("语音验证码不正确")

        validate = _text(data.get("validate"))
        if not validate:
            raise ApiError("没有拿到验证码校验结果")
        captcha_token = f"geetest|{captcha.challenge}|{validate}|{validate}|jordan"
        self._post_form_json(
            "/account/sendcode",
            {
                "login_name": phone,
                "post_type": "16",
                "region": "CN",
                "captcha_token": captcha_token,
            },
        )

    def sms_login(self, phone: str, sms_code: str) -> str:
        self._post_form_json(
            "/account/smslogin",
            {
                "mobile": phone,
                "identify_code": sms_code,
                "remember_me": "1",
                "region": "CN",
            },
        )
        cookie = self._session_cookie_header()
        if not cookie:
            raise ApiError("登录成功但没有拿到 Cookie")
        self._account_info_cache = None
        self.cookie_header = cookie
        self.session.headers["Cookie"] = cookie
        return cookie

    def account_nickname(self) -> str:
        return self.account_info().nickname

    def check_in(self) -> CheckInResult:
        data = self._get_json_allow_failure("/member/getcatears", {"gtype": 1})
        if data.get("success") is True:
            info = data.get("info") or {}
            if isinstance(info, dict):
                message = self._first_scalar_value(info, ("message", "msg", "text", "content"))
            else:
                message = _text(info)
            message = message.strip() or "签到成功"
            self._account_info_cache = None
            return CheckInResult(True, message, self._fish_count_from_text(message), data)

        message = self._payload_message(data)
        if message == "需要登录":
            raise ApiError(message)
        return CheckInResult(False, message, self._fish_count_from_text(message), data)

    def account_info(self, user_id: int | None = None) -> AccountInfo:
        if user_id is None and self._account_info_cache is not None:
            return self._account_info_cache

        params = {"user_id": user_id} if user_id is not None else None
        data = self._get("/account/userinfo", params)
        info = data.get("info") or {}
        if not isinstance(info, dict):
            raise ApiError("没有拿到账号信息")

        resolved_user_id = self._first_direct_int_value(
            info,
            ("id", "user_id", "uid", "userid", "member_id"),
        )
        if resolved_user_id is None:
            resolved_user_id = self._first_int_value(
                info,
                ("id", "user_id", "uid", "userid", "member_id"),
            )

        nickname = self._first_scalar_value(
            info,
            ("nickname", "nick_name", "username", "user_name", "name", "uname"),
        )
        if not nickname:
            raise ApiError("没有拿到账号昵称")

        display_info = dict(info)
        if resolved_user_id is not None:
            follow_count = self._account_attention_count(resolved_user_id, 0)
            fans_count = self._account_attention_count(resolved_user_id, 1)
            if follow_count is not None:
                display_info["followNum"] = follow_count
            if fans_count is not None:
                display_info["fansNum"] = fans_count

        account_info = AccountInfo(
            user_id=resolved_user_id,
            nickname=nickname,
            text=self._account_info_text(display_info),
            raw=display_info,
        )
        if user_id is None:
            self._account_info_cache = account_info
        return account_info

    def _account_attention_count(self, user_id: int, attention_type: int) -> int | None:
        try:
            data = self._get(
                "/person/getuserattention",
                {"type": attention_type, "user_id": user_id, "page_size": 1, "p": 1},
            )
        except (ApiError, requests.RequestException, ValueError):
            return None
        info = data.get("info") or {}
        if not isinstance(info, dict):
            return None
        pagination = info.get("pagination") or {}
        if not isinstance(pagination, dict):
            return None
        return _to_int(pagination.get("count"))

    def drama_detail_text(self, drama_id: int) -> str:
        data = self._get("/dramaapi/getdrama", {"drama_id": drama_id})
        info = data.get("info") or {}
        drama = info.get("drama") or {}
        episodes = info.get("episodes") or {}

        lines: list[str] = []
        self._append_field(lines, "名称", drama.get("name"))
        self._append_field(lines, "ID", drama.get("id") or drama_id)
        self._append_field(lines, "分类", drama.get("catalog_name") or drama.get("catalog"))
        self._append_field(lines, "作者", drama.get("author"))
        self._append_field(lines, "原作", drama.get("original_author") or drama.get("origin_author"))
        self._append_field(lines, "状态", drama.get("status_name") or drama.get("status"))
        self._append_field(lines, "最新", drama.get("newest"))
        self._append_field(lines, "播放", drama.get("view_count") or drama.get("play_count"))
        self._append_field(lines, "收藏", drama.get("favorite_count") or drama.get("collect_count"))
        self._append_field(lines, "更新时间", drama.get("update_time") or drama.get("updated_at"))

        intro = (
            drama.get("abstract")
            or drama.get("intro")
            or drama.get("description")
            or drama.get("summary")
            or drama.get("content")
        )
        self._append_field(lines, "简介", self._html_to_text(intro))

        if isinstance(episodes, dict):
            counts = []
            for key, label in (("episode", "正剧"), ("ft", "花絮"), ("music", "音乐")):
                value = episodes.get(key)
                if isinstance(value, list):
                    counts.append(f"{label}{len(value)}集")
            if counts:
                lines.append("分集：" + "，".join(counts))

        cv_lines = self._drama_cv_lines(info)
        if cv_lines:
            if lines:
                lines.append("")
            lines.extend(cv_lines)

        return "\n".join(lines).strip() or "没有拿到广播剧详情"

    def _drama_cv_lines(self, info: dict[str, Any]) -> list[str]:
        cvs = info.get("cvs")
        if not isinstance(cvs, list):
            return []

        title = _text(info.get("cv_module_title") or "参演 CV").strip() or "参演 CV"
        lines = [f"{title}："]
        seen: set[str] = set()
        for item in cvs:
            if not isinstance(item, dict):
                continue

            character = _text(
                item.get("character")
                or item.get("character_name")
                or item.get("role")
                or item.get("role_name")
            ).strip()

            cv_name = ""
            cv_info = item.get("cv_info")
            if isinstance(cv_info, dict):
                cv_name = _text(
                    cv_info.get("name")
                    or cv_info.get("nickname")
                    or cv_info.get("cv_name")
                    or cv_info.get("username")
                ).strip()
            if not cv_name:
                cv_name = _text(item.get("cv_name") or item.get("cv") or item.get("name")).strip()

            if character and cv_name:
                line = f"{character}：{cv_name}"
            else:
                line = character or cv_name
            if line and line not in seen:
                seen.add(line)
                lines.append(line)

        return lines if len(lines) > 1 else []

    def item_drama_id(self, item: MediaItem) -> int:
        if item.kind == "drama":
            return item.id
        if item.drama_id is not None:
            return item.drama_id

        drama_id = self._first_int_value(
            item.raw,
            ("drama_id", "dramaId", "radio_drama_id", "radioDramaId"),
        )
        if drama_id is not None:
            return drama_id

        if item.kind == "sound":
            return self.drama_id_by_sound(item.id)
        raise ApiError("当前项目不是广播剧")

    def drama_id_by_sound(self, sound_id: int) -> int:
        data = self._get("/dramaapi/getdramabysound", {"sound_id": sound_id})
        info = data.get("info") or {}
        drama_id = self._first_int_value(
            info,
            ("id", "drama_id", "dramaId", "radio_drama_id", "radioDramaId"),
        )
        if drama_id is None:
            raise ApiError("没有找到这个声音所属的广播剧")
        return drama_id

    def sound_comments(
        self,
        sound_id: int,
        order: int = COMMENT_SORT_HOTTEST,
        page: int = 1,
        page_size: int = 20,
    ) -> CommentPage:
        return self.comments(
            target_type=COMMENT_TARGET_SOUND,
            target_id=sound_id,
            order=order,
            page=page,
            page_size=page_size,
        )

    def sound_danmaku(self, sound_id: int) -> list[DanmakuItem]:
        xml_text = self._get_text("/sound/getdm", {"soundid": int(sound_id)})
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ApiError(f"弹幕数据解析失败: {exc}") from exc

        items: list[DanmakuItem] = []
        for element in root.findall(".//d"):
            text = (element.text or "").strip()
            if not text:
                continue
            parts = (element.get("p") or "").split(",")
            items.append(
                DanmakuItem(
                    time=_to_float(parts[0]) if len(parts) > 0 else 0.0,
                    text=text,
                    mode=(_to_int(parts[1], 0) or 0) if len(parts) > 1 else 0,
                    size=(_to_int(parts[2], 0) or 0) if len(parts) > 2 else 0,
                    color=(_to_int(parts[3], 0) or 0) if len(parts) > 3 else 0,
                    created_at=self._danmaku_time(parts[4]) if len(parts) > 4 else "",
                    user_id=parts[6] if len(parts) > 6 else "",
                    danmaku_id=parts[7] if len(parts) > 7 else "",
                )
            )
        return sorted(items, key=lambda item: item.time)

    def sound_subtitles(self, sound_id: int) -> list[DanmakuItem]:
        return [item for item in self.sound_danmaku(sound_id) if item.mode == DANMAKU_MODE_SUBTITLE]

    def comments(
        self,
        target_type: int,
        target_id: int,
        order: int = COMMENT_SORT_HOTTEST,
        page: int = 1,
        page_size: int = 20,
    ) -> CommentPage:
        data = self._get(
            "/site/getcomment",
            {
                "type": int(target_type),
                "e_id": int(target_id),
                "order": int(order),
                "p": int(page),
                "pagesize": int(page_size),
            },
        )
        info = data.get("info") or {}
        comment = info.get("comment") if isinstance(info, dict) else {}
        if not isinstance(comment, dict):
            comment = {}
        raw_items = comment.get("Datas") or []
        comments = [self._comment_item(item) for item in raw_items if isinstance(item, dict)]
        return self._comment_page(comment, comments, page, page_size)

    def comment_replies(self, comment_id: int, page: int = 1, page_size: int = 20) -> CommentPage:
        data = self._get(
            "/site/getsubcomment",
            {"c_id": int(comment_id), "p": int(page), "pagesize": int(page_size)},
        )
        info = data.get("info") or {}
        subcomment = info.get("subcomment") if isinstance(info, dict) else {}
        if not isinstance(subcomment, dict):
            subcomment = {}
        raw_items = subcomment.get("Datas") or []
        comments = [self._comment_item(item) for item in raw_items if isinstance(item, dict)]
        return self._comment_page(subcomment, comments, page, page_size)

    def save_cookie(self, cookie: str, filename: str = "cookey") -> Path:
        path = cookie_path(filename)
        path.write_text(cookie.strip(), encoding="utf-8")
        return path

    def saved_cookie_path(self, filename: str = "cookey") -> Path:
        return cookie_path(filename)

    def set_cookie(self, cookie: str) -> None:
        self._account_info_cache = None
        self.cookie_header = cookie.strip()
        if self.cookie_header:
            self.session.headers["Cookie"] = self.cookie_header
        else:
            self.session.headers.pop("Cookie", None)

    def clear_saved_cookie(self, filename: str = "cookey") -> None:
        for path in (cookie_path(filename, create_parent=False), Path(__file__).with_name(filename)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _legacy_cookie_paths(*filenames: str) -> tuple[Path, ...]:
        root = Path(__file__).resolve().parent
        return tuple(root / filename for filename in filenames)

    def _post_form_json(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        url = path if path.startswith("http") else BASE_URL + path
        response = self.session.post(
            url,
            data=data,
            headers={
                "Accept": "application/json",
                "Referer": BASE_URL + "/member/login?backurl=https%3A%2F%2Fwww.missevan.com%2F",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ApiError(self._payload_message(payload))
        return payload

    def _session_cookie_header(self) -> str:
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.session.cookies)

    @staticmethod
    def _jsonp_callback() -> str:
        return "geetest_" + str(int(time.time() * 1000))

    @staticmethod
    def _parse_jsonp(text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("{"):
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            raise ApiError("验证码接口返回格式不正确")
        start = text.find("(")
        end = text.rfind(")")
        if start == -1 or end == -1 or end <= start:
            raise ApiError("验证码接口返回格式不正确")
        data = json.loads(text[start + 1 : end])
        if not isinstance(data, dict):
            raise ApiError("验证码接口返回格式不正确")
        return data

    @staticmethod
    def _absolute_geetest_url(server: str, path: str) -> str:
        if not path:
            return ""
        if path.startswith("http"):
            return path
        server = server.strip()
        if not server.startswith("http"):
            server = "https://" + server
        return server.rstrip("/") + "/" + path.lstrip("/")

    def _geetest_gettype(self, gt: str) -> None:
        response = self.session.get(
            "https://api.geetest.com/gettype.php",
            params={"gt": gt, "callback": self._jsonp_callback()},
            headers=self._geetest_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()

    def _geetest_voice_fallback(self, gt: str, challenge: str) -> dict[str, Any]:
        response = self.session.get(
            "https://api.geetest.com/get.php",
            params={
                "is_next": "true",
                "type": "voice",
                "gt": gt,
                "challenge": challenge,
                "lang": "zh-cn",
                "https": "false",
                "protocol": "https://",
                "offline": "false",
                "product": "embed",
                "api_server": "api.geetest.com",
                "isPC": "true",
                "autoReset": "true",
                "width": "100%",
                "callback": self._jsonp_callback(),
            },
            headers=self._geetest_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._parse_jsonp(response.text)

    def _geetest_voice(self, gt: str, challenge: str) -> dict[str, Any]:
        response = self.session.get(
            "https://api.geetest.com/get.php",
            params={
                "gt": gt,
                "challenge": challenge,
                "type": "voice",
                "lang": "zh-cn",
                "callback": self._jsonp_callback(),
            },
            headers=self._geetest_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._parse_jsonp(response.text)

    def _geetest_unlock_fullpage(self, gt: str, challenge: str) -> None:
        response = self.session.get(
            "https://api.geetest.com/ajax.php",
            params={
                "gt": gt,
                "challenge": challenge,
                "lang": "zh-cn",
                "pt": "0",
                "client_type": "web",
                "w": "",
                "callback": self._jsonp_callback(),
            },
            headers=self._geetest_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()

    @staticmethod
    def _geetest_headers() -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Referer": BASE_URL + "/",
        }

    def _extract_geetest_voice_url(self, payload: dict[str, Any]) -> str:
        voice_path = self._find_first_string(
            payload,
            lambda value: "/voice/" in value and value.lower().endswith(".mp3"),
        )
        if not voice_path:
            voice_path = self._find_first_string(payload, lambda value: value.lower().endswith(".mp3"))
        if not voice_path:
            return ""
        server = self._find_first_string(
            payload,
            lambda value: "static.geetest.com" in value or "static.geevisit.com" in value,
        )
        if not server:
            server = "static.geetest.com/"
        return self._absolute_geetest_url(server, voice_path)

    @staticmethod
    def _find_first_string(value: Any, predicate) -> str:
        if isinstance(value, str):
            return value if predicate(value) else ""
        if isinstance(value, dict):
            preferred_keys = (
                "voice_path",
                "new_voice_path",
                "voicePath",
                "audio",
                "audio_path",
                "static_servers",
                "resource_servers",
                "data",
            )
            for key in preferred_keys:
                if key in value:
                    found = MaoerApi._find_first_string(value[key], predicate)
                    if found:
                        return found
            for item in value.values():
                found = MaoerApi._find_first_string(item, predicate)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = MaoerApi._find_first_string(item, predicate)
                if found:
                    return found
        return ""

    @staticmethod
    def _first_named_value(value: Any, names: tuple[str, ...]) -> str:
        if isinstance(value, dict):
            for name in names:
                candidate = value.get(name)
                if candidate:
                    return _text(candidate)
            lower_names = {name.lower() for name in names}
            for key, candidate in value.items():
                if isinstance(key, str) and key.lower() in lower_names and candidate:
                    return _text(candidate)
            for item in value.values():
                found = MaoerApi._first_named_value(item, names)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = MaoerApi._first_named_value(item, names)
                if found:
                    return found
        return ""

    @staticmethod
    def _first_scalar_value(value: Any, names: tuple[str, ...]) -> str:
        if isinstance(value, dict):
            for name in names:
                candidate = value.get(name)
                if isinstance(candidate, (dict, list)) or candidate is None:
                    continue
                text = _text(candidate).strip()
                if text:
                    return text
            lower_names = {name.lower() for name in names}
            for key, candidate in value.items():
                if not isinstance(key, str) or key.lower() not in lower_names:
                    continue
                if isinstance(candidate, (dict, list)) or candidate is None:
                    continue
                text = _text(candidate).strip()
                if text:
                    return text
            for item in value.values():
                found = MaoerApi._first_scalar_value(item, names)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = MaoerApi._first_scalar_value(item, names)
                if found:
                    return found
        return ""

    @staticmethod
    def _first_direct_int_value(value: Any, names: tuple[str, ...]) -> int | None:
        if not isinstance(value, dict):
            return None
        for name in names:
            candidate = _to_int(value.get(name))
            if candidate is not None:
                return candidate
        lower_names = {name.lower() for name in names}
        for key, candidate in value.items():
            if isinstance(key, str) and key.lower() in lower_names:
                candidate_int = _to_int(candidate)
                if candidate_int is not None:
                    return candidate_int
        return None

    @staticmethod
    def _first_int_value(value: Any, names: tuple[str, ...]) -> int | None:
        if isinstance(value, dict):
            for name in names:
                candidate = _to_int(value.get(name))
                if candidate is not None:
                    return candidate
            lower_names = {name.lower() for name in names}
            for key, candidate in value.items():
                if isinstance(key, str) and key.lower() in lower_names:
                    candidate_int = _to_int(candidate)
                    if candidate_int is not None:
                        return candidate_int
            for item in value.values():
                found = MaoerApi._first_int_value(item, names)
                if found is not None:
                    return found
        if isinstance(value, list):
            for item in value:
                found = MaoerApi._first_int_value(item, names)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _append_field(lines: list[str], label: str, value: Any) -> None:
        text = _text(value).strip()
        if text:
            lines.append(f"{label}：{text}")

    @staticmethod
    def _html_to_text(value: Any) -> str:
        text = _text(value)
        if not text:
            return ""
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    def _comment_page(
        self,
        payload: dict[str, Any],
        comments: list[CommentItem],
        fallback_page: int,
        fallback_page_size: int,
    ) -> CommentPage:
        pagination = payload.get("pagination") or {}
        if not isinstance(pagination, dict):
            pagination = {}

        page = _to_int(pagination.get("p") or pagination.get("page"), fallback_page) or fallback_page
        max_page = (
            _to_int(pagination.get("maxpage") or pagination.get("page_count"), None)
            or _to_int(payload.get("maxpage") or payload.get("page_count"), None)
            or page
        )
        total = (
            _to_int(payload.get("num"), None)
            or _to_int(pagination.get("count"), None)
            or len(comments)
        )
        raw_has_more = pagination.get("hasMore")
        if raw_has_more is None:
            raw_has_more = payload.get("hasMore")
        has_more = _to_bool(raw_has_more) if raw_has_more is not None else page < max_page

        page_size = _to_int(pagination.get("pagesize"), fallback_page_size) or fallback_page_size
        if page_size > 0 and total > page_size:
            max_page = max(max_page, (total + page_size - 1) // page_size)
            has_more = has_more or page < max_page

        return CommentPage(
            comments=comments,
            page=page,
            max_page=max_page,
            has_more=has_more,
            total=total,
        )

    def _comment_item(self, raw: dict[str, Any]) -> CommentItem:
        comment_id = _to_int(raw.get("id"), 0) or 0
        preview_raw = raw.get("subcomments") or []
        preview_replies = [
            self._comment_item(item)
            for item in preview_raw
            if isinstance(item, dict)
        ]
        return CommentItem(
            id=comment_id,
            username=self._comment_username(raw),
            content=self._comment_content(raw.get("comment_content")),
            created_at=self._comment_time(raw.get("ctime")),
            like_count=_to_int(raw.get("like_num"), 0) or 0,
            reply_count=_to_int(raw.get("sub_comment_num"), 0) or 0,
            preview_replies=preview_replies,
            raw=raw,
        )

    def _comment_username(self, raw: dict[str, Any]) -> str:
        username = _text(raw.get("username")).strip()
        if username:
            return username
        user = raw.get("user")
        if isinstance(user, dict):
            username = _text(user.get("username") or user.get("name") or user.get("nickname")).strip()
            if username:
                return username
        return "匿名用户"

    def _comment_content(self, value: Any) -> str:
        text = self._html_to_text(value)
        return text.replace("\u200b", "").strip()

    @staticmethod
    def _comment_time(value: Any) -> str:
        timestamp = _to_int(value)
        if timestamp is None or timestamp <= 0:
            return ""
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return _text(value)

    @staticmethod
    def _danmaku_time(value: Any) -> str:
        timestamp = _to_int(value)
        if timestamp is None or timestamp <= 0:
            return ""
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return _text(value)

    def _account_info_text(self, info: dict[str, Any]) -> str:
        fields: list[tuple[str, tuple[str, ...]]] = [
            ("昵称", ("nickname", "nick_name", "username", "user_name", "name", "uname")),
            ("用户ID", ("id", "user_id", "uid", "userid", "member_id")),
            ("等级", ("level", "lv", "user_level", "rank")),
            ("小鱼干", ("point", "fish", "fish_count", "catears", "cat_ears")),
            ("钻石余额", ("balance", "diamond", "diamond_balance", "diamonds")),
            ("关注数", ("followNum", "follow_num", "follow_count", "following_count", "follows", "following_num", "attention_count")),
            ("粉丝数", ("fansNum", "fans_num", "fans_count", "fan_count", "followers_count", "follower_count", "follower_num")),
            ("声音数", ("sound_count", "sounds_count", "sound_num", "music_count")),
            ("广播剧数", ("drama_count", "dramas_count", "drama_num")),
            ("订阅广播剧", ("subscription_count", "subscriptions_count", "subscribe_count", "subscribed_count")),
            ("收藏数", ("favorite_count", "favorites_count", "collect_count", "collection_count")),
            ("播放数", ("play_count", "view_count", "views_count")),
            ("简介", ("intro", "signature", "sign", "description", "desc")),
        ]

        lines: list[str] = []
        seen_labels: set[str] = set()
        for label, names in fields:
            value = self._first_scalar_value(info, names)
            if not value:
                continue
            if label == "简介":
                value = self._html_to_text(value)
            if value:
                lines.append(f"{label}：{value}")
                seen_labels.add(label)

        auth_info = info.get("auth_info")
        if isinstance(auth_info, dict):
            auth_title = self._first_scalar_value(auth_info, ("title", "name", "auth_name"))
            auth_subtitle = self._first_scalar_value(auth_info, ("subtitle", "desc", "description"))
            if auth_title and "认证" not in seen_labels:
                lines.append(f"认证：{auth_title}")
            if auth_subtitle:
                lines.append(f"认证说明：{auth_subtitle}")

        return "\n".join(lines).strip() or "没有拿到可显示的账号信息"

    @staticmethod
    def _payload_message(payload: dict[str, Any]) -> str:
        code = _to_int(payload.get("code"))
        if code == 100010006:
            return "需要登录"
        info = payload.get("info")
        if isinstance(info, list) and info:
            item = info[0]
            if isinstance(item, dict):
                return _text(item.get("message") or item.get("msg") or "接口返回失败")
        if isinstance(info, dict):
            return _text(info.get("message") or info.get("msg") or "接口返回失败")
        if code == 100010007:
            return _text(info or "参数有误")
        return _text(payload.get("message") or payload.get("msg") or info or "接口返回失败")

    @staticmethod
    def _fish_count_from_text(text: str) -> int | None:
        match = re.search(r"小鱼干\s*[×xX*]\s*(\d+)", text)
        if not match:
            return None
        return _to_int(match.group(1))

    def homepage(self) -> list[MediaItem]:
        self._open_homepage_shell()

        items: list[MediaItem] = []

        try:
            data = self._get("/site/homepage")
            info = data.get("info") or {}

            sounds = info.get("sounds") or {}
            for section_name, section_items in sounds.items():
                label = self._homepage_label(section_name)
                for sound in section_items or []:
                    item = self._sound_item(sound, subtitle=label)
                    if item:
                        items.append(item)

            for album in info.get("albums") or []:
                item = self._album_item(album)
                if item:
                    items.append(item)

            for link in info.get("links") or []:
                item = self._link_item(link)
                if item:
                    items.append(item)
        except (ApiError, requests.RequestException, ValueError):
            pass

        for loader in (self._homepage_drama_sections, self._homepage_drama_rank):
            try:
                items.extend(loader())
            except (ApiError, requests.RequestException, ValueError):
                pass

        return self._dedupe_items(items)

    def _open_homepage_shell(self) -> None:
        try:
            self._get_text("/")
        except requests.RequestException:
            pass

    def _homepage_drama_sections(self) -> list[MediaItem]:
        data = self._get("/dramaapi/summerdrama")
        groups = data.get("info") or []
        items: list[MediaItem] = []
        for index, group in enumerate(groups):
            if not isinstance(group, list):
                continue
            section = self._homepage_drama_section_label(index)
            for drama in group:
                if not isinstance(drama, dict):
                    continue
                item = self._drama_item(drama, fallback_subtitle=section)
                if item:
                    items.append(item)
        return items

    def _homepage_drama_rank(self) -> list[MediaItem]:
        data = self._get(
            "/reward/drama-reward-rank",
            {"period": 1, "page": 1, "page_size": 10},
        )
        ranks = ((data.get("info") or {}).get("ranks") or {}).get("Datas") or []
        items: list[MediaItem] = []
        for rank, drama in enumerate(ranks, start=1):
            if not isinstance(drama, dict):
                continue
            item = self._drama_item(drama, fallback_subtitle=f"首页 / 打赏榜第 {rank} 名")
            if item:
                items.append(item)
        return items

    def _dedupe_items(self, items: list[MediaItem]) -> list[MediaItem]:
        seen: set[tuple[str, int]] = set()
        result: list[MediaItem] = []
        for item in items:
            key = (item.kind, item.id)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def search(self, keyword: str, page: int = 1, page_size: int = 30) -> list[MediaItem]:
        keyword = keyword.strip()
        if not keyword:
            return self.homepage()

        items: list[MediaItem] = []

        drama_data = self._get("/dramaapi/search", {"s": keyword, "page": page})
        for drama in ((drama_data.get("info") or {}).get("Datas") or []):
            item = self._drama_item(drama)
            if item:
                items.append(item)

        sound_data = self._get(
            "/sound/getsearch",
            {"s": keyword, "p": page, "type": 3, "page_size": page_size},
        )
        for sound in ((sound_data.get("info") or {}).get("Datas") or []):
            item = self._sound_item(sound, subtitle=_text(sound.get("username")))
            if item:
                items.append(item)

        return items

    def collection_items(self, item: MediaItem) -> list[MediaItem]:
        if item.kind == "drama":
            return self.drama_episodes(item.id)
        if item.kind == "album":
            return self.album_sounds(item.id)
        raise ApiError(f"不支持的列表类型: {item.kind}")

    def drama_episodes(self, drama_id: int) -> list[MediaItem]:
        data = self._get("/dramaapi/getdrama", {"drama_id": drama_id})
        info = data.get("info") or {}
        drama = info.get("drama") or {}
        drama_name = _text(drama.get("name"))
        episodes = info.get("episodes") or {}

        items: list[MediaItem] = []
        for group_key, group_label in (
            ("episode", "正剧"),
            ("ft", "花絮"),
            ("music", "音乐"),
        ):
            for episode in episodes.get(group_key) or []:
                sound_id = _to_int(episode.get("sound_id"))
                if sound_id is None:
                    continue
                title = _text(episode.get("name") or episode.get("soundstr") or sound_id)
                pay_type = _to_int(episode.get("pay_type"))
                items.append(
                    MediaItem(
                        kind="sound",
                        id=sound_id,
                        title=title,
                        subtitle=f"{drama_name} / {group_label}" if drama_name else group_label,
                        duration_ms=_duration_ms(episode.get("duration")),
                        need_pay=_to_bool(episode.get("need_pay")),
                        pay_type=pay_type,
                        drama_id=drama_id,
                        raw=episode,
                    )
                )
        return items

    def drama_episodes_page(self, drama_id: int, page: int = 1, page_size: int = 30) -> list[MediaItem]:
        data = self._get(
            "/dramaapi/getdramaepisodedetails",
            {"drama_id": drama_id, "p": page, "page_size": page_size},
        )
        sounds = ((data.get("info") or {}).get("Datas") or [])
        items: list[MediaItem] = []
        for sound in sounds:
            if not isinstance(sound, dict):
                continue
            item = self._sound_item(sound, subtitle="广播剧")
            if item:
                item.drama_id = drama_id
                items.append(item)
        return items

    def subscribed_dramas(self, page: int = 1, page_size: int = 30) -> list[MediaItem]:
        account = self.account_info()
        if account.user_id is None:
            raise ApiError("没有拿到账号用户 ID，无法加载剧集订阅")
        return self.user_subscribed_dramas(account.user_id, page=page, page_size=page_size)

    def purchased_dramas(self, page: int = 1, page_size: int = 30) -> list[MediaItem]:
        try:
            data = self._get("/mperson/getdramabought", {"page": page, "page_size": page_size})
        except ApiError as exc:
            message = str(exc)
            if "暂无已购" in message or "没有已购" in message:
                return []
            raise
        info = data.get("info") or {}
        if not isinstance(info, dict):
            return []

        dramas = info.get("data") or info.get("Datas") or info.get("datas") or []
        items: list[MediaItem] = []
        for drama in dramas:
            if not isinstance(drama, dict):
                continue
            item = self._purchased_drama_item(drama)
            if item:
                items.append(item)
        return items

    def user_subscribed_dramas(self, user_id: int, page: int = 1, page_size: int = 30) -> list[MediaItem]:
        data = self._get(
            "/dramaapi/getusersubscriptions",
            {"user_id": user_id, "page": page, "p": page, "page_size": page_size},
        )
        info = data.get("info") or {}
        if not isinstance(info, dict):
            return []

        dramas = info.get("Datas") or info.get("datas") or info.get("dramas") or []
        items: list[MediaItem] = []
        for drama in dramas:
            if not isinstance(drama, dict):
                continue
            item = self._subscription_drama_item(drama)
            if item:
                items.append(item)
        return items

    def album_sounds(self, album_id: int) -> list[MediaItem]:
        data = self._get("/sound/soundalllist", {"albumid": album_id})
        info = data.get("info") or {}
        album = info.get("album") or {}
        album_title = _text(album.get("title"))
        items: list[MediaItem] = []
        for sound in info.get("sounds") or []:
            item = self._sound_item(
                sound,
                subtitle=album_title or _text(sound.get("username")),
                album_id=album_id,
            )
            if item:
                items.append(item)
        return items

    def playback_info(self, item: MediaItem) -> PlaybackInfo:
        if item.need_pay:
            raise PurchaseRequired(f"《{item.title}》需要购买后才能播放。")

        data = self._get("/sound/getsound", {"soundid": item.id})
        info = data.get("info") or {}
        sound = info.get("sound") or {}
        title = _text(sound.get("soundstr") or item.title)

        url = _text(sound.get("soundurl") or sound.get("soundurl_128"))
        if not url:
            if _to_bool(sound.get("need_pay")) or _to_int(sound.get("pay_type"), 0):
                raise PurchaseRequired(f"《{title}》需要购买后才能播放。")

        return PlaybackInfo(
            sound_id=item.id,
            title=title,
            url=url,
            drama_id=item.drama_id,
            page_url=f"{BASE_URL}/sound/player?id={item.id}",
            drm=self._is_bili_drm_sound(sound),
            duration_ms=_duration_ms(sound.get("duration")) or item.duration_ms,
        )

    def add_play_times(self, playback: PlaybackInfo) -> None:
        params: dict[str, Any] = {"sound_id": playback.sound_id}
        if playback.drama_id:
            params["drama_id"] = playback.drama_id
        try:
            self._post("/sound/addplaytimes", params=params)
        except requests.RequestException:
            pass

    def _sound_item(
        self,
        sound: dict[str, Any],
        subtitle: str = "",
        album_id: int | None = None,
    ) -> MediaItem | None:
        sound_id = _to_int(sound.get("id"))
        if sound_id is None:
            return None
        pay_type = _to_int(sound.get("pay_type"))
        return MediaItem(
            kind="sound",
            id=sound_id,
            title=_text(sound.get("soundstr") or sound.get("title") or sound_id),
            subtitle=subtitle,
            duration_ms=_duration_ms(sound.get("duration")),
            need_pay=_to_bool(sound.get("need_pay")),
            pay_type=pay_type,
            album_id=album_id,
            raw=sound,
        )

    def _subscription_drama_item(self, data: dict[str, Any]) -> MediaItem | None:
        item = self._drama_item(data, fallback_subtitle="剧集订阅")
        if item:
            return item

        for key in ("drama", "drama_info", "radio_drama", "radioDrama", "mdrama"):
            drama = data.get(key)
            if isinstance(drama, dict):
                item = self._drama_item(drama, fallback_subtitle="剧集订阅")
                if item:
                    return item

        drama_id = self._first_int_value(
            data,
            ("drama_id", "dramaId", "radio_drama_id", "radioDramaId", "mdrama_id"),
        )
        if drama_id is None:
            return None

        title = self._first_scalar_value(
            data,
            ("drama_name", "dramaName", "name", "title", "soundstr"),
        )
        return MediaItem(
            kind="drama",
            id=drama_id,
            title=title or str(drama_id),
            subtitle="剧集订阅",
            pay_type=_to_int(data.get("pay_type")),
            raw=data,
        )

    def _purchased_drama_item(self, data: dict[str, Any]) -> MediaItem | None:
        drama_id = self._first_direct_int_value(
            data,
            ("id", "drama_id", "dramaId", "radio_drama_id", "radioDramaId", "mdrama_id"),
        )
        if drama_id is None:
            return self._drama_item(data, fallback_subtitle="已购广播剧")

        parts = ["已购广播剧"]
        suborders_num = _to_int(data.get("suborders_num"))
        if _to_int(data.get("pay_type")) == 1 and suborders_num is not None:
            parts.append(f"已购 {suborders_num} 集")

        episode_count = _to_int(data.get("episode_count"))
        newest = _text(data.get("newest")).strip()
        if _to_int(data.get("integrity")) == 2 and episode_count is not None:
            parts.append(f"共 {episode_count} 期")
        elif newest:
            parts.append(f"更新至 {newest}")

        return MediaItem(
            kind="drama",
            id=drama_id,
            title=_text(data.get("name") or data.get("drama_name") or data.get("title") or drama_id),
            subtitle=" / ".join(parts),
            pay_type=_to_int(data.get("pay_type")),
            raw=data,
        )

    def _drama_item(self, drama: dict[str, Any], fallback_subtitle: str = "") -> MediaItem | None:
        drama_id = _to_int(drama.get("id"))
        if drama_id is None:
            drama_id = self._first_direct_int_value(
                drama,
                ("drama_id", "dramaId", "radio_drama_id", "radioDramaId", "mdrama_id"),
            )
        if drama_id is None:
            return None
        parts = [
            fallback_subtitle,
            _text(drama.get("catalog_name") or drama.get("catalog")),
            _text(drama.get("author")),
            _text(drama.get("newest")),
        ]
        subtitle = " / ".join(part for part in parts if part)
        return MediaItem(
            kind="drama",
            id=drama_id,
            title=_text(drama.get("name") or drama.get("drama_name") or drama.get("title") or drama_id),
            subtitle=subtitle,
            pay_type=_to_int(drama.get("pay_type")),
            raw=drama,
        )

    def _album_item(self, album: dict[str, Any]) -> MediaItem | None:
        album_id = _to_int(album.get("id"))
        if album_id is None:
            return None
        count = _to_int(album.get("music_count"))
        subtitle = _text(album.get("username"))
        if count is not None:
            subtitle = f"{subtitle} / {count} 个声音" if subtitle else f"{count} 个声音"
        return MediaItem(
            kind="album",
            id=album_id,
            title=_text(album.get("title") or album_id),
            subtitle=subtitle,
            raw=album,
        )

    def _link_item(self, link: dict[str, Any]) -> MediaItem | None:
        raw_url = _text(link.get("url"))
        title = _text(link.get("title") or raw_url)
        if not raw_url:
            return None

        parsed = urlparse(raw_url)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in {"sound", "mdrama", "album"}:
            item_id = _to_int(parts[1])
            if item_id is None:
                return None
            if parts[0] == "sound":
                return MediaItem(kind="sound", id=item_id, title=title, subtitle="首页推荐", raw=link)
            if parts[0] == "mdrama":
                return MediaItem(kind="drama", id=item_id, title=title, subtitle="首页推荐", raw=link)
            return MediaItem(kind="album", id=item_id, title=title, subtitle="首页推荐", raw=link)
        return None

    def _is_bili_drm_sound(self, sound: dict[str, Any]) -> bool:
        dash = sound.get("dash") or {}
        audios = dash.get("audio") if isinstance(dash, dict) else []
        if any(isinstance(audio, dict) and audio.get("bilidrm_uri") for audio in audios or []):
            return True
        urls = [sound.get("soundurl"), sound.get("soundurl_128"), sound.get("videourl")]
        return any(isinstance(url, str) and "/drm/" in url for url in urls)

    def _homepage_label(self, section_name: str) -> str:
        labels = {
            "day3": "首页 / 近三日热门声音",
            "month3": "首页 / 近三月热门声音",
            "new": "首页 / 最新声音",
        }
        return labels.get(section_name, f"首页 / {section_name}")

    def _homepage_drama_section_label(self, index: int) -> str:
        labels = [
            "首页 / 主推广播剧",
            "首页 / 广播剧推荐",
            "首页 / 近期上新",
            "首页 / 热门广播剧",
            "首页 / 完结推荐",
            "首页 / 免费试听",
            "首页 / 更多推荐",
        ]
        if index < len(labels):
            return labels[index]
        return f"首页 / 广播剧推荐 {index + 1}"
