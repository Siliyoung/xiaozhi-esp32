"""Validated Pomodoro commands and a lightweight per-device state mirror."""

from __future__ import annotations

import threading
import time
from typing import Any

from app.device_context import current_device_key


_LOCK = threading.Lock()
_TIMERS: dict[str, dict[str, Any]] = {}

def has_active_pomodoro() -> bool:
    """Return whether the current device has a timer that can be cancelled."""
    with _LOCK:
        timer = _TIMERS.get(current_device_key())
        if timer is None:
            return False
        return timer.get("state") in ("running", "paused", "finished")



def _remaining(timer: dict[str, Any], now: float) -> int:
    if timer["state"] == "running":
        return max(0, int(timer["deadline"] - now + 0.999))
    return int(timer.get("remaining_seconds", 0))


def _public_state(timer: dict[str, Any], now: float) -> dict[str, Any]:
    remaining = _remaining(timer, now)
    state = timer["state"]
    if state == "running" and remaining == 0:
        state = "finished"
        timer["state"] = state
    return {
        "state": state,
        "remaining_seconds": remaining,
        "total_seconds": timer["total_seconds"],
        "label": timer["label"],
    }


def start_pomodoro(arguments: dict[str, Any]) -> dict[str, Any]:
    minutes = int(arguments.get("duration_minutes", 25))
    if minutes < 1 or minutes > 180:
        raise ValueError("duration_minutes must be between 1 and 180")
    label = str(arguments.get("label") or "番茄钟").strip()[:40] or "番茄钟"
    duration_seconds = minutes * 60
    now = time.monotonic()
    timer = {
        "state": "running",
        "deadline": now + duration_seconds,
        "remaining_seconds": duration_seconds,
        "total_seconds": duration_seconds,
        "label": label,
    }
    with _LOCK:
        _TIMERS[current_device_key()] = timer
    return {
        **_public_state(timer, now),
        "device_command": {
            "action": "start",
            "duration_seconds": duration_seconds,
            "label": label,
        },
    }


def control_pomodoro(arguments: dict[str, Any]) -> dict[str, Any]:
    action = arguments["action"]
    device_key = current_device_key()
    now = time.monotonic()
    with _LOCK:
        timer = _TIMERS.get(device_key)
        if timer is None:
            if action in ("cancel", "show", "status"):
                return {
                    "state": "idle",
                    "remaining_seconds": 0,
                    "device_command": {"action": action},
                }
            raise ValueError("there is no active Pomodoro timer")

        remaining = _remaining(timer, now)
        if action == "pause":
            if timer["state"] != "running" or remaining <= 0:
                raise ValueError("Pomodoro timer is not running")
            timer["state"] = "paused"
            timer["remaining_seconds"] = remaining
        elif action == "resume":
            if timer["state"] != "paused" or remaining <= 0:
                raise ValueError("Pomodoro timer is not paused")
            timer["state"] = "running"
            timer["deadline"] = now + remaining
        elif action == "cancel":
            _TIMERS.pop(device_key, None)
            return {
                "state": "idle",
                "remaining_seconds": 0,
                "device_command": {"action": "cancel"},
            }
        elif action not in ("show", "status"):
            raise ValueError("unsupported Pomodoro action")

        return {
            **_public_state(timer, now),
            "device_command": {"action": "show" if action == "status" else action},
        }
