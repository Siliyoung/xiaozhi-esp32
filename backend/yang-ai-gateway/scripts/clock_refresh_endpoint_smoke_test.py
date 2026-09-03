"""Offline smoke test for the lightweight standby-clock refresh endpoint."""

import asyncio
import os

os.environ.setdefault("DEVICE_TOKEN", "x" * 32)

from fastapi import HTTPException

from app import main


class FakeClient:
    host = "203.0.113.8"


class FakeRequest:
    headers = {"x-real-ip": "8.8.8.8"}
    client = FakeClient()


class FakeRequestWithoutPublicIp:
    headers = {}
    client = None


calls = {"count": 0, "ip": None}


def fake_build_clock_context(public_ip: str | None) -> dict:
    if public_ip is None:
        raise RuntimeError("device public IP is unavailable")
    calls["count"] += 1
    calls["ip"] = public_ip
    return {
        "city": "广州",
        "condition": "晴",
        "temperature_c": 28.0,
        "humidity_percent": 60,
    }


main.build_clock_context = fake_build_clock_context
result = asyncio.run(main.clock(FakeRequest()))
assert result["city"] == "广州"
assert result["condition"] == "晴"
assert calls == {"count": 1, "ip": "8.8.8.8"}
try:
    asyncio.run(main.clock(FakeRequestWithoutPublicIp()))
except HTTPException as exc:
    assert exc.status_code == 503
else:
    raise AssertionError("clock refresh must reject requests without a public IP")


def fake_configured_clock_context(public_ip: str | None) -> dict:
    assert public_ip is None
    return {
        "city": "Configured City",
        "condition": "Cloudy",
        "temperature_c": 27.0,
        "humidity_percent": 72,
    }


main.build_clock_context = fake_configured_clock_context
configured_result = asyncio.run(main.clock(FakeRequestWithoutPublicIp()))
assert configured_result["city"] == "Configured City"
assert configured_result["condition"] == "Cloudy"
assert configured_result["humidity_percent"] == 72
print("clock-refresh-endpoint-smoke-ok model_calls=0 configured_city=true")
