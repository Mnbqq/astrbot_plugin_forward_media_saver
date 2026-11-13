from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.video_probe import (  # noqa: E402
    probe_mp4_duration,
)


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def make_mp4(seconds: float, timescale: int = 1000) -> bytes:
    payload = (
        b"\x00\x00\x00\x00"
        + b"\x00" * 8
        + timescale.to_bytes(4, "big")
        + int(seconds * timescale).to_bytes(4, "big")
        + b"\x00" * 80
    )
    return _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2") + _box(
        b"moov", _box(b"mvhd", payload)
    )


class VideoProbeTests(unittest.TestCase):
    def test_probe_mp4_duration(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            handle.write(make_mp4(2.5))
            path = Path(handle.name)
        try:
            self.assertAlmostEqual(probe_mp4_duration(path), 2.5, places=3)
        finally:
            path.unlink(missing_ok=True)

    def test_probe_invalid_file(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            handle.write(b"not an mp4")
            path = Path(handle.name)
        try:
            self.assertEqual(probe_mp4_duration(path), 0.0)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
