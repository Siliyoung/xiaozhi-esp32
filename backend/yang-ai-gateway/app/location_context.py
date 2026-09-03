"""Per-session, city-level location derived from the device public IP."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Any


_CLIENT_IP: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "client_public_ip", default=None
)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()

LOCATION_TIMEOUT_SECONDS = max(
    1.0, min(float(os.getenv("LOCATION_TIMEOUT_SECONDS", "5")), 15.0)
)
LOCATION_CACHE_TTL_SECONDS = max(
    300, min(int(os.getenv("LOCATION_CACHE_TTL_SECONDS", "3600")), 86400)
)


def _configured_location() -> dict[str, Any] | None:
    """Return an optional city-level location configured by the device owner."""
    city = os.getenv("DEVICE_LOCATION_CITY", "").strip()
    if not city:
        return None

    latitude_text = os.getenv("DEVICE_LOCATION_LATITUDE", "").strip()
    longitude_text = os.getenv("DEVICE_LOCATION_LONGITUDE", "").strip()
    if not latitude_text or not longitude_text:
        raise RuntimeError(
            "DEVICE_LOCATION_LATITUDE and DEVICE_LOCATION_LONGITUDE are required "
            "when DEVICE_LOCATION_CITY is configured"
        )
    try:
        latitude = float(latitude_text)
        longitude = float(longitude_text)
    except ValueError as exc:
        raise RuntimeError("configured device coordinates are invalid") from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise RuntimeError("configured device coordinates are out of range")

    return {
        "city": city,
        "region": os.getenv("DEVICE_LOCATION_REGION", "").strip(),
        "country": os.getenv("DEVICE_LOCATION_COUNTRY", "China").strip() or "China",
        "country_code": os.getenv("DEVICE_LOCATION_COUNTRY_CODE", "CN").strip() or "CN",
        "latitude": latitude,
        "longitude": longitude,
        "timezone": os.getenv("DEVICE_LOCATION_TIMEZONE", "Asia/Shanghai").strip()
        or "Asia/Shanghai",
        "accuracy": "configured-city",
        "source": "configuration",
    }


def set_client_public_ip(ip_address: str | None) -> contextvars.Token:
    """Set an ephemeral client IP for the current conversation context."""
    return _CLIENT_IP.set(ip_address)


def reset_client_public_ip(token: contextvars.Token) -> None:
    _CLIENT_IP.reset(token)


def _cache_key(ip_address: str) -> str:
    return hashlib.sha256(ip_address.encode("ascii")).hexdigest()


def _normalized_location(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("success"):
        raise RuntimeError("IP location lookup failed")
    timezone_value = payload.get("timezone") or {}
    timezone_name = (
        timezone_value.get("id") if isinstance(timezone_value, dict) else timezone_value
    )
    required = ("city", "country", "latitude", "longitude")
    if any(payload.get(field) in (None, "") for field in required) or not timezone_name:
        raise RuntimeError("IP location response is incomplete")
    return {
        "city": str(payload["city"]),
        "region": str(payload.get("region") or ""),
        "country": str(payload["country"]),
        "country_code": str(payload.get("country_code") or ""),
        "latitude": float(payload["latitude"]),
        "longitude": float(payload["longitude"]),
        "timezone": str(timezone_name),
        "accuracy": "city-level",
        "source": "ipwho.is",
    }


def resolve_current_location() -> dict[str, Any]:
    """Resolve and cache the current device location without returning its IP."""
    configured = _configured_location()
    if configured is not None:
        return configured

    ip_address = _CLIENT_IP.get()
    if not ip_address:
        raise RuntimeError("device public IP is unavailable")

    key = _cache_key(ip_address)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return dict(cached[1])
        if cached:
            _CACHE.pop(key, None)

    fields = (
        "success,city,region,country,country_code,latitude,longitude,timezone"
    )
    url = (
        "https://ipwho.is/"
        f"{urllib.parse.quote(ip_address, safe='')}?fields={fields}&lang=zh-CN"
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": "yang-ai-gateway/1.0"}
    )
    with urllib.request.urlopen(request, timeout=LOCATION_TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise RuntimeError(f"IP location HTTP {response.status}")
        payload = json.loads(response.read(64_000).decode("utf-8"))
    location = _normalized_location(payload)
    with _CACHE_LOCK:
        _CACHE[key] = (now + LOCATION_CACHE_TTL_SECONDS, location)
    return dict(location)
