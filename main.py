"""AstrBot 插件：自动保存合并转发里的图片和视频。

工作流程：
1. 监听 aiocqhttp（OneBot v11）的全部消息；
2. 只在消息里出现合并转发、且发送者不是机器人自己时介入；
3. 通过协议端 ``get_forward_msg`` 取回节点（支持嵌套转发）；
4. 把节点里的图片/视频下载到 data/plugin_data 下；
5. 可选择在会话里回一条保存摘要。

所有耗时操作都放在后台任务里，不会阻塞 AstrBot 的 LLM 回复流程。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .forward_parser import (
    ForwardTarget,
    MediaRef,
    cq_string_to_segments,
    extract_forward_targets,
    parse_forward_payload,
)
from .index_store import IndexHashStore, IndexStore
from .media_saver import MediaSaver, SaveContext, SaveOutcome
from .naming import format_datetime
from .settings import PLUGIN_NAME, Settings

_MAX_PENDING_TASKS = 64
_API_FILE_FIELDS = ("url", "base64", "file", "path")


@dataclass
class _EventSnapshot:
    """从事件里提取的、后台任务需要的全部信息。

    后台任务不长期持有 AstrMessageEvent，避免事件对象被回收/复用后出现
    难以排查的问题。
    """

    umo: str
    group_id: str
    sender_id: str
    sender_name: str
    self_id: str
    message_id: str = ""
    group_name: str = ""
    targets: list[ForwardTarget] = field(default_factory=list)
    bot: Any = None

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)

    @property
    def scope(self) -> str:
        return "group" if self.is_group else "private"

    @property
    def scope_id(self) -> str:
        return self.group_id if self.is_group else self.sender_id


class Main(Star):
    """自动保存合并转发媒体。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        self._settings = Settings.from_mapping(self.config)
        self._log = self._pick_logger()

        self._data_dir = self._resolve_data_dir()
        self._root = self._resolve_media_root()
        self._search_roots = self._resolve_search_roots()
        self._index = IndexStore(self._data_dir / "index.db", self._log)
        self._manifest_lock = asyncio.Lock()
        self._hash_store = (
            IndexHashStore(self._index) if self._settings.dedupe else None
        )
        self._saver = MediaSaver(
            root=self._root,
            settings=self._settings,
            log=self._log,
            session_provider=self._get_session,
            resolver=self._resolve_media_via_api,
            hash_store=self._hash_store,
            search_roots=self._search_roots,
        )

        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._prepare_lock = asyncio.Lock()
        self._prepared = False
        self._closing = False
        self._semaphore = asyncio.Semaphore(self._settings.concurrency)
        self._tasks: set[asyncio.Task] = set()
        self._stats: Counter[str] = Counter()
        self._protocol_name: str | None = None
        self._protocol_version: str = ""

        self._log.info(
            "合并转发媒体保存已加载：目录=%s，监听图片=%s，视频=%s，群聊=%s，私聊=%s",
            self._root,
            self._settings.save_images,
            self._settings.save_videos,
            self._settings.save_group,
            self._settings.save_private,
        )
        if self._search_roots:
            self._log.info(
                "本地缓存搜索目录：%s",
                "；".join(str(item) for item in self._search_roots),
            )
        self._register_web_api()

    # ------------------------------------------------------------------
    # WebUI 页面 API
    # ------------------------------------------------------------------

    def _register_web_api(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            return
        try:
            from astrbot.api.web import json_response, request  # noqa: F401
        except Exception:
            self._log.debug("当前 AstrBot 不支持 astrbot.api.web，跳过 WebUI 页面 API")
            return
        plugin_name = getattr(self, "name", "") or PLUGIN_NAME
        routes = (
            (
                f"/{plugin_name}/stats",
                self.web_stats,
                ["GET"],
                "合并转发媒体统计",
            ),
            (
                f"/{plugin_name}/recent",
                self.web_recent,
                ["GET"],
                "最近保存记录",
            ),
            (
                f"/{plugin_name}/clean",
                self.web_clean,
                ["POST"],
                "清理旧媒体",
            ),
            (
                f"/{plugin_name}/paths",
                self.web_paths,
                ["GET"],
                "保存路径信息",
            ),
        )
        for route, handler, methods, desc in routes:
            try:
                register(route, handler, methods, desc)
            except Exception:
                self._log.warning("注册 Web API 失败：%s", route)

    async def web_stats(self):
        from astrbot.api.web import json_response, request

        days = request.query.get("days", 30, type=int)
        data = await self._index.stats(days)
        data["jobs"] = await self._index.job_counts()
        data["recent"] = await self._index.recent(10)
        return json_response(data)

    async def web_recent(self):
        from astrbot.api.web import json_response, request

        limit = request.query.get("limit", 20, type=int)
        scope = request.query.get("scope", "")
        scope_id = request.query.get("scope_id", "")
        rows = await self._index.recent(limit, scope, scope_id)
        return json_response({"items": rows})

    async def web_clean(self):
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            payload = {}
        days = _to_int(payload.get("days", self._settings.retention_days), 0)
        if days <= 0:
            return error_response("days must be greater than 0", status_code=400)
        count, size = await self._clean(days, self._settings.max_total_mb)
        return json_response({"deleted": count, "freed": size})

    async def web_paths(self):
        from astrbot.api.web import json_response

        return json_response(
            {
                "media": str(self._root),
                "index": str(self._data_dir / "index.db"),
                "manifest": str(self._data_dir / "manifest.jsonl"),
            }
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """插件被激活时准备目录、数据库与会话。"""
        await self._index.initialize()
        reset = await self._index.reset_running_jobs()
        if reset:
            self._log.info("已将 %s 个中断的任务重置为待重试", reset)
        await self._ensure_prepared()

    async def terminate(self) -> None:
        """插件被禁用/重载时取消后台任务并关闭连接。"""
        self._closing = True
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()
        await self._index.close()
        self._log.info("合并转发媒体保存已停止：%s", self._stats.copy())

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """监听全部消息，发现合并转发时在后台自动保存其中的媒体。"""
        if not self._settings.enabled or self._closing:
            return

        try:
            snapshot = self._build_snapshot(event)
        except Exception:
            self._log.exception("解析消息事件失败，已忽略")
            return
        if snapshot is None:
            return

        if len(self._tasks) >= _MAX_PENDING_TASKS:
            self._log.warning(
                "待处理的合并转发任务过多（%s），本条消息已跳过", len(self._tasks)
            )
            return

        job_id = await self._enqueue_job(snapshot)
        if job_id == "":
            return

        task = asyncio.create_task(
            self._process_snapshot(snapshot, job_id),
            name=f"forward-media-{snapshot.scope}-{snapshot.scope_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _build_snapshot(self, event: AstrMessageEvent) -> _EventSnapshot | None:
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return None

        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or getattr(message_obj, "self_id", "") or "")
        # 只处理“别人发的”合并转发。
        if sender_id and self_id and sender_id == self_id:
            return None

        group_id = str(event.get_group_id() or "")
        if group_id:
            if not self._settings.save_group:
                return None
            if not self._settings.group_allowed(group_id):
                return None
        else:
            if not self._settings.save_private:
                return None
        if not self._settings.user_allowed(sender_id):
            return None

        components = []
        getter = getattr(event, "get_messages", None)
        if callable(getter):
            try:
                components = list(getter() or [])
            except Exception:
                components = []
        if not components:
            components = list(getattr(message_obj, "message", None) or [])

        raw_message = getattr(message_obj, "raw_message", None)
        targets = extract_forward_targets(components, raw_message)
        if not targets:
            return None

        group = getattr(message_obj, "group", None)
        return _EventSnapshot(
            umo=str(getattr(event, "unified_msg_origin", "") or ""),
            group_id=group_id,
            sender_id=sender_id,
            sender_name=str(event.get_sender_name() or ""),
            self_id=self_id,
            message_id=str(getattr(message_obj, "message_id", "") or ""),
            group_name=str(getattr(group, "group_name", "") or ""),
            targets=targets,
            bot=getattr(event, "bot", None),
        )

    async def _enqueue_job(self, snapshot: _EventSnapshot) -> str | None:
        """持久化任务；返回 job_id，None 表示数据库不可用，空串表示重复任务。"""
        key = (
            snapshot.message_id
            if snapshot.message_id and snapshot.message_id not in ("0", "unknown")
            else uuid.uuid4().hex
        )
        job_id = f"{snapshot.scope}:{snapshot.scope_id}:{key}"
        targets = [
            {"forward_id": target.forward_id, "inline": target.inline}
            for target in snapshot.targets
        ]
        meta = {
            "umo": snapshot.umo,
            "group_id": snapshot.group_id,
            "sender_id": snapshot.sender_id,
            "sender_name": snapshot.sender_name,
            "self_id": snapshot.self_id,
            "message_id": snapshot.message_id,
            "group_name": snapshot.group_name,
        }
        try:
            created = await self._index.create_job(
                job_id,
                snapshot.scope,
                snapshot.scope_id,
                targets,
                meta,
            )
        except Exception:
            self._log.exception("写入任务索引失败，本条转发仍会继续处理")
            return None
        if not created:
            self._log.debug("重复的转发任务，已跳过：%s", job_id)
            return ""
        return job_id

    async def _process_snapshot(
        self, snapshot: _EventSnapshot, job_id: str | None = None
    ) -> None:
        if job_id:
            try:
                await self._index.mark_job(job_id, "running", inc_attempts=True)
            except Exception:
                self._log.exception("更新任务状态失败：%s", job_id)
        try:
            counts = await self._process_snapshot_inner(snapshot)
        except asyncio.CancelledError:
            if job_id:
                try:
                    await self._index.mark_job(job_id, "pending", "任务被取消")
                except Exception:
                    pass
            raise
        except Exception as exc:
            if job_id:
                try:
                    await self._index.mark_job(job_id, "failed", str(exc))
                except Exception:
                    pass
            raise
        if job_id:
            failed = (
                (counts or Counter()).get("failed", 0)
                + (counts or Counter()).get("fetch_failed", 0)
                + (counts or Counter()).get("parse_failed", 0)
            )
            try:
                await self._index.mark_job(
                    job_id,
                    "failed" if failed else "done",
                    f"{failed} 个媒体/节点失败" if failed else "",
                )
            except Exception:
                self._log.exception("更新任务状态失败：%s", job_id)

    async def _process_snapshot_inner(self, snapshot: _EventSnapshot) -> Counter | None:
        bot = snapshot.bot
        if bot is None:
            self._log.warning("当前事件没有 bot 实例，无法取回合并转发内容")
            return None

        await self._ensure_prepared()
        await self._probe_protocol(bot)
        async with self._semaphore:
            counts: Counter[str] = Counter()
            saved_outcomes: list[SaveOutcome] = []
            queue: list[tuple[ForwardTarget, int]] = [
                (target, 0) for target in snapshot.targets
            ]
            visited: set[str] = set()
            media_index = 0
            context = SaveContext(
                scope=snapshot.scope,
                scope_id=snapshot.scope_id,
                scope_name=snapshot.group_name,
                received_at=int(time.time()),
            )

            while queue and media_index < self._settings.max_media_per_forward:
                target, depth = queue.pop(0)
                if target.forward_id:
                    if target.forward_id in visited:
                        continue
                    if len(visited) >= self._settings.max_media_per_forward + 20:
                        counts["limit_skipped"] += 1
                        break
                    visited.add(target.forward_id)
                    payload = await self._fetch_forward(
                        bot, target.forward_id, snapshot.self_id
                    )
                    if payload is None:
                        counts["fetch_failed"] += 1
                        self._log.warning("取回合并转发失败：id=%s", target.forward_id)
                        continue
                else:
                    payload = target.inline

                try:
                    parsed = parse_forward_payload(payload)
                except Exception:
                    counts["parse_failed"] += 1
                    self._log.exception("解析合并转发内容失败，已跳过该节点")
                    continue

                for nested_id in parsed.nested:
                    if depth < self._settings.max_depth:
                        queue.append((ForwardTarget(forward_id=nested_id), depth + 1))
                    else:
                        counts["depth_skipped"] += 1
                        self._log.debug(
                            "嵌套转发层数超过 %s，已跳过：%s",
                            self._settings.max_depth,
                            nested_id,
                        )

                current_forward_id = target.forward_id
                for ref in parsed.media:
                    if media_index >= self._settings.max_media_per_forward:
                        counts["limit_skipped"] += 1
                        break
                    media_index += 1
                    if self._settings.skip_emoji and _is_emoji_ref(ref):
                        counts["skipped"] += 1
                        self._log.debug("跳过表情包：%s", ref.file_name or ref.file)
                        continue
                    outcome = await self._saver.save(
                        ref, context, sequence=media_index, bot=bot
                    )
                    counts[outcome.status] += 1
                    if outcome.status == "saved":
                        saved_outcomes.append(outcome)
                        self._stats["saved"] += 1
                        await self._record_saved(
                            snapshot, ref, outcome, current_forward_id
                        )
                        self._log.info(
                            "已保存%s：%s（%s）",
                            _kind_label(outcome.kind),
                            outcome.path,
                            _human_size(outcome.size),
                        )
                    elif outcome.status == "duplicate":
                        self._stats["duplicate"] += 1
                        self._log.debug("跳过重复媒体：%s", outcome.reason)
                    elif outcome.status == "skipped":
                        self._stats["skipped"] += 1
                        self._log.debug("跳过媒体：%s", outcome.reason)
                    else:
                        self._stats["failed"] += 1
                        self._log.warning(
                            "媒体保存失败：%s（类型=%s，url=%s，path=%s，"
                            "file=%s，file_id=%s，message_id=%s）",
                            outcome.reason,
                            ref.kind,
                            _brief(ref.url),
                            _brief(ref.path),
                            _brief(ref.file),
                            _brief(ref.file_id),
                            _brief(ref.message_id),
                        )
                        if ref.path and ref.path.startswith("/"):
                            try:
                                path_obj = Path(ref.path)
                                exists = path_obj.exists()
                                is_file = path_obj.is_file()
                                readable = os.access(path_obj, os.R_OK)
                            except (OSError, ValueError):
                                exists = is_file = readable = False
                            uid = os.getuid() if hasattr(os, "getuid") else "?"
                            gid = os.getgid() if hasattr(os, "getgid") else "?"
                            self._log.warning(
                                "本地路径诊断：exists=%s，is_file=%s，readable=%s，"
                                "当前 UID/GID=%s/%s，路径=%s",
                                exists,
                                is_file,
                                readable,
                                uid,
                                gid,
                                ref.path,
                            )
                            self._log.warning(
                                "如果 exists=False：AstrBot 容器没有挂载该目录，"
                                "或 NapCat 根本没有下载这个视频；"
                                "如果 readable=False：检查 UID/GID 与文件权限。"
                            )

            if counts["fetch_failed"]:
                self._log.warning(
                    "本次合并转发有 %s 个节点取回失败（可能是协议端不支持 "
                    "get_forward_msg 或转发已过期）",
                    counts["fetch_failed"],
                )

            await self._maybe_notify(snapshot, counts, saved_outcomes)
            return counts

    async def _record_saved(
        self,
        snapshot: _EventSnapshot,
        ref: MediaRef,
        outcome: SaveOutcome,
        forward_id: str,
    ) -> None:
        record = {
            "saved_at": int(time.time()),
            "kind": outcome.kind,
            "scope": snapshot.scope,
            "scope_id": snapshot.scope_id,
            "scope_name": snapshot.group_name,
            "sender_id": ref.sender_id or snapshot.sender_id,
            "sender_name": ref.sender_name or snapshot.sender_name,
            "message_id": ref.message_id or snapshot.message_id,
            "forward_id": forward_id,
            "path": str(outcome.path or ""),
            "size": int(outcome.size),
            "sha256": outcome.sha256,
            "orig_name": ref.file_name,
        }
        try:
            await self._index.record_media(record, store_hash=self._settings.dedupe)
        except Exception:
            self._log.exception("写入媒体索引失败：%s", outcome.path)
        if self._settings.save_manifest:
            try:
                await self._append_manifest(record)
            except Exception:
                self._log.exception("写入 manifest 失败：%s", outcome.path)

    async def _append_manifest(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        path = self._data_dir / "manifest.jsonl"

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

        async with self._manifest_lock:
            await asyncio.to_thread(_write)

    # ------------------------------------------------------------------
    # 管理命令
    # ------------------------------------------------------------------

    @filter.command("fms")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fms_command(self, event: AstrMessageEvent, sub: str = ""):
        """合并转发媒体管理：/fms stats|path|last|clean|retry|export|help"""
        parts = event.message_str.strip().split()
        token = parts[0].lstrip("/").lower() if parts else ""
        if token in ("fms", "fms@bot") or token.startswith("fms"):
            args = parts[1:]
        elif sub:
            args = [sub] + parts[1:]
        else:
            args = parts
        action = (args[0] if args else "help").lower()

        if action in ("help", "帮助"):
            yield event.plain_result(
                "合并转发媒体管理：\n"
                "/fms stats [天数] - 保存统计\n"
                "/fms last [条数] - 最近保存记录\n"
                "/fms path - 目录、索引、清单路径\n"
                "/fms clean [天数] - 清理旧文件（默认 retention_days）\n"
                "/fms retry - 重试当前会话失败/中断的任务\n"
                "/fms export - 导出 manifest.jsonl"
            )
            return

        if action in ("stats", "统计"):
            days = _to_int(args[1] if len(args) > 1 else 0, 0)
            data = await self._index.stats(days)
            jobs = await self._index.job_counts()
            lines = [
                f"媒体统计（最近 {days} 天）" if days else "媒体统计（全部）",
                f"记录 {data['count']} 条，总大小 {_human_size(data['size'])}",
            ]
            for item in data["kinds"]:
                lines.append(
                    f"- {_kind_label(item['kind'])}：{item['count']} 个，"
                    f"{_human_size(item['size'])}"
                )
            for item in data["scopes"][:5]:
                name = item["scope_name"] or item["scope_id"]
                lines.append(
                    f"  {name}：{item['count']} 个，{_human_size(item['size'])}"
                )
            if jobs:
                lines.append(
                    "任务："
                    + "，".join(f"{key}={value}" for key, value in jobs.items())
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("last", "最近"):
            limit = _to_int(args[1] if len(args) > 1 else 10, 10)
            scope, scope_id = _event_scope(event)
            rows = await self._index.recent(limit, scope, scope_id)
            if not rows:
                yield event.plain_result("当前会话还没有保存记录。")
                return
            lines = ["最近保存："]
            for row in rows:
                lines.append(
                    f"- {format_datetime(row['saved_at'], '%m-%d %H:%M', self._settings.timezone)} "
                    f"{_kind_label(row['kind'])} {Path(row['path']).name} "
                    f"({_human_size(row['size'])})"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("path", "路径"):
            yield event.plain_result(
                "媒体目录：" + str(self._root) + "\n"
                "索引数据库：" + str(self._data_dir / "index.db") + "\n"
                "清单文件：" + str(self._data_dir / "manifest.jsonl")
            )
            return

        if action in ("clean", "清理"):
            days = _to_int(
                args[1] if len(args) > 1 else self._settings.retention_days,
                self._settings.retention_days,
            )
            if days <= 0:
                yield event.plain_result(
                    "请指定天数，例如 /fms clean 30；或在插件配置中设置 retention_days。"
                )
                return
            count, size = await self._clean(days, self._settings.max_total_mb)
            yield event.plain_result(
                f"已清理 {count} 个文件，释放 {_human_size(size)}（保留最近 {days} 天）。"
            )
            return

        if action in ("retry", "重试"):
            count = await self._retry_jobs(event)
            yield event.plain_result(
                f"已提交 {count} 个待重试任务。"
                if count
                else "当前会话没有待重试任务。"
            )
            return

        if action in ("export", "导出"):
            path = await self._export_manifest()
            yield event.plain_result(f"已导出清单：{path}")
            return

        yield event.plain_result("未知子命令，使用 /fms help 查看帮助。")

    async def _clean(self, days: int, max_total_mb: int = 0) -> tuple[int, int]:
        cutoff = int(time.time() - max(1, days) * 86400)
        rows = await self._index.old_media(cutoff)
        by_id = {int(row["id"]): row for row in rows}
        if max_total_mb > 0:
            all_rows = await self._index.all_media_oldest()
            total = sum(int(row["size"] or 0) for row in all_rows)
            limit_bytes = max_total_mb * 1024 * 1024
            for row in all_rows:
                if total <= limit_bytes:
                    break
                by_id.setdefault(int(row["id"]), row)
                total -= int(row["size"] or 0)

        deleted_ids: list[int] = []
        freed = 0
        root = self._root.resolve()
        for row in by_id.values():
            path = Path(str(row.get("path") or ""))
            try:
                resolved = path.resolve()
                if resolved != root and root not in resolved.parents:
                    continue
                if resolved.is_file():
                    freed += resolved.stat().st_size
                    resolved.unlink()
                deleted_ids.append(int(row["id"]))
            except OSError:
                continue
        if deleted_ids:
            await self._index.delete_media(deleted_ids)
        await self._index.prune_jobs(max(days, 30))
        return len(deleted_ids), freed

    async def _retry_jobs(self, event: AstrMessageEvent) -> int:
        bot = getattr(event, "bot", None)
        scope, scope_id = _event_scope(event)
        if bot is None:
            return 0
        jobs = await self._index.retryable_jobs(
            scope, scope_id, limit=10, max_attempts=self._settings.max_attempts
        )
        count = 0
        for job in jobs:
            targets = [
                ForwardTarget(
                    forward_id=str(item.get("forward_id") or ""),
                    inline=item.get("inline"),
                )
                for item in job.get("targets", [])
                if isinstance(item, dict)
            ]
            if not targets:
                continue
            meta = job.get("meta") or {}
            snapshot = _EventSnapshot(
                umo=str(getattr(event, "unified_msg_origin", "") or ""),
                group_id=str(meta.get("group_id") or ""),
                sender_id=str(meta.get("sender_id") or ""),
                sender_name=str(meta.get("sender_name") or ""),
                self_id=str(meta.get("self_id") or ""),
                message_id=str(meta.get("message_id") or ""),
                group_name=str(meta.get("group_name") or ""),
                targets=targets,
                bot=bot,
            )
            if len(self._tasks) >= _MAX_PENDING_TASKS:
                break
            task = asyncio.create_task(
                self._process_snapshot(snapshot, job["job_id"]),
                name=f"forward-retry-{job['job_id']}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._on_task_done)
            count += 1
        return count

    async def _export_manifest(self) -> Path:
        rows = await self._index.recent(100000)
        path = self._data_dir / "export_manifest.jsonl"

        def _write() -> None:
            with open(path, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        await asyncio.to_thread(_write)
        return path

    # ------------------------------------------------------------------
    # 协议端交互
    # ------------------------------------------------------------------

    async def _probe_protocol(self, bot: Any) -> None:
        """记录一次协议端名称/版本，方便排查 get_file 等扩展接口差异。"""
        if self._protocol_name is not None:
            return
        call = getattr(bot, "call_action", None)
        if not callable(call):
            self._protocol_name = "unknown"
            return
        try:
            result = call("get_version_info")
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=5)
        except Exception as exc:  # noqa: BLE001
            self._protocol_name = "unknown"
            self._log.info("无法获取协议端版本信息：%r", exc)
            return
        if isinstance(result, dict):
            data = (
                result.get("data") if isinstance(result.get("data"), dict) else result
            )
            self._protocol_name = str(data.get("app_name") or "unknown")
            self._protocol_version = str(data.get("app_version") or "")
        else:
            self._protocol_name = "unknown"
        self._log.info("协议端：%s %s", self._protocol_name, self._protocol_version)

    async def _fetch_forward(self, bot: Any, forward_id: str, self_id: str = "") -> Any:
        """取回合并转发内容，兼容 id/message_id 两种参数名。"""
        call = getattr(bot, "call_action", None)
        if not callable(call):
            return None

        # 一些协议端会拒绝 self_id 这个额外参数；先带 self_id，再回退到不带。
        self_ids = (self_id, "") if self_id else ("",)
        for params in ({"id": forward_id}, {"message_id": forward_id}):
            for sid in self_ids:
                payload = await self._call_action(call, "get_forward_msg", params, sid)
                if self._payload_ok(payload):
                    return payload

        # 少数协议端只实现了 get_msg，且会把节点直接内联在 message 里。
        for sid in self_ids:
            payload = await self._call_action(
                call, "get_msg", {"message_id": forward_id}, sid
            )
            if self._payload_ok(payload):
                return payload
        return None

    async def _call_action(
        self,
        call: Any,
        action: str,
        params: dict[str, Any],
        self_id: str = "",
    ) -> Any:
        kwargs = dict(params)
        if self_id:
            # AstrBot 自身调用 OneBot 接口时传的是 int；部分协议端对类型敏感。
            kwargs.setdefault("self_id", int(self_id) if self_id.isdigit() else self_id)
        try:
            result = call(action, **kwargs)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(
                    result, timeout=self._settings.api_timeout
                )
            return result
        except asyncio.TimeoutError:
            self._log.warning("调用 %s 超时（%ss）", action, self._settings.api_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 协议端返回的错误不应让插件崩溃
            self._log.debug("调用 %s 失败：%s", action, exc)
        return None

    @staticmethod
    def _payload_ok(payload: Any) -> bool:
        if not isinstance(payload, (dict, list)):
            return False
        if isinstance(payload, dict):
            if isinstance(payload.get("data"), (dict, list)) and not any(
                key in payload for key in ("messages", "message", "nodes")
            ):
                payload = payload["data"]
            status = str(payload.get("status", "")).strip().lower()
            if status in {"failed", "error"}:
                return False
            if payload.get("retcode") not in (None, 0, "0"):
                return False
            if not any(
                key in payload for key in ("messages", "message", "nodes", "content")
            ) and not payload.get("type"):
                return False
        return True

    async def _refresh_media_from_message(
        self, ref: MediaRef, bot: Any
    ) -> tuple[str | None, str] | None:
        """用节点自身的 message_id 重新取消息，提取刷新后的媒体地址/文件码。"""
        call = getattr(bot, "call_action", None)
        if not callable(call):
            return None
        payload = await self._call_action(
            call, "get_msg", {"message_id": ref.message_id}
        )
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("data"), dict):
            payload = payload["data"]
        segments: list[Any] = []
        message = payload.get("message")
        if isinstance(message, list):
            segments.extend(message)
        elif isinstance(message, str):
            segments.extend(cq_string_to_segments(message))
        elif isinstance(message, dict):
            segments.append(message)
        raw_message = payload.get("raw_message")
        if isinstance(raw_message, str):
            segments.extend(cq_string_to_segments(raw_message))

        file_code = ""
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type", "")).lower()
            if seg_type != ref.kind:
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            candidate_file = data.get("file")
            if isinstance(candidate_file, str) and candidate_file.strip():
                file_code = candidate_file.strip()
            for key in ("url", "path", "file_path"):
                value = data.get(key)
                if not isinstance(value, str):
                    continue
                value = value.strip()
                if not value:
                    continue
                if _is_usable_source(value):
                    return value, file_code
            base64_value = data.get("base64")
            if isinstance(base64_value, str) and base64_value.strip():
                value = base64_value.strip()
                return (
                    value if value.startswith("base64://") else "base64://" + value,
                    file_code,
                )
            if file_code and _is_usable_source(file_code):
                return file_code, file_code
        return (None, file_code) if file_code else None

    async def _resolve_media_via_api(self, ref: MediaRef, bot: Any) -> str | None:
        """URL/本地路径都不可用时，让协议端把文件取回来。

        * 图片优先 ``get_image``；
        * 视频以及其他类型尝试 NapCat 的 ``get_file``（接受 ``file_id`` 或
          ``file``），它可能返回本地路径、URL 或 base64。
        """
        if bot is None:
            return None
        call = getattr(bot, "call_action", None)
        if not callable(call):
            return None

        # NapCat 的 get_file 对合并转发里的视频经常拿不到父消息上下文，
        # 先尝试用节点的 message_id 重新 get_msg，通常能拿到刷新后的 url。
        refreshed_file = ""
        if ref.message_id:
            refreshed = await self._refresh_media_from_message(ref, bot)
            if refreshed:
                source, refreshed_file = refreshed
                if source:
                    return source

        # 合并转发视频的 path 是 NapCat 容器里的缓存路径。缓存不存在时，
        # NapCat 的 get_file 会在 downloadRichMedia 上固定等 2 分钟再失败，
        # 这里直接跳过，避免每条视频都卡两分钟。
        if ref.kind == "video" and ref.path and not Path(ref.path).is_file():
            if not (refreshed_file and refreshed_file != ref.file):
                self._log.warning(
                    "视频本地缓存不存在：%s；NapCat 对合并转发视频的 "
                    "get_file 已知会超时，已跳过。请检查 AstrBot 能否访问该目录。",
                    ref.path,
                )
                return None

        attempts: list[tuple[str, dict[str, Any]]] = []
        if ref.kind == "image":
            file_value = ref.file or ref.file_id
            if file_value:
                attempts.append(("get_image", {"file": file_value}))

        # get_file 是 NapCat 的扩展接口；不同版本对 file/file_id 的偏好不同。
        file_values: list[str] = []
        if refreshed_file:
            file_values.append(refreshed_file)
        if ref.file:
            file_values.append(ref.file)
        for file_value in dict.fromkeys(item for item in file_values if item):
            # NapCat 的 get_file 接受 file 或 file_id；合并转发视频的 file
            # 是 FileNapCatOneBotUUID 文件码，直接传 file 即可。
            attempts.append(("get_file", {"file": file_value}))
        if ref.file_id:
            attempts.append(("get_file", {"file_id": ref.file_id}))
        if not attempts:
            return None

        for action, params in attempts:
            timeout = (
                self._settings.file_api_timeout
                if action == "get_file"
                else self._settings.api_timeout
            )
            try:
                result = call(action, **params)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "协议端 %s 兜底失败（%s，超时 %ss）：%r",
                    action,
                    type(exc).__name__,
                    timeout,
                    exc,
                )
                continue
            resolved = _extract_api_file(result)
            if resolved:
                return resolved
            self._log.warning("协议端 %s 没有返回可用文件：%s", action, result)
        return None

    # ------------------------------------------------------------------
    # 通知与基础设施
    # ------------------------------------------------------------------

    async def _maybe_notify(
        self,
        snapshot: _EventSnapshot,
        counts: Counter[str],
        saved: list[SaveOutcome],
    ) -> None:
        if not self._settings.notify:
            return
        if not saved and not self._settings.notify_empty:
            return

        by_kind = Counter(item.kind for item in saved)
        lines = ["📥 合并转发媒体保存完成"]
        lines.append(
            "，".join(
                f"{_kind_label(kind)} {count} 个" for kind, count in by_kind.items()
            )
            or "没有新的媒体"
        )
        extra: list[str] = []
        if counts["duplicate"]:
            extra.append(f"重复 {counts['duplicate']} 个")
        skipped_total = (
            counts["skipped"] + counts["limit_skipped"] + counts["depth_skipped"]
        )
        if skipped_total:
            extra.append(f"跳过 {skipped_total} 个")
        failed_total = (
            counts["failed"] + counts["fetch_failed"] + counts["parse_failed"]
        )
        if failed_total:
            extra.append(f"失败 {failed_total} 个")
        if extra:
            lines.append("，".join(extra))
        if saved:
            relative = saved[0].relative_dir
            lines.append(f"目录：{relative or '根目录'}")

        if not snapshot.umo:
            return
        try:
            await self.context.send_message(
                snapshot.umo, MessageChain([Plain("\n".join(lines))])
            )
        except Exception as exc:  # noqa: BLE001
            self._log.debug("发送保存摘要失败：%s", exc)

    async def _ensure_prepared(self) -> None:
        if self._prepared:
            return
        async with self._prepare_lock:
            if self._prepared:
                return
            try:
                await self._saver.prepare()
                self._prepared = True
            except Exception as exc:  # noqa: BLE001
                self._log.warning("准备媒体目录失败：%s", exc)

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(
                    limit=max(4, self._settings.concurrency * 2)
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(
                        total=self._settings.download_timeout
                    ),
                )
            return self._session

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log.error(
                "处理合并转发任务时出现未捕获异常：%s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _pick_logger(self) -> Any:
        inherited = getattr(self, "logger", None)
        return inherited if inherited is not None else logger

    def _resolve_data_dir(self) -> Path:
        name = getattr(self, "name", "") or PLUGIN_NAME
        try:
            return Path(StarTools.get_data_dir(name))
        except Exception as exc:  # noqa: BLE001
            logger.debug("StarTools.get_data_dir 不可用，改用数据目录：%s", exc)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / name
        except Exception:  # noqa: BLE001
            return Path("data") / "plugin_data" / name

    def _resolve_search_roots(self) -> tuple[Path, ...]:
        """协议端缓存目录，用于只有文件名、没有 URL 的视频。"""
        cwd = Path.cwd()
        candidates: list[Path] = [
            cwd / "cache",
            cwd / "data" / "cache",
            cwd / "data" / "videos",
            cwd / "data" / "temp",
            cwd / "data" / "lagrange",
            cwd / "napcat" / "cache",
            cwd / "NapCat" / "cache",
            cwd / "Lagrange.OneBot",
            cwd / "Lagrange.OneBot" / "cache",
            cwd / "Lagrange.OneBot" / "bin" / "Debug" / "net8.0" / "cache",
            cwd / "Lagrange.OneBot" / "bin" / "Release" / "net8.0" / "cache",
            cwd / "bin" / "Debug" / "net8.0" / "cache",
            cwd / "bin" / "Release" / "net8.0" / "cache",
            Path("/app/.config/QQ"),
            Path("/app/.local/share"),
            Path("/app/data"),
            Path("/app/napcat"),
            Path.home() / ".config" / "QQ",
            Path.home() / ".local" / "share" / "QQ",
        ]
        for raw in self._settings.search_dirs:
            expanded = os.path.expandvars(os.path.expanduser(raw))
            path = Path(expanded)
            if not path.is_absolute():
                path = cwd / path
            candidates.append(path)

        roots: list[Path] = []
        seen: set[Path] = set()
        for path in candidates:
            try:
                resolved = path.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            roots.append(resolved)
        return tuple(roots)

    def _resolve_media_root(self) -> Path:
        custom = self._settings.custom_dir
        if not custom:
            return self._data_dir / "media"
        expanded = os.path.expandvars(os.path.expanduser(custom))
        path = Path(expanded)
        if not path.is_absolute():
            path = self._data_dir / path
        try:
            return path.resolve()
        except OSError:
            return path


def _is_usable_source(value: str) -> bool:
    """判断 get_msg 返回的 url/file 是否能直接交给下载器。"""
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "base64://", "data:", "file://")):
        return True
    try:
        return Path(value).is_file()
    except (OSError, ValueError):
        return False


def _brief(value: str, limit: int = 80) -> str:
    if not value:
        return "无"
    text = str(value)
    if text.startswith("base64://"):
        return f"base64://<{len(text) - 9} chars>"
    if len(text) > limit:
        return text[: limit // 2] + "..." + text[-(limit // 2) :]
    return text


def _extract_api_file(result: Any) -> str | None:
    """从 get_image/get_file 的返回值里提取一个可用来源。

    NapCat 的 ``get_file`` 可能同时返回：
    * ``url``：在线直链，或视频下载失败时的本地 filePath；
    * ``file``：协议端本地路径；
    * ``base64``：开启 ``enableLocalFile2Url`` 后的文件内容。

    优先在线 URL，其次 base64（跨机器最可靠），最后本地路径。
    """
    if isinstance(result, str):
        value = result.strip()
        return value or None
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("data"), dict):
        result = result["data"]

    url = result.get("url")
    url_text = url.strip() if isinstance(url, str) else ""
    if url_text.startswith(("http://", "https://")):
        return url_text

    base64_value = result.get("base64")
    if isinstance(base64_value, str) and base64_value.strip():
        value = base64_value.strip()
        return value if value.startswith("base64://") else "base64://" + value

    for key in ("file", "path"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if url_text:
        return url_text
    return None


def _kind_label(kind: str) -> str:
    return {
        "image": "图片",
        "video": "视频",
        "audio": "语音",
        "file": "文件",
    }.get(kind, "媒体")


def _is_emoji_ref(ref: MediaRef) -> bool:
    if ref.kind != "image":
        return False
    if ref.sub_type in (1, "1", True):
        return True
    if ref.summary and "表情" in ref.summary:
        return True
    for value in (ref.file_name, ref.file):
        if not value:
            continue
        clean = value.split("?")[0].split("#")[0].lower()
        if clean.endswith(".gif"):
            return True
    return False


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _event_scope(event: AstrMessageEvent) -> tuple[str, str]:
    group_id = str(event.get_group_id() or "")
    if group_id:
        return "group", group_id
    return "private", str(event.get_sender_id() or "")


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"
