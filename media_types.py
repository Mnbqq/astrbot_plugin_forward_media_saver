"""媒体类型识别与安全命名（无第三方依赖）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_SAFE_NAME = re.compile(r"[^\w-]+", re.UNICODE)

IMAGE_EXTS = {
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "bmp",
    "heic",
    "avif",
    "tif",
    "tiff",
}
AUDIO_EXTS = {
    "mp3",
    "m4a",
    "wav",
    "amr",
    "ogg",
    "flac",
    "silk",
    "aac",
    "wma",
}

VIDEO_EXTS = {
    "mp4",
    "mov",
    "webm",
    "mkv",
    "avi",
    "flv",
    "m4v",
    "ts",
    "mpg",
    "mpeg",
    "3gp",
}

CONTENT_TYPE_KIND_EXT: dict[str, tuple[str, str]] = {
    "image/jpeg": ("image", "jpg"),
    "image/jpg": ("image", "jpg"),
    "image/png": ("image", "png"),
    "image/gif": ("image", "gif"),
    "image/webp": ("image", "webp"),
    "image/bmp": ("image", "bmp"),
    "image/heic": ("image", "heic"),
    "image/heif": ("image", "heic"),
    "image/avif": ("image", "avif"),
    "image/tiff": ("image", "tiff"),
    "video/mp4": ("video", "mp4"),
    "video/quicktime": ("video", "mov"),
    "video/webm": ("video", "webm"),
    "video/x-matroska": ("video", "mkv"),
    "video/x-flv": ("video", "flv"),
    "video/x-msvideo": ("video", "avi"),
    "video/3gpp": ("video", "3gp"),
    "video/mpeg": ("video", "mpg"),
    "audio/mpeg": ("audio", "mp3"),
    "audio/mp3": ("audio", "mp3"),
    "audio/mp4": ("audio", "m4a"),
    "audio/x-m4a": ("audio", "m4a"),
    "audio/wav": ("audio", "wav"),
    "audio/x-wav": ("audio", "wav"),
    "audio/amr": ("audio", "amr"),
    "audio/ogg": ("audio", "ogg"),
    "audio/flac": ("audio", "flac"),
    "audio/aac": ("audio", "aac"),
    "video/mp2t": ("video", "ts"),
}


_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(value: Any, fallback: str = "unknown", limit: int = 64) -> str:
    """把 ID/名称变成安全的路径片段。

    * 保留 Unicode 字母/数字（中文群名不会被替换成 unknown）；
    * 路径分隔符、点号、控制字符等替换为下划线，挡住 ``..``/``/``；
    * 去掉首尾的 ``.``/``_``/``-``，避免隐藏文件和空片段；
    * 对 Windows 设备名追加下划线；
    * 控制 UTF-8 字节长度，避免中文文件名超过文件系统限制。
    """
    text = _SAFE_NAME.sub("_", str(value or "")).strip("._-")
    text = (text or fallback)[:limit]
    if text.upper() in _RESERVED_NAMES:
        text += "_"
    while len(text.encode("utf-8")) > 240:
        text = text[:-1]
    return text or fallback


def sniff_media(head: bytes) -> tuple[str | None, str | None]:
    """根据文件头判断 (kind, 扩展名)。"""
    if not head:
        return None, None

    if head.startswith(b"\xff\xd8\xff"):
        return "image", "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image", "gif"
    if head.startswith(b"BM"):
        return "image", "bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image", "tiff"

    if head[:4] == b"RIFF" and len(head) >= 12:
        brand = head[8:12]
        if brand == b"WEBP":
            return "image", "webp"
        if brand == b"AVI ":
            return "video", "avi"
        if brand == b"WAVE":
            return None, None  # 音频，不在本插件范围

    if head.startswith(b"\x1aE\xdf\xa3"):
        if b"webm" in head[:64].lower():
            return "video", "webm"
        return "video", "mkv"

    if head.startswith(b"FLV"):
        return "video", "flv"
    if head.startswith(b"\x00\x00\x01\xba") or head.startswith(b"\x00\x00\x01\xb3"):
        return "video", "mpg"

    ftyp_index = head.find(b"ftyp")
    if 0 <= ftyp_index <= 12 and len(head) >= ftyp_index + 8:
        brand = head[ftyp_index + 4 : ftyp_index + 8]
        if brand == b"qt  ":
            return "video", "mov"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "image", "heic"
        if brand in (b"avif", b"avis"):
            return "image", "avif"
        if brand.startswith(b"3g"):
            return "video", "3gp"
        return "video", "mp4"

    return None, None


def kind_ext_from_name(name: str) -> tuple[str | None, str | None]:
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix in IMAGE_EXTS:
        return "image", "jpg" if suffix == "jpeg" else suffix
    if suffix in VIDEO_EXTS:
        return "video", suffix
    if suffix in AUDIO_EXTS:
        return "audio", suffix
    return None, None


def sniff_audio(head: bytes) -> str | None:
    """识别常见音频文件头，返回扩展名。"""
    if not head:
        return None
    if (
        head.startswith(b"ID3")
        or head.startswith(b"\xff\xfb")
        or head.startswith(b"\xff\xf3")
    ):
        return "mp3"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"#!AMR"):
        return "amr"
    if head.startswith(b"SILK"):
        return "silk"
    return None
