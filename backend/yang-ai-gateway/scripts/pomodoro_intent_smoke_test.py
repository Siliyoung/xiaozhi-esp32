"""Offline checks for deterministic Pomodoro cancellation routing."""

from app.pomodoro_intent import is_direct_cancel


assert is_direct_cancel("取消番茄钟", False)
assert is_direct_cancel("把这个倒计时关掉", False)
assert is_direct_cancel("取消它吧", True)
assert is_direct_cancel("停止", True)
assert not is_direct_cancel("取消明天的会议", True)
assert not is_direct_cancel("停止播放音乐", False)
assert not is_direct_cancel("番茄钟还有多久", True)

print("pomodoro-intent-smoke-ok explicit=true followup=true false_positive=false")
