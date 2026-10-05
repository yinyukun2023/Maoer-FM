from __future__ import annotations

from dataclasses import dataclass
import hashlib
import mmap
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import av
import requests

from maoer_api import ApiError, MaoerApi, MediaItem, PlaybackInfo, PurchaseRequired


class DownloadCancelled(Exception):
    pass


class DownloadControl(threading.Event):
    """Cancellation plus cooperative pause; active time excludes pauses."""
    def __init__(self) -> None:
        super().__init__()
        self._gate = threading.Event()
        self._gate.set()
        self._pause_lock = threading.Lock()
        self._paused_at: float | None = None
        self._paused_seconds = 0.0

    def pause(self) -> None:
        with self._pause_lock:
            if self._paused_at is None and not self.is_set():
                self._paused_at = time.monotonic()
                self._gate.clear()

    def resume(self) -> None:
        with self._pause_lock:
            if self._paused_at is not None:
                self._paused_seconds += time.monotonic() - self._paused_at
                self._paused_at = None
            self._gate.set()

    def is_paused(self) -> bool:
        return not self._gate.is_set()

    def active_time(self) -> float:
        with self._pause_lock:
            now = self._paused_at if self._paused_at is not None else time.monotonic()
            return now - self._paused_seconds

    def set(self) -> None:
        super().set()
        self.resume()

    def checkpoint(self) -> None:
        while not self._gate.wait(0.1):
            if self.is_set():
                raise DownloadCancelled()
        if self.is_set():
            raise DownloadCancelled()


@dataclass
class DownloadSelection:
    title: str
    publisher: str
    items: list[MediaItem]
    checked: list[int]
    number_width: int = 2


EPISODE_NUMBER = re.compile(r'(第\s*)([0-9０-９零〇一二两三四五六七八九十百千万]+)(\s*[集期])')


def _episode_match(item: MediaItem):
    if item.subtitle.rsplit('/', 1)[-1].strip() in {'花絮', '音乐'}:
        return None
    match = EPISODE_NUMBER.search(item.title)
    if match and not re.search(r'番外|预告|花絮|小剧场|片花', item.title[:match.start()]):
        return match
    return None


def _chinese_number(value: str) -> int:
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if value.isdecimal():
        return int(value)
    if all(char in digits for char in value):
        return int(''.join(str(digits[char]) for char in value))
    total = section = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
        elif char == '万':
            total += (section + number) * 10000
            section = number = 0
        else:
            section += (number or 1) * {'十': 10, '百': 100, '千': 1000}[char]
            number = 0
    return total + section + number


def numbered_title(item: MediaItem, width: int) -> str:
    match = _episode_match(item)
    if match is None:
        return item.title
    start, end = match.span(2)
    return item.title[:start] + str(_chinese_number(match[2])).zfill(width) + item.title[end:]


def load_selection(api: MaoerApi, item: MediaItem, whole_drama: bool) -> DownloadSelection:
    if item.kind not in {"sound", "drama"}:
        raise ApiError("请选择音频或剧集")
    if item.kind == "drama":
        drama = item
    else:
        try:
            drama = api.drama_for_sound(item.id)
        except ApiError as exc:
            if whole_drama or str(exc) != "该音频没有关联的剧集":
                raise
            # Standalone sounds have no drama name: use their list name for both.
            return DownloadSelection(item.title, api.publisher_name_for_item(item), [item], [0])
    episodes = list({entry.id: entry for entry in api.drama_episodes(drama.id)}.values())
    if not episodes:
        raise ApiError("该剧集没有可下载的音频")
    main_count = sum(entry.subtitle.rsplit('/', 1)[-1].strip() == '正剧' for entry in episodes)
    if not main_count:
        main_count = sum(_episode_match(entry) is not None for entry in episodes)
    number_width = 3 if main_count >= 100 else 2
    if item.kind == "sound":
        current = next((entry for entry in episodes if entry.id == item.id), None)
        if current is None:
            raise ApiError("该剧集的音频列表中没有找到当前音频")
        if not whole_drama:
            episodes = [current]
        checked = [i for i, entry in enumerate(episodes) if entry.id == item.id]
    else:
        checked = []
    return DownloadSelection(drama.title, api.publisher_name_for_item(drama), episodes, checked, number_width)


def file_name(title: str) -> str:
    """Preserve list names except characters Windows cannot store in a component."""
    table = str.maketrans('<>:"/\\|?*', '＜＞：＂／＼｜？＊')
    value = ''.join('_' if ord(c) < 32 else c for c in title).translate(table)
    value = re.sub(r'[ .]+$', lambda m: m[0].translate(str.maketrans(' .', '　．')), value)
    if not value:
        raise ApiError("列表名称为空，无法创建下载文件")
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', value, re.I):
        value = '_' + value
    return value


