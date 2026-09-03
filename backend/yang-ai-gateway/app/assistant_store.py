"""SQLite persistence for the personal-assistant application layer."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_DB_PATH = os.getenv(
    "ASSISTANT_DB_PATH", "/var/lib/yang-ai-gateway/assistant.db"
)


class AssistantStore:
    """Small thread-safe SQLite repository scoped by device key."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or DEFAULT_DB_PATH
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db_path = Path(self.path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._ensure_schema(connection)
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    due_at INTEGER,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_todos_device_status
                    ON todos(device_key, status, created_at);
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'reminder',
                    trigger_at INTEGER NOT NULL,
                    local_epoch INTEGER NOT NULL,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    created_at INTEGER NOT NULL,
                    cancelled_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_device_status_time
                    ON reminders(device_key, status, trigger_at);
                """
            )
            self._schema_ready = True

    @staticmethod
    def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def create_todo(
        self, device_key: str, title: str, due_at: int | None, priority: str
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO todos
                   (device_key, title, due_at, priority, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (device_key, title, due_at, priority, now),
            )
            row = connection.execute(
                "SELECT * FROM todos WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def list_todos(
        self, device_key: str, status: str = "pending", limit: int = 20
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM todos WHERE device_key=?"
        values: list[Any] = [device_key]
        if status != "all":
            query += " AND status=?"
            values.append(status)
        query += " ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, created_at LIMIT ?"
        values.append(limit)
        with self._connection() as connection:
            rows = connection.execute(query, values).fetchall()
        return self._rows(rows)

    def resolve_todo(
        self, device_key: str, todo_id: int | None, title: str | None
    ) -> dict[str, Any]:
        with self._connection() as connection:
            if todo_id is not None:
                row = connection.execute(
                    "SELECT * FROM todos WHERE id=? AND device_key=?",
                    (todo_id, device_key),
                ).fetchone()
            elif title:
                rows = connection.execute(
                    """SELECT * FROM todos
                       WHERE device_key=? AND status='pending' AND title LIKE ?
                       ORDER BY created_at DESC LIMIT 2""",
                    (device_key, f"%{title}%"),
                ).fetchall()
                if len(rows) > 1:
                    raise ValueError("multiple pending todos match that title")
                row = rows[0] if rows else None
            else:
                raise ValueError("todo_id or title is required")
        if row is None:
            raise ValueError("todo was not found")
        return dict(row)

    def complete_todo(
        self, device_key: str, todo_id: int | None, title: str | None
    ) -> dict[str, Any]:
        todo = self.resolve_todo(device_key, todo_id, title)
        if todo["status"] == "completed":
            return todo
        completed_at = int(time.time())
        with self._connection() as connection:
            connection.execute(
                "UPDATE todos SET status='completed', completed_at=? WHERE id=? AND device_key=?",
                (completed_at, todo["id"], device_key),
            )
            row = connection.execute(
                "SELECT * FROM todos WHERE id=?", (todo["id"],)
            ).fetchone()
        return dict(row)

    def delete_todo(
        self, device_key: str, todo_id: int | None, title: str | None
    ) -> dict[str, Any]:
        todo = self.resolve_todo(device_key, todo_id, title)
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM todos WHERE id=? AND device_key=?",
                (todo["id"], device_key),
            )
        return todo

    def create_reminder(
        self,
        device_key: str,
        title: str,
        kind: str,
        trigger_at: int,
        local_epoch: int,
        timezone: str,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO reminders
                   (device_key, title, kind, trigger_at, local_epoch, timezone, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?)""",
                (device_key, title, kind, trigger_at, local_epoch, timezone, now),
            )
            row = connection.execute(
                "SELECT * FROM reminders WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def list_reminders(
        self, device_key: str, status: str = "scheduled", limit: int = 20
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM reminders WHERE device_key=?"
        values: list[Any] = [device_key]
        if status != "all":
            query += " AND status=?"
            values.append(status)
        query += " ORDER BY trigger_at LIMIT ?"
        values.append(limit)
        with self._connection() as connection:
            connection.execute(
                "UPDATE reminders SET status='fired' "
                "WHERE device_key=? AND status='scheduled' AND trigger_at<=?",
                (device_key, int(time.time())),
            )
            rows = connection.execute(query, values).fetchall()
        return self._rows(rows)

    def cancel_reminder(self, device_key: str, reminder_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE id=? AND device_key=?",
                (reminder_id, device_key),
            ).fetchone()
            if row is None:
                raise ValueError("reminder was not found")
            if row["status"] == "scheduled":
                connection.execute(
                    "UPDATE reminders SET status='cancelled', cancelled_at=? WHERE id=?",
                    (int(time.time()), reminder_id),
                )
        result = dict(row)
        result["status"] = "cancelled"
        return result
