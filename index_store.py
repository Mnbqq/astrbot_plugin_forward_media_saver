"""SQLite 媒体索引、去重表和任务表。

使用标准库 sqlite3，所有数据库操作都在线程中执行并由一个 asyncio.Lock
串行化，避免插件把事件循环阻塞在磁盘 I/O 上。

表结构：
* media   已保存媒体清单（统计、最近记录、清理）
* hashes  内容去重索引（替代原来的 seen_hashes.txt）
* jobs    转发的处理任务（重载后可通过 /fms retry 继续）
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class IndexStore:
    """线程安全的异步 SQLite 封装。"""

    def __init__(self, path: Path, log: Any) -> None:
        self._path = path
        self._log = log
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._init)

    def _init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                saved_at INTEGER NOT NULL,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '',
                scope_name TEXT NOT NULL DEFAULT '',
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                forward_id TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                orig_name TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_media_saved_at ON media(saved_at);
            CREATE INDEX IF NOT EXISTS idx_media_scope ON media(scope, scope_id);

            CREATE TABLE IF NOT EXISTS hashes (
                sha256 TEXT PRIMARY KEY,
                saved_at INTEGER NOT NULL,
                path TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '',
                targets_json TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_scope ON jobs(scope, scope_id);
            """
        )
        conn.commit()
        self._conn = conn
        self._migrate_seen_hashes()

    def _migrate_seen_hashes(self) -> None:
        """把旧版 seen_hashes.txt 导入 hashes 表，保留旧文件。"""
        if self._conn is None:
            return
        count = self._conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
        if count:
            return
        old_path = self._path.parent / "seen_hashes.txt"
        if not old_path.is_file():
            return
        try:
            lines = old_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return
        rows = [(line.strip(), int(time.time()), "") for line in lines if line.strip()]
        if not rows:
            return
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO hashes(sha256, saved_at, path) VALUES (?, ?, ?)",
                rows,
            )
            self._conn.commit()
            self._log.info("已从 seen_hashes.txt 迁移 %s 条去重记录", len(rows))
        except sqlite3.Error as exc:
            self._log.warning("迁移 seen_hashes.txt 失败：%s", exc)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close)

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    async def _run(self, fn, *args):
        async with self._lock:
            if self._conn is None:
                raise RuntimeError("index store is not initialized")
            return await asyncio.to_thread(fn, *args)

    # ---- 去重 ---------------------------------------------------------

    async def has_hash(self, sha256: str) -> bool:
        return await self._run(self._has_hash, sha256)

    def _has_hash(self, sha256: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM hashes WHERE sha256 = ? LIMIT 1", (sha256,)
        ).fetchone()
        return row is not None

    async def add_hash(self, sha256: str, path: str = "") -> None:
        await self._run(self._add_hash, sha256, path)

    def _add_hash(self, sha256: str, path: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO hashes(sha256, saved_at, path) VALUES (?, ?, ?)",
            (sha256, int(time.time()), path),
        )
        self._conn.commit()

    # ---- 媒体清单 -----------------------------------------------------

    async def record_media(self, record: dict[str, Any], store_hash: bool) -> int:
        return await self._run(self._record_media, record, store_hash)

    def _record_media(self, record: dict[str, Any], store_hash: bool) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO media(
                saved_at, kind, scope, scope_id, scope_name, sender_id,
                sender_name, message_id, forward_id, path, size, sha256, orig_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(record.get("saved_at") or time.time()),
                str(record.get("kind", "")),
                str(record.get("scope", "")),
                str(record.get("scope_id", "")),
                str(record.get("scope_name", "")),
                str(record.get("sender_id", "")),
                str(record.get("sender_name", "")),
                str(record.get("message_id", "")),
                str(record.get("forward_id", "")),
                str(record.get("path", "")),
                int(record.get("size") or 0),
                str(record.get("sha256", "")),
                str(record.get("orig_name", "")),
            ),
        )
        if store_hash and record.get("sha256"):
            self._conn.execute(
                "INSERT OR IGNORE INTO hashes(sha256, saved_at, path) VALUES (?, ?, ?)",
                (str(record["sha256"]), int(time.time()), str(record.get("path", ""))),
            )
        self._conn.commit()
        return int(cursor.lastrowid)

    async def stats(self, days: int = 0) -> dict[str, Any]:
        return await self._run(self._stats, days)

    def _stats(self, days: int) -> dict[str, Any]:
        where = ""
        params: tuple = ()
        if days > 0:
            where = "WHERE saved_at >= ?"
            params = (int(time.time() - days * 86400),)
        totals = self._conn.execute(
            f"SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS size FROM media {where}",
            params,
        ).fetchone()
        kinds = [
            {"kind": row["kind"], "count": row["count"], "size": row["size"]}
            for row in self._conn.execute(
                f"SELECT kind, COUNT(*) AS count, COALESCE(SUM(size), 0) AS size "
                f"FROM media {where} GROUP BY kind ORDER BY count DESC",
                params,
            )
        ]
        scopes = [
            {
                "scope": row["scope"],
                "scope_id": row["scope_id"],
                "scope_name": row["scope_name"],
                "count": row["count"],
                "size": row["size"],
            }
            for row in self._conn.execute(
                f"SELECT scope, scope_id, scope_name, COUNT(*) AS count, "
                f"COALESCE(SUM(size), 0) AS size FROM media {where} "
                f"GROUP BY scope, scope_id, scope_name ORDER BY size DESC LIMIT 20",
                params,
            )
        ]
        return {
            "days": days,
            "count": int(totals["count"]),
            "size": int(totals["size"]),
            "kinds": kinds,
            "scopes": scopes,
        }

    async def recent(
        self, limit: int = 20, scope: str = "", scope_id: str = ""
    ) -> list[dict[str, Any]]:
        return await self._run(self._recent, limit, scope, scope_id)

    def _recent(self, limit: int, scope: str, scope_id: str) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, saved_at, kind, scope, scope_id, scope_name, sender_id, "
            "sender_name, path, size, sha256, orig_name FROM media"
        )
        params: list[Any] = []
        if scope and scope_id:
            sql += " WHERE scope = ? AND scope_id = ?"
            params.extend([scope, scope_id])
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(100000, int(limit))))
        return [dict(row) for row in self._conn.execute(sql, params)]

    async def old_media(self, cutoff: int) -> list[dict[str, Any]]:
        return await self._run(self._old_media, cutoff)

    def _old_media(self, cutoff: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT id, path, sha256, size FROM media WHERE saved_at < ?",
                (cutoff,),
            )
        ]

    async def all_media_oldest(self) -> list[dict[str, Any]]:
        return await self._run(self._all_media_oldest)

    def _all_media_oldest(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT id, path, sha256, size FROM media ORDER BY saved_at ASC, id ASC"
            )
        ]

    async def delete_media(self, ids: list[int]) -> int:
        return await self._run(self._delete_media, ids)

    def _delete_media(self, ids: list[int]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        shas = [
            row["sha256"]
            for row in self._conn.execute(
                f"SELECT sha256 FROM media WHERE id IN ({placeholders})", ids
            )
            if row["sha256"]
        ]
        self._conn.execute(f"DELETE FROM media WHERE id IN ({placeholders})", ids)
        # 没有其他媒体再引用这个 hash 时，才删除去重记录。
        for sha in shas:
            remaining = self._conn.execute(
                "SELECT 1 FROM media WHERE sha256 = ? LIMIT 1", (sha,)
            ).fetchone()
            if not remaining:
                self._conn.execute("DELETE FROM hashes WHERE sha256 = ?", (sha,))
        self._conn.commit()
        return len(ids)

    # ---- 任务表 -------------------------------------------------------

    async def create_job(
        self,
        job_id: str,
        scope: str,
        scope_id: str,
        targets: list[dict[str, Any]],
        meta: dict[str, Any],
    ) -> bool:
        return await self._run(self._create_job, job_id, scope, scope_id, targets, meta)

    def _create_job(
        self,
        job_id: str,
        scope: str,
        scope_id: str,
        targets: list[dict[str, Any]],
        meta: dict[str, Any],
    ) -> bool:
        now = int(time.time())
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO jobs(
                job_id, scope, scope_id, targets_json, meta_json,
                status, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                job_id,
                scope,
                scope_id,
                json.dumps(targets, ensure_ascii=False, default=str),
                json.dumps(meta, ensure_ascii=False, default=str),
                now,
                now,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    async def mark_job(
        self, job_id: str, status: str, error: str = "", inc_attempts: bool = False
    ) -> None:
        await self._run(self._mark_job, job_id, status, error, inc_attempts)

    def _mark_job(
        self, job_id: str, status: str, error: str, inc_attempts: bool
    ) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, last_error = ?, updated_at = ?, "
            "attempts = attempts + ? WHERE job_id = ?",
            (status, error[:2000], int(time.time()), 1 if inc_attempts else 0, job_id),
        )
        self._conn.commit()

    async def reset_running_jobs(self) -> int:
        return await self._run(self._reset_running_jobs)

    def _reset_running_jobs(self) -> int:
        cursor = self._conn.execute(
            "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'running'",
            (int(time.time()),),
        )
        self._conn.commit()
        return cursor.rowcount

    async def retryable_jobs(
        self,
        scope: str,
        scope_id: str,
        limit: int = 10,
        max_attempts: int = 3,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._retryable_jobs, scope, scope_id, limit, max_attempts
        )

    def _retryable_jobs(
        self, scope: str, scope_id: str, limit: int, max_attempts: int
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT job_id, targets_json, meta_json, status, attempts, last_error
            FROM jobs
            WHERE scope = ? AND scope_id = ?
              AND status IN ('pending', 'failed')
              AND attempts < ?
            ORDER BY created_at ASC LIMIT ?
            """,
            (
                scope,
                scope_id,
                max(1, int(max_attempts)),
                max(1, min(50, limit)),
            ),
        )
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["targets"] = json.loads(item.pop("targets_json"))
            except ValueError:
                item["targets"] = []
            try:
                item["meta"] = json.loads(item.pop("meta_json"))
            except ValueError:
                item["meta"] = {}
            result.append(item)
        return result

    async def job_counts(self) -> dict[str, int]:
        return await self._run(self._job_counts)

    def _job_counts(self) -> dict[str, int]:
        return {
            row["status"]: row["count"]
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            )
        }

    async def prune_jobs(self, days: int) -> int:
        return await self._run(self._prune_jobs, days)

    def _prune_jobs(self, days: int) -> int:
        cutoff = int(time.time() - max(0, days) * 86400)
        cursor = self._conn.execute(
            "DELETE FROM jobs WHERE status = 'done' AND updated_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cursor.rowcount


class IndexHashStore:
    """适配 MediaSaver 的 contains/add 接口。"""

    def __init__(self, store: IndexStore) -> None:
        self._store = store

    async def contains(self, sha256: str) -> bool:
        return await self._store.has_hash(sha256)

    async def add(self, sha256: str) -> None:
        await self._store.add_hash(sha256)
