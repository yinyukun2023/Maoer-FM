from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import re
import tempfile

from app_paths import app_data_dir


COOKIE_ATTRIBUTES = frozenset({
    "domain", "path", "expires", "max-age", "secure", "httponly", "samesite",
    "priority", "partitioned", "version", "comment", "$path", "$domain", "$version",
})


def normalize_cookie(text: str) -> str:
    """Extract request cookies, never guess which unknown cookie names are authentication."""
    text = text.strip().lstrip("\ufeff")
    if not text or len(text) > 131072:
        raise ValueError("请粘贴有效的 Cookie（最多 128 KB）")
    cookies: dict[str, str] = {}

    def add(name: str, value: str) -> None:
        if name.lower() in COOKIE_ATTRIBUTES:
            return
        if not name or any(ord(char) < 32 or ord(char) >= 127 for char in name + value):
            raise ValueError("Cookie 包含无效字符，请重新复制")
        parsed = SimpleCookie()
        try:
            parsed[name] = value
        except CookieError:
            raise ValueError("Cookie 字段格式不正确") from None
        if any(char.isspace() or char in '\";,\\' for char in value):
            raise ValueError("Cookie 值包含无效分隔符，请重新复制原始 Cookie")
        # Preserve '=', '%' and other token bytes; quoting base64 can invalidate a login.
        if name in cookies and cookies[name] != value:
            raise ValueError("Cookie 中有同名字段且值不同，请只粘贴一个账号的 Cookie")
        cookies[name] = value

    if text.startswith(("[", "{")):
        try:
            data = json.loads(text)
        except ValueError:
            raise ValueError("Cookie JSON 格式不正确") from None
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        if isinstance(data, dict) and isinstance(data.get("Cookie"), str):
            return normalize_cookie(data["Cookie"])
        if isinstance(data, dict) and "name" in data and "value" in data:
            data = [data]
        if isinstance(data, dict):
            data = [{"name": name, "value": value} for name, value in data.items()]
        if not isinstance(data, list):
            raise ValueError("Cookie JSON 格式不正确")
        for item in data:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain", "")).lstrip(".").lower()
            if domain and domain != "missevan.com" and not domain.endswith(".missevan.com"):
                continue
            name, value = item.get("name"), item.get("value")
            if isinstance(name, str) and isinstance(value, str):
                add(name, value)
    else:
        headers = re.findall(r"(?im)^\s*(?:cookie|set-cookie)\s*:\s*([^\r\n]*)", text)
        for part in re.split(r"[;\r\n]", ";".join(headers) if headers else text):
            name, separator, value = part.strip().partition("=")
            name = name.strip()
            if not separator or name.lower() in COOKIE_ATTRIBUTES:
                continue
            parsed = SimpleCookie()
            try:
                parsed.load(part.strip())
            except CookieError:
                continue
            if len(parsed) == 1 and name in parsed:
                add(name, parsed[name].value)
    if not cookies:
        raise ValueError("没有找到猫耳 Cookie，请粘贴 Cookie 内容或浏览器导出的 Cookie JSON")
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


@dataclass(frozen=True)
class LoginCredentials:
    username: str
    password: str = field(default="", repr=False)
    region: str = "CN"
    region_label: str = "中国大陆 +86"


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


def _crypt_login_data(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("账号密码加密需要 Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    result = _DataBlob()
    try:
        # Current Windows user scope; CRYPTPROTECT_UI_FORBIDDEN prevents native prompts.
        if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel32.LocalFree(result.data)


def protect_login(login: LoginCredentials) -> str:
    try:
        data = json.dumps(asdict(login), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(_crypt_login_data(data)).decode("ascii")
    except (OSError, ValueError) as exc:
        raise ValueError("无法加密保存账号密码，请重试") from exc


def unprotect_login(value: str) -> LoginCredentials | None:
    if not value:
        return None
    try:
        data = json.loads(_crypt_login_data(base64.b64decode(value, validate=True), decrypt=True))
        if not isinstance(data, dict) or not all(isinstance(data.get(key), str)
                for key in ("username", "password", "region", "region_label")):
            raise ValueError
        return LoginCredentials(**data)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("无法读取已保存的账号密码，请重新填写") from exc


@dataclass(frozen=True)
class SavedAccount:
    user_id: int
    nickname: str
    cookie: str
    note: str = ""
    credentials: str = field(default="", repr=False)


@dataclass(frozen=True)
class AccountState:
    accounts: tuple[SavedAccount, ...] = ()
    active_user_id: int | None = None

    def get(self, user_id: int | None) -> SavedAccount | None:
        return next((account for account in self.accounts if account.user_id == user_id), None)

    def updated(self, account: SavedAccount, *, activate: bool = False) -> AccountState:
        accounts = tuple(account if saved.user_id == account.user_id else saved for saved in self.accounts)
        if self.get(account.user_id) is None:
            accounts += (account,)
        return AccountState(accounts, account.user_id if activate else self.active_user_id)

    def removed(self, user_id: int) -> AccountState:
        return AccountState(
            tuple(account for account in self.accounts if account.user_id != user_id),
            None if self.active_user_id == user_id else self.active_user_id,
        )

    def moved(self, user_id: int, position: int) -> AccountState:
        index = next((i for i, account in enumerate(self.accounts) if account.user_id == user_id), None)
        if index is None:
            return self
        position = max(0, min(position, len(self.accounts) - 1))
        if index == position:
            return self
        accounts = list(self.accounts)
        accounts.insert(position, accounts.pop(index))
        return AccountState(tuple(accounts), self.active_user_id)


def load_accounts() -> AccountState | None:
    """A missing file permits legacy migration; an empty saved state means logged out."""
    path = app_data_dir(create=False) / "accounts.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise ValueError("账号文件格式不正确")
    accounts = []
    for item in data["accounts"]:
        if (not isinstance(item, dict) or type(item.get("user_id")) is not int or item["user_id"] <= 0
                or not all(isinstance(item.get(key, ""), str) for key in ("nickname", "cookie", "note", "credentials"))):
            raise ValueError("账号文件格式不正确")
        accounts.append(SavedAccount(item["user_id"], item.get("nickname", ""),
                                     normalize_cookie(item.get("cookie", "")), item.get("note", ""),
                                     item.get("credentials", "")))
    if len({account.user_id for account in accounts}) != len(accounts):
        raise ValueError("账号文件包含重复账号")
    active_id = data.get("active_user_id")
    if active_id is not None and (type(active_id) is not int or active_id not in {a.user_id for a in accounts}):
        raise ValueError("账号文件的当前账号无效")
    return AccountState(tuple(accounts), active_id)


def save_accounts(state: AccountState) -> None:
    directory = app_data_dir()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix="accounts-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(asdict(state), temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        os.replace(temporary_path, directory / "accounts.json")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
