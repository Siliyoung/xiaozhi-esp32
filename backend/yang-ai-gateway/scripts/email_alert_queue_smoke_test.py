"""Deterministic outbox, idempotency, and delivery test without network."""

import asyncio
import os
import sqlite3
import tempfile

from app.email_alerting import EmailAlertConfig, EmailAlertManager


class FakeSender:
    def __init__(self) -> None:
        self.messages = []

    def send(self, subject: str, body: str) -> None:
        self.messages.append((subject, body))


async def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "usage.db")
        sqlite3.connect(db_path).close()
        config = EmailAlertConfig(
            enabled=True,
            host="smtp.qq.com",
            port=465,
            username="test@qq.com",
            password="not-a-real-secret",
            sender="test@qq.com",
            recipient="test@qq.com",
            thresholds=(80, 95, 100),
            include_device_alerts=True,
            poll_seconds=2,
            max_attempts=5,
            smtp_timeout_seconds=10,
        )
        manager = EmailAlertManager(db_path, config)
        fake = FakeSender()
        manager.sender = fake
        manager.enqueue_thresholds("2026-08-24", "global", 239, 300)
        assert not await manager.send_pending_once()
        manager.enqueue_thresholds("2026-08-24", "global", 240, 300)
        manager.enqueue_thresholds("2026-08-24", "global", 241, 300)
        assert await manager.send_pending_once()
        assert len(fake.messages) == 1
        assert "80%" in fake.messages[0][0]
        assert not await manager.send_pending_once()

    print("email-alert-queue-ok durable=true idempotent=true async_delivery=true")


if __name__ == "__main__":
    asyncio.run(main())
