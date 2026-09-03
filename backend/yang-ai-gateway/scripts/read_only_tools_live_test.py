"""Call the three read-only tools without involving the language model."""

import json

from app.read_only_tools import build_read_only_registry


registry = build_read_only_registry()
cases = [
    ("get_current_time", {}),
    ("get_current_weather", {"location": "深圳"}),
    ("get_server_status", {}),
]
for name, arguments in cases:
    result = json.loads(registry.execute(name, arguments))
    assert result["ok"] is True, result
    print(name, json.dumps(result["data"], ensure_ascii=False, separators=(",", ":")))
print("read-only-tools-live-ok tools=3")
