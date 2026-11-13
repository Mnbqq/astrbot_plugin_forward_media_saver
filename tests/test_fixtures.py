from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.forward_parser import (  # noqa: E402
    parse_forward_payload,
)


class FixtureTests(unittest.TestCase):
    def test_napcat_fixture(self):
        payload = json.loads(
            (ROOT / "tests/fixtures/napcat_forward.json").read_text(encoding="utf-8")
        )
        parsed = parse_forward_payload(payload)
        self.assertEqual(
            [item.kind for item in parsed.media], ["image", "video", "audio", "file"]
        )
        self.assertEqual(parsed.media[0].message_id, "900001")
        self.assertEqual(parsed.media[3].file_name, "报告.pdf")


if __name__ == "__main__":
    unittest.main()
