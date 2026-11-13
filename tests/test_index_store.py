from __future__ import annotations

import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.index_store import (  # noqa: E402
    IndexStore,
)


class IndexStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "index.db"
        self.store = IndexStore(self.path, logging.getLogger("test-index"))
        await self.store.initialize()

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    async def test_record_and_stats(self):
        await self.store.record_media(
            {
                "saved_at": int(time.time()),
                "kind": "image",
                "scope": "group",
                "scope_id": "1",
                "scope_name": "测试群",
                "sender_id": "2",
                "path": "/tmp/a.jpg",
                "size": 123,
                "sha256": "a" * 64,
            },
            store_hash=True,
        )
        await self.store.record_media(
            {
                "saved_at": int(time.time()),
                "kind": "video",
                "scope": "group",
                "scope_id": "1",
                "path": "/tmp/b.mp4",
                "size": 456,
                "sha256": "b" * 64,
            },
            store_hash=True,
        )
        stats = await self.store.stats()
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["size"], 579)
        self.assertEqual(len(await self.store.recent(10)), 2)
        self.assertTrue(await self.store.has_hash("a" * 64))

    async def test_delete_media_prunes_hash(self):
        await self.store.record_media(
            {
                "path": "/tmp/a.jpg",
                "size": 1,
                "sha256": "c" * 64,
                "saved_at": int(time.time()),
                "kind": "image",
                "scope": "group",
                "scope_id": "1",
            },
            store_hash=True,
        )
        rows = await self.store.recent(10)
        self.assertEqual(len(rows), 1)
        await self.store.delete_media([rows[0]["id"]])
        self.assertFalse(await self.store.has_hash("c" * 64))
        self.assertEqual(await self.store.recent(10), [])

    async def test_jobs_lifecycle(self):
        created = await self.store.create_job(
            "job-1", "group", "1", [{"forward_id": "f1"}], {"sender_id": "2"}
        )
        self.assertTrue(created)
        self.assertFalse(
            await self.store.create_job(
                "job-1", "group", "1", [{"forward_id": "f1"}], {}
            )
        )
        jobs = await self.store.retryable_jobs("group", "1", max_attempts=3)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["targets"], [{"forward_id": "f1"}])

        await self.store.mark_job("job-1", "running", inc_attempts=True)
        await self.store.mark_job("job-1", "failed", "boom")
        jobs = await self.store.retryable_jobs("group", "1", max_attempts=3)
        self.assertEqual(len(jobs), 1)

        await self.store.mark_job("job-1", "running", inc_attempts=True)
        await self.store.mark_job("job-1", "running", inc_attempts=True)
        await self.store.mark_job("job-1", "failed", "again")
        jobs = await self.store.retryable_jobs("group", "1", max_attempts=3)
        self.assertEqual(jobs, [])

    async def test_reset_running(self):
        await self.store.create_job("job-2", "group", "1", [], {})
        await self.store.mark_job("job-2", "running")
        self.assertEqual(await self.store.reset_running_jobs(), 1)
        jobs = await self.store.retryable_jobs("group", "1", max_attempts=3)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "pending")

    async def test_old_media(self):
        old = int(time.time() - 10 * 86400)
        await self.store.record_media(
            {
                "path": "/tmp/old.jpg",
                "size": 10,
                "sha256": "d" * 64,
                "saved_at": old,
                "kind": "image",
                "scope": "group",
                "scope_id": "1",
            },
            store_hash=True,
        )
        rows = await self.store.old_media(int(time.time() - 5 * 86400))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
