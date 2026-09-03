"""Read-only tools exposed to the voice assistant."""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.assistant_tools import register_assistant_tools
from app.location_context import resolve_current_location
from app.pomodoro_tools import control_pomodoro, start_pomodoro
from app.tool_registry import ToolRegistry, ToolSpec


PROCESS_STARTED_AT = time.monotonic()
DEFAULT_TIMEZONE = os.getenv("ASSISTANT_TIMEZONE", "Asia/Shanghai")
WEATHER_TIMEOUT_SECONDS = max(1.0, min(float(os.getenv("WEATHER_TIMEOUT_SECONDS", "6")), 15.0))


WEATHER_CODES = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴大冰雹",
}


def _get_json(url: str, timeout: float = WEATHER_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "yang-ai-gateway/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read(256_000).decode("utf-8"))


def get_current_time(arguments: dict[str, Any]) -> dict[str, Any]:
    timezone_name = arguments.get("timezone")
    location = None
    if not timezone_name:
        try:
            location = resolve_current_location()
            timezone_name = location["timezone"]
        except (OSError, RuntimeError, ValueError):
            timezone_name = DEFAULT_TIMEZONE
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    now = datetime.now(zone)
    weekdays = "一二三四五六日"
    result = {
        "timezone": timezone_name,
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y年%m月%d日"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": f"星期{weekdays[now.weekday()]}",
    }
    if location is not None:
        result.update(
            {
                "automatic_location": True,
                "location": location["city"],
                "region": location["region"],
                "country": location["country"],
                "location_accuracy": location["accuracy"],
            }
        )
    return result


def get_current_location(arguments: dict[str, Any]) -> dict[str, Any]:
    return resolve_current_location()


def _number_or_none(value: Any, converter: type = float) -> Any:
    if value in (None, "", "none", "None"):
        return None
    try:
        return converter(value)
    except (TypeError, ValueError):
        return None


def _get_amap_weather(location: dict[str, Any], api_key: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "city": location["adcode"],
            "extensions": "base",
            "output": "json",
            "key": api_key,
        }
    )
    payload = _get_json(f"https://restapi.amap.com/v3/weather/weatherInfo?{query}")
    lives = payload.get("lives") or []
    if payload.get("status") != "1" or not lives:
        raise RuntimeError("AMap weather lookup failed")
    live = lives[0]
    return {
        "found": True,
        "location": location["city"],
        "region": live.get("province") or location.get("region", ""),
        "country": location.get("country", "China"),
        "observed_at": live.get("reporttime"),
        "condition": str(live.get("weather") or "--"),
        "temperature_c": _number_or_none(live.get("temperature")),
        "apparent_temperature_c": None,
        "humidity_percent": _number_or_none(live.get("humidity"), int),
        "precipitation_mm": None,
        "wind_speed_kmh": _number_or_none(live.get("windpower")),
        "source": "AMap",
        "automatic_location": True,
        "location_accuracy": "city-level",
    }


def get_current_weather(arguments: dict[str, Any]) -> dict[str, Any]:
    location = str(arguments.get("location") or "").strip()
    automatic_location = not location
    if automatic_location:
        current_location = resolve_current_location()
        amap_key = os.getenv("AMAP_WEB_KEY", "").strip()
        if amap_key and current_location.get("source") == "amap" and current_location.get("adcode"):
            try:
                return _get_amap_weather(current_location, amap_key)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                pass
        place = {
            "name": current_location["city"],
            "admin1": current_location["region"],
            "country": current_location["country"],
            "latitude": current_location["latitude"],
            "longitude": current_location["longitude"],
        }
    else:
        geocode_query = urllib.parse.urlencode(
            {"name": location, "count": 1, "language": "zh", "format": "json"}
        )
        geocode = _get_json(
            f"https://geocoding-api.open-meteo.com/v1/search?{geocode_query}"
        )
        results = geocode.get("results") or []
        if not results:
            return {
                "found": False,
                "requested_location": location,
                "source": "Open-Meteo",
            }
        place = results[0]
    forecast_query = urllib.parse.urlencode(
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": ",".join(
                [
                    "temperature_2m",
                    "apparent_temperature",
                    "relative_humidity_2m",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                ]
            ),
            "timezone": "auto",
        }
    )
    forecast = _get_json(f"https://api.open-meteo.com/v1/forecast?{forecast_query}")
    current = forecast.get("current") or {}
    code = int(current.get("weather_code", -1))
    return {
        "found": True,
        "location": place.get("name", location),
        "region": place.get("admin1", ""),
        "country": place.get("country", ""),
        "observed_at": current.get("time"),
        "condition": WEATHER_CODES.get(code, f"未知天气代码{code}"),
        "temperature_c": current.get("temperature_2m"),
        "apparent_temperature_c": current.get("apparent_temperature"),
        "humidity_percent": current.get("relative_humidity_2m"),
        "precipitation_mm": current.get("precipitation"),
        "wind_speed_kmh": current.get("wind_speed_10m"),
        "source": "Open-Meteo",
        "automatic_location": automatic_location,
        "location_accuracy": "city-level" if automatic_location else "geocoded",
    }


