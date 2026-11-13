from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.forward_parser import (  # noqa: E402
    ForwardTarget,
    extract_forward_targets,
    parse_forward_payload,
)


class GoCqhttpPayloadTests(unittest.TestCase):
    def test_messages_with_image_and_video(self):
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 111, "nickname": "甲"},
                    "time": 1700000000,
                    "message_id": 987654321,
                    "message": [
                        {"type": "text", "data": {"text": "看图"}},
                        {
                            "type": "image",
                            "data": {
                                "file": "abc.jpg",
                                "url": "https://example.com/a.jpg?x=1&amp;y=2",
                                "file_size": "12345",
                            },
                        },
                    ],
                },
                {
                    "sender": {"user_id": 222, "card": "乙"},
                    "time": 1700000001,
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "v.mp4",
                                "url": "https://example.com/v.mp4",
                            },
                        }
                    ],
                },
            ]
        }

        parsed = parse_forward_payload(payload)

        self.assertEqual(len(parsed.media), 2)
        image, video = parsed.media
        self.assertEqual(image.kind, "image")
        self.assertEqual(image.sender_id, "111")
        self.assertEqual(image.sender_name, "甲")
        self.assertEqual(image.message_id, "987654321")
        self.assertEqual(image.url, "https://example.com/a.jpg?x=1&y=2")
        self.assertEqual(image.size_hint, 12345)
        self.assertEqual(video.kind, "video")
        self.assertEqual(video.sender_id, "222")
        self.assertEqual(video.sender_name, "乙")

    def test_nested_forward_id_is_returned(self):
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {"type": "forward", "data": {"id": "nested-1"}},
                        {
                            "type": "image",
                            "data": {"url": "https://example.com/x.png"},
                        },
                    ],
                }
            ]
        }

        parsed = parse_forward_payload(payload)

        self.assertEqual(parsed.nested, ["nested-1"])
        self.assertEqual(len(parsed.media), 1)

    def test_forward_with_inline_content_and_no_id(self):
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "forward",
                            "data": {
                                "content": [
                                    {
                                        "type": "node",
                                        "data": {
                                            "user_id": 2,
                                            "content": [
                                                {
                                                    "type": "image",
                                                    "data": {
                                                        "url": "https://e.com/x.jpg"
                                                    },
                                                }
                                            ],
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                }
            ]
        }

        parsed = parse_forward_payload(payload)
        self.assertEqual(len(parsed.media), 1)
        self.assertEqual(parsed.media[0].url, "https://e.com/x.jpg")

    def test_local_path_in_url_is_preserved(self):
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "abc.mp4",
                                "url": (
                                    "/app/.config/QQ/nt_qq_x/nt_data/Video/"
                                    "2025-02/Ori/abc.mp4"
                                ),
                            },
                        }
                    ],
                }
            ]
        }
        parsed = parse_forward_payload(payload)
        self.assertEqual(len(parsed.media), 1)
        self.assertEqual(parsed.media[0].url, "")
        self.assertTrue(parsed.media[0].path.endswith("Ori/abc.mp4"))

    def test_audio_and_file_segments(self):
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "record",
                            "data": {"file": "a.amr", "url": "https://e.com/a.amr"},
                        },
                        {
                            "type": "file",
                            "data": {
                                "file": "doc.pdf",
                                "file_name": "报告.pdf",
                                "url": "https://e.com/doc.pdf",
                                "file_size": 100,
                            },
                        },
                    ],
                }
            ]
        }
        parsed = parse_forward_payload(payload)
        self.assertEqual([item.kind for item in parsed.media], ["audio", "file"])
        self.assertEqual(parsed.media[1].file_name, "报告.pdf")

    def test_emoji_fields(self):
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 1},
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": "a.gif",
                                "url": "https://e.com/a.gif",
                                "sub_type": 1,
                                "summary": "[动画表情]",
                            },
                        }
                    ],
                }
            ]
        }
        ref = parse_forward_payload(payload).media[0]
        self.assertEqual(ref.sub_type, 1)
        self.assertEqual(ref.summary, "[动画表情]")

    def test_duplicate_url_is_collapsed(self):
        payload = {
            "messages": [
                {
                    "message": [
                        {"type": "image", "data": {"url": "https://e.com/1.png"}},
                        {"type": "image", "data": {"url": "https://e.com/1.png"}},
                    ]
                }
            ]
        }
        self.assertEqual(len(parse_forward_payload(payload).media), 1)


