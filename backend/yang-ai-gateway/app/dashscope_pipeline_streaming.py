"""Protected DashScope pipeline with incremental Qwen text generation."""

import logging
import os
import time
from datetime import datetime
from http import HTTPStatus
from zoneinfo import ZoneInfo

from dashscope import Generation

from app.dashscope_pipeline_protected import (
    DashScopePipeline as ProtectedPipeline,
    UpstreamError,
)


logger = logging.getLogger("yang-ai-gateway.streaming-upstream")


class DashScopeStreamingPipeline(ProtectedPipeline):
    def iter_answer_deltas(
        self, transcript: str, history: list[dict[str, str]]
    ):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        weekday = "一二三四五六日"[now.weekday()]
        time_context = now.strftime("当前北京时间：%Y年%m月%d日 %H:%M")
        time_context += f"，星期{weekday}。"
        messages = [
            {"role": "system", "content": f"{self.system_prompt}\n{time_context}"},
            *history[-8:],
            {"role": "user", "content": transcript},
        ]

        for attempt in range(1, self.max_attempts + 1):
            emitted_chars = 0
            started = time.monotonic()
            try:
                responses = Generation.call(
                    model=self.llm_model,
                    messages=messages,
                    result_format="message",
                    max_tokens=self.llm_max_tokens,
                    temperature=0.7,
                    enable_thinking=False,
                    stream=True,
                    incremental_output=True,
                    request_timeout=self.llm_timeout,
                )
                for response in responses:
                    if response.status_code != HTTPStatus.OK:
                        raise UpstreamError("llm", int(response.status_code))
                    choices = getattr(response.output, "choices", None) or []
                    if not choices:
                        continue
                    content = choices[0].message.content or ""
                    if not content:
                        continue
                    remaining = self.max_answer_chars - emitted_chars
                    if remaining <= 0:
                        break
                    delta = content[:remaining]
                    emitted_chars += len(delta)
                    yield delta
                    if emitted_chars >= self.max_answer_chars:
                        break
                if emitted_chars == 0:
                    raise UpstreamError("llm")
                logger.info(
                    "Streaming stage completed stage=llm chars=%d duration_ms=%d",
                    emitted_chars,
                    int((time.monotonic() - started) * 1000),
                )
                return
            except Exception as exc:
                if (
                    emitted_chars > 0
                    or attempt >= self.max_attempts
                    or not self._retryable(exc)
                ):
                    raise
                logger.warning(
                    "Streaming upstream retry stage=llm attempt=%d error_type=%s",
                    attempt,
                    type(exc).__name__,
                )
                time.sleep(self.retry_delay * attempt)
