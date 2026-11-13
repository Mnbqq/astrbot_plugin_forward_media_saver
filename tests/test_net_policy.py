from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.net_policy import (  # noqa: E402
    check_url,
    host_allowed,
    is_private_address,
)


class NetPolicyTests(unittest.TestCase):
    def test_private_addresses(self):
        self.assertTrue(is_private_address("127.0.0.1"))
        self.assertTrue(is_private_address("10.1.2.3"))
        self.assertTrue(is_private_address("192.168.1.1"))
        self.assertTrue(is_private_address("::1"))
        self.assertFalse(is_private_address("8.8.8.8"))
        self.assertFalse(is_private_address("example.com"))

    def test_host_allowlist(self):
        self.assertTrue(host_allowed("a.qq.com", ("qq.com",)))
        self.assertTrue(host_allowed("qq.com", ("qq.com",)))
        self.assertFalse(host_allowed("evilqq.com", ("qq.com",)))
        self.assertTrue(host_allowed("any.com", ()))

    def test_block_private_url(self):
        allowed, reason = check_url("http://127.0.0.1:3000/a.mp4", block_private=True)
        self.assertFalse(allowed)
        self.assertIn("内网", reason)

        allowed, _ = check_url("http://127.0.0.1:3000/a.mp4", block_private=False)
        self.assertTrue(allowed)

    def test_host_allowlist_url(self):
        allowed, _ = check_url(
            "https://gchat.qpic.cn/a.jpg", allowed_hosts=("qpic.cn",)
        )
        self.assertTrue(allowed)
        allowed, _ = check_url("https://evil.example/a.jpg", allowed_hosts=("qpic.cn",))
        self.assertFalse(allowed)

    def test_non_http_scheme(self):
        allowed, _ = check_url("ftp://example.com/a.mp4")
        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
