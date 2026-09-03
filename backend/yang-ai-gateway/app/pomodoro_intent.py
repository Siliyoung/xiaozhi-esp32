"""Deterministic routing for short, safety-critical Pomodoro controls."""

from __future__ import annotations

import re


_CANCEL_WORDS = ("取消", "停止", "结束", "关掉", "关闭", "不要了")
_TIMER_WORDS = ("番茄钟", "番茄", "倒计时", "计时器", "专注计时")
_SHORT_CANCEL_PHRASES = {
    "取消", "取消吧", "取消它", "取消它吧",
    "停止", "停止吧", "结束", "结束吧",
    "关掉", "关掉吧", "关闭", "不要了",
}



def is_direct_cancel(transcript: str, timer_active: bool) -> bool:
    """Recognize explicit timer cancellation and short follow-up cancellation."""
    normalized = re.sub(r"[\s，。！？、,.!?]", "", transcript).lower()
    if not normalized or not any(word in normalized for word in _CANCEL_WORDS):
        return False
    if any(word in normalized for word in _TIMER_WORDS):
        return True
    return timer_active and normalized in _SHORT_CANCEL_PHRASES
