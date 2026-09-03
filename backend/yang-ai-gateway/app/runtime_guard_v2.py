"""Leak-free UsageStore implementation for the runtime guard."""

import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path

from app.runtime_guard import GuardConfig, SessionLimiter, TurnDecision, device_key


class UsageStore:
    """Atomic daily counters with explicitly closed SQLite connections."""

    def __init__(self, path: str, config: GuardConfig) -> None:
        self.path = path
        self.config = config
        self._lock = threading.Lock()
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_turns (
                    day TEXT NOT NULL,
                    device_key TEXT NOT NULL,
                    turns INTEGER NOT NULL,
                    PRIMARY KEY (day, device_key)
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _day() -> str:
        return __import__("datetime").datetime.now(
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
        ).date().isoformat()

    def check_ready(self) -> bool:
        try:
            with self._lock, closing(self._connect()) as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def reserve_turn(self, key: str) -> TurnDecision:
        now = time.monotonic()
        cutoff = now - 60
        with self._lock:
            recent = self._recent[key]
            while recent and recent[0] <= cutoff:
                recent.popleft()
            if len(recent) >= self.config.turns_per_minute_per_device:
                return TurnDecision(False, "minute_limit", 0, 0)

            day = self._day()
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                device_turns = connection.execute(
                    "SELECT turns FROM daily_turns WHERE day=? AND device_key=?",
                    (day, key),
                ).fetchone()
                total_turns = connection.execute(
                    "SELECT COALESCE(SUM(turns), 0) FROM daily_turns WHERE day=?",
                    (day,),
                ).fetchone()
                device_count = int(device_turns[0]) if device_turns else 0
                total_count = int(total_turns[0])

                if device_count >= self.config.daily_turns_per_device:
                    connection.rollback()
                    return TurnDecision(
                        False, "device_daily_limit", device_count, total_count
                    )
                if total_count >= self.config.daily_turns_total:
                    connection.rollback()
                    return TurnDecision(
                        False, "global_daily_limit", device_count, total_count
                    )

                connection.execute(
                    """
                    INSERT INTO daily_turns(day, device_key, turns) VALUES (?, ?, 1)
                    ON CONFLICT(day, device_key)
                    DO UPDATE SET turns=turns + 1
                    """,
                    (day, key),
                )
                connection.commit()

            recent.append(now)
            return TurnDecision(True, "ok", device_count + 1, total_count + 1)


__all__ = [
    "GuardConfig",
    "SessionLimiter",
    "TurnDecision",
    "UsageStore",
    "device_key",
]
