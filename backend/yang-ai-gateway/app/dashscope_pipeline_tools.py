"""Streaming Qwen pipeline with a bounded Function Calling loop."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from http import HTTPStatus
from typing import Any

from dashscope import Generation

from app.dashscope_pipeline_protected import UpstreamError
from app.dashscope_pipeline_streaming import (
    DashScopeStreamingPipeline as BaseStreamingPipeline,
)
from app.read_only_tools import build_read_only_registry
from app.tool_events import emit_tool_event


logger = logging.getLogger("yang-ai-gateway.function-calling")
AUTO_LOCATION_INSTRUCTIONS = (
    "Use get_current_location when the user asks where they are. "
    "For weather without an explicit place, call get_current_weather with an empty object "
    "so it uses the device's city; never guess a city. For local time without an explicit "
    "timezone, call get_current_time with an empty object so it uses the device's timezone."
)
POMODORO_INSTRUCTIONS = (
    "Pomodoro and focus countdown requests must use start_pomodoro or control_pomodoro. "
    "Use 25 minutes when the user gives no duration. Never claim a timer was started, "
    "paused, resumed, cancelled, displayed, or queried without calling the tool. "
    "The countdown runs locally on the device after the command is sent."
)
PERSONAL_ASSISTANT_INSTRUCTIONS = (
    "Todo requests must use create_todo, list_todos, complete_todo or delete_todo; "
    "never claim persistent data changed without a successful tool result. "
    "Alarm and reminder requests must use create_reminder, list_reminders or "
    "cancel_reminder. For relative times such as later today or in half an hour, "
    "first call get_current_time and then pass an explicit future ISO 8601 time to "
    "create_reminder. Repeat the exact scheduled date and time in the confirmation. "
    "Daily briefing requests must use generate_daily_briefing."
)


def _compact_intent(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?;；:：'\"“”‘’（）()]+", "", text).lower()


def deterministic_tool_request(transcript: str) -> tuple[str, dict[str, Any]] | None:
    """Route unambiguous realtime queries without relying on model tool selection."""
    text = _compact_intent(transcript)
    if not text or len(text) > 80:
        return None
    if any(word in text for word in ("服务器状态", "服务器运行", "服务器负载", "网关状态")):
        return "get_server_status", {}
    if any(word in text for word in ("我在哪里", "我在哪儿", "我在哪", "当前位置", "当前地址")):
        return "get_current_location", {}
    if "天气" in text and not any(word in text for word in ("明天", "后天", "未来", "预报")):
        location = text.split("天气", 1)[0]
        for prefix in ("请问", "请帮我查一下", "帮我查一下", "帮我看看", "查一下", "看看", "告诉我"):
            if location.startswith(prefix):
                location = location[len(prefix):]
        for temporal in ("今天", "现在", "当前", "此刻"):
            location = location.replace(temporal, "")
        if location in ("", "当地", "本地", "这里", "外面", "当前地点", "我的位置"):
            return "get_current_weather", {}
        if 1 <= len(location) <= 20:
            return "get_current_weather", {"location": location}
    blocked_time_contexts = ("提醒", "闹钟", "待办", "番茄", "倒计时")
    time_queries = (
        "几点", "当前时间", "现在时间", "现在是几点", "此刻时间",
        "今天几号", "现在几号", "今天星期几", "今天周几", "当前日期", "现在日期",
    )
    if not any(word in text for word in blocked_time_contexts) and any(word in text for word in time_queries):
        return "get_current_time", {}
    return None


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class DashScopeToolsPipeline(BaseStreamingPipeline):
    def __init__(self) -> None:
        super().__init__()
        self.tool_registry = build_read_only_registry()
        self.max_tool_rounds = max(1, min(int(os.getenv("MAX_TOOL_ROUNDS", "2")), 4))
        self.tool_system_prompt = os.getenv(
            "TOOL_SYSTEM_PROMPT",
            "涉及当前日期时间、实时天气或当前服务器运行状态的问题，必须调用对应工具获取实时数据，"
            "不要依靠记忆猜测。工具失败时如实简短说明，不要编造结果。",
        )

    def _messages(self, transcript: str, history: list[dict[str, str]]) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": f"{self.system_prompt}\n{self.tool_system_prompt}\n{AUTO_LOCATION_INSTRUCTIONS}\n{POMODORO_INSTRUCTIONS}\n{PERSONAL_ASSISTANT_INSTRUCTIONS}",
            },
            *history[-8:],
            {"role": "user", "content": transcript},
        ]

    @staticmethod
    def _merge_tool_calls(
        accumulated: dict[int, dict[str, Any]], chunks: list[Any]
    ) -> None:
        for fallback_index, chunk in enumerate(chunks):
            index = int(_value(chunk, "index", fallback_index) or 0)
            function = _value(chunk, "function", {}) or {}
            current = accumulated.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            call_id = _value(chunk, "id", "") or ""
            call_type = _value(chunk, "type", "") or ""
            name = _value(function, "name", "") or ""
            arguments = _value(function, "arguments", "") or ""
            if call_id:
                current["id"] = call_id
            if call_type:
                current["type"] = call_type
            if name:
                current["function"]["name"] = name
            current["function"]["arguments"] += arguments

    def _stream_once(self, messages: list[dict[str, Any]], *, use_tools: bool = True):
        options: dict[str, Any] = {}
        if use_tools:
            options.update(tools=self.tool_registry.definitions(), parallel_tool_calls=True)
        return Generation.call(
            model=self.llm_model,
            messages=messages,
            result_format="message",
            max_tokens=self.llm_max_tokens,
            temperature=0.7 if use_tools else 0.2,
            enable_thinking=False,
            stream=True,
            incremental_output=True,
            request_timeout=self.llm_timeout,
            **options,
        )

    def _run_deterministic_tool(self, transcript, history, request):
        name, arguments = request
        result = self.tool_registry.execute(name, arguments)
        payload = json.loads(result)
        emit_tool_event(name, payload)
        logger.info("Tool invoked tool=%s route=deterministic", name)
        messages = self._messages(transcript, history)
        messages[0]["content"] += (
            "\nThe server has already executed the required realtime tool deterministically. "
            "Answer the user's question briefly in Chinese using only this tool result. "
            "Do not claim another tool call and do not invent missing values.\n"
            f"Tool: {name}\nTool result: {result}"
        )
        emitted_chars = 0
        for response in self._stream_once(messages, use_tools=False):
            if response.status_code != HTTPStatus.OK:
                raise UpstreamError("llm", int(response.status_code))
            choices = _value(response.output, "choices", []) or []
            if not choices:
                continue
            message = _value(choices[0], "message", {}) or {}
            content = _value(message, "content", "") or ""
            if not content:
                continue
            remaining = self.max_answer_chars - emitted_chars
            if remaining <= 0:
                return
            delta = content[:remaining]
            emitted_chars += len(delta)
            yield delta
        if emitted_chars == 0:
            raise UpstreamError("llm")

    def _run_conversation(self, messages: list[dict[str, Any]]):
        emitted_chars = 0
        for tool_round in range(self.max_tool_rounds + 1):
            tool_calls: dict[int, dict[str, Any]] = {}
            assistant_content = ""
            responses = self._stream_once(messages)
            for response in responses:
                if response.status_code != HTTPStatus.OK:
                    raise UpstreamError("llm", int(response.status_code))
                choices = _value(response.output, "choices", []) or []
                if not choices:
                    continue
                message = _value(choices[0], "message", {}) or {}
                chunks = _value(message, "tool_calls", None)
                if chunks:
                    self._merge_tool_calls(tool_calls, chunks)
                content = _value(message, "content", "") or ""
                if content:
                    assistant_content += content
                    if not tool_calls:
                        remaining = self.max_answer_chars - emitted_chars
                        if remaining > 0:
                            delta = content[:remaining]
                            emitted_chars += len(delta)
                            yield delta

            if not tool_calls:
                if emitted_chars == 0:
                    raise UpstreamError("llm")
                return
            if assistant_content:
                raise RuntimeError("LLM returned both streamed content and tool calls")
            if tool_round >= self.max_tool_rounds:
                raise RuntimeError("maximum tool rounds exceeded")

            normalized_calls = [tool_calls[index] for index in sorted(tool_calls)]
            for index, call in enumerate(normalized_calls):
                if not call["id"]:
                    call["id"] = f"local_tool_call_{tool_round}_{index}"
                if not call["function"]["name"]:
                    raise RuntimeError("LLM returned a tool call without a name")
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": normalized_calls,
                }
            )
            for call in normalized_calls:
                name = call["function"]["name"]
                result = self.tool_registry.execute(
                    name, call["function"]["arguments"]
                )
                emit_tool_event(name, json.loads(result))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    }
                )
                logger.info("Tool invoked tool=%s round=%d", name, tool_round + 1)

        raise RuntimeError("tool conversation ended unexpectedly")

    def iter_answer_deltas(
        self, transcript: str, history: list[dict[str, str]]
    ):
        deterministic_request = deterministic_tool_request(transcript)
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            emitted = 0
            try:
                if deterministic_request is not None:
                    deltas = self._run_deterministic_tool(transcript, history, deterministic_request)
                else:
                    deltas = self._run_conversation(self._messages(transcript, history))
                for delta in deltas:
                    emitted += len(delta)
                    yield delta
                logger.info(
                    "Streaming stage completed stage=llm tools=%s chars=%d duration_ms=%d",
                    "deterministic" if deterministic_request is not None else "model",
                    emitted,
                    int((time.monotonic() - started) * 1000),
                )
                return
            except Exception as exc:
                if emitted > 0 or attempt >= self.max_attempts or not self._retryable(exc):
                    raise
                logger.warning(
                    "Function calling retry attempt=%d error_type=%s",
                    attempt,
                    type(exc).__name__,
                )
                time.sleep(self.retry_delay * attempt)


# Keep the existing import name used by conversation_streaming.py.
DashScopeStreamingPipeline = DashScopeToolsPipeline