def _read_rss_mb() -> float | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def _read_cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="utf-8") as stat_file:
            fields = stat_file.readline().split()
        if not fields or fields[0] != "cpu":
            return None
        values = [int(value) for value in fields[1:]]
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, sum(values)
    except (FileNotFoundError, OSError, ValueError):
        return None


def _read_network_bytes() -> tuple[int, int] | None:
    try:
        rx_total = 0
        tx_total = 0
        with open("/proc/net/dev", encoding="utf-8") as network_file:
            for line in network_file.readlines()[2:]:
                interface, values_text = line.split(":", 1)
                if interface.strip() == "lo":
                    continue
                values = values_text.split()
                rx_total += int(values[0])
                tx_total += int(values[8])
        return rx_total, tx_total
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def _sample_cpu_and_network(sample_seconds: float = 0.3) -> dict[str, float | int | None]:
    cpu_before = _read_cpu_times()
    network_before = _read_network_bytes()
    if cpu_before is None and network_before is None:
        return {
            "cpu_used_percent": None,
            "network_rx_kbps": None,
            "network_tx_kbps": None,
            "sample_window_ms": 0,
        }
    started = time.monotonic()
    time.sleep(sample_seconds)
    elapsed = max(time.monotonic() - started, 0.001)
    cpu_after = _read_cpu_times()
    network_after = _read_network_bytes()

    cpu_percent = None
    if cpu_before is not None and cpu_after is not None:
        idle_delta = cpu_after[0] - cpu_before[0]
        total_delta = cpu_after[1] - cpu_before[1]
        if total_delta > 0:
            cpu_percent = round(max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta))), 1)

    rx_kbps = None
    tx_kbps = None
    if network_before is not None and network_after is not None:
        rx_kbps = round(max(0, network_after[0] - network_before[0]) * 8 / elapsed / 1000, 1)
        tx_kbps = round(max(0, network_after[1] - network_before[1]) * 8 / elapsed / 1000, 1)
    return {
        "cpu_used_percent": cpu_percent,
        "network_rx_kbps": rx_kbps,
        "network_tx_kbps": tx_kbps,
        "sample_window_ms": round(elapsed * 1000),
    }


def get_server_status(arguments: dict[str, Any]) -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    live_metrics = _sample_cpu_and_network()
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
        loads = [round(load_1m, 2), round(load_5m, 2), round(load_15m, 2)]
    except (AttributeError, OSError):
        loads = None
    return {
        "sampled_at": datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).strftime("%H:%M:%S"),
        "status": "running",
        "service": "yang-ai-gateway",
        "mode": "streaming-function-calling",
        "uptime_seconds": int(time.monotonic() - PROCESS_STARTED_AT),
        "memory_rss_mb": _read_rss_mb(),
        "system_load_1m_5m_15m": loads,
        "disk_used_percent": round((disk.used / disk.total) * 100, 1),
        **live_metrics,
    }


def build_read_only_registry() -> ToolRegistry:
    registry = ToolRegistry(max_result_chars=3000)
    registry.register(
        ToolSpec(
            name="get_current_time",
            description="查询当前准确日期、时间和星期。省略时区时自动使用设备城市对应时区。",
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "可选 IANA 时区名，例如 Asia/Shanghai；省略时自动定位。",
                        "minLength": 1,
                        "maxLength": 64,
                    }
                },
            },
            handler=get_current_time,
        )
    )
    registry.register(
        ToolSpec(
            name="get_current_location",
            description="查询设备当前所在城市、地区、国家和时区。结果是基于公网 IP 的城市级位置。",
            parameters={"type": "object", "properties": {}},
            handler=get_current_location,
        )
    )
    registry.register(
        ToolSpec(
            name="get_current_weather",
            description="查询实时天气。用户未指定城市时省略 location，自动使用设备当前城市。",
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "可选城市或区县名；省略时自动定位设备当前城市。",
                        "minLength": 1,
                        "maxLength": 80,
                    }
                },
            },
            handler=get_current_weather,
        )
    )
    registry.register(
        ToolSpec(
            name="start_pomodoro",
            description="启动新的番茄钟或专注倒计时。用户未说明时长时默认 25 分钟。",
            parameters={
                "type": "object",
                "properties": {
                    "duration_minutes": {
                        "type": "integer",
                        "description": "倒计时分钟数，范围 1 到 180；省略时为 25。",
                    },
                    "label": {
                        "type": "string",
                        "description": "可选的专注任务名称。",
                        "maxLength": 40,
                    },
                },
            },
            handler=start_pomodoro,
        )
    )
    registry.register(
        ToolSpec(
            name="control_pomodoro",
            description="暂停、继续、取消、显示或查询当前番茄钟。",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pause", "resume", "cancel", "show", "status"],
                        "description": "要执行的番茄钟操作。",
                    }
                },
                "required": ["action"],
            },
            handler=control_pomodoro,
        )
    )
    registry.register(
        ToolSpec(
            name="get_server_status",
            description="查询当前 AI 网关服务器的实时运行状态、内存、负载、磁盘和进程运行时间。",
            parameters={"type": "object", "properties": {}},
            handler=get_server_status,
        )
    )
    register_assistant_tools(registry)
    return registry
