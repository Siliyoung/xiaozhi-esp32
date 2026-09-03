"""Offline smoke tests for tool validation and the streaming tool loop."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from types import SimpleNamespace

os.environ.setdefault("DASHSCOPE_API_KEY", "offline-test-key")

from app import dashscope_pipeline_tools as tools_pipeline
from app.read_only_tools import build_read_only_registry, get_current_time, get_server_status


def response(*, content: str = "", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(
        status_code=HTTPStatus.OK,
        output=SimpleNamespace(choices=[choice]),
    )


def tool_chunk(index: int, call_id: str, name: str | None, arguments: str):
    return SimpleNamespace(
        index=index,
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_registry() -> None:
    registry = build_read_only_registry()
    names = [item["function"]["name"] for item in registry.definitions()]
    assert set(names) == {
        "get_current_time", "get_current_location", "get_current_weather",
        "start_pomodoro", "control_pomodoro", "get_server_status",
        "create_todo", "list_todos", "complete_todo", "delete_todo",
        "create_reminder", "list_reminders", "cancel_reminder",
        "generate_daily_briefing",
    }
    assert "required" not in registry.definitions()[2]["function"]["parameters"]
    started = json.loads(registry.execute("start_pomodoro", {"duration_minutes": 1}))
    assert started["ok"] is True and started["data"]["remaining_seconds"] == 60
    paused = json.loads(registry.execute("control_pomodoro", {"action": "pause"}))
    assert paused["ok"] is True and paused["data"]["state"] == "paused"
    cancelled = json.loads(registry.execute("control_pomodoro", {"action": "cancel"}))
    assert cancelled["ok"] is True and cancelled["data"]["state"] == "idle"
    unknown = json.loads(registry.execute("delete_everything", "{}"))
    assert unknown["ok"] is False
    assert get_current_time({})["timezone"] == "Asia/Shanghai"
    assert get_server_status({})["status"] == "running"


def test_plain_stream() -> None:
    pipeline = tools_pipeline.DashScopeToolsPipeline()
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return iter([response(content="你好，"), response(content="很高兴见到你。")])

    original = tools_pipeline.Generation.call
    tools_pipeline.Generation.call = fake_call
    try:
        answer = "".join(pipeline.iter_answer_deltas("你好", []))
    finally:
        tools_pipeline.Generation.call = original
    assert answer == "你好，很高兴见到你。"
    assert len(calls) == 1 and len(calls[0]["tools"]) == 14


def test_deterministic_routes() -> None:
    assert tools_pipeline.deterministic_tool_request("现在几点") == ("get_current_time", {})
    assert tools_pipeline.deterministic_tool_request("今天天气怎么样") == ("get_current_weather", {})
    assert tools_pipeline.deterministic_tool_request("深圳天气怎么样") == ("get_current_weather", {"location": "深圳"})
    assert tools_pipeline.deterministic_tool_request("服务器状态怎么样") == ("get_server_status", {})
    assert tools_pipeline.deterministic_tool_request("半小时后提醒我喝水") is None
    pipeline = tools_pipeline.DashScopeToolsPipeline()
    requests = []
    def fake_call(**kwargs):
        requests.append(kwargs)
        return iter([response(content="现在是北京时间十点。")])
    original = tools_pipeline.Generation.call
    tools_pipeline.Generation.call = fake_call
    try:
        answer = "".join(pipeline.iter_answer_deltas("现在几点", []))
    finally:
        tools_pipeline.Generation.call = original
    assert answer == "现在是北京时间十点。"
    assert len(requests) == 1 and "tools" not in requests[0]
    assert "Tool: get_current_time" in requests[0]["messages"][0]["content"]


def test_tool_stream() -> None:
    pipeline = tools_pipeline.DashScopeToolsPipeline()
    requests = []

    def fake_call(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return iter(
                [
                    response(
                        tool_calls=[
                            tool_chunk(0, "call_time", "get_current_time", "{")
                        ]
                    ),
                    response(
                        tool_calls=[tool_chunk(0, "", None, '"timezone":"Asia/Shanghai"}')]
                    ),
                ]
            )
        return iter([response(content="现在是北京时间十点。")])

    original = tools_pipeline.Generation.call
    tools_pipeline.Generation.call = fake_call
    try:
        answer = "".join(pipeline.iter_answer_deltas("请执行一次工具往返测试", []))
    finally:
        tools_pipeline.Generation.call = original
    assert answer == "现在是北京时间十点。"
    assert len(requests) == 2
    followup = requests[1]["messages"]
    assert followup[-2]["role"] == "assistant"
    assert followup[-1]["role"] == "tool"
    result = json.loads(followup[-1]["content"])
    assert result["ok"] is True and result["tool"] == "get_current_time"


if __name__ == "__main__":
    test_registry()
    test_plain_stream()
    test_deterministic_routes()
    test_tool_stream()
    print("function-calling-smoke-ok tools=14 deterministic=true assistant=true pomodoro=true plain_stream=true tool_roundtrip=true")