def folder_name(selection: DownloadSelection, include_publisher: bool) -> str:
    title = selection.title
    if include_publisher and selection.publisher:
        title += f"【{selection.publisher}】"
    return file_name(title)


def check_cancel(cancel: threading.Event) -> None:
    if isinstance(cancel, DownloadControl):
        cancel.checkpoint()
    if cancel.is_set():
        raise DownloadCancelled()


def download_error(exc: Exception) -> str:
    # Network/codec exceptions may contain signed URLs or command options.
    if isinstance(exc, requests.RequestException):
        return "网络请求失败，请检查网络后重试"
    if isinstance(exc, FileExistsError):
        return "同名文件已存在，已跳过（未覆盖）"
    if isinstance(exc, PermissionError):
        return "没有写入权限，请更换下载路径"
    if isinstance(exc, av.error.FFmpegError):
        return "音频解密或封装失败，请重试"
    if isinstance(exc, OSError):
        return "文件读写失败，请检查剩余空间、路径长度和文件权限"
    if isinstance(exc, (ApiError, PurchaseRequired)):
        return str(exc)
    return f"下载失败（{type(exc).__name__}）"


def normalize_empty_saiz(data: mmap.mmap) -> int:
    """Remove only the site's redundant CBCS constant-IV auxiliary-size boxes."""
    def boxes(start: int, end: int):
        while start < end:
            if end - start < 8:
                raise ApiError("音频文件不完整")
            size, kind = struct.unpack_from('>I4s', data, start)
            header = 8
            if size == 1:
                if end - start < 16:
                    raise ApiError("音频文件不完整")
                size = struct.unpack_from('>Q', data, start + 8)[0]
                header = 16
            elif size == 0:
                size = end - start
            if size < header or start + size > end:
                raise ApiError("音频文件不完整")
            yield start, size, kind, header
            start += size

    top = list(boxes(0, len(data)))
    moov = next(((p, n) for p, n, k, _ in top if k == b'moov'), None)
    if moov is None:
        raise ApiError("音频缺少 MP4 文件头")
    init = data[moov[0]:moov[0] + moov[1]]
    tenc = init.find(b'tenc') + 4
    constant_iv = (tenc >= 4 and init.count(b'tenc') == 1 and b'cbcs' in init
                   and len(init) >= tenc + 41 and init[tenc] == 1
                   and init[tenc + 5:tenc + 8] == bytes([0, 1, 0])
                   and init[tenc + 24] == 16)
    fixed = 0
    for offset, size, kind, header in top:
        if kind != b'moof':
            continue
        for traf, length, name, head in boxes(offset + header, offset + size):
            if name != b'traf':
                continue
            children = list(boxes(traf + head, traf + length))
            for p, n, k, h in children:
                if k != b'saiz' or n != 17 or h != 8 or data[p + 8:p + n] != bytes(9):
                    continue
                senc = next((q for q, z, t, _ in children if t == b'senc' and z == 16), None)
                if (not constant_iv or senc is None or data[senc + 8:senc + 12] != bytes(4)
                        or int.from_bytes(data[senc + 12:senc + 16], 'big') == 0):
                    raise ApiError("音频使用了尚不支持的加密结构")
                # Box length and all media offsets stay unchanged.
                data[p + 4:p + 8] = b'free'
                fixed += 1
    return fixed


def remux_audio(source_file, destination: Path, key: bytes | None,
                cancel: threading.Event, expected_ms: int | None) -> None:
    options = {"decryption_key": key.hex()} if key else {}
    digest = hashlib.sha256()
    count = 0
    with av.open(source_file, mode='r', options=options) as source:
        if not source.streams.audio:
            raise ApiError("文件中没有音频")
        stream = source.streams.audio[0]
        if stream.codec_context.name != 'aac':
            raise ApiError("暂不支持该音频格式的无损封装")
        with av.open(str(destination), mode='w', format='mp4') as output:
            # A template also copies DRM initialization data. Copy AAC settings
            # and encoded packets only; never decode/encode the saved audio.
            target = output.add_stream('aac', rate=stream.rate)
            target.layout = stream.layout
            target.codec_context.extradata = stream.codec_context.extradata
            target.time_base = stream.time_base
            output.start_encoding()
            if target.codec_context.extradata != stream.codec_context.extradata:
                raise ApiError("音频参数发生变化，已停止保存")
            for packet in source.demux(stream):
                check_cancel(cancel)
                if packet.pts is None:
                    continue
                digest.update(struct.pack('>I', packet.size))
                digest.update(bytes(packet))
                packet.stream = target
                output.mux(packet)
                count += 1
    if not count:
        raise ApiError("下载的音频为空")
    # Verify the full encoded payload and duration before publishing the file.
    actual = hashlib.sha256()
    with av.open(str(destination)) as check:
        if expected_ms and (check.duration is None or abs(check.duration / 1000 - expected_ms) > 2000):
            raise ApiError("下载的音频时长不完整")
        for packet in check.demux(audio=0):
            check_cancel(cancel)
            if packet.pts is not None:
                actual.update(struct.pack('>I', packet.size))
                actual.update(bytes(packet))
    if actual.digest() != digest.digest():
        raise ApiError("音频完整性校验失败")
    with av.open(str(destination)) as check:
        if next(check.decode(audio=0), None) is None:
            raise ApiError("下载的音频无法播放")
        if check.duration and check.duration > 4 * av.time_base:
            check.seek(check.duration - 2 * av.time_base)
            if next(check.decode(audio=0), None) is None:
                raise ApiError("下载的音频末尾不完整")


