"""Per-conversation device identity for device-control tools."""

from __future__ import annotations

from contextvars import ContextVar, Token


_DEVICE_KEY: ContextVar[str] = ContextVar("device_key", default="unknown")


def set_current_device_key(device_key: str) -> Token:
    return _DEVICE_KEY.set(device_key)


def reset_current_device_key(token: Token) -> None:
    _DEVICE_KEY.reset(token)


def current_device_key() -> str:
    return _DEVICE_KEY.get()
