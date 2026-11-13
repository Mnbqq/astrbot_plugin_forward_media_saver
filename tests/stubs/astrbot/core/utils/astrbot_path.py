from __future__ import annotations

import os


def get_astrbot_data_path() -> str:
    return os.environ.get("ASTRBOT_TEST_DATA", "/tmp/astrbot-test-data")
