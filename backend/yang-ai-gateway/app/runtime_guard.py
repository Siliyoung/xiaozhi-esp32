"""Runtime limits and privacy-preserving persistent usage counters."""

import asyncio
import hashlib
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class GuardConfig:
    max_sessions_total: int
    max_sessions_per_device: int
    turns_per_minute_per_device: int
    daily_turns_per_device: int
    daily_turns_total: int
    max_turns_per_session: int
    session_max_seconds: int
    usage_db_path: str

    @classmethod
    def from_environment(cls) -> "GuardConfig":
        return cls(
            max_sessions_total=_positive_int("MAX_SESSIONS_TOTAL", 4),
            max_sessions_per_device=_positive_int("MAX_SESSIONS_PER_DEVICE", 1),
            turns_per_minute_per_device=_positive_int(
                "TURNS_PER_MINUTE_PER_DEVICE", 10
            ),
            daily_turns_per_device=_positive_int("DAILY_TURNS_PER_DEVICE", 200),
            daily_turns_total=_positive_int("DAILY_TURNS_TOTAL", 300),
            max_turns_per_session=_positive_int("MAX_TURNS_PER_SESSION", 30),
            session_max_seconds=_positive_int("SESSION_MAX_SECONDS", 900),
            usage_db_path=os.getenv("USAGE_DB_PATH", "data/usage.db"),
        )


def device_key(device_id: str, client_id: str = "") -> str:
    """Return a stable non-reversible label so raw identifiers are not logged."""
    identity = f"{device_id}\x00{client_id}".encode("utf-8", errors="replace")
    return hashlib.sha256(identity).hexdigest()[:16]


@dataclass(frozen=True)
class TurnDecision:
    allowed: bool
    reason: str
    device_daily_turns: int
    total_daily_turns: int


class UsageStore:
    """Atomic daily counters; no transcripts, audio, tokens, or raw IDs."""

    def __init__(self, path: str, config: GuardConfig) -> None:
        self.path = path
        self.config = config
        self._lock = threading.Lock()
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _day() -> str:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

    def check_ready(self) -> bool:
        try:
            with self._lock, self._connect() as connection:
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
            with self._connect() as connection:
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


class SessionLimiter:
    def __init__(self, config: GuardConfig) -> None:
        self.config = config
        self._lock = asyncio.Lock()
        self._total = 0
        self._per_device: dict[str, int] = defaultdict(int)

    async def acquire(self, key: str) -> tuple[bool, str]:
        async with self._lock:
            if self._total >= self.config.max_sessions_total:
                return False, "global_session_limit"
            if self._per_device[key] >= self.config.max_sessions_per_device:
                return False, "device_session_limit"
            self._total += 1
            self._per_device[key] += 1
            return True, "ok"

    async def release(self, key: str) -> None:
        async with self._lock:
            if self._per_device.get(key, 0) > 0:
                self._per_device[key] -= 1
                self._total -= 1
                if self._per_device[key] == 0:
                    del self._per_device[key]
