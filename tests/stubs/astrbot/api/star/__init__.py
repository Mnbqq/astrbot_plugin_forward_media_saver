"""Minimal astrbot.api.star stub."""

from __future__ import annotations

import os
from pathlib import Path


class Star:
    def __init__(self, context, config=None):
        self.context = context

    async def initialize(self):
        return None

    async def terminate(self):
        return None


class StarTools:
    _root = None

    @classmethod
    def set_root(cls, root):
        cls._root = Path(root)

    @classmethod
    def get_data_dir(cls, plugin_name=None):
        base = cls._root or Path(os.environ.get("TMPDIR", "/tmp")) / "astrbot-test"
        target = Path(base) / "plugin_data" / (plugin_name or "unknown")
        target.mkdir(parents=True, exist_ok=True)
        return target


class Context:
    def __init__(self):
        self.sent = []
        self.registered_web_apis = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True

    def register_web_api(self, route, handler, methods, desc):
        self.registered_web_apis.append((route, handler, methods, desc))


def register(*args, **kwargs):
    def decorator(cls):
        return cls

    return decorator
