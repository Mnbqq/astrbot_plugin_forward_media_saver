"""目录/文件名模板渲染。

模板占位符：
{date} {year} {month} {day} {time} {datetime} {timestamp}
{media_date} {media_year} {media_month} {media_day}
{media_time} {media_datetime} {media_timestamp}
{scope} {scope_id} {scope_name} {kind}
{sender} {sender_name} {seq} {hash} {orig}

其中 {date}/{time} 等使用保存时间，{media_date}/{media_time} 等使用
转发节点自带的原始时间（缺失时回退到保存时间）。

模板为空时沿用旧版 folder_layout 和默认文件名，保证升级后行为不变。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from datetime import timezone as _dt_timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

from .media_types import sanitize_component


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def resolve_timezone(name: str):
    """把配置里的时区名解析成 tzinfo；失败时回退系统本地时区。"""
    name = (name or "").strip()
    if not name:
        return None
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    # 容器里可能没有 tzdata；Asia/Shanghai 用固定 +8 兜底。
    if name in {"Asia/Shanghai", "PRC", "Asia/Chongqing"}:
        return _dt_timezone(timedelta(hours=8))
    return None


def format_datetime(epoch: int | float, fmt: str, tz_name: str = "") -> str:
    when = float(epoch or 0)
    return datetime.fromtimestamp(when, resolve_timezone(tz_name)).strftime(fmt)


def render_template(template: str, values: dict[str, Any]) -> str:
    if not template:
        return ""
    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, AttributeError):
        return ""


def build_values(
    *,
    scope: str,
    scope_id: str,
    scope_name: str,
    sender_id: str,
    sender_name: str,
    kind: str,
    timestamp: int,
    sequence: int,
    sha256: str,
    original_name: str = "",
    media_timestamp: int = 0,
    timezone: str = "Asia/Shanghai",
) -> dict[str, str]:
    when = int(timestamp or time.time())
    media_when = int(media_timestamp or when)
    if media_when < 946684800 or media_when > time.time() + 86400:
        media_when = when
    orig = Path(original_name).stem if original_name else ""
    return {
        "date": format_datetime(when, "%Y-%m-%d", timezone),
        "year": format_datetime(when, "%Y", timezone),
        "month": format_datetime(when, "%m", timezone),
        "day": format_datetime(when, "%d", timezone),
        "time": format_datetime(when, "%H%M%S", timezone),
        "datetime": format_datetime(when, "%Y%m%d-%H%M%S", timezone),
        "timestamp": str(when),
        "media_date": format_datetime(media_when, "%Y-%m-%d", timezone),
        "media_year": format_datetime(media_when, "%Y", timezone),
        "media_month": format_datetime(media_when, "%m", timezone),
        "media_day": format_datetime(media_when, "%d", timezone),
        "media_time": format_datetime(media_when, "%H%M%S", timezone),
        "media_datetime": format_datetime(media_when, "%Y%m%d-%H%M%S", timezone),
        "media_timestamp": str(media_when),
        "scope": sanitize_component(scope, fallback="chat", limit=16),
        "scope_id": sanitize_component(scope_id, fallback="unknown"),
        "scope_name": (
            sanitize_component(scope_name, fallback="", limit=64)
            or sanitize_component(scope_id, fallback="unknown")
        ),
        "kind": sanitize_component(kind, fallback="media", limit=16),
        "sender": sanitize_component(sender_id, fallback="unknown", limit=32),
        "sender_name": sanitize_component(sender_name, fallback="", limit=48),
        "seq": f"{max(1, int(sequence)):02d}",
        "hash": sanitize_component((sha256 or "")[:8], fallback="nohash", limit=8),
        "orig": sanitize_component(orig, fallback="", limit=64),
    }


def build_template_folder(root: Path, template: str, values: dict[str, str]) -> Path:
    rendered = render_template(template, values).replace("\\", "/")
    parts = [part for part in rendered.split("/") if part.strip()]
    safe_parts = [
        sanitize_component(part, fallback="unknown", limit=64) for part in parts
    ]
    return root.joinpath(*safe_parts) if safe_parts else root


def build_template_filename(
    template: str, values: dict[str, str], fallback: str
) -> str:
    rendered = sanitize_component(
        render_template(template, values), fallback=fallback, limit=120
    )
    return rendered or fallback
