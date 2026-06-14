"""Proxy helpers for Telegram and HTTP clients."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession

logger = logging.getLogger(__name__)


def parse_proxy_config(proxy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not proxy or not proxy.get("enabled", False):
        return None
    return proxy


def build_proxy_url(proxy: dict[str, Any]) -> str:
    ptype = proxy.get("type", "socks5").lower()
    host = proxy["host"]
    port = int(proxy["port"])
    user = proxy.get("username", "")
    password = proxy.get("password", "")
    if user and password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    else:
        auth = ""
    scheme = "socks5" if ptype == "socks5" else "http"
    return f"{scheme}://{auth}{host}:{port}"


def create_bot_session(proxy_url: str | None) -> AiohttpSession:
    if proxy_url:
        logger.info("Telegram API via proxy: %s", _mask_proxy(proxy_url))
        return AiohttpSession(proxy=proxy_url)
    return AiohttpSession()


def create_aiohttp_session(proxy_url: str | None) -> aiohttp.ClientSession:
    if not proxy_url:
        return aiohttp.ClientSession()
    from aiohttp_socks import ProxyConnector

    connector = ProxyConnector.from_url(proxy_url)
    logger.info("HTTP client via proxy: %s", _mask_proxy(proxy_url))
    return aiohttp.ClientSession(connector=connector)


def _mask_proxy(url: str) -> str:
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
