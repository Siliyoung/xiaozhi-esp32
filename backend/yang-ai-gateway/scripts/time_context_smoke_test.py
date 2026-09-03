"""Verify that the LLM receives current Beijing date context."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.dashscope_pipeline_v2 import DashScopePipeline


def main() -> None:
    pipeline = DashScopePipeline()
    answer = pipeline.generate_answer("今天是星期几？", [])
    weekday = "一二三四五六日"[datetime.now(ZoneInfo("Asia/Shanghai")).weekday()]
    assert f"星期{weekday}" in answer, (weekday, answer)
    print("time-context-ok", repr(answer))


if __name__ == "__main__":
    main()
