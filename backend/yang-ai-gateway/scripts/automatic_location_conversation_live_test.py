"""Live LLM tool-routing test for a weather question without a city."""

from __future__ import annotations

import os

from app.dashscope_pipeline_tools import DashScopeToolsPipeline
from app.location_context import reset_client_public_ip, set_client_public_ip


client_ip = os.environ.get("LOCATION_TEST_IP")
if not client_ip:
    raise RuntimeError("LOCATION_TEST_IP is required")

token = set_client_public_ip(client_ip)
try:
    answer = "".join(
        DashScopeToolsPipeline().iter_answer_deltas("今天天气怎么样？", [])
    )
finally:
    reset_client_public_ip(token)

if not answer.strip():
    raise RuntimeError("LLM returned an empty answer")
print(f"auto-location-conversation-ok answer={answer}")
