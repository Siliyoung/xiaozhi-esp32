"""Local deterministic checks for persistent quota and session limits."""

import asyncio
import os
import tempfile

from app.runtime_guard import GuardConfig, SessionLimiter, UsageStore, device_key


async def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        config = GuardConfig(
            max_sessions_total=2,
            max_sessions_per_device=1,
            turns_per_minute_per_device=2,
            daily_turns_per_device=3,
            daily_turns_total=4,
            max_turns_per_session=5,
            session_max_seconds=60,
            usage_db_path=os.path.join(temp_dir, "usage.db"),
        )
        key_a = device_key("device-a", "client-a")
        key_b = device_key("device-b", "client-b")
        assert key_a != "device-a"

        limiter = SessionLimiter(config)
        assert await limiter.acquire(key_a) == (True, "ok")
        assert await limiter.acquire(key_a) == (False, "device_session_limit")
        assert await limiter.acquire(key_b) == (True, "ok")
        assert await limiter.acquire("third") == (False, "global_session_limit")
        await limiter.release(key_a)
        await limiter.release(key_b)

        store = UsageStore(config.usage_db_path, config)
        assert store.check_ready()
        assert store.reserve_turn(key_a).allowed
        assert store.reserve_turn(key_a).allowed
        assert store.reserve_turn(key_a).reason == "minute_limit"

        # A new process loses the minute window but retains daily counters.
        restarted = UsageStore(config.usage_db_path, config)
        third = restarted.reserve_turn(key_a)
        assert third.allowed and third.device_daily_turns == 3
        assert restarted.reserve_turn(key_a).reason == "device_daily_limit"
        assert restarted.reserve_turn(key_b).allowed
        assert restarted.reserve_turn("device-c").reason == "global_daily_limit"

    print("runtime-guard-ok persistence=true privacy_hash=true limits=true")


if __name__ == "__main__":
    asyncio.run(main())
