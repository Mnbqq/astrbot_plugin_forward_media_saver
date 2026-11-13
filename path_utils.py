"""跨平台本地路径识别与规范化。

移植自参考插件 astrbot_plugin_media_saver 的路径处理思路，并做了收紧：
* 支持 Windows 盘符、Unix 绝对路径、UNC、file:// URI；
* 支持 JSON 里可能带引号的路径；
* 对 file:// URI 做 URL 解码，并兼容 Windows 的 /C:/... 形式；
* 返回真正存在且可访问的绝对路径，避免把普通文件名误当路径。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse


def _clean(value: str) -> str:
    return (value or "").strip().strip('"').strip("'")


def is_local_path(value: str) -> bool:
    """判断字符串看起来是不是本地文件路径（不检查存在性）。"""
    text = _clean(value)
    if not text:
        return False
    if text.startswith("file://"):
        return True
    if len(text) >= 3 and text[1] == ":" and text[0].isalpha():
        return True
    return text.startswith(("/", "\\\\"))


def normalize_path_candidates(value: str) -> list[str]:
    """生成同一路径的多种写法，兼容 Windows/Unix/file:// 与转义差异。"""
    text = _clean(value)
    if not text:
        return []
    candidates: list[str] = []

    if text.startswith("file://"):
        parsed = urlparse(text)
        path = unquote(parsed.path)
        # Windows: file:///C:/... -> /C:/... -> C:/...
        if len(path) >= 3 and path[0] == "/" and path[2] == ":" and path[1].isalpha():
            path = path[1:]
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path = f"//{parsed.netloc}{path}"
        candidates.append(path)
        candidates.append(path.replace("/", "\\"))
    else:
        candidates.append(text)
        if "\\" in text:
            candidates.append(text.replace("\\", "/"))
        if "/" in text:
            candidates.append(text.replace("/", "\\"))

    result: list[str] = []
    for item in candidates:
        if item and item not in result:
            result.append(item)
    return result


def local_path_exists(value: str) -> Path | None:
    """路径存在时返回解析后的绝对 Path，否则返回 None。"""
    if not value or len(value) > 4096:
        return None
    for candidate in normalize_path_candidates(value):
        try:
            path = Path(candidate)
            if path.is_file():
                return path.resolve()
        except (OSError, ValueError):
            continue
    return None
