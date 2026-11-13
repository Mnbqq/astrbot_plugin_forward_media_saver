"""媒体下载与落盘。

职责：
* 从 URL / base64 / 本地路径 / 协议端 API 兜底结果中拿到字节流；
* 边下边算 SHA-256，限制单文件大小，拒绝非图片/视频内容；
* 按配置生成目录和文件名，原子写入；
* 可选的跨消息内容去重。

模块本身不依赖 AstrBot，方便单元测试。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes

import aiohttp

from .forward_parser import MediaRef
from .media_types import (
    AUDIO_EXTS,
    CONTENT_TYPE_KIND_EXT,
    IMAGE_EXTS,
    VIDEO_EXTS,
    kind_ext_from_name,
    sanitize_component,
    sniff_audio,
    sniff_media,
)
from .naming import (
    build_template_filename,
    build_template_folder,
    build_values,
    format_datetime,
)
from .net_policy import check_url
from .path_utils import local_path_exists
from .settings import Settings
from .video_probe import probe_video_duration

_CHUNK_SIZE = 64 * 1024
_SNIFF_BYTES = 96
_LOCAL_SEARCH_MAX_DEPTH = 6
_LOCAL_SEARCH_MAX_ENTRIES = 50000
_LOCAL_SEARCH_SECONDS = 5.0
_LOCAL_MISS_TTL = 60.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class SaveContext:
    """保存时的会话上下文。"""

    scope: str = "group"  # group / private
    scope_id: str = ""
    scope_name: str = ""
    received_at: int = 0


@dataclass
class SaveOutcome:
    status: str  # saved / duplicate / skipped / failed
    kind: str = ""
    path: Path | None = None
    relative_dir: str = ""
    size: int = 0
    sha256: str = ""
    duration: float = 0.0
    reason: str = ""


@dataclass
class _Source:
    kind: str  # url / path / bytes
    value: Any
    content_type: str = ""


@dataclass
class _Fetched:
    temp_path: Path
    size: int
    sha256: str
    head: bytes
    content_type: str


class MediaTooLarge(Exception):
    pass


class PermanentFetchError(Exception):
    pass


class HashStore:
    """追加式 SHA-256 索引，用于跨消息去重。

    文件是一行一个 hash。启动后第一次使用才加载，避免拖慢插件启动；
    写失败只记日志，不影响保存本身。
    """

    def __init__(self, path: Path, log: Any) -> None:
        self._path = path
        self._log = log
        self._hashes: set[str] = set()
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            try:
                if self._path.exists():
                    text = await asyncio.to_thread(
                        self._path.read_text, encoding="utf-8", errors="ignore"
                    )
                    self._hashes = {
                        line.strip() for line in text.splitlines() if line.strip()
                    }
            except OSError as exc:
                self._log.warning("读取去重索引失败，将在内存中去重：%s", exc)
            self._loaded = True

    async def contains(self, sha256: str) -> bool:
        await self._ensure_loaded()
        return sha256 in self._hashes

    async def add(self, sha256: str) -> None:
        await self._ensure_loaded()
        self._hashes.add(sha256)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = sha256 + "\n"
            await asyncio.to_thread(self._append, line)
        except OSError as exc:
            self._log.warning("写去重索引失败（不影响保存）：%s", exc)

    def _append(self, line: str) -> None:
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line)


def _decode_data_uri(uri: str) -> tuple[bytes, str]:
    header, _, payload = uri.partition(",")
    if not payload:
        raise ValueError("空 data URI")
    if ";base64" in header.lower():
        data = base64.b64decode(payload, validate=False)
    else:
        data = unquote_to_bytes(payload)
    content_type = (
        header[5:].split(";", 1)[0].strip().lower()
        if header.startswith("data:")
        else ""
    )
    return data, content_type


def _decode_base64(value: str) -> bytes:
    payload = value[9:] if value.startswith("base64://") else value
    payload = "".join(payload.split())
    padding = (-len(payload)) % 4
    if padding:
        payload += "=" * padding
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"base64 解码失败: {exc}") from exc


def _fallback_extension(
    ref: MediaRef, content_type: str = "", head: bytes = b""
) -> str:
    """音频/文件类型的扩展名兜底。"""
    if ref.kind == "audio":
        audio_ext = sniff_audio(head)
        if audio_ext:
            return audio_ext
    content = CONTENT_TYPE_KIND_EXT.get(content_type or "")
    if content and content[0] == ref.kind:
        return content[1]
    for name in (ref.file_name, ref.file):
        if not name:
            continue
        clean = name.split("?")[0].split("#")[0]
        suffix = Path(clean).suffix.lower().lstrip(".")
        if not suffix or len(suffix) > 8 or not suffix.isalnum():
            continue
        if ref.kind == "file" or suffix in (
            AUDIO_EXTS if ref.kind == "audio" else IMAGE_EXTS | VIDEO_EXTS
        ):
            return suffix
    if ref.kind == "image":
        return "jpg"
    if ref.kind == "video":
        return "mp4"
    if ref.kind == "audio":
        return "mp3"
    return "bin"


def build_folder(
    root: Path,
    layout: str,
    context: SaveContext,
    when: float,
    timezone: str = "",
) -> Path:
    date_text = format_datetime(when, "%Y-%m-%d", timezone)
    scope = sanitize_component(context.scope, fallback="chat", limit=16)
    scope_id = sanitize_component(context.scope_id, fallback="unknown")
    scope_part = f"{scope}_{scope_id}"

    if layout == "date_scope":
        return root / date_text / scope_part
    if layout == "date":
        return root / date_text
    if layout == "scope":
        return root / scope_part
    return root


class MediaSaver:
    """把 :class:`MediaRef` 保存为本地文件。"""

    def __init__(
        self,
        root: Path,
        settings: Settings,
        log: Any,
        session_provider: Callable[[], Awaitable[aiohttp.ClientSession]],
        resolver: Callable[[MediaRef, Any], Awaitable[Any]] | None = None,
        hash_store: HashStore | None = None,
        search_roots: Iterable[Path] = (),
    ) -> None:
        self.root = root
        self.settings = settings
        self.log = log
        self._session_provider = session_provider
        self._resolver = resolver
        self._hash_store = hash_store
        self._hash_lock = asyncio.Lock()
        self._path_lock = asyncio.Lock()
        self._search_roots = tuple(Path(item) for item in search_roots)
        self._local_hits: dict[str, Path] = {}
        self._local_misses: dict[str, float] = {}
        self._local_lock = asyncio.Lock()
        self._tmp_dir = root / ".tmp"

    async def prepare(self) -> None:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self._tmp_dir.mkdir, parents=True, exist_ok=True)
        # 清理上次进程残留的临时文件。
        try:
            now = time.time()
            for item in self._tmp_dir.glob("dl_*"):
                try:
                    if item.is_file() and now - item.stat().st_mtime > 86400:
                        item.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass

    async def save(
        self,
        ref: MediaRef,
        context: SaveContext,
        sequence: int = 1,
        bot: Any = None,
    ) -> SaveOutcome:
        if not self.settings.media_kind_enabled(ref.kind):
            return SaveOutcome(status="skipped", kind=ref.kind, reason="该类型已关闭")

        if ref.size_hint and ref.size_hint > self.settings.max_file_bytes:
            return SaveOutcome(
                status="skipped",
                kind=ref.kind,
                reason=f"文件过大（{ref.size_hint} 字节）",
            )

        if (
            ref.kind == "video"
            and self.settings.min_video_seconds > 0
            and ref.duration > 0
            and ref.duration < self.settings.min_video_seconds
        ):
            return SaveOutcome(
                status="skipped",
                kind=ref.kind,
                duration=ref.duration,
                reason=(
                    f"视频时长 {ref.duration:.1f}s 小于 "
                    f"{self.settings.min_video_seconds}s"
                ),
            )

        if not self._has_free_space():
            return SaveOutcome(
                status="skipped", kind=ref.kind, reason="磁盘剩余空间不足"
            )

        last_reason = "没有可用的下载地址"
        async for source in self._iter_sources(ref, bot):
            fetched: _Fetched | None = None
            try:
                fetched = await self._fetch(source)
                if fetched.size <= 0:
                    last_reason = "下载结果为空"
                    continue

                if ref.kind in ("image", "video"):
                    detected_kind, ext = sniff_media(fetched.head)
                    if detected_kind is None or ext is None:
                        detected_kind, ext = CONTENT_TYPE_KIND_EXT.get(
                            fetched.content_type, (None, None)
                        )
                    if detected_kind is None or ext is None:
                        detected_kind, ext = kind_ext_from_name(
                            ref.file_name or ref.file or ""
                        )
                    if detected_kind is None or ext is None:
                        last_reason = "无法识别的媒体格式"
                        continue
                else:
                    detected_kind = ref.kind
                    ext = _fallback_extension(ref, fetched.content_type, fetched.head)
                if not self.settings.media_kind_enabled(detected_kind):
                    return SaveOutcome(
                        status="skipped",
                        kind=detected_kind,
                        reason="识别后的类型已关闭",
                    )

                duration = 0.0
                if detected_kind == "video" and self.settings.min_video_seconds > 0:
                    duration = ref.duration
                    if duration <= 0:
                        duration = await probe_video_duration(
                            fetched.temp_path,
                            self.settings.video_probe_timeout,
                        )
                    if duration > 0 and duration < self.settings.min_video_seconds:
                        return SaveOutcome(
                            status="skipped",
                            kind="video",
                            duration=duration,
                            reason=(
                                f"视频时长 {duration:.1f}s 小于 "
                                f"{self.settings.min_video_seconds}s"
                            ),
                        )
                    if duration <= 0 and self.settings.skip_unknown_video_duration:
                        return SaveOutcome(
                            status="skipped",
                            kind="video",
                            reason="无法识别视频时长，按配置跳过",
                        )

                if not self._has_free_space():
                    last_reason = "磁盘剩余空间不足"
                    continue

                when = context.received_at or ref.timestamp or int(time.time())
                values = build_values(
                    scope=context.scope,
                    scope_id=context.scope_id,
                    scope_name=context.scope_name,
                    sender_id=ref.sender_id,
                    sender_name=ref.sender_name,
                    kind=detected_kind,
                    timestamp=when,
                    sequence=sequence,
                    sha256=fetched.sha256,
                    original_name=ref.file_name,
                    media_timestamp=ref.timestamp,
                    timezone=self.settings.timezone,
                )
                if self.settings.folder_template:
                    target_dir = build_template_folder(
                        self.root, self.settings.folder_template, values
                    )
                else:
                    target_dir = build_folder(
                        self.root,
                        self.settings.folder_layout,
                        context,
                        when,
                        self.settings.timezone,
                    )
                await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)

                if self._hash_store is not None:
                    async with self._hash_lock:
                        if await self._hash_store.contains(fetched.sha256):
                            return SaveOutcome(
                                status="duplicate",
                                kind=detected_kind,
                                sha256=fetched.sha256,
                                reason="相同内容已保存过",
                            )
                        target = self._build_target_path(
                            target_dir, ref, fetched, ext, sequence, values
                        )
                        await asyncio.to_thread(os.replace, fetched.temp_path, target)
                        try:
                            await self._hash_store.add(fetched.sha256)
                        except Exception as exc:  # noqa: BLE001
                            self.log.warning("更新去重索引失败（文件已保存）：%s", exc)
                else:
                    async with self._path_lock:
                        target = self._build_target_path(
                            target_dir, ref, fetched, ext, sequence, values
                        )
                        await asyncio.to_thread(os.replace, fetched.temp_path, target)

                sha256 = fetched.sha256
                size = fetched.size
                fetched = None  # 已 move，禁止 finally 删除
                try:
                    relative = str(target.parent.relative_to(self.root))
                except ValueError:
                    relative = str(target.parent)
                if relative == ".":
                    relative = ""
                return SaveOutcome(
                    status="saved",
                    kind=detected_kind,
                    path=target,
                    relative_dir=relative,
                    size=size,
                    sha256=sha256,
                    duration=duration,
                )
            except MediaTooLarge:
                return SaveOutcome(
                    status="skipped",
                    kind=ref.kind,
                    reason=f"超过 {self.settings.max_file_mb} MB 限制",
                )
            except PermanentFetchError as exc:
                last_reason = str(exc)
                self.log.debug("媒体来源不可用：%s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单条失败不应影响整批
                last_reason = str(exc)
                self.log.warning("保存媒体失败（%s）：%s", source.kind, exc)
            finally:
                if fetched is not None:
                    try:
                        fetched.temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        return SaveOutcome(status="failed", kind=ref.kind, reason=last_reason)

    def _has_free_space(self) -> bool:
        try:
            usage = shutil.disk_usage(self.root)
        except OSError:
            return True
        return usage.free >= self.settings.min_free_bytes

    def _build_target_path(
        self,
        target_dir: Path,
        ref: MediaRef,
        fetched: _Fetched,
        ext: str,
        sequence: int,
        values: dict[str, str] | None = None,
    ) -> Path:
        stamp = ref.timestamp or int(time.time())
        if stamp < 946684800 or stamp > time.time() + 86400:
            stamp = int(time.time())
        date_time = format_datetime(stamp, "%Y%m%d-%H%M%S", self.settings.timezone)
        sender = sanitize_component(ref.sender_id, fallback="unknown", limit=32)
        default_stem = f"{date_time}_{sender}_{sequence:02d}_{fetched.sha256[:8]}"
        if self.settings.filename_template and values is not None:
            stem = build_template_filename(
                self.settings.filename_template, values, default_stem
            )
        else:
            stem = default_stem
        candidate = target_dir / f"{stem}.{ext}"
        # 极低概率的 hash 前缀碰撞：追加序号而不是覆盖。
        counter = 1
        while candidate.exists():
            candidate = target_dir / f"{stem}_{counter}.{ext}"
            counter += 1
        return candidate

    async def _sources(self, ref: MediaRef) -> list[_Source]:
        """按可靠性顺序列出所有可尝试的数据来源。

        同一个媒体可能同时带 URL、path 和 file 字段；URL 过期时仍可回退到
        协议端同机的本地文件，反之亦然。
        """
        sources: list[_Source] = []
        seen: set[tuple[str, str]] = set()

        def add(source: _Source) -> None:
            if source.kind == "bytes":
                key = ("bytes", hashlib.sha1(bytes(source.value)).hexdigest())
            else:
                key = (source.kind, str(source.value))
            if key in seen:
                return
            seen.add(key)
            sources.append(source)

        def add_value(value: str, content_type: str = "") -> None:
            value = value.strip()
            if not value:
                return
            lowered = value.lower()
            if lowered.startswith(("http://", "https://")):
                add(_Source("url", value))
            elif lowered.startswith("base64://"):
                try:
                    add(_Source("bytes", _decode_base64(value)))
                except ValueError as exc:
                    self.log.debug("base64 媒体解码失败：%s", exc)
            elif lowered.startswith("data:"):
                try:
                    data, detected_type = _decode_data_uri(value)
                except (ValueError, binascii.Error) as exc:
                    self.log.debug("data URI 解码失败：%s", exc)
                else:
                    add(_Source("bytes", data, content_type or detected_type))
            else:
                # file://、Windows 盘符、Unix 绝对路径、带引号的路径都交给
                # path_utils 统一规范化并检查存在性。
                path = local_path_exists(value)
                if path is not None:
                    add(_Source("path", path))

        if ref.url:
            add_value(ref.url)
        if ref.file:
            add_value(ref.file)
        if ref.path:
            add_value(ref.path)

        return sources

    def _search_name(self, ref: MediaRef) -> str:
        """只在 file 是纯文件名时做本地搜索。"""
        raw = (ref.file or "").strip()
        if not raw or "://" in raw or "/" in raw or "\\" in raw:
            return ""
        name = Path(raw).name
        if name != raw or name in {".", ".."}:
            return ""
        suffix = Path(name).suffix.lower().lstrip(".")
        allowed = VIDEO_EXTS if ref.kind == "video" else IMAGE_EXTS
        if suffix not in allowed and suffix not in ("video", "image"):
            return ""
        return name

    async def _find_local_file(self, ref: MediaRef) -> Path | None:
        """在协议端缓存目录里按文件名查找媒体。

        go-cqhttp / Lagrange 等实现有时只在视频段里给出缓存文件名，不给 URL；
        当 AstrBot 与协议端共享文件系统时，可以直接复制这个文件。
        """
        name = self._search_name(ref)
        if not name or not self._search_roots:
            return None

        now = time.monotonic()
        hit = self._local_hits.get(name)
        if hit is not None:
            try:
                if hit.is_file():
                    return hit
            except OSError:
                pass
            self._local_hits.pop(name, None)
        if now < self._local_misses.get(name, 0.0):
            return None

        async with self._local_lock:
            hit = self._local_hits.get(name)
            if hit is not None:
                try:
                    if hit.is_file():
                        return hit
                except OSError:
                    self._local_hits.pop(name, None)
            deadline = time.monotonic() + _LOCAL_SEARCH_SECONDS
            for root in self._search_roots:
                if time.monotonic() >= deadline:
                    break
                try:
                    found = await asyncio.to_thread(
                        self._scan_local, root, name, ref.size_hint, deadline
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("搜索本地缓存失败（%s）：%s", root, exc)
                    continue
                if found is not None:
                    self._local_hits[name] = found
                    self.log.info("在本地缓存找到媒体文件：%s", found)
                    return found
            self._local_misses[name] = time.monotonic() + _LOCAL_MISS_TTL
            if len(self._local_misses) > 1000:
                self._local_misses.clear()
        return None

    @staticmethod
    def _scan_local(
        root: Path,
        filename: str,
        size_hint: int = 0,
        deadline: float | None = None,
    ) -> Path | None:
        """深度优先、限时限量地查找文件名；返回最新的匹配项。"""
        if deadline is None:
            deadline = time.monotonic() + _LOCAL_SEARCH_SECONDS
        seen_entries = 0
        best: Path | None = None
        best_mtime = -1.0
        stack: list[tuple[Path, int]] = [(root, 0)]
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}

        while stack:
            current, depth = stack.pop()
            try:
                iterator = os.scandir(current)
            except OSError:
                continue
            with iterator:
                for entry in iterator:
                    seen_entries += 1
                    if (
                        seen_entries > _LOCAL_SEARCH_MAX_ENTRIES
                        or time.monotonic() > deadline
                    ):
                        return best
                    try:
                        if entry.is_file(follow_symlinks=False):
                            if entry.name != filename:
                                continue
                            path = Path(entry.path)
                            size = entry.stat(follow_symlinks=False).st_size
                            if size_hint and abs(size - size_hint) > 4096:
                                continue
                            mtime = path.stat().st_mtime
                            if mtime > best_mtime:
                                best = path
                                best_mtime = mtime
                        elif (
                            depth < _LOCAL_SEARCH_MAX_DEPTH
                            and entry.is_dir(follow_symlinks=False)
                            and entry.name not in skip_dirs
                        ):
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
        return best

    async def _iter_sources(self, ref: MediaRef, bot: Any = None):
        """先尝试消息自带的数据源，都失败后再请求协议端兜底。"""
        for source in await self._sources(ref):
            yield source

        # 先看本地缓存：命中时不需要等待协议端下载视频。
        local_file = await self._find_local_file(ref)
        if local_file is not None:
            yield _Source("path", local_file)

        if self._resolver is not None and (ref.file or ref.file_id):
            try:
                resolved = await self._resolver(ref, bot)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("协议端兜底取文件失败：%s", exc)
                resolved = None
            if resolved:
                resolved_text = str(resolved)
                resolved_ref = MediaRef(
                    kind=ref.kind,
                    url=resolved_text if resolved_text.startswith("http") else "",
                    file=resolved_text,
                )
                for source in await self._sources(resolved_ref):
                    yield source

    async def _fetch(self, source: _Source) -> _Fetched:
        if source.kind == "url":
            return await self._fetch_url(str(source.value))
        if source.kind == "path":
            return await self._fetch_path(Path(source.value))
        if source.kind == "bytes":
            return await self._fetch_bytes(bytes(source.value), source.content_type)
        raise ValueError(f"未知来源类型: {source.kind}")

    async def _fetch_url(self, url: str) -> _Fetched:
        allowed, reason = await asyncio.to_thread(
            check_url,
            url,
            allowed_hosts=self.settings.allowed_media_hosts,
            block_private=self.settings.block_private_urls,
        )
        if not allowed:
            raise PermanentFetchError(reason)
        session = await self._session_provider()
        timeout = aiohttp.ClientTimeout(
            total=self.settings.download_timeout, sock_connect=15
        )
        headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
        last_error: Exception | None = None

        for attempt in range(self.settings.retries + 1):
            if attempt:
                await asyncio.sleep(0.8 * attempt)
            try:
                async with session.get(
                    url, headers=headers, timeout=timeout, allow_redirects=True
                ) as response:
                    if response.status != 200:
                        if response.status in (401, 403, 404, 410):
                            raise PermanentFetchError(
                                f"HTTP {response.status}，链接可能已过期"
                            )
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"HTTP {response.status}",
                        )
                    content_type = (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    self._check_length(response.headers.get("Content-Length"))
                    return await self._stream_to_temp(response.content, content_type)
            except MediaTooLarge:
                raise
            except PermanentFetchError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                self.log.debug(
                    "下载失败（第 %s/%s 次）：%s",
                    attempt + 1,
                    self.settings.retries + 1,
                    exc,
                )
        raise PermanentFetchError(f"下载失败：{last_error}")

    def _check_length(self, value: str | None) -> None:
        if not value:
            return
        try:
            length = int(value)
        except (TypeError, ValueError):
            return
        if length > self.settings.max_file_bytes:
            raise MediaTooLarge

    async def _stream_to_temp(self, stream: Any, content_type: str) -> _Fetched:
        fd, name = tempfile.mkstemp(prefix="dl_", dir=self._tmp_dir)
        temp_path = Path(name)
        os.close(fd)
        hasher = hashlib.sha256()
        head = b""
        total = 0
        try:
            with open(temp_path, "wb") as handle:
                async for chunk in stream.iter_chunked(_CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.settings.max_file_bytes:
                        raise MediaTooLarge
                    if len(head) < _SNIFF_BYTES:
                        head += chunk[: _SNIFF_BYTES - len(head)]
                    hasher.update(chunk)
                    handle.write(chunk)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return _Fetched(
            temp_path=temp_path,
            size=total,
            sha256=hasher.hexdigest(),
            head=head,
            content_type=content_type,
        )

    async def _fetch_path(self, path: Path) -> _Fetched:
        fd, name = tempfile.mkstemp(prefix="dl_", dir=self._tmp_dir)
        temp_path = Path(name)
        os.close(fd)
        hasher = hashlib.sha256()
        head = b""
        total = 0
        try:
            with open(path, "rb") as source, open(temp_path, "wb") as target:
                while True:
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.settings.max_file_bytes:
                        raise MediaTooLarge
                    if len(head) < _SNIFF_BYTES:
                        head += chunk[: _SNIFF_BYTES - len(head)]
                    hasher.update(chunk)
                    target.write(chunk)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return _Fetched(
            temp_path=temp_path,
            size=total,
            sha256=hasher.hexdigest(),
            head=head,
            content_type="",
        )

    async def _fetch_bytes(self, data: bytes, content_type: str = "") -> _Fetched:
        if len(data) > self.settings.max_file_bytes:
            raise MediaTooLarge
        fd, name = tempfile.mkstemp(prefix="dl_", dir=self._tmp_dir)
        temp_path = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return _Fetched(
            temp_path=temp_path,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            head=data[:_SNIFF_BYTES],
            content_type=content_type,
        )
