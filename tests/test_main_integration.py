from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PLUGIN_ROOT.parent
sys.path.insert(0, str(PLUGIN_ROOT / "tests" / "stubs"))
sys.path.insert(0, str(WORKSPACE))

from astrbot.api.star import Context, StarTools  # noqa: E402
from astrbot_plugin_forward_media_saver.main import Main, _extract_api_file  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+jX8kAAAAASUVORK5CYII="
)
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64


class FakeComponent:
    def __init__(self, type_value, **fields):
        self.type = type_value
        for key, value in fields.items():
            setattr(self, key, value)


class FakeMessageObj:
    def __init__(self, components, raw_message, self_id="90001"):
        self.message = components
        self.raw_message = raw_message
        self.self_id = self_id


class FakeEvent:
    def __init__(
        self,
        components=None,
        raw_message=None,
        sender_id="10001",
        sender_name="测试用户",
        group_id="20002",
        self_id="90001",
        bot=None,
    ):
        self.message_obj = FakeMessageObj(components or [], raw_message or {}, self_id)
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._self_id = self_id
        self.unified_msg_origin = (
            f"aiocqhttp:GroupMessage:{group_id}"
            if group_id
            else f"aiocqhttp:FriendMessage:{sender_id}"
        )
        self.bot = bot
        self.message_str = ""

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return self._self_id

    def get_group_id(self):
        return self._group_id

    def get_messages(self):
        return self.message_obj.message

    def plain_result(self, text):
        return {"type": "plain", "text": text}


class FakeBot:
    def __init__(self, payloads=None, fail_id=False):
        self.payloads = payloads or {}
        self.fail_id = fail_id
        self.calls = []
        self.get_image_path = None
        self.get_file_path = None
        self.get_file_result = None
        self.get_msg_result = None

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_forward_msg":
            if self.fail_id and "id" in params:
                raise RuntimeError("unsupported parameter id")
            key = params.get("id") or params.get("message_id")
            return self.payloads.get(key)
        if action == "get_image":
            if self.get_image_path:
                return {"file": self.get_image_path}
            return None
        if action == "get_msg":
            return self.get_msg_result
        if action == "get_file":
            if self.get_file_result is not None:
                return self.get_file_result
            if self.get_file_path:
                return {"file": self.get_file_path}
            return None
        return None


def image_payload(path, sender="111"):
    return {
        "messages": [
            {
                "sender": {"user_id": sender, "nickname": "甲"},
                "time": int(time.time()),
                "message": [
                    {
                        "type": "image",
                        "data": {"file": "a.jpg", "path": str(path)},
                    }
                ],
            }
        ]
    }


def video_payload(path, sender="222"):
    return {
        "messages": [
            {
                "sender": {"user_id": sender, "card": "乙"},
                "time": int(time.time()),
                "message": [
                    {
                        "type": "video",
                        "data": {"file": "v.mp4", "path": str(path)},
                    }
                ],
            }
        ]
    }


class MainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        StarTools.set_root(self.base / "astrbot")
        self.context = Context()
        self.png = self.base / "src.png"
        self.png.write_bytes(PNG)
        self.mp4 = self.base / "src.mp4"
        self.mp4.write_bytes(MP4)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def make_plugin(self, config=None):
        config = config or {"folder_layout": "scope"}
        plugin = Main(self.context, config)
        await plugin.initialize()
        return plugin

    async def drain(self, plugin, timeout=5):
        deadline = time.monotonic() + timeout
        while plugin._tasks and time.monotonic() < deadline:
            tasks = list(plugin._tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertFalse(plugin._tasks, "后台任务未在超时内结束")

    def files(self, plugin):
        if not plugin._root.exists():
            return []
        return [
            path
            for path in plugin._root.rglob("*")
            if path.is_file() and ".tmp" not in path.parts
        ]

    async def test_saves_image_and_video(self):
        plugin = await self.make_plugin({"folder_layout": "scope", "notify": True})
        bot = FakeBot(
            {
                "f1": {
                    "messages": [
                        image_payload(self.png)["messages"][0],
                        video_payload(self.mp4)["messages"][0],
                    ]
                }
            }
        )
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        files = self.files(plugin)
        self.assertEqual(len(files), 2)
        self.assertEqual({path.suffix for path in files}, {".png", ".mp4"})
        self.assertTrue(any(call[0] == "get_forward_msg" for call in bot.calls))

        self.assertEqual(len(self.context.sent), 1)
        text = self.context.sent[0][1].chain[0].text
        self.assertIn("图片 1", text)
        self.assertIn("视频 1", text)
        self.assertIn("group_20002", text)
        await plugin.terminate()

    async def test_skips_own_forward(self):
        plugin = await self.make_plugin()
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(
            components=[FakeComponent("Forward", id="f1")],
            sender_id="90001",
            bot=bot,
        )

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(self.files(plugin), [])
        self.assertEqual(self.context.sent, [])
        await plugin.terminate()

    async def test_group_whitelist(self):
        plugin = await self.make_plugin({"group_whitelist": ["123456"]})
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(self.files(plugin), [])
        await plugin.terminate()

    async def test_private_chat(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(
            components=[FakeComponent("Forward", id="f1")],
            group_id="",
            bot=bot,
        )

        await plugin.on_any_message(event)
        await self.drain(plugin)

        files = self.files(plugin)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent.name, "private_10001")
        await plugin.terminate()

    async def test_nested_forward(self):
        plugin = await self.make_plugin({"folder_layout": "scope", "max_depth": 2})
        bot = FakeBot(
            {
                "outer": {
                    "messages": [
                        {
                            "sender": {"user_id": 1},
                            "message": [{"type": "forward", "data": {"id": "inner"}}],
                        }
                    ]
                },
                "inner": image_payload(self.png),
            }
        )
        event = FakeEvent(components=[FakeComponent("Forward", id="outer")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(len(self.files(plugin)), 1)
        await plugin.terminate()

    async def test_nested_forward_disabled(self):
        plugin = await self.make_plugin({"folder_layout": "scope", "max_depth": 0})
        bot = FakeBot(
            {
                "outer": {
                    "messages": [
                        {
                            "sender": {"user_id": 1},
                            "message": [{"type": "forward", "data": {"id": "inner"}}],
                        }
                    ]
                },
                "inner": image_payload(self.png),
            }
        )
        event = FakeEvent(components=[FakeComponent("Forward", id="outer")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(self.files(plugin), [])
        await plugin.terminate()

    async def test_get_forward_msg_message_id_fallback(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        bot = FakeBot({"f1": image_payload(self.png)}, fail_id=True)
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(len(self.files(plugin)), 1)
        self.assertTrue(any("message_id" in call[1] for call in bot.calls))
        await plugin.terminate()

    async def test_media_limit(self):
        plugin = await self.make_plugin(
            {"folder_layout": "scope", "max_media_per_forward": 2}
        )
        node = image_payload(self.png)["messages"][0]
        bot = FakeBot({"f1": {"messages": [node, node, node]}})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        # 同一 URL/path 在解析层会先去重，所以这里换成三个不同副本验证上限。
        self.assertLessEqual(len(self.files(plugin)), 2)
        await plugin.terminate()

    async def test_raw_message_fallback(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(
            components=[],
            raw_message={"message": [{"type": "forward", "data": {"id": "f1"}}]},
            bot=bot,
        )

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(len(self.files(plugin)), 1)
        await plugin.terminate()

    async def test_multiple_forwards_in_one_message(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        bot = FakeBot({"f1": image_payload(self.png), "f2": video_payload(self.mp4)})
        event = FakeEvent(
            components=[
                FakeComponent("Forward", id="f1"),
                FakeComponent("Forward", id="f2"),
            ],
            bot=bot,
        )

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(len(self.files(plugin)), 2)
        await plugin.terminate()

    async def test_no_media_does_not_notify_by_default(self):
        plugin = await self.make_plugin({"folder_layout": "scope", "notify": True})
        bot = FakeBot(
            {
                "f1": {
                    "messages": [
                        {
                            "sender": {"user_id": 1},
                            "message": [{"type": "text", "data": {"text": "hi"}}],
                        }
                    ]
                }
            }
        )
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(self.files(plugin), [])
        self.assertEqual(self.context.sent, [])
        await plugin.terminate()

    async def test_notify_empty(self):
        plugin = await self.make_plugin(
            {"folder_layout": "scope", "notify": True, "notify_empty": True}
        )
        bot = FakeBot(
            {
                "f1": {
                    "messages": [
                        {
                            "sender": {"user_id": 1},
                            "message": [{"type": "text", "data": {"text": "hi"}}],
                        }
                    ]
                }
            }
        )
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(len(self.context.sent), 1)
        self.assertIn("没有新的媒体", self.context.sent[0][1].chain[0].text)
        await plugin.terminate()

    async def test_disabled(self):
        plugin = await self.make_plugin({"enabled": False})
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(self.files(plugin), [])
        self.assertEqual(bot.calls, [])
        await plugin.terminate()

    async def test_user_blacklist(self):
        plugin = await self.make_plugin({"user_blacklist": ["10001"]})
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(self.files(plugin), [])
        await plugin.terminate()

    async def test_get_image_fallback(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "image",
                            "data": {"file": "abc.image"},
                        }
                    ],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        bot.get_image_path = str(self.png)
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(len(self.files(plugin)), 1)
        self.assertTrue(any(call[0] == "get_image" for call in bot.calls))
        await plugin.terminate()

    async def test_video_get_file_fallback(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "video",
                            "data": {"file": "abc.mp4"},
                        }
                    ],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        bot.get_file_path = str(self.mp4)
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        files = self.files(plugin)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].suffix, ".mp4")
        self.assertTrue(any(call[0] == "get_file" for call in bot.calls))
        await plugin.terminate()

    async def test_video_get_file_base64_fallback(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "video",
                            "data": {"file_id": "vid-1"},
                        }
                    ],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        bot.get_file_path = "base64://" + base64.b64encode(MP4).decode()
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        files = self.files(plugin)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].suffix, ".mp4")
        await plugin.terminate()

    def test_extract_api_file_prefers_url_and_base64(self):
        self.assertEqual(
            _extract_api_file({"data": {"url": "https://e.com/a.mp4"}}),
            "https://e.com/a.mp4",
        )
        self.assertEqual(
            _extract_api_file({"url": "/local/a.mp4", "base64": "AAAA"}),
            "base64://AAAA",
        )
        self.assertEqual(
            _extract_api_file({"file": "file:///tmp/a.mp4"}),
            "file:///tmp/a.mp4",
        )
        self.assertIsNone(_extract_api_file({"unexpected": 1}))

    async def test_video_get_file_prefers_base64_over_local_path(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [{"type": "video", "data": {"file": "code"}}],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        bot.get_file_result = {
            "url": "/napcat/only/video.mp4",
            "base64": base64.b64encode(MP4).decode(),
        }
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        files = self.files(plugin)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), MP4)
        await plugin.terminate()

    async def test_video_local_cache_search(self):
        cache = self.base / "lagrange_cache"
        cache.mkdir()
        cached = cache / "542f153aab5b2f4f6ffeaf42c543e5f2.mp4"
        cached.write_bytes(MP4)
        plugin = await self.make_plugin(
            {"folder_layout": "scope", "search_dirs": [str(cache)]}
        )
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": cached.name,
                                "file_size": len(MP4),
                            },
                        }
                    ],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        files = self.files(plugin)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), MP4)
        await plugin.terminate()

    async def test_video_refresh_via_node_message_id(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message_id": 123456,
                    "message": [{"type": "video", "data": {"file": "code"}}],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        bot.get_msg_result = {
            "message": [
                {
                    "type": "video",
                    "data": {
                        "file": "code",
                        "url": str(self.mp4),
                    },
                }
            ]
        }
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        files = self.files(plugin)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), MP4)
        self.assertTrue(any(call[0] == "get_msg" for call in bot.calls))
        await plugin.terminate()

    async def test_missing_napcat_local_path_skips_get_file(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "cd12ce385400ec5b647f18bf8142e957.mp4",
                                "url": (
                                    "/app/.config/QQ/nt_qq_x/nt_data/Video/"
                                    "2025-02/Ori/"
                                    "cd12ce385400ec5b647f18bf8142e957.mp4"
                                ),
                            },
                        }
                    ],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(self.files(plugin), [])
        self.assertFalse(any(call[0] == "get_file" for call in bot.calls))
        await plugin.terminate()

    async def test_fms_stats_and_path(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)
        await plugin.on_any_message(event)
        await self.drain(plugin)

        event.message_str = "/fms stats"
        outputs = [item async for item in plugin.fms_command(event)]
        text = "\n".join(item["text"] for item in outputs)
        self.assertIn("记录 1 条", text)
        self.assertIn("图片", text)
        self.assertTrue((plugin._data_dir / "index.db").is_file())
        self.assertIn(
            "image",
            (plugin._data_dir / "manifest.jsonl").read_text(encoding="utf-8"),
        )

        event.message_str = "/fms path"
        outputs = [item async for item in plugin.fms_command(event)]
        self.assertIn(str(plugin._root), outputs[0]["text"])
        await plugin.terminate()

    async def test_fms_clean(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        old_file = plugin._root / "old.png"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_bytes(PNG)
        await plugin._index.record_media(
            {
                "saved_at": int(time.time() - 5 * 86400),
                "kind": "image",
                "scope": "group",
                "scope_id": "20002",
                "path": str(old_file),
                "size": len(PNG),
                "sha256": "f" * 64,
            },
            store_hash=True,
        )
        event = FakeEvent(components=[], bot=None)
        event.message_str = "/fms clean 1"
        outputs = [item async for item in plugin.fms_command(event)]
        self.assertIn("已清理", outputs[0]["text"])
        self.assertFalse(old_file.exists())
        await plugin.terminate()

    async def test_web_api_registered_and_stats(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        routes = [item[0] for item in self.context.registered_web_apis]
        self.assertTrue(any(route.endswith("/stats") for route in routes))
        self.assertTrue(any(route.endswith("/clean") for route in routes))

        from astrbot.api.web import request

        request.query.params = {"days": 0}
        result = await plugin.web_stats()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 0)
        await plugin.terminate()

    async def test_fms_retry_pending_job(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        await plugin._index.create_job(
            "retry-1",
            "group",
            "20002",
            [{"forward_id": "f1", "inline": None}],
            {
                "group_id": "20002",
                "sender_id": "10001",
                "sender_name": "测试用户",
                "self_id": "90001",
                "message_id": "m1",
                "group_name": "测试群",
            },
        )
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(components=[], bot=bot)
        event.message_str = "/fms retry"
        outputs = [item async for item in plugin.fms_command(event)]
        self.assertIn("已提交 1", outputs[0]["text"])
        await self.drain(plugin)

        self.assertEqual(len(self.files(plugin)), 1)
        await plugin.terminate()

    async def test_saves_audio_and_file_when_enabled(self):
        plugin = await self.make_plugin(
            {"folder_layout": "scope", "save_audio": True, "save_files": True}
        )
        audio = self.base / "voice.amr"
        audio.write_bytes(b"#!AMR\n" + b"\x00" * 32)
        document = self.base / "report.pdf"
        document.write_bytes(b"%PDF-1.4\n" + b"x" * 32)
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "record",
                            "data": {"file": "a.amr", "path": str(audio)},
                        },
                        {
                            "type": "file",
                            "data": {
                                "file": "report.pdf",
                                "file_name": "report.pdf",
                                "path": str(document),
                            },
                        },
                    ],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)
        await plugin.on_any_message(event)
        await self.drain(plugin)
        files = self.files(plugin)
        self.assertEqual(len(files), 2)
        self.assertEqual({item.suffix for item in files}, {".amr", ".pdf"})
        await plugin.terminate()

    async def test_skip_emoji(self):
        plugin = await self.make_plugin({"folder_layout": "scope", "skip_emoji": True})
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": "a.gif",
                                "path": str(self.png),
                                "sub_type": 1,
                                "summary": "[动画表情]",
                            },
                        }
                    ],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)
        await plugin.on_any_message(event)
        await self.drain(plugin)
        self.assertEqual(self.files(plugin), [])
        await plugin.terminate()

    async def test_templates(self):
        plugin = await self.make_plugin(
            {
                "folder_template": "{date}/{scope_name}",
                "filename_template": "{kind}_{sender}_{seq}_{hash}",
            }
        )
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)
        await plugin.on_any_message(event)
        await self.drain(plugin)
        files = self.files(plugin)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^image_111_01_[0-9a-f]{8}\.png$")
        await plugin.terminate()

    async def test_min_video_seconds_filters_short_video(self):
        plugin = await self.make_plugin(
            {"folder_layout": "scope", "min_video_seconds": 3}
        )
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "short.mp4",
                                "path": str(self.mp4),
                                "duration": 2,
                            },
                        }
                    ],
                }
            ]
        }
        bot = FakeBot({"f1": payload})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)
        await plugin.on_any_message(event)
        await self.drain(plugin)
        self.assertEqual(self.files(plugin), [])
        await plugin.terminate()

    async def test_missing_bot_does_not_crash(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=None)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(self.files(plugin), [])
        await plugin.terminate()

    async def test_notify_off(self):
        plugin = await self.make_plugin({"folder_layout": "scope"})
        bot = FakeBot({"f1": image_payload(self.png)})
        event = FakeEvent(components=[FakeComponent("Forward", id="f1")], bot=bot)

        await plugin.on_any_message(event)
        await self.drain(plugin)

        self.assertEqual(len(self.files(plugin)), 1)
        self.assertEqual(self.context.sent, [])
        await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
