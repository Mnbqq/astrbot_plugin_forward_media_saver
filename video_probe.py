"""视频时长探测。

优先使用系统 ffprobe；没有 ffprobe 时，对 MP4/MOV/M4V 使用内置的
``mvhd`` box 解析。其他容器格式在无 ffprobe 时返回 0（未知）。

探测只返回秒数，0 表示未知；调用方决定“未知时长”是保存还是跳过。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


def _read_box_header(handle, end: int):
    start = handle.tell()
    if start + 8 > end:
        return None
    raw = handle.read(8)
    if len(raw) < 8:
        return None
    size = int.from_bytes(raw[:4], "big")
    box_type = raw[4:8]
    header_size = 8
    if size == 1:
        extra = handle.read(8)
        if len(extra) < 8:
            return None
        size = int.from_bytes(extra, "big")
        header_size = 16
    elif size == 0:
        size = end - start
    if size < header_size:
        return None
    return box_type, start, size, header_size


def _find_mvhd(handle, end: int, depth: int = 0) -> float | None:
    if depth > 4:
        return None
    while handle.tell() + 8 <= end:
        header = _read_box_header(handle, end)
        if header is None:
            return None
        box_type, start, size, header_size = header
        payload_start = start + header_size
        box_end = min(end, start + size)

        if box_type == b"moov":
            found = _find_mvhd(handle, box_end, depth + 1)
            if found is not None:
                return found
        elif box_type == b"mvhd":
            payload = handle.read(min(64, max(0, box_end - payload_start)))
            if len(payload) < 20:
                return None
            version = payload[0]
            try:
                if version == 1:
                    if len(payload) < 32:
                        return None
                    timescale = int.from_bytes(payload[20:24], "big")
                    duration = int.from_bytes(payload[24:32], "big")
                else:
                    timescale = int.from_bytes(payload[12:16], "big")
                    duration = int.from_bytes(payload[16:20], "big")
            except (IndexError, ValueError):
                return None
            if timescale <= 0 or duration <= 0:
                return None
            seconds = duration / timescale
            return seconds if 0 < seconds < 86400 else None

        handle.seek(box_end)
    return None


def probe_mp4_duration(path: Path) -> float:
    """解析 MP4/MOV 的 mvhd，返回秒数；失败返回 0。"""
    try:
        file_size = path.stat().st_size
        if file_size <= 0:
            return 0.0
        with open(path, "rb") as handle:
            value = _find_mvhd(handle, file_size)
        return float(value or 0.0)
    except (OSError, ValueError):
        return 0.0


async def _probe_ffprobe(path: Path, timeout: float) -> float:
    executable = shutil.which("ffprobe")
    if not executable:
        return 0.0
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        if process is not None and process.returncode is None:
            try:
                process.kill()
                await process.wait()
            except OSError:
                pass
        return 0.0
    try:
        value = float(stdout.decode("utf-8", "ignore").strip())
    except (TypeError, ValueError):
        return 0.0
    return value if 0 < value < 86400 else 0.0


async def probe_video_duration(path: Path, timeout: float = 10.0) -> float:
    """优先 ffprobe，失败时回退 MP4 解析；返回 0 表示未知。"""
    value = await _probe_ffprobe(path, timeout)
    if value > 0:
        return value
    return await asyncio.to_thread(probe_mp4_duration, path)
