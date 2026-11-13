from __future__ import annotations

import base64
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.forward_parser import MediaRef  # noqa: E402
from astrbot_plugin_forward_media_saver.media_saver import (  # noqa: E402
    HashStore,
    MediaSaver,
    SaveContext,
)
from astrbot_plugin_forward_media_saver.settings import Settings  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+jX8kAAAAASUVORK5CYII="
)
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64


class MediaServer:
    def __init__(self):
        self.app = web.Application()
        self.app.router.add_get("/img", self.img)
        self.app.router.add_get("/vid", self.vid)
        self.app.router.add_get("/big", self.big)
        self.app.router.add_get("/html", self.html)
        self.app.router.add_get("/redirect", self.redirect)
        self.app.router.add_get("/flaky", self.flaky)
        self.runner: web.AppRunner | None = None
        self.port = 0
        self.flaky_count = 0

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()

    async def img(self, request):
        return web.Response(body=PNG, content_type="image/png")

    async def vid(self, request):
        return web.Response(body=MP4, content_type="video/mp4")

    async def big(self, request):
        return web.Response(body=b"\x00" * (1024 * 1024 + 10), content_type="video/mp4")

    async def html(self, request):
        return web.Response(text="<html>not found</html>", content_type="text/html")

    async def redirect(self, request):
        raise web.HTTPFound("/img")

    async def flaky(self, request):
        self.flaky_count += 1
        if self.flaky_count == 1:
            return web.Response(status=500, text="oops")
        return web.Response(body=PNG, content_type="image/png")


class MediaSaverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = MediaServer()
        await self.server.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "media"
        self.log = logging.getLogger("test-media-saver")
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.stop()
        self.tmp.cleanup()

    def make_saver(self, **overrides):
        search_roots = overrides.pop("search_roots", ())
        options = dict(
            save_images=True,
            save_videos=True,
            max_file_mb=5,
            min_free_mb=0,
            download_timeout=10,
            retries=0,
            dedupe=False,
            folder_layout="date_scope",
        )
        options.update(overrides)
        settings = Settings(**options)
        store = (
            HashStore(Path(self.tmp.name) / "seen.txt", self.log)
            if settings.dedupe
            else None
        )

        async def provider():
            return self.session

        return MediaSaver(
            root=self.root,
            settings=settings,
            log=self.log,
            session_provider=provider,
            hash_store=store,
            search_roots=search_roots,
        )

    def url(self, path):
        return f"http://127.0.0.1:{self.server.port}{path}"

    def ctx(self):
        return SaveContext(
            scope="group", scope_id="20002", received_at=int(time.time())
        )

    async def test_save_image_from_url(self):
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(
            kind="image",
            url=self.url("/img"),
            sender_id="10001",
            timestamp=int(time.time()),
        )
        outcome = await saver.save(ref, self.ctx(), sequence=1)

        self.assertEqual(outcome.status, "saved")
        self.assertTrue(outcome.path.exists())
        self.assertEqual(outcome.path.suffix, ".png")
        self.assertEqual(outcome.path.read_bytes(), PNG)
        self.assertIn("group_20002", str(outcome.path.parent))
        self.assertGreater(outcome.size, 0)

    async def test_save_video_from_url(self):
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(kind="video", url=self.url("/vid"), sender_id="10001")
        outcome = await saver.save(ref, self.ctx(), sequence=1)

        self.assertEqual(outcome.status, "saved")
        self.assertEqual(outcome.path.suffix, ".mp4")

    async def test_redirect_is_followed(self):
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(kind="image", url=self.url("/redirect"), sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")

    async def test_too_large_is_skipped(self):
        saver = self.make_saver(max_file_mb=1)
        await saver.prepare()
        ref = MediaRef(kind="video", url=self.url("/big"), sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "skipped")

    async def test_html_is_rejected(self):
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(kind="image", url=self.url("/html"), sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "failed")

    async def test_retry(self):
        saver = self.make_saver(retries=1)
        await saver.prepare()
        ref = MediaRef(kind="image", url=self.url("/flaky"), sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")
        self.assertEqual(self.server.flaky_count, 2)

    async def test_url_failure_falls_back_to_local_path(self):
        source = Path(self.tmp.name) / "fallback.png"
        source.write_bytes(PNG)
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(
            kind="image",
            url=self.url("/missing"),
            path=str(source),
            sender_id="1",
        )
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")
        self.assertEqual(outcome.path.read_bytes(), PNG)

    async def test_non_media_url_falls_back_to_local_path(self):
        source = Path(self.tmp.name) / "fallback.png"
        source.write_bytes(PNG)
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(
            kind="image",
            url=self.url("/html"),
            path=str(source),
            sender_id="1",
        )
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")

    async def test_data_uri_source(self):
        saver = self.make_saver()
        await saver.prepare()
        data_uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
        ref = MediaRef(kind="image", file=data_uri, sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")

    async def test_invalid_base64_returns_failed(self):
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(kind="image", file="base64://!!!!", sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "failed")

    async def test_base64_source(self):
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(
            kind="image",
            file="base64://" + base64.b64encode(PNG).decode(),
            sender_id="1",
        )
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")
        self.assertEqual(outcome.path.read_bytes(), PNG)

    async def test_local_path_source(self):
        source = Path(self.tmp.name) / "local.png"
        source.write_bytes(PNG)
        saver = self.make_saver()
        await saver.prepare()
        ref = MediaRef(kind="image", path=str(source), sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")
        self.assertEqual(outcome.path.read_bytes(), PNG)

    async def test_resolver_fallback(self):
        source = Path(self.tmp.name) / "fallback.png"
        source.write_bytes(PNG)

        async def resolver(ref, bot):
            return str(source)

        settings = Settings(max_file_mb=5, min_free_mb=0, retries=0)
        saver = MediaSaver(
            root=self.root,
            settings=settings,
            log=self.log,
            session_provider=self._provider,
            resolver=resolver,
        )
        await saver.prepare()
        ref = MediaRef(kind="image", file="not-a-real-file.image", sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")

    async def _provider(self):
        return self.session

    async def test_dedupe_within_store(self):
        saver = self.make_saver(dedupe=True)
        await saver.prepare()
        ref = MediaRef(kind="image", url=self.url("/img"), sender_id="1")

        first = await saver.save(ref, self.ctx(), sequence=1)
        second = await saver.save(ref, self.ctx(), sequence=2)

        self.assertEqual(first.status, "saved")
        self.assertEqual(second.status, "duplicate")
        files = [
            p for p in self.root.rglob("*") if p.is_file() and ".tmp" not in p.parts
        ]
        self.assertEqual(len(files), 1)

    async def test_dedupe_survives_restart(self):
        saver = self.make_saver(dedupe=True)
        await saver.prepare()
        ref = MediaRef(kind="image", url=self.url("/img"), sender_id="1")
        self.assertEqual(
            (await saver.save(ref, self.ctx(), sequence=1)).status, "saved"
        )

        # 新实例读取同一个索引文件，应识别为重复。
        saver2 = self.make_saver(dedupe=True)
        await saver2.prepare()
        outcome = await saver2.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "duplicate")

    async def test_local_cache_filename(self):
        cache = Path(self.tmp.name) / "protocol_cache"
        cache.mkdir()
        source = cache / "542f153aab5b2f4f6ffeaf42c543e5f2.mp4"
        source.write_bytes(MP4)

        saver = self.make_saver(search_roots=(cache,))
        await saver.prepare()
        ref = MediaRef(
            kind="video",
            file=source.name,
            size_hint=len(MP4),
            sender_id="1",
        )
        outcome = await saver.save(ref, self.ctx(), sequence=1)

        self.assertEqual(outcome.status, "saved")
        self.assertEqual(outcome.path.read_bytes(), MP4)

    async def test_local_cache_wrong_size_is_rejected(self):
        cache = Path(self.tmp.name) / "protocol_cache"
        cache.mkdir()
        source = cache / "abc.mp4"
        source.write_bytes(MP4)

        saver = self.make_saver(search_roots=(cache,))
        await saver.prepare()
        ref = MediaRef(
            kind="video",
            file=source.name,
            size_hint=len(MP4) + 100000,
            sender_id="1",
        )
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "failed")

    async def test_audio_source(self):
        source = Path(self.tmp.name) / "voice.amr"
        source.write_bytes(b"#!AMR\n" + b"\x00" * 32)
        saver = self.make_saver(save_audio=True)
        await saver.prepare()
        ref = MediaRef(kind="audio", path=str(source), sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")
        self.assertEqual(outcome.path.suffix, ".amr")

    async def test_file_source(self):
        source = Path(self.tmp.name) / "report.pdf"
        source.write_bytes(b"%PDF-1.4\n" + b"x" * 32)
        saver = self.make_saver(save_files=True)
        await saver.prepare()
        ref = MediaRef(
            kind="file", path=str(source), file_name="report.pdf", sender_id="1"
        )
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")
        self.assertEqual(outcome.path.suffix, ".pdf")

    @staticmethod
    def make_mp4(seconds: float, timescale: int = 1000) -> bytes:
        payload = (
            b"\x00\x00\x00\x00"
            + b"\x00" * 8
            + timescale.to_bytes(4, "big")
            + int(seconds * timescale).to_bytes(4, "big")
            + b"\x00" * 80
        )

        def box(box_type: bytes, data: bytes) -> bytes:
            return (len(data) + 8).to_bytes(4, "big") + box_type + data

        return box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2") + box(
            b"moov", box(b"mvhd", payload)
        )

    async def test_short_video_is_skipped(self):
        source = Path(self.tmp.name) / "short.mp4"
        source.write_bytes(self.make_mp4(2.0))
        saver = self.make_saver(min_video_seconds=3)
        await saver.prepare()
        ref = MediaRef(kind="video", path=str(source), sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("视频时长", outcome.reason)
        self.assertEqual(self.files(saver), [])

    async def test_long_video_is_saved(self):
        source = Path(self.tmp.name) / "long.mp4"
        source.write_bytes(self.make_mp4(5.0))
        saver = self.make_saver(min_video_seconds=3)
        await saver.prepare()
        ref = MediaRef(kind="video", path=str(source), sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "saved")
        self.assertAlmostEqual(outcome.duration, 5.0, places=1)

    async def test_unknown_duration_policy(self):
        source = Path(self.tmp.name) / "unknown.mp4"
        source.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40)

        strict = self.make_saver(min_video_seconds=3, skip_unknown_video_duration=True)
        await strict.prepare()
        ref = MediaRef(kind="video", path=str(source), sender_id="1")
        outcome = await strict.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("无法识别视频时长", outcome.reason)

        loose = self.make_saver(min_video_seconds=3, skip_unknown_video_duration=False)
        await loose.prepare()
        outcome = await loose.save(ref, self.ctx(), sequence=2)
        self.assertEqual(outcome.status, "saved")

    async def test_duration_from_payload_skips_before_download(self):
        saver = self.make_saver(min_video_seconds=3)
        await saver.prepare()
        ref = MediaRef(kind="video", duration=2.0, sender_id="1")
        outcome = await saver.save(ref, self.ctx(), sequence=1)
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("视频时长", outcome.reason)

    def files(self, saver):
        if not saver.root.exists():
            return []
        return [
            path
            for path in saver.root.rglob("*")
            if path.is_file() and ".tmp" not in path.parts
        ]

    async def test_folder_layouts(self):
        for layout, checker in (
            ("date", lambda p: p.parent.parent == self.root),
            ("scope", lambda p: p.parent.name == "group_20002"),
            ("flat", lambda p: p.parent == self.root),
        ):
            saver = self.make_saver(folder_layout=layout)
            await saver.prepare()
            ref = MediaRef(kind="image", url=self.url("/img"), sender_id="1")
            outcome = await saver.save(ref, self.ctx(), sequence=1)
            self.assertEqual(outcome.status, "saved")
            self.assertTrue(checker(outcome.path), layout)


if __name__ == "__main__":
    unittest.main()
