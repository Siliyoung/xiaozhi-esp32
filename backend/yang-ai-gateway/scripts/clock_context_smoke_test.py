"""Offline smoke tests for standby-clock weather normalization and caching."""

from app import clock_context


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

normalized = clock_context.clock_context_from_weather(fake_weather({}))
assert normalized["humidity_percent"] == 66
assert "public_ip" not in normalized
print("clock-context-smoke-ok cached=true privacy=true")
