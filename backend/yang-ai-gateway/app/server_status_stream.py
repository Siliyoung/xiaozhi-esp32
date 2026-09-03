"""Live server-metric updates for an active device dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from fastapi import WebSocket

from app.read_only_tools import get_server_status


logger = logging.getLogger("yang-ai-gateway.server-status-stream")
METRIC_KEYS = (
    "sampled_at",
    "cpu_used_percent",
    "disk_used_percent",
    "network_rx_kbps",
    "network_tx_kbps",
)


class ServerStatusStream:
    def __init__(self, websocket: WebSocket, session_id: str, safe_device: str) -> None:
        self.websocket = websocket
        self.session_id = session_id
        self.safe_device = safe_device
        self.interval_seconds = max(
            1.0, min(float(os.getenv("SERVER_STATUS_INTERVAL_SECONDS", "2")), 10.0)
        )
        self.active = False
        self._task: asyncio.Task | None = None

    @staticmethod
    def _dashboard_data(data: dict[str, Any]) -> dict[str, Any]:
        return {key: data.get(key) for key in METRIC_KEYS}

    async def _send(self, data: dict[str, Any], state: str = "update") -> None:
        await self.websocket.send_text(
            json.dumps(
                {
                    "type": "server_status",
                    "session_id": self.session_id,
                    "state": state,
                    "data": self._dashboard_data(data),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    async def start(self, initial_data: dict[str, Any]) -> None:
        self.active = True
        await self._send(initial_data)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name=f"server-status-{self.session_id}"
            )
        logger.info(
            "Live dashboard started device=%s session=%s interval_s=%.1f",
            self.safe_device,
            self.session_id,
            self.interval_seconds,
        )

    async def enter_passive_mode(self) -> None:
        if self.active:
            await self.websocket.send_text(
                json.dumps(
                    {
                        "type": "server_status",
                        "session_id": self.session_id,
                        "state": "active",
                    },
                    separators=(",", ":"),
                )
            )

    async def _run(self) -> None:
        try:
            while self.active:
                await asyncio.sleep(self.interval_seconds)
                if not self.active:
                    break
                data = await asyncio.to_thread(get_server_status, {})
                await self._send(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.active = False
            logger.warning(
                "Live dashboard stopped unexpectedly device=%s session=%s error_type=%s",
                self.safe_device,
                self.session_id,
                type(exc).__name__,
            )

    async def stop(self, notify_device: bool = False) -> None:
        was_active = self.active
        self.active = False
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if was_active and notify_device:
            await self.websocket.send_text(
                json.dumps(
                    {
                        "type": "server_status",
                        "session_id": self.session_id,
                        "state": "stop",
                    },
                    separators=(",", ":"),
                )
            )
        if was_active:
            logger.info(
                "Live dashboard stopped device=%s session=%s",
                self.safe_device,
                self.session_id,
            )
