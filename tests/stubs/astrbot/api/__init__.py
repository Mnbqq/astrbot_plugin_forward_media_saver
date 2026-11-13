"""Minimal astrbot.api stub used by unit tests."""

from __future__ import annotations

import logging


logger = logging.getLogger("astrbot-stub")
logger.addHandler(logging.NullHandler())


class AstrBotConfig(dict):
    def save_config(self, *args, **kwargs):
        return None
