"""Small, safe registry for model-invoked application tools."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable


logger = logging.getLogger("yang-ai-gateway.tools")


class ToolValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, max_result_chars: int = 3000) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.max_result_chars = max(256, max_result_chars)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or not spec.name.replace("_", "").isalnum():
            raise ValueError("tool name must contain only letters, digits, and underscores")
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def definitions(self) -> list[dict[str, Any]]:
        return [spec.definition() for spec in self._tools.values()]

    @staticmethod
    def _validate(arguments: Any, schema: dict[str, Any]) -> dict[str, Any]:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolValidationError("arguments must be a JSON object")

        properties = schema.get("properties", {})
        required = schema.get("required", [])
        unknown = set(arguments) - set(properties)
        if unknown:
            raise ToolValidationError(f"unknown arguments: {', '.join(sorted(unknown))}")
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ToolValidationError(f"missing required arguments: {', '.join(missing)}")

        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for name, value in arguments.items():
            rule = properties.get(name, {})
            expected_name = rule.get("type")
            expected = type_map.get(expected_name)
            if expected and (not isinstance(value, expected) or expected_name == "integer" and isinstance(value, bool)):
                raise ToolValidationError(f"argument {name} must be {expected_name}")
            if isinstance(value, str):
                if len(value) < int(rule.get("minLength", 0)):
                    raise ToolValidationError(f"argument {name} is too short")
                if len(value) > int(rule.get("maxLength", 10000)):
                    raise ToolValidationError(f"argument {name} is too long")
            if "enum" in rule and value not in rule["enum"]:
                raise ToolValidationError(f"argument {name} has an unsupported value")
        return arguments

    def execute(self, name: str, raw_arguments: str | dict[str, Any] | None) -> str:
        started = time.monotonic()
        spec = self._tools.get(name)
        if spec is None:
            return json.dumps(
                {"ok": False, "tool": name, "error": "unknown tool"},
                ensure_ascii=False,
            )
        try:
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments or "{}")
            else:
                arguments = raw_arguments or {}
            arguments = self._validate(arguments, spec.parameters)
            data = spec.handler(arguments)
            payload = {"ok": True, "tool": name, "data": data}
            status = "ok"
        except (json.JSONDecodeError, ToolValidationError) as exc:
            payload = {"ok": False, "tool": name, "error": str(exc)}
            status = "invalid"
        except Exception as exc:
            logger.warning("Tool execution failed tool=%s error_type=%s", name, type(exc).__name__)
            payload = {
                "ok": False,
                "tool": name,
                "error": "tool service is temporarily unavailable",
            }
            status = "error"

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > self.max_result_chars:
            encoded = json.dumps(
                {"ok": False, "tool": name, "error": "tool result exceeded size limit"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        logger.info(
            "Tool completed tool=%s status=%s duration_ms=%d",
            name,
            status,
            int((time.monotonic() - started) * 1000),
        )
        return encoded
