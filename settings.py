"""插件配置模型。

这里刻意不依赖 AstrBot 的任何模块，方便在普通 Python 环境里做单元测试。
所有配置项都做了类型转换和范围限制，避免用户在 WebUI 里填了奇怪的值之后
让后台下载任务崩溃。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

PLUGIN_NAME = "astrbot_plugin_forward_media_saver"

FOLDER_LAYOUTS = ("date_scope", "date", "scope", "flat")

DEFAULT_GLOBAL_TIMEOUT = 30
DEFAULT_DOWNLOAD_TIMEOUT = 60


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "y"}:
            return True
        if lowered in {"0", "false", "no", "off", "n", ""}:
            return False
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Iterable[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = value
    else:
        raw = (value,)
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _as_id_set(value: Any) -> frozenset[str]:
    """把列表/逗号分隔字符串统一成 ID 集合。"""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        raw: Iterable[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = value
    else:
        raw = (value,)
    result: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if text:
            result.add(text)
    return frozenset(result)


@dataclass(frozen=True)
class Settings:
    """一份经过校验的运行时配置。"""

    enabled: bool = True
    save_images: bool = True
    save_videos: bool = True
    save_audio: bool = False
    save_files: bool = False
    skip_emoji: bool = False
    save_group: bool = True
    save_private: bool = True

    group_whitelist: frozenset[str] = frozenset()
    group_blacklist: frozenset[str] = frozenset()
    user_blacklist: frozenset[str] = frozenset()

    max_depth: int = 3
    max_media_per_forward: int = 100
    max_file_mb: int = 200
    min_free_mb: int = 200
    api_timeout: int = DEFAULT_GLOBAL_TIMEOUT
    file_api_timeout: int = 300
    download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT
    concurrency: int = 2
    retries: int = 2

    dedupe: bool = False
    folder_layout: str = "date_scope"
    folder_template: str = ""
    filename_template: str = ""
    timezone: str = "Asia/Shanghai"
    custom_dir: str = ""
    search_dirs: tuple[str, ...] = ()
    save_manifest: bool = True
    retention_days: int = 0
    max_total_mb: int = 0
    min_video_seconds: int = 0
    skip_unknown_video_duration: bool = False
    video_probe_timeout: int = 10
    max_attempts: int = 3
    block_private_urls: bool = False
    allowed_media_hosts: tuple[str, ...] = ()

    notify: bool = False
    notify_empty: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> Settings:
        data = data or {}
        layout = _as_str(data.get("folder_layout"), "date_scope").lower()
        if layout not in FOLDER_LAYOUTS:
            layout = "date_scope"
        return cls(
            enabled=_as_bool(data.get("enabled", True), True),
            save_images=_as_bool(data.get("save_images", True), True),
            save_videos=_as_bool(data.get("save_videos", True), True),
            save_audio=_as_bool(data.get("save_audio", False), False),
            save_files=_as_bool(data.get("save_files", False), False),
            skip_emoji=_as_bool(data.get("skip_emoji", False), False),
            save_group=_as_bool(data.get("save_group", True), True),
            save_private=_as_bool(data.get("save_private", True), True),
            group_whitelist=_as_id_set(data.get("group_whitelist")),
            group_blacklist=_as_id_set(data.get("group_blacklist")),
            user_blacklist=_as_id_set(data.get("user_blacklist")),
            # 0 表示不解析嵌套转发，只处理第一层。
            max_depth=_as_int(data.get("max_depth"), 3, 0, 10),
            max_media_per_forward=_as_int(
                data.get("max_media_per_forward"), 100, 1, 2000
            ),
            max_file_mb=_as_int(data.get("max_file_mb"), 200, 1, 4096),
            min_free_mb=_as_int(data.get("min_free_mb"), 200, 0, 102400),
            api_timeout=_as_int(data.get("api_timeout"), 30, 3, 300),
            file_api_timeout=_as_int(data.get("file_api_timeout"), 300, 5, 3600),
            download_timeout=_as_int(data.get("download_timeout"), 60, 5, 1800),
            concurrency=_as_int(data.get("concurrency"), 2, 1, 16),
            retries=_as_int(data.get("retries"), 2, 0, 5),
            dedupe=_as_bool(data.get("dedupe", False), False),
            folder_layout=layout,
            folder_template=_as_str(data.get("folder_template"), ""),
            filename_template=_as_str(data.get("filename_template"), ""),
            timezone=_as_str(data.get("timezone"), "Asia/Shanghai"),
            custom_dir=_as_str(data.get("custom_dir"), ""),
            search_dirs=_as_str_tuple(data.get("search_dirs")),
            save_manifest=_as_bool(data.get("save_manifest", True), True),
            retention_days=_as_int(data.get("retention_days"), 0, 0, 36500),
            max_total_mb=_as_int(data.get("max_total_mb"), 0, 0, 1048576),
            min_video_seconds=_as_int(data.get("min_video_seconds"), 0, 0, 86400),
            skip_unknown_video_duration=_as_bool(
                data.get("skip_unknown_video_duration", False), False
            ),
            video_probe_timeout=_as_int(data.get("video_probe_timeout"), 10, 1, 120),
            max_attempts=_as_int(data.get("max_attempts"), 3, 1, 20),
            block_private_urls=_as_bool(data.get("block_private_urls", False), False),
            allowed_media_hosts=_as_str_tuple(data.get("allowed_media_hosts")),
            notify=_as_bool(data.get("notify", False), False),
            notify_empty=_as_bool(data.get("notify_empty", False), False),
        )

    # ---- 便捷判断 -------------------------------------------------------

    def group_allowed(self, group_id: str) -> bool:
        if not group_id:
            return False
        if group_id in self.group_blacklist:
            return False
        if self.group_whitelist and group_id not in self.group_whitelist:
            return False
        return True

    def user_allowed(self, user_id: str) -> bool:
        return not user_id or user_id not in self.user_blacklist

    def media_kind_enabled(self, kind: str) -> bool:
        if kind == "image":
            return self.save_images
        if kind == "video":
            return self.save_videos
        if kind == "audio":
            return self.save_audio
        if kind == "file":
            return self.save_files
        return False

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def min_free_bytes(self) -> int:
        return self.min_free_mb * 1024 * 1024
