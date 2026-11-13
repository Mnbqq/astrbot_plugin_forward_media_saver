"""Minimal astrbot.api.web stub for tests."""

from __future__ import annotations


class _Query:
    def __init__(self):
        self.params = {}

    def get(self, key, default=None, type=None):
        value = self.params.get(key, default)
        if type is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value


class _Request:
    def __init__(self):
        self.query = _Query()
        self.body = {}

    async def json(self, default=None):
        return dict(self.body)


request = _Request()


def json_response(data):
    return {"ok": True, "data": data}


def error_response(message, status_code=400):
    return {"ok": False, "message": message, "status_code": status_code}
