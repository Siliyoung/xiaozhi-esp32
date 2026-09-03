"""Offline persistence and tool-contract tests for personal assistant features."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import assistant_tools, read_only_tools
from app.assistant_store import AssistantStore
from app.device_context import reset_current_device_key, set_current_device_key


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        path = str(Path(temporary_directory) / "assistant.db")
        assistant_tools.store = AssistantStore(path)
        token = set_current_device_key("smoke-device")
        original_weather = read_only_tools.get_current_weather
        read_only_tools.get_current_weather = lambda _: {
            "found": True,
            "location": "深圳",
            "condition": "晴",
            "temperature_c": 28,
        }
        try:
            created = assistant_tools.create_todo(
                {"title": "完成 ESP32 项目", "priority": "high"}
            )
            assert created["status"] == "pending" and created["id"] > 0
            assert assistant_tools.list_todos({})["count"] == 1
            completed = assistant_tools.complete_todo({"todo_id": created["id"]})
            assert completed["status"] == "completed"

            second = assistant_tools.create_todo({"title": "整理 README"})
            persisted = AssistantStore(path).list_todos("smoke-device")
            assert [item["id"] for item in persisted] == [second["id"]]

            trigger = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(hours=1)
            reminder = assistant_tools.create_reminder(
                {
                    "title": "提交简历",
                    "kind": "reminder",
                    "trigger_at": trigger.isoformat(timespec="seconds"),
                }
            )
            command = reminder["device_command"]
            assert command["action"] == "schedule" and command["trigger_at_epoch"] > 0
            assert assistant_tools.list_reminders({})["count"] == 1

            briefing = assistant_tools.generate_daily_briefing({})
            assert briefing["weather"]["location"] == "深圳"
            assert briefing["counts"]["pending_todos"] == 1

            cancelled = assistant_tools.cancel_reminder(
                {"reminder_id": reminder["id"]}
            )
            assert cancelled["device_command"] == {
                "action": "cancel",
                "id": reminder["id"],
            }
            deleted = assistant_tools.delete_todo({"title": "整理 README"})
            assert deleted["deleted"] is True

            names = {
                item["function"]["name"]
                for item in read_only_tools.build_read_only_registry().definitions()
            }
            expected = {
                "create_todo", "list_todos", "complete_todo", "delete_todo",
                "create_reminder", "list_reminders", "cancel_reminder",
                "generate_daily_briefing",
            }
            assert expected <= names
        finally:
            read_only_tools.get_current_weather = original_weather
            reset_current_device_key(token)

    print("assistant-tools-smoke-ok sqlite=true todos=true reminders=true briefing=true tools=14")


if __name__ == "__main__":
    main()
