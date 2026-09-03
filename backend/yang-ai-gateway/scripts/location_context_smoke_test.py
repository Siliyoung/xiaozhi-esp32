"""Offline tests for privacy-preserving automatic location propagation."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from app import location_context
from app.location_context import (
    reset_client_public_ip,
    resolve_current_location,
    set_client_public_ip,
)
from app import read_only_tools


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return json.dumps(
            {
                "success": True,
                "city": "Test City",
                "region": "Test Region",
                "country": "Test Country",
                "country_code": "TC",
                "latitude": 22.5,
                "longitude": 114.0,
                "timezone": {"id": "Asia/Shanghai"},
            }
        ).encode()


token = set_client_public_ip("203.0.113.9")
try:
    with patch("app.location_context.urllib.request.urlopen", return_value=FakeResponse()) as lookup:
        location = resolve_current_location()
        assert resolve_current_location() == location
        assert lookup.call_count == 1
    assert "ip" not in location
    assert location["accuracy"] == "city-level"
    local_time = read_only_tools.get_current_time({})
    assert local_time["timezone"] == "Asia/Shanghai"
    assert local_time["location"] == "Test City"

    forecast_payload = {
        "current": {
            "time": "2026-08-27T12:00",
            "weather_code": 0,
            "temperature_2m": 30,
            "apparent_temperature": 32,
            "relative_humidity_2m": 70,
            "precipitation": 0,
            "wind_speed_10m": 8,
        }
    }
    with patch.object(read_only_tools, "_get_json", return_value=forecast_payload):
        weather = read_only_tools.get_current_weather({})
    assert weather["automatic_location"] is True
    assert weather["location"] == "Test City"
finally:
    reset_client_public_ip(token)


class FakeAmapResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return json.dumps(
            {
                "status": "1",
                "info": "OK",
                "infocode": "10000",
                "province": "Test Province",
                "city": "Test City",
                "adcode": "000000",
                "rectangle": "100.0,20.0;101.0,21.0",
            }
        ).encode()


location_context._CACHE.clear()
with patch.dict(os.environ, {"AMAP_WEB_KEY": "test-amap-key"}):
    token = set_client_public_ip("203.0.113.10")
    try:
        with patch(
            "app.location_context.urllib.request.urlopen",
            return_value=FakeAmapResponse(),
        ) as lookup:
            amap_location = resolve_current_location()
    finally:
        reset_client_public_ip(token)
assert lookup.call_count == 1
assert amap_location["source"] == "amap"
assert amap_location["city"] == "Test City"
assert amap_location["adcode"] == "000000"

configured_environment = {
    "DEVICE_LOCATION_CITY": "Configured City",
    "DEVICE_LOCATION_REGION": "Configured Region",
    "DEVICE_LOCATION_COUNTRY": "Test Country",
    "DEVICE_LOCATION_COUNTRY_CODE": "TC",
    "DEVICE_LOCATION_LATITUDE": "22.5000",
    "DEVICE_LOCATION_LONGITUDE": "114.0000",
    "DEVICE_LOCATION_TIMEZONE": "Asia/Shanghai",
}
with patch.dict(os.environ, configured_environment):
    with patch("app.location_context.urllib.request.urlopen") as lookup:
        configured = resolve_current_location()
    assert lookup.call_count == 0
    assert configured["city"] == "Configured City"
    assert configured["accuracy"] == "configured-city"
    assert configured["latitude"] == 22.5
print(
    "location-context-smoke-ok cache=true privacy=true "
    "auto_time=true auto_weather=true amap=true configured_city=true"
)
