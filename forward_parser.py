"""合并转发消息解析。

这个模块只做「把协议端返回的数据变成统一的媒体描述」，不依赖 AstrBot，
也不发起任何网络请求，因此可以单独测试。

兼容的常见返回结构：

* go-cqhttp / Lagrange::

      {"messages": [
          {"sender": {...}, "time": 123, "message": [{"type": "image", ...}]}
      ]}

* NapCat 等::

      {"message": [
          {"type": "node", "data": {"content": [...], "user_id": ..., ...}}
      ]}

* 部分协议端把单个节点直接放在 ``message`` 里，或者外面再包一层 ``data``。

无论结构如何，最终都会得到 :class:`MediaRef` 列表和嵌套转发的 ID 列表。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_HTTP_PREFIXES = ("http://", "https://")
_CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[^\]]*)?)\]")
_NODE_TYPES = {"node", "nodes"}
_MEDIA_TYPES = {"image", "video", "record", "file"}
_MAX_INLINE_DEPTH = 8


@dataclass(frozen=True)
class MediaRef:
    """一条待下载的媒体记录。"""

    kind: str  # image / video / audio / file
    sender_id: str = ""
    sender_name: str = ""
    timestamp: int = 0
    message_id: str = ""
    url: str = ""
    file: str = ""
    path: str = ""
    file_id: str = ""
    file_name: str = ""
    size_hint: int = 0
    duration: float = 0.0
    sub_type: int = 0
    summary: str = ""

    @property
    def identity(self) -> str:
        """同一条转发内用于去重的标识。"""
        for prefix, value in (
            ("url", self.url),
            ("fid", self.file_id),
            ("path", self.path),
            ("file", self.file),
        ):
            if not value:
                continue
            if len(value) > 256:
                value = hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()
            return f"{prefix}:{value}"
        return ""


@dataclass(frozen=True)
class ForwardTarget:
    """一个待处理的合并转发。

    二选一：``forward_id`` 表示需要通过协议端 API 取回内容；
    ``inline`` 表示消息里已经带了完整节点，直接解析即可。
    """

    forward_id: str = ""
    inline: Any = None


@dataclass
class ParsedForward:
    """一层转发解析后的结果。"""

    media: list[MediaRef] = field(default_factory=list)
    nested: list[str] = field(default_factory=list)


@dataclass
class _NodeRecord:
    sender_id: str = ""
    sender_name: str = ""
    timestamp: int = 0
    message_id: str = ""
    segments: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "ignore")
        except Exception:
            return ""
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", 0, "0"):
            return value
    return None


def _segment_type(segment: Mapping[str, Any]) -> str:
    value = segment.get("type") or segment.get("_type") or ""
    if hasattr(value, "value"):
        value = value.value
    text = str(value).strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _clean_url(value: Any) -> str:
    if not value:
        return ""
    text = html.unescape(_text(value)).strip()
    if text.startswith("//"):
        text = "https:" + text
    if text.lower().startswith(_HTTP_PREFIXES):
        return text
    return ""


def _unescape_cq(value: str) -> str:
    return (
        value.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def _parse_cq_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    raw = raw.lstrip(",")
    if not raw:
        return params
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        params[key.strip()] = _unescape_cq(value.strip())
    return params


def cq_string_to_segments(text: str) -> list[dict]:
    """把 CQ 码字符串转成消息段列表（仅用于兜底）。"""
    if not text:
        return []
    segments: list[dict] = []
    cursor = 0
    for match in _CQ_PATTERN.finditer(text):
        before = text[cursor : match.start()]
        if before:
            segments.append({"type": "text", "data": {"text": before}})
        segments.append(
            {
                "type": match.group(1).lower(),
                "data": _parse_cq_params(match.group(2)),
            }
        )
        cursor = match.end()
    tail = text[cursor:]
    if tail:
        segments.append({"type": "text", "data": {"text": tail}})
    return segments


def _coerce_segments(content: Any, depth: int = 0) -> list[dict]:
    """把 message/content 字段统一成消息段列表。"""
    if content is None or depth > _MAX_INLINE_DEPTH:
        return []
    if isinstance(content, str):
        return [seg for seg in cq_string_to_segments(content) if isinstance(seg, dict)]
    if isinstance(content, Mapping):
        seg_type = _segment_type(content)
        if seg_type in _NODE_TYPES:
            return _coerce_segments(_node_content(content), depth=depth + 1)
        if seg_type:
            return [dict(content)]
        # 没有 type，但带 content/message 的节点
        return _coerce_segments(
            _first(content.get("content"), content.get("message")),
            depth=depth + 1,
        )
    if isinstance(content, Iterable):
        segments: list[dict] = []
        for item in content:
            segments.extend(_coerce_segments(item, depth=depth + 1))
        return segments
    return []


def _node_content(node: Mapping[str, Any]) -> Any:
    data = node.get("data")
    if isinstance(data, Mapping):
        return _first(
            data.get("content"),
            data.get("message"),
            node.get("content"),
            node.get("message"),
        )
    return _first(node.get("content"), node.get("message"))


# ---------------------------------------------------------------------------
# 返回结构归一化
# ---------------------------------------------------------------------------


def _is_segment(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    seg_type = _segment_type(item)
    if not seg_type or seg_type in _NODE_TYPES:
        return False
    if "message" in item or "content" in item or "sender" in item:
        return False
    return True


def _looks_like_node(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    seg_type = _segment_type(item)
    if seg_type in _NODE_TYPES:
        return True
    if "message" in item and isinstance(item.get("message"), (list, str)):
        if any(
            key in item
            for key in ("sender", "user_id", "uin", "nickname", "name", "time")
        ):
            return True
        message = item.get("message")
        if (
            isinstance(message, list)
            and message
            and all(_is_segment(seg) for seg in message)
        ):
            return True
    if "content" in item and any(
        key in item for key in ("sender", "user_id", "uin", "data", "time")
    ):
        return True
    return False


def _node_records(payload: Any, depth: int = 0) -> list[_NodeRecord]:
    """从各种返回结构里抽出节点记录。"""
    if depth > _MAX_INLINE_DEPTH or payload is None:
        return []

    if isinstance(payload, str):
        text = payload.strip()
        if not text.startswith(("{", "[")):
            return []
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return []

    if isinstance(payload, Mapping):
        # 统一响应外层：{"status": "ok", "data": {...}}
        data_value = payload.get("data")
        inner_has_nodes = isinstance(data_value, Mapping) and any(
            key in data_value for key in ("messages", "message", "nodes", "content")
        )
        if (
            "data" in payload
            and "type" not in payload
            and not _looks_like_node(payload)
            and (
                inner_has_nodes
                or (
                    isinstance(data_value, (Mapping, list))
                    and not any(
                        key in payload for key in ("messages", "message", "nodes")
                    )
                )
            )
        ):
            return _node_records(data_value, depth + 1)

        if isinstance(payload.get("messages"), list):
            records: list[_NodeRecord] = []
            for item in payload["messages"]:
                records.extend(_node_records(item, depth + 1))
            return records

        if isinstance(payload.get("nodes"), list):
            records = []
            for item in payload["nodes"]:
                records.extend(_node_records(item, depth + 1))
            return records

        if _looks_like_node(payload):
            return [_normalize_node(payload)]

        if "message" in payload:
            message = payload["message"]
            if isinstance(message, str):
                return [
                    _build_record(
                        segments=cq_string_to_segments(message),
                        sender={},
                        node=payload,
                    )
                ]
            if isinstance(message, list):
                if message and all(_is_segment(seg) for seg in message):
                    return [_normalize_node(payload)]
                records = []
                for item in message:
                    records.extend(_node_records(item, depth + 1))
                return records
            if isinstance(message, Mapping):
                return _node_records(message, depth + 1)

        if "content" in payload:
            return [_normalize_node(payload)]

        if _segment_type(payload):
            return [_build_record(segments=[dict(payload)], sender={}, node=payload)]
        return []

    if isinstance(payload, list):
        if payload and all(_is_segment(seg) for seg in payload):
            return [_build_record(segments=list(payload), sender={}, node={})]
        records = []
        for item in payload:
            records.extend(_node_records(item, depth + 1))
        return records

    return []


def _normalize_node(node: Mapping[str, Any]) -> _NodeRecord:
    data = node.get("data")
    data = data if isinstance(data, Mapping) else {}
    sender = _first(node.get("sender"), data.get("sender"))
    sender = sender if isinstance(sender, Mapping) else {}

    content = _node_content(node)
    segments = _coerce_segments(content)
    if not segments and _segment_type(node) not in _NODE_TYPES:
        # 节点自身就是消息段（少数协议端会这样返回）
        segments = [dict(node)]

    return _build_record(segments=segments, sender=sender, node=node)


def _build_record(
    segments: list[dict], sender: Mapping[str, Any], node: Mapping[str, Any]
) -> _NodeRecord:
    data = node.get("data")
    data = data if isinstance(data, Mapping) else {}

    sender_id = _first(
        sender.get("user_id"),
        sender.get("uin"),
        data.get("user_id"),
        data.get("uin"),
        node.get("user_id"),
        node.get("uin"),
    )
    sender_name = _first(
        sender.get("card"),
        sender.get("nickname"),
        data.get("nickname"),
        data.get("name"),
        node.get("nickname"),
        node.get("name"),
    )
    timestamp = _int(
        _first(
            data.get("time"),
            node.get("time"),
            data.get("timestamp"),
            node.get("timestamp"),
        )
    )
    message_id = _first(
        node.get("message_id"),
        node.get("message_seq"),
        node.get("real_id"),
        data.get("message_id"),
        data.get("message_seq"),
        data.get("real_id"),
    )
    return _NodeRecord(
        sender_id=_text(sender_id),
        sender_name=_text(sender_name),
        timestamp=timestamp,
        message_id=_text(message_id),
        segments=[seg for seg in segments if isinstance(seg, dict)],
    )


# ---------------------------------------------------------------------------
# 媒体段提取
# ---------------------------------------------------------------------------


def _media_ref(segment: Mapping[str, Any], node: _NodeRecord) -> MediaRef | None:
    seg_type = _segment_type(segment)
    if seg_type not in _MEDIA_TYPES:
        return None
    data = segment.get("data")
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            parsed = {}
        data = parsed
    if not isinstance(data, Mapping):
        data = {}

    raw_url = _text(_first(data.get("url"), data.get("origin_url")))
    url = _clean_url(raw_url)
    file_value = _text(data.get("file"))
    path = _text(_first(data.get("path"), data.get("file_path")))
    # NapCat 在合并转发里常把视频 url 写成协议端本地路径
    # （/app/.config/QQ/.../nt_data/Video/.../Ori/xxx.mp4），而不是 HTTP URL。
    if not path and raw_url and not url:
        if raw_url.startswith("file://"):
            path = raw_url
        elif "://" not in raw_url and ("/" in raw_url or "\\" in raw_url):
            path = raw_url
    file_id = _text(
        _first(
            data.get("file_id"),
            data.get("fileId"),
            data.get("file_uuid"),
            data.get("uuid"),
            data.get("fid"),
        )
    )
    file_name = _text(
        _first(data.get("file_name"), data.get("filename"), data.get("name"))
    )
    size_hint = _int(_first(data.get("file_size"), data.get("size")))
    duration = _float(
        _first(
            data.get("duration"),
            data.get("video_time"),
            data.get("file_time"),
            data.get("length"),
        )
    )
    if duration < 0:
        duration = 0.0
    sub_type = _int(data.get("sub_type"))
    summary = _text(data.get("summary"))

    # 有些协议端把 URL 放在 file 字段里。
    if not url and file_value.lower().startswith(_HTTP_PREFIXES):
        url = _clean_url(file_value)
    if not file_name and file_value and "://" not in file_value:
        file_name = file_value

    if not any((url, file_value, path, file_id)):
        return None

    kind = {
        "image": "image",
        "video": "video",
        "record": "audio",
        "file": "file",
    }.get(seg_type, seg_type)

    return MediaRef(
        kind=kind,
        sender_id=node.sender_id,
        sender_name=node.sender_name,
        timestamp=node.timestamp,
        message_id=node.message_id,
        url=url,
        file=file_value,
        path=path,
        file_id=file_id,
        file_name=file_name,
        size_hint=size_hint,
        duration=duration,
        sub_type=sub_type,
        summary=summary,
    )


def _consume_segments(
    segments: Iterable[Any],
    node: _NodeRecord,
    parsed: ParsedForward,
    depth: int = 0,
) -> None:
    if depth > _MAX_INLINE_DEPTH:
        return
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        seg_type = _segment_type(segment)
        if seg_type in _MEDIA_TYPES:
            ref = _media_ref(segment, node)
            if ref is not None:
                parsed.media.append(ref)
        elif seg_type == "forward":
            data = segment.get("data")
            data = data if isinstance(data, Mapping) else {}
            inline = _first(data.get("content"), data.get("messages"))
            if inline is not None:
                _consume_segments(
                    _coerce_segments(inline, depth=depth + 1),
                    node,
                    parsed,
                    depth + 1,
                )
                continue
            forward_id = _text(
                _first(data.get("id"), data.get("forward_id"), data.get("res_id"))
            )
            if forward_id:
                parsed.nested.append(forward_id)
        elif seg_type in _NODE_TYPES:
            _consume_segments(
                _coerce_segments(_node_content(segment), depth=depth + 1),
                node,
                parsed,
                depth + 1,
            )


def parse_forward_payload(payload: Any) -> ParsedForward:
    """解析一层合并转发返回内容。

    ``payload`` 可以是协议端 API 的返回值、单个节点，或者节点列表。
    嵌套的转发只会返回其 ID，由调用方决定是否继续取回。
    """
    parsed = ParsedForward()
    for record in _node_records(payload):
        _consume_segments(record.segments, record, parsed)

    # 协议端偶尔会在多个节点里重复带出同一张图；同层内按来源去重。
    deduped: list[MediaRef] = []
    seen: set[str] = set()
    for ref in parsed.media:
        key = ref.identity
        if key:
            if key in seen:
                continue
            seen.add(key)
        deduped.append(ref)
    parsed.media = deduped
    return parsed


# ---------------------------------------------------------------------------
# 从 AstrBot 事件里定位合并转发
# ---------------------------------------------------------------------------


def _component_type(component: Any) -> str:
    value = getattr(component, "type", "")
    if hasattr(value, "value"):
        value = value.value
    text = str(value).strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _forward_id_from_component(component: Any) -> str:
    for name in ("id", "forward_id", "res_id"):
        value = getattr(component, name, None)
        if value not in (None, "", 0, "0"):
            return _text(value).strip()
    return ""


def _component_to_segment(component: Any, depth: int = 0) -> dict:
    """把 AstrBot 的消息组件转成 OneBot 风格的消息段。"""
    seg_type = _component_type(component)
    data: dict[str, Any] = {}
    for name in (
        "file",
        "url",
        "path",
        "file_id",
        "file_name",
        "name",
        "uin",
        "text",
        "id",
    ):
        value = getattr(component, name, None)
        if value not in (None, "", [], {}):
            data[name] = value

    if seg_type in _NODE_TYPES and depth < _MAX_INLINE_DEPTH:
        content = getattr(component, "content", None)
        if content is not None:
            if not isinstance(content, (list, tuple)):
                content = [content]
            data["content"] = [
                _component_to_segment(item, depth + 1) for item in content
            ]
        nodes = getattr(component, "nodes", None)
        if nodes is not None:
            if not isinstance(nodes, (list, tuple)):
                nodes = [nodes]
            data["content"] = [_component_to_segment(item, depth + 1) for item in nodes]
    return {"type": seg_type, "data": data}


def _component_inline_payload(component: Any) -> Any:
    seg_type = _component_type(component)
    if seg_type == "nodes":
        nodes = getattr(component, "nodes", None) or []
        return {"messages": [_component_to_segment(node) for node in nodes]}
    if seg_type == "node":
        return {"messages": [_component_to_segment(component)]}
    if seg_type == "forward":
        content = getattr(component, "content", None) or getattr(
            component, "messages", None
        )
        if content:
            if not isinstance(content, (list, tuple)):
                content = [content]
            if content and not isinstance(content[0], Mapping):
                content = [_component_to_segment(item) for item in content]
            return {"messages": content}
    return None


def _targets_from_raw_message(raw_message: Any) -> list[ForwardTarget]:
    if raw_message is None:
        return []
    message = None
    if isinstance(raw_message, Mapping):
        message = raw_message.get("message")
    else:
        message = getattr(raw_message, "message", None)
    if message is None:
        return []

    if isinstance(message, str):
        segments = cq_string_to_segments(message)
    elif isinstance(message, list):
        segments = message
    else:
        return []

    targets: list[ForwardTarget] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        seg_type = _segment_type(segment)
        data = segment.get("data")
        data = data if isinstance(data, Mapping) else {}
        if seg_type == "forward":
            forward_id = _text(
                _first(data.get("id"), data.get("forward_id"), data.get("res_id"))
            ).strip()
            if forward_id:
                targets.append(ForwardTarget(forward_id=forward_id))
            elif data.get("content") or data.get("messages"):
                targets.append(ForwardTarget(inline={"messages": [segment]}))
        elif seg_type in _NODE_TYPES:
            targets.append(ForwardTarget(inline={"message": [segment]}))
    return targets


def _dedupe_targets(targets: Iterable[ForwardTarget]) -> list[ForwardTarget]:
    result: list[ForwardTarget] = []
    seen_ids: set[str] = set()
    for target in targets:
        if target.forward_id:
            if target.forward_id in seen_ids:
                continue
            seen_ids.add(target.forward_id)
            result.append(target)
        elif target.inline is not None:
            result.append(target)
    return result


def extract_forward_targets(
    components: Iterable[Any] | None, raw_message: Any = None
) -> list[ForwardTarget]:
    """从 AstrBot 消息组件/原始消息中找到合并转发目标。

    优先使用 AstrBot 已解析的消息链；如果消息链里没有（例如某些协议端的
    非标准结构导致组件解析失败），再回退到原始消息段。
    """
    targets: list[ForwardTarget] = []
    for component in components or []:
        seg_type = _component_type(component)
        if seg_type == "forward":
            forward_id = _forward_id_from_component(component)
            if forward_id:
                targets.append(ForwardTarget(forward_id=forward_id))
        elif seg_type in _NODE_TYPES:
            inline = _component_inline_payload(component)
            if inline is not None:
                targets.append(ForwardTarget(inline=inline))
    if not targets:
        targets = _targets_from_raw_message(raw_message)
    return _dedupe_targets(targets)