def download_audio(api: MaoerApi, item: MediaItem, folder: Path, cancel: threading.Event,
                   get_key: Callable[[PlaybackInfo], bytes],
                   progress: Callable[[str, int], None], *, number_width: int | None = None) -> Path:
    check_cancel(cancel)
    progress('正在检查播放权限', 0)
    playback = api.playback_info(item)
    # The official player uses native MP4 when videourl is present, even if
    # has_video is false and a separate encrypted DASH audio track exists.
    audio = {} if playback.video_url else playback.dash_audio
    url = str(playback.video_url or audio.get('base_url') or playback.url)
    extension = '.m4a' if audio else Path(urlsplit(url).path).suffix.lower()
    if not audio:
        allowed = {'.mp4'} if playback.video_url else {'.mp3', '.m4a'}
        if extension not in allowed or (playback.drm and not playback.video_url):
            raise ApiError("官网没有返回可下载的完整媒体文件")
    if urlsplit(url).scheme not in {'http', 'https'}:
        raise ApiError("官网返回的音频地址无效")
    folder.mkdir(parents=True, exist_ok=True)
    title = numbered_title(item, number_width) if number_width is not None else item.title
    final = folder / (file_name(title) + extension)
    if final.exists():
        raise FileExistsError()
    check_cancel(cancel)
    key = None
    if not playback.video_url and (audio.get('bilidrm_uri') or playback.drm):
        progress('正在准备音频', 0)
        key = get_key(playback)
    check_cancel(cancel)
    with tempfile.NamedTemporaryFile(dir=folder, prefix='.maoer-', suffix='.part', delete=False) as output:
        temporary = Path(output.name)
    try:
        with tempfile.TemporaryFile(dir=folder) as source_file:
            expected = int(audio.get('size') or 0)
            # A separate anonymous session must never send the account Cookie to a CDN.
            with MaoerApi(cookie='').session as session, session.get(
                    url, stream=True, timeout=(15, 15), headers={'Accept-Encoding': 'identity'}) as response:
                if response.status_code != 200:
                    raise ApiError(f"音频服务器返回错误（HTTP {response.status_code}）")
                length = int(response.headers.get('Content-Length') or 0)
                if expected and length and expected != length:
                    raise ApiError("音频文件大小与列表信息不一致，请重试")
                expected = expected or length
                received = 0
                last_percent = -1
                for chunk in response.iter_content(256 * 1024):
                    check_cancel(cancel)
                    source_file.write(chunk)
                    received += len(chunk)
                    percent = min(99, int(received * 100 / expected)) if expected else 0
                    if percent != last_percent:
                        progress('正在下载', percent)
                        last_percent = percent
                if not received or (expected and received != expected):
                    raise ApiError("音频下载不完整，请重试")
            check_cancel(cancel)
            source_file.flush()
            if audio:
                progress('正在解密、封装和校验', 99)
                if key:
                    with mmap.mmap(source_file.fileno(), 0) as data:
                        normalize_empty_saiz(data)
                source_file.seek(0)
                remux_audio(source_file, temporary, key, cancel, playback.duration_ms)
            else:
                source_file.seek(0)
                with temporary.open('wb') as output:
                    while chunk := source_file.read(256 * 1024):
                        check_cancel(cancel)
                        output.write(chunk)
                with av.open(str(temporary)) as check:
                    if next(check.decode(audio=0), None) is None:
                        raise ApiError("下载的音频无法播放")
            check_cancel(cancel)
        # Windows rename is atomic and refuses to overwrite an existing name.
        if os.name == 'nt':
            temporary.rename(final)
        else:
            os.link(temporary, final)
        progress('下载完成', 100)
        return final
    finally:
        temporary.unlink(missing_ok=True)
