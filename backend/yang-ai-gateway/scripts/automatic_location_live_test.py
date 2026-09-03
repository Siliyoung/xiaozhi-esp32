"""Live automatic-location test; requires LOCATION_TEST_IP in the environment."""

from __future__ import annotations

import os

from app.location_context import reset_client_public_ip, set_client_public_ip
from app.read_only_tools import (
    get_current_location,
    get_current_time,
    get_current_weather,
)


client_ip = os.environ.get("LOCATION_TEST_IP")
if not client_ip:
    raise RuntimeError("LOCATION_TEST_IP is required")

token = set_client_public_ip(client_ip)
try:
    location = get_current_location({})
    local_time = get_current_time({})
    weather = get_current_weather({})
finally:
    reset_client_public_ip(token)

assert weather["found"] is True
assert weather["automatic_location"] is True
assert weather["location"] == location["city"]
assert local_time["timezone"] == location["timezone"]
print(
    "live-auto-location-ok "
    f"city={location['city']} timezone={local_time['timezone']} "
    f"weather={weather['found']} automatic={weather['automatic_location']}"
)