class NapCatPayloadTests(unittest.TestCase):
    def test_message_with_node_segments(self):
        payload = {
            "message": [
                {
                    "type": "node",
                    "data": {
                        "user_id": 333,
                        "nickname": "丙",
                        "time": 1700000002,
                        "content": [
                            {
                                "type": "image",
                                "data": {
                                    "file": "//abc",
                                    "url": "//gchat.qpic.cn/a.jpg",
                                },
                            }
                        ],
                    },
                }
            ]
        }

        parsed = parse_forward_payload(payload)

        self.assertEqual(len(parsed.media), 1)
        ref = parsed.media[0]
        self.assertEqual(ref.sender_id, "333")
        self.assertEqual(ref.sender_name, "丙")
        self.assertEqual(ref.url, "https://gchat.qpic.cn/a.jpg")

    def test_data_wrapper_around_messages(self):
        payload = {
            "status": "ok",
            "retcode": 0,
            "data": {
                "messages": [
                    {
                        "sender": {"user_id": 4},
                        "message": [
                            {"type": "image", "data": {"url": "https://e.com/2.png"}}
                        ],
                    }
                ]
            },
        }
        self.assertEqual(len(parse_forward_payload(payload).media), 1)

    def test_inline_node_inside_node_content(self):
        payload = {
            "message": [
                {
                    "type": "node",
                    "data": {
                        "user_id": 5,
                        "content": [
                            {
                                "type": "node",
                                "data": {
                                    "user_id": 6,
                                    "content": [
                                        {
                                            "type": "image",
                                            "data": {"url": "https://e.com/3.png"},
                                        }
                                    ],
                                },
                            }
                        ],
                    },
                }
            ]
        }

        parsed = parse_forward_payload(payload)
        self.assertEqual(len(parsed.media), 1)
        # 内联节点的发送者信息由外层记录兜底，媒体本身不能丢。
        self.assertEqual(parsed.media[0].url, "https://e.com/3.png")

    def test_response_with_extra_message_status_field(self):
        payload = {
            "status": "ok",
            "retcode": 0,
            "message": "success",
            "data": {
                "messages": [
                    {
                        "sender": {"user_id": 8},
                        "message": [
                            {"type": "image", "data": {"url": "https://e.com/8.png"}}
                        ],
                    }
                ]
            },
        }
        parsed = parse_forward_payload(payload)
        self.assertEqual(len(parsed.media), 1)

    def test_single_segment_payload(self):
        parsed = parse_forward_payload(
            {"type": "image", "data": {"url": "https://e.com/4.png"}}
        )
        self.assertEqual(len(parsed.media), 1)


class CqStringTests(unittest.TestCase):
    def test_cq_image_and_forward(self):
        payload = {
            "messages": [
                {
                    "sender": {"user_id": 7},
                    "message": (
                        "[CQ:image,file=a.jpg,url=https://e.com/5.jpg]"
                        "[CQ:forward,id=abc]"
                    ),
                }
            ]
        }
        parsed = parse_forward_payload(payload)
        self.assertEqual(len(parsed.media), 1)
        self.assertEqual(parsed.media[0].url, "https://e.com/5.jpg")
        self.assertEqual(parsed.nested, ["abc"])


class FakeComponent:
    def __init__(self, type_value, **fields):
        self.type = type_value
        for key, value in fields.items():
            setattr(self, key, value)


class ExtractTargetsTests(unittest.TestCase):
    def test_forward_component(self):
        comp = FakeComponent("Forward", id="f-1")
        targets = extract_forward_targets([comp])
        self.assertEqual(targets, [ForwardTarget(forward_id="f-1")])

    def test_nodes_component(self):
        text = FakeComponent("Plain", text="hi")
        node = FakeComponent("Node", content=[text], name="某人", uin="123", time=0)
        nodes = FakeComponent("Nodes", nodes=[node])

        targets = extract_forward_targets([nodes])

        self.assertEqual(len(targets), 1)
        self.assertIsNotNone(targets[0].inline)

    def test_raw_fallback_when_components_missed(self):
        raw = {
            "message": [
                {"type": "forward", "data": {"id": "raw-1"}},
                {"type": "text", "data": {"text": "x"}},
            ]
        }
        targets = extract_forward_targets([], raw)
        self.assertEqual(targets, [ForwardTarget(forward_id="raw-1")])

    def test_raw_cq_fallback(self):
        raw = {"message": "[CQ:forward,id=cq-1]"}
        targets = extract_forward_targets([], raw)
        self.assertEqual(targets, [ForwardTarget(forward_id="cq-1")])

    def test_no_forward(self):
        comp = FakeComponent("Plain", text="hello")
        self.assertEqual(extract_forward_targets([comp], {"message": []}), [])


if __name__ == "__main__":
    unittest.main()
