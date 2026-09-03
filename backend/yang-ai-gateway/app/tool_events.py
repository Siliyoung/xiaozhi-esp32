"""Per-conversation event bridge for successful model tool calls."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Callable


ToolEventHandler = Callable[[str, dict[str, Any]], None]
_handler: ContextVar[ToolEventHandler | None] = ContextVar(
    "tool_event_handler", default=None
)


def set_tool_event_handler(handler: ToolEventHandler) -> Token:
    return _handler.set(handler)


def reset_tool_event_handler(token: Token) -> None:
    _handler.reset(token)


def emit_tool_event(name: str, payload: dict[str, Any]) -> None:
    handler = _handler.get()
    if handler is not None:
        handler(name, payload)
