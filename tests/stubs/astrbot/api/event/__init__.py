"""Minimal astrbot.api.event stub."""

from __future__ import annotations


class MessageChain:
    def __init__(self, chain=None, **kwargs):
        self.chain = list(chain or [])


class AstrMessageEvent:
    pass


class _Filter:
    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"
        ALL = "all"

    class EventMessageType:
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"
        ALL = "all"

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    @staticmethod
    def platform_adapter_type(*args, **kwargs):
        return lambda fn: fn

    @staticmethod
    def event_message_type(*args, **kwargs):
        return lambda fn: fn

    @staticmethod
    def command(*args, **kwargs):
        return lambda fn: fn

    @staticmethod
    def permission_type(*args, **kwargs):
        return lambda fn: fn


filter = _Filter()
