"""下载 URL 的安全策略。

* 可选的私网/回环地址拦截（SSRF）；
* 可选的媒体域名白名单；
* DNS 结果带短 TTL 缓存，避免每个媒体都解析一次。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlparse

_DNS_CACHE: dict[str, tuple[bool, float]] = {}
_DNS_TTL = 300.0


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = (host or "").lower().strip(".")
    if not allowed_hosts:
        return True
    for item in allowed_hosts:
        domain = item.lower().strip().strip(".")
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            return True
    return False


def is_private_address(host: str) -> bool:
    """判断 IP 字面量是否为私网/回环/链路本地/保留地址。"""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _resolve_private(host: str) -> bool:
    now = time.monotonic()
    cached = _DNS_CACHE.get(host)
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        # 解析失败时不在策略层拦截，让真正的下载报网络错误。
        private = False
    else:
        private = any(is_private_address(str(info[4][0])) for info in infos if info[4])
    _DNS_CACHE[host] = (private, now + _DNS_TTL)
    if len(_DNS_CACHE) > 512:
        _DNS_CACHE.clear()
    return private


def check_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] = (),
    block_private: bool = False,
) -> tuple[bool, str]:
    """返回 (是否允许, 原因)。只处理 http/https。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "URL 解析失败"
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的 URL 协议：{parsed.scheme}"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL 缺少 host"
    if not host_allowed(host, allowed_hosts):
        return False, f"域名不在白名单：{host}"
    if block_private:
        if is_private_address(host) or host in {"localhost", "localhost.localdomain"}:
            return False, f"拒绝内网/回环地址：{host}"
        if _resolve_private(host):
            return False, f"域名解析到内网地址：{host}"
    return True, ""
