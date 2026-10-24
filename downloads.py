from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit

import av
import requests

from maoer_api import BASE_URL, ApiError, MaoerApi, MediaItem, PlaybackInfo, PurchaseRequired


DOWNLOAD_TIMEOUT = (30, 30)  # Connection timeout, read timeout (seconds).


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
    sequence_positions: dict[int, int] = field(default_factory=dict)


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
            return DownloadSelection(item.title, api.publisher_name_for_item(item), [item], [0],
                                     sequence_positions={item.id: 1})
    episodes = list({entry.id: entry for entry in api.drama_episodes(drama.id)}.values())
    if not episodes:
        raise ApiError("该剧集没有可下载的音频")
    sequence_positions = {entry.id: index + 1 for index, entry in enumerate(episodes)}
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
    return DownloadSelection(drama.title, api.publisher_name_for_item(drama), episodes, checked,
                             number_width, sequence_positions)


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
                cancel: threading.Event) -> None:
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
    # Verify the full encoded payload before publishing; site durations can be stale.
    actual = hashlib.sha256()
    with av.open(str(destination)) as check:
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


def _subtitle_milliseconds(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(number) if math.isfinite(number) and number >= 0 else None


def subtitles_to_srt(data: object, duration_ms: int | None = None,
                     cancel: threading.Event | None = None) -> str:
    """Convert independent subtitle rows to UTF-8-ready SubRip text."""
    if not isinstance(data, list):
        raise ApiError('字幕文件格式不正确，无法转换为 SRT')
    rows = []
    duration = _subtitle_milliseconds(duration_ms)
    duration = duration if duration else None
    for row in data:
        if cancel is not None:
            check_cancel(cancel)
        captions = MaoerApi._parse_independent_subtitles([row])
        if not captions:
            continue
        start = _subtitle_milliseconds(row.get('start_time'))
        if start is None or (duration is not None and start >= duration):
            continue
        # Blank lines delimit SRT cues, so retain text lines without empty separators.
        text = '\n'.join(line for line in captions[0].text.splitlines() if line.strip())
        if text:
            rows.append((start, _subtitle_milliseconds(row.get('end_time')), text))
    rows.sort(key=lambda row: row[0])
    starts = sorted({row[0] for row in rows})
    following = dict(zip(starts, starts[1:]))

    def timestamp(milliseconds: int) -> str:
        seconds, milliseconds = divmod(milliseconds, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'

    blocks = []
    for start, end, text in rows:
        if cancel is not None:
            check_cancel(cancel)
        if end is None or end <= start:
            # Missing end times are estimates, capped to avoid hanging captions.
            end = min(following.get(start, start + 5000), start + 5000)
        if duration is not None:
            end = min(end, duration)
        if end > start:
            blocks.append(f'{len(blocks) + 1}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n\n')
    return ''.join(blocks)


def _subtitle_playback_info(api: MaoerApi, item: MediaItem, cancel: threading.Event) -> PlaybackInfo:
    """Recover only a false 'not found'; never replace current-account permission denial."""
    last_error = None
    check_cancel(cancel)
    try:
        return api.playback_info(item)
    except PurchaseRequired:
        raise
    except ApiError as exc:
        if str(exc).strip().rstrip('。.!！') not in {'该音频不存在', '音频不存在'}:
            raise
        last_error = exc
    check_cancel(cancel)
    if not api.cookie_header:
        raise last_error
    # A guest can confirm a public free item even when authenticated metadata
    # misreports it. Do not carry account purchase flags into this check.
    public_item = MediaItem('sound', item.id, item.title, duration_ms=item.duration_ms,
                            drama_id=item.drama_id)
    public = MaoerApi(cookie='', timeout=api.timeout)
    try:
        return public.playback_info(public_item)
    except PurchaseRequired as exc:
        raise ApiError('当前登录状态下暂时无法获取音频信息，请重试') from exc
    except ApiError as exc:
        if str(exc).strip().rstrip('。.!！') in {'该音频不存在', '音频不存在'}:
            raise last_error
        raise ApiError('音频信息获取失败，请重试') from exc
    finally:
        public.session.close()


def download_subtitle(api: MaoerApi, item: MediaItem, folder: Path, cancel: threading.Event,
                      progress: Callable[[str, int], None], *, number_width: int | None = None,
                      filename_prefix: str = '', playback: PlaybackInfo | None = None) -> Path | None:
    """Prefer independent JSON; otherwise export the official XML subtitle track."""
    check_cancel(cancel)
    progress('正在获取字幕', 0)
    playback = playback if playback is not None else _subtitle_playback_info(api, item, cancel)
    title = numbered_title(item, number_width) if number_width is not None else item.title
    final = folder / (file_name(filename_prefix + title) + '.srt')
    if final.exists():
        raise FileExistsError()
    check_cancel(cancel)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, prefix='.maoer-', suffix='.part', delete=False) as output:
        temporary = Path(output.name)
    try:
        srt, source_error, independent_available = '', None, False
        if playback.subtitle_url:
            try:
                data = _fetch_subtitle_json(playback.subtitle_url, temporary, cancel, progress)
                independent_available = bool(MaoerApi._parse_independent_subtitles(data))
                srt = subtitles_to_srt(data, playback.duration_ms, cancel)
            except (ApiError, requests.RequestException, ValueError, UnicodeError) as exc:
                source_error = exc
        if independent_available and not srt:
            raise ApiError('字幕时间超出音频总时长，无法转换为 SRT')
        if not independent_available:
            check_cancel(cancel)
            progress('正在获取弹幕式字幕', 0)
            # Empty URL explicitly selects XML only; never mix two subtitle sources.
            try:
                captions = api.sound_subtitles(item.id, subtitle_url='')
            except (ApiError, requests.RequestException) as exc:
                raise ApiError('字幕获取失败，请检查网络后重试') from exc
            rows = [{'start_time': caption.time * 1000, 'content': caption.text}
                    for caption in captions]
            srt = subtitles_to_srt(rows, playback.duration_ms, cancel)
        if not srt and source_error is not None:
            if isinstance(source_error, (ApiError, requests.RequestException)):
                raise source_error
            raise ApiError('字幕文件格式不正确，且没有可用的弹幕式字幕') from source_error
        if not srt:
            return None
        check_cancel(cancel)
        progress('正在转换 SRT 字幕', 99)
        temporary.write_text(srt, encoding='utf-8', newline='\n')
        check_cancel(cancel)
        if os.name == 'nt':
            temporary.rename(final)
        else:
            os.link(temporary, final)
        progress('字幕下载完成', 100)
        return final
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_subtitle_json(url: str, temporary: Path, cancel: threading.Event,
                         progress: Callable[[str, int], None]) -> object:
    url = urljoin(BASE_URL + '/', url)
    if urlsplit(url).scheme not in {'http', 'https'}:
        raise ApiError('官网返回的字幕地址无效')
    # A separate anonymous session must never forward an account Cookie to a CDN.
    with MaoerApi(cookie='').session as session, session.get(
            url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers={'Accept-Encoding': 'identity'}) as response:
        if response.status_code != 200:
            raise ApiError(f'字幕服务器返回错误（HTTP {response.status_code}）')
        expected = int(response.headers.get('Content-Length') or 0)
        limit = 16 * 1024 * 1024
        if expected > limit:
            raise ApiError('字幕文件过大，无法保存')
        received = 0
        with temporary.open('wb') as output:
            for chunk in response.iter_content(256 * 1024):
                check_cancel(cancel)
                received += len(chunk)
                if received > limit:
                    raise ApiError('字幕文件过大，无法保存')
                output.write(chunk)
                progress('正在下载字幕', min(99, int(received * 100 / expected)) if expected else 0)
        if not received or (expected and expected != received):
            raise ApiError('字幕下载不完整，请重试')
    check_cancel(cancel)
    try:
        return json.loads(temporary.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise ApiError('字幕文件格式不正确，无法转换为 SRT，请重试') from exc


def download_audio(api: MaoerApi, item: MediaItem, folder: Path, cancel: threading.Event,
                   get_key: Callable[[PlaybackInfo], bytes],
                   progress: Callable[[str, int], None], *, number_width: int | None = None,
                   filename_prefix: str = '',
                   on_playback: Callable[[PlaybackInfo], None] | None = None) -> Path:
    check_cancel(cancel)
    progress('正在检查播放权限', 0)
    playback = api.playback_info(item)
    if on_playback is not None:
        on_playback(playback)
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
    final = folder / (file_name(filename_prefix + title) + extension)
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
                    url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers={'Accept-Encoding': 'identity'}) as response:
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
                remux_audio(source_file, temporary, key, cancel)
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
