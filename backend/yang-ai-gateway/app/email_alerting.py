"""Durable, asynchronous quota alerts delivered over SMTP."""

import asyncio
import logging
import math
import os
import smtplib
import sqlite3
import ssl
import time
from contextlib import closing
from dataclasses import dataclass
from email.message import EmailMessage


logger = logging.getLogger("yang-ai-gateway.email-alert")


def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EmailAlertConfig:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str
    thresholds: tuple[int, ...]
    include_device_alerts: bool
    poll_seconds: float
    max_attempts: int
    smtp_timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "EmailAlertConfig":
        enabled = _enabled("EMAIL_ALERT_ENABLED")
        raw_thresholds = os.getenv("ALERT_THRESHOLDS", "80,95,100")
        thresholds = tuple(
            sorted({int(item.strip()) for item in raw_thresholds.split(",") if item.strip()})
        )
        if not thresholds or any(value < 1 or value > 100 for value in thresholds):
            raise ValueError("ALERT_THRESHOLDS must contain percentages from 1 to 100")
        config = cls(
            enabled=enabled,
            host=os.getenv("SMTP_HOST", "smtp.qq.com"),
            port=int(os.getenv("SMTP_PORT", "465")),
            username=os.getenv("SMTP_USERNAME", ""),
            password=os.getenv("SMTP_PASSWORD", ""),
            sender=os.getenv("ALERT_EMAIL_FROM", os.getenv("SMTP_USERNAME", "")),
            recipient=os.getenv("ALERT_EMAIL_TO", ""),
            thresholds=thresholds,
            include_device_alerts=_enabled("ALERT_INCLUDE_DEVICE", True),
            poll_seconds=max(2.0, float(os.getenv("ALERT_POLL_SECONDS", "10"))),
            max_attempts=max(1, min(int(os.getenv("ALERT_MAX_ATTEMPTS", "5")), 10)),
            smtp_timeout_seconds=max(
                5.0, float(os.getenv("SMTP_TIMEOUT_SECONDS", "20"))
            ),
        )
        if config.enabled:
            required = {
                "SMTP_USERNAME": config.username,
                "SMTP_PASSWORD": config.password,
                "ALERT_EMAIL_FROM": config.sender,
                "ALERT_EMAIL_TO": config.recipient,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise RuntimeError(
                    "Email alerts enabled but configuration is missing: "
                    + ", ".join(missing)
                )
        return config


class EmailSender:
    def __init__(self, config: EmailAlertConfig) -> None:
        self.config = config

    def send(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.sender
        message["To"] = self.config.recipient
        message.set_content(body)
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            self.config.host,
            self.config.port,
            timeout=self.config.smtp_timeout_seconds,
            context=context,
        ) as smtp:
            smtp.login(self.config.username, self.config.password)
            smtp.send_message(message)


class EmailAlertManager:
    def __init__(self, db_path: str, config: EmailAlertConfig) -> None:
        self.db_path = db_path
        self.config = config
        self.sender = EmailSender(config)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS email_alert_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    threshold INTEGER NOT NULL,
                    current_value INTEGER NOT NULL,
                    limit_value INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    last_error_type TEXT,
                    created_at INTEGER NOT NULL,
                    sent_at INTEGER,
                    UNIQUE(day, scope, threshold)
                )
                """
            )
            connection.execute(
                "UPDATE email_alert_outbox SET status='pending' WHERE status='sending'"
            )
            connection.commit()

    def enqueue_thresholds(
        self, day: str, scope: str, current_value: int, limit_value: int
    ) -> None:
        if not self.config.enabled:
            return
        now = int(time.time())
        with closing(self._connect()) as connection:
            for threshold in self.config.thresholds:
                trigger_value = math.ceil(limit_value * threshold / 100)
                if current_value < trigger_value:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO email_alert_outbox(
                        day, scope, threshold, current_value, limit_value, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (day, scope, threshold, current_value, limit_value, now),
                )
            connection.commit()

    def _claim_one(self) -> sqlite3.Row | None:
        now = int(time.time())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM email_alert_outbox
                WHERE status='pending' AND next_attempt_at <= ?
                ORDER BY id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE email_alert_outbox SET status='sending' WHERE id=?",
                (row["id"],),
            )
            connection.commit()
            return row

    @staticmethod
    def _content(row: sqlite3.Row) -> tuple[str, str]:
        scope_label = "全站" if row["scope"] == "global" else "单设备"
        remaining = max(0, row["limit_value"] - row["current_value"])
        subject = f"[Yang AI] {scope_label}每日调用量达到 {row['threshold']}%"
        body = (
            "Yang AI 后端调用量提醒\n\n"
            f"日期：{row['day']}\n"
            f"范围：{scope_label}\n"
            f"当前调用：{row['current_value']} / {row['limit_value']}\n"
            f"剩余调用：{remaining}\n"
            f"触发阈值：{row['threshold']}%\n"
            "服务状态：运行中\n\n"
            "此邮件不包含对话正文、语音或原始设备标识。"
        )
        return subject, body

    def _mark_sent(self, event_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE email_alert_outbox SET status='sent', sent_at=? WHERE id=?",
                (int(time.time()), event_id),
            )
            connection.commit()

    def _mark_failed(self, row: sqlite3.Row, exc: Exception) -> None:
        attempts = int(row["attempts"]) + 1
        terminal = attempts >= self.config.max_attempts
        delay = min(3600, 60 * (2 ** max(0, attempts - 1)))
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE email_alert_outbox
                SET status=?, attempts=?, next_attempt_at=?, last_error_type=?
                WHERE id=?
                """,
                (
                    "failed" if terminal else "pending",
                    attempts,
                    int(time.time()) + delay,
                    type(exc).__name__[:80],
                    row["id"],
                ),
            )
            connection.commit()

    async def send_pending_once(self) -> bool:
        if not self.config.enabled:
            return False
        row = await asyncio.to_thread(self._claim_one)
        if row is None:
            return False
        subject, body = self._content(row)
        try:
            await asyncio.to_thread(self.sender.send, subject, body)
            await asyncio.to_thread(self._mark_sent, int(row["id"]))
            logger.info(
                "Email alert sent scope=%s threshold=%d day=%s",
                row["scope"],
                row["threshold"],
                row["day"],
            )
        except Exception as exc:
            await asyncio.to_thread(self._mark_failed, row, exc)
            logger.warning(
                "Email alert failed scope=%s threshold=%d error_type=%s",
                row["scope"],
                row["threshold"],
                type(exc).__name__,
            )
        return True

    async def _worker(self) -> None:
        while not self._stopping.is_set():
            sent_or_failed = await self.send_pending_once()
            if sent_or_failed:
                continue
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.config.poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("Email alerts disabled")
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._worker(), name="email-alert-worker")
        logger.info(
            "Email alerts enabled recipient=%s thresholds=%s",
            self.config.recipient,
            ",".join(str(value) for value in self.config.thresholds),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
