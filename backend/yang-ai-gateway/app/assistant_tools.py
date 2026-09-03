"""Voice-first todo, reminder and daily-briefing tools."""

from __future__ import annotations

import calendar
import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.assistant_store import AssistantStore
from app.device_context import current_device_key
from app.tool_registry import ToolRegistry, ToolSpec


DEFAULT_TIMEZONE = os.getenv("ASSISTANT_TIMEZONE", "Asia/Shanghai")
store = AssistantStore()


def _zone(name: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc


def _parse_datetime(value: Any, timezone_name: str | None = None) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("time must be an ISO 8601 date or date-time") from exc
    zone = _zone(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _display_time(epoch: int | None, timezone_name: str = DEFAULT_TIMEZONE) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, _zone(timezone_name)).strftime("%Y-%m-%d %H:%M")


def _public_todo(todo: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": todo["id"],
        "title": todo["title"],
        "status": todo["status"],
        "priority": todo["priority"],
        "due_at": _display_time(todo.get("due_at")),
    }


def _todo_device_command(todo: dict[str, Any], action: str) -> dict[str, Any] | None:
    due_at = todo.get("due_at")
    if due_at is None:
        return None
    # Negative IDs keep todo alarms separate from reminder table IDs on firmware.
    device_id = -int(todo["id"])
    if action == "cancel":
        return {"action": "cancel", "id": device_id}
    local_due = datetime.fromtimestamp(int(due_at), _zone())
    local_epoch = calendar.timegm(local_due.replace(tzinfo=None).timetuple())
    return {
        "action": "schedule",
        "id": device_id,
        "title": todo["title"],
        "kind": "todo",
        "trigger_at_epoch": local_epoch,
        "trigger_at": local_due.strftime("%Y-%m-%d %H:%M"),
    }

def create_todo(arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()[:120]
    if not title:
        raise ValueError("title is required")
    priority = str(arguments.get("priority") or "normal")
    if priority not in ("low", "normal", "high"):
        raise ValueError("priority must be low, normal or high")
    due = _parse_datetime(arguments.get("due_at"))
    if due is not None and due <= datetime.now(_zone()) + timedelta(seconds=10):
        raise ValueError("due_at must be in the future")
    todo = store.create_todo(
        current_device_key(), title, int(due.timestamp()) if due else None, priority
    )
    result = _public_todo(todo)
    command = _todo_device_command(todo, "schedule")
    if command is not None:
        result["device_command"] = command
    return result


def list_todos(arguments: dict[str, Any]) -> dict[str, Any]:
    status = str(arguments.get("status") or "pending")
    if status not in ("pending", "completed", "all"):
        raise ValueError("unsupported todo status")
    items = store.list_todos(current_device_key(), status)
    return {"count": len(items), "items": [_public_todo(item) for item in items]}


def _selector(arguments: dict[str, Any]) -> tuple[int | None, str | None]:
    raw_id = arguments.get("todo_id")
    todo_id = int(raw_id) if raw_id is not None else None
    title = str(arguments.get("title") or "").strip()[:120] or None
    return todo_id, title


def complete_todo(arguments: dict[str, Any]) -> dict[str, Any]:
    todo_id, title = _selector(arguments)
    todo = store.complete_todo(current_device_key(), todo_id, title)
    result = _public_todo(todo)
    command = _todo_device_command(todo, "cancel")
    if command is not None:
        result["device_command"] = command
    return result


def delete_todo(arguments: dict[str, Any]) -> dict[str, Any]:
    todo_id, title = _selector(arguments)
    deleted = store.delete_todo(current_device_key(), todo_id, title)
    result = {"deleted": True, **_public_todo(deleted)}
    command = _todo_device_command(deleted, "cancel")
    if command is not None:
        result["device_command"] = command
    return result


def _public_reminder(reminder: dict[str, Any]) -> dict[str, Any]:
    timezone_name = reminder.get("timezone") or DEFAULT_TIMEZONE
    return {
        "id": reminder["id"],
        "title": reminder["title"],
        "kind": reminder["kind"],
        "status": reminder["status"],
        "trigger_at": _display_time(reminder["trigger_at"], timezone_name),
        "timezone": timezone_name,
    }


def create_reminder(arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()[:120]
    if not title:
        raise ValueError("title is required")
    kind = str(arguments.get("kind") or "reminder")
    if kind not in ("alarm", "reminder"):
        raise ValueError("kind must be alarm or reminder")
    timezone_name = str(arguments.get("timezone") or DEFAULT_TIMEZONE)
    trigger = _parse_datetime(arguments.get("trigger_at"), timezone_name)
    if trigger is None:
        raise ValueError("trigger_at is required")
    now = datetime.now(_zone(timezone_name))
    if trigger <= now + timedelta(seconds=10):
        raise ValueError("trigger_at must be in the future")
    if trigger > now + timedelta(days=366):
        raise ValueError("trigger_at must be within 366 days")
    # Firmware currently stores wall-clock epoch because OTA time sync applies
    # the configured timezone offset before calling settimeofday().
    local_epoch = calendar.timegm(trigger.replace(tzinfo=None).timetuple())
    reminder = store.create_reminder(
        current_device_key(),
        title,
        kind,
        int(trigger.timestamp()),
        local_epoch,
        timezone_name,
    )
    return {
        **_public_reminder(reminder),
        "device_command": {
            "action": "schedule",
            "id": reminder["id"],
            "title": title,
            "kind": kind,
            "trigger_at_epoch": local_epoch,
            "trigger_at": trigger.strftime("%Y-%m-%d %H:%M"),
        },
    }


def list_reminders(arguments: dict[str, Any]) -> dict[str, Any]:
    status = str(arguments.get("status") or "scheduled")
    if status not in ("scheduled", "cancelled", "fired", "all"):
        raise ValueError("unsupported reminder status")
    items = store.list_reminders(current_device_key(), status)
    return {"count": len(items), "items": [_public_reminder(item) for item in items]}


def cancel_reminder(arguments: dict[str, Any]) -> dict[str, Any]:
    reminder_id = int(arguments["reminder_id"])
    reminder = store.cancel_reminder(current_device_key(), reminder_id)
    return {
        **_public_reminder(reminder),
        "device_command": {"action": "cancel", "id": reminder_id},
    }


def generate_daily_briefing(arguments: dict[str, Any]) -> dict[str, Any]:
    from app.read_only_tools import get_current_weather

    zone = _zone(str(arguments.get("timezone") or DEFAULT_TIMEZONE))
    now = datetime.now(zone)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    todos = store.list_todos(current_device_key(), "pending")
    reminders = [
        item
        for item in store.list_reminders(current_device_key(), "scheduled")
        if item["trigger_at"] < int(tomorrow.timestamp())
    ]
    try:
        weather = get_current_weather({})
    except (OSError, RuntimeError, ValueError):
        weather = {"found": False, "unavailable": True}
    return {
        "date": now.strftime("%Y-%m-%d"),
        "weather": weather,
        "pending_todos": [_public_todo(item) for item in todos[:5]],
        "today_reminders": [_public_reminder(item) for item in reminders[:5]],
        "counts": {"pending_todos": len(todos), "today_reminders": len(reminders)},
    }


def register_assistant_tools(registry: ToolRegistry) -> None:
    registry.register(ToolSpec(
        name="create_todo",
        description="保存一条新的待办事项。只有用户明确要求记下、添加或创建待办时调用。",
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "maxLength": 120, "description": "待办内容"},
            "due_at": {"type": "string", "description": "可选的 ISO 8601 截止日期或时间"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        }, "required": ["title"]}, handler=create_todo))
    registry.register(ToolSpec(
        name="list_todos", description="查询用户保存的待办事项。",
        parameters={"type": "object", "properties": {
            "status": {"type": "string", "enum": ["pending", "completed", "all"]}
        }}, handler=list_todos))
    selector = {"type": "object", "properties": {
        "todo_id": {"type": "integer", "minimum": 1},
        "title": {"type": "string", "maxLength": 120, "description": "无法获知编号时使用待办标题"},
    }}
    registry.register(ToolSpec(
        name="complete_todo", description="将指定待办标记为已完成。",
        parameters=selector, handler=complete_todo))
    registry.register(ToolSpec(
        name="delete_todo", description="永久删除指定待办事项。",
        parameters=selector, handler=delete_todo))
    registry.register(ToolSpec(
        name="create_reminder",
        description="创建一次性闹钟或提醒。相对时间必须先结合当前时间转换为明确的 ISO 8601 时间。",
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "maxLength": 120, "description": "到点显示的事项"},
            "trigger_at": {"type": "string", "description": "带日期的 ISO 8601 触发时间"},
            "kind": {"type": "string", "enum": ["alarm", "reminder"]},
            "timezone": {"type": "string", "description": "IANA 时区，默认 Asia/Shanghai"},
        }, "required": ["title", "trigger_at"]}, handler=create_reminder))
    registry.register(ToolSpec(
        name="list_reminders", description="查询已有闹钟和提醒。",
        parameters={"type": "object", "properties": {
            "status": {"type": "string", "enum": ["scheduled", "cancelled", "fired", "all"]}
        }}, handler=list_reminders))
    registry.register(ToolSpec(
        name="cancel_reminder", description="按编号取消一个尚未触发的闹钟或提醒。",
        parameters={"type": "object", "properties": {
            "reminder_id": {"type": "integer", "minimum": 1}
        }, "required": ["reminder_id"]}, handler=cancel_reminder))
    registry.register(ToolSpec(
        name="generate_daily_briefing",
        description="生成今天的个人简报，汇总天气、未完成待办和今天的提醒。",
        parameters={"type": "object", "properties": {
            "timezone": {"type": "string", "description": "可选 IANA 时区"}
        }}, handler=generate_daily_briefing))
