"""Offline smoke tests for standby-clock weather normalization and caching."""

import os
from unittest.mock import patch

from app import clock_context, read_only_tools


clock_context._CACHE.clear()
calls = {"location": 0, "weather": 0}


def fake_location():
    calls["location"] += 1
    return {"city": "Guangzhou"}


def fake_weather(_arguments):
    calls["weather"] += 1
    return {
        "found": True,
        "location": "广州",
        "condition": "晴",
        "temperature_c": 29.5,
        "humidity_percent": 66,
        "source": "Open-Meteo",
        "location_accuracy": "city-level",
    }


clock_context.resolve_current_location = fake_location
clock_context.get_current_weather = fake_weather
first = clock_context.build_clock_context("203.0.113.7")
second = clock_context.build_clock_context("203.0.113.7")
assert first == second
assert first["city"] == "广州"
assert first["condition"] == "晴"
assert first["temperature_c"] == 29.5
assert calls == {"location": 1, "weather": 1}

amap_location = {
    "city": "Test City",
    "region": "Test Province",
    "country": "China",
    "latitude": 20.5,
    "longitude": 100.5,
    "source": "amap",
    "adcode": "000000",
}
amap_weather_payload = {
    "status": "1",
    "lives": [
        {
            "province": "Test Province",
            "city": "Test City",
            "adcode": "000000",
            "weather": "Cloudy",
            "temperature": "27",
            "humidity": "72",
            "windpower": "3",
            "reporttime": "2026-09-03 18:00:00",
        }
    ],
}
with patch.dict(os.environ, {"AMAP_WEB_KEY": "test-amap-key"}):
    with patch.object(read_only_tools, "resolve_current_location", return_value=amap_location):
        with patch.object(read_only_tools, "_get_json", return_value=amap_weather_payload):
            amap_weather = read_only_tools.get_current_weather({})
assert amap_weather["source"] == "AMap"
assert amap_weather["condition"] == "Cloudy"
assert amap_weather["temperature_c"] == 27.0
assert amap_weather["humidity_percent"] == 72
normalized = clock_context.clock_context_from_weather(fake_weather({}))
assert normalized["humidity_percent"] == 66
assert "public_ip" not in normalized
print("clock-context-smoke-ok cached=true privacy=true")
