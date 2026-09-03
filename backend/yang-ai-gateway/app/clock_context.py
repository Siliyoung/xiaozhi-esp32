"""Cached city-level weather context for the device standby clock."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any

from app.location_context import (
    reset_client_public_ip,
    resolve_current_location,
    set_client_public_ip,
)
from app.read_only_tools import get_current_weather


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = max(300, min(int(os.getenv("CLOCK_WEATHER_TTL_SECONDS", "1800")), 7200))


def _key(public_ip: str) -> str:
    return hashlib.sha256(public_ip.encode("ascii")).hexdigest()


def build_clock_context(public_ip: str) -> dict[str, Any]:
    """Resolve weather without returning or logging the device public IP."""
    cache_key = _key(public_ip)
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])

    token = set_client_public_ip(public_ip)
    try:
        location = resolve_current_location()
        result: dict[str, Any] = {
            "city": location["city"],
            "condition": "--",
            "temperature_c": None,
            "humidity_percent": None,
            "source": "Open-Meteo",
            "location_accuracy": "city-level",
        }
        try:
            weather = get_current_weather({})
            if weather.get("found"):
                result.update(
                    {
                        "city": weather.get("location") or location["city"],
                        "condition": weather.get("condition") or "--",
                        "temperature_c": weather.get("temperature_c"),
                        "humidity_percent": weather.get("humidity_percent"),
                    }
                )
        except Exception:
            # City and the local clock remain useful when the weather provider is down.
            pass
    finally:
        reset_client_public_ip(token)

    with _LOCK:
        _CACHE[cache_key] = (now + _TTL_SECONDS, dict(result))
    return result


def clock_context_from_weather(weather: dict[str, Any]) -> dict[str, Any]:
    """Normalize a successful weather tool result for an already-open device session."""
    return {
        "city": str(weather.get("location") or "--"),
        "condition": str(weather.get("condition") or "--"),
        "temperature_c": weather.get("temperature_c"),
        "humidity_percent": weather.get("humidity_percent"),
        "source": str(weather.get("source") or "Open-Meteo"),
        "location_accuracy": str(weather.get("location_accuracy") or "city-level"),
    }
