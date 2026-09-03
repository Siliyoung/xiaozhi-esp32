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


def fake_build_clock_context(public_ip: str) -> dict:
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

print("clock-refresh-endpoint-smoke-ok model_calls=0")
