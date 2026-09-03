# Function Calling

网关在原有 `ASR → 流式 LLM → 分句 TTS` 链路中加入了通用只读工具层。

## 调用流程

1. 将工具 JSON Schema 与对话上下文发送给 Qwen。
2. 普通问题直接增量输出，继续使用现有分句 TTS。
3. 实时信息问题由模型返回流式 `tool_calls`。
4. 网关按 `index` 拼接函数名和 JSON 参数，交给工具注册表校验并执行。
5. 工具结果以 `role=tool` 回传 Qwen，最终回答继续流式输出。
6. 最多允许两轮工具调用，防止模型进入无限工具循环。

## 已注册工具

- `get_current_time`：返回指定 IANA 时区的日期、时间和星期，默认 `Asia/Shanghai`。
- `get_current_weather`：使用 Open-Meteo 地理编码和实时天气接口，查询城市或区县天气。
- `get_server_status`：返回当前网关进程运行时间、RSS 内存、系统负载和磁盘使用率。

三个工具都是只读操作。工具注册表会拒绝未知工具、未知参数、缺少必填参数和类型错误，并限制返回结果长度。

## 扩展工具

在 `app/read_only_tools.py` 的 `build_read_only_registry()` 中注册新的 `ToolSpec`：

```python
registry.register(
    ToolSpec(
        name="get_example",
        description="说明模型应该在什么情况下调用该工具。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 80},
            },
            "required": ["query"],
        },
        handler=get_example,
    )
)
```

处理函数接收已经校验的字典，返回可被 JSON 序列化的数据。不要把任意模型参数直接拼接到 Shell、SQL 或 URL 中。

## 配置

- `MAX_TOOL_ROUNDS`：单次回答最多工具调用轮数，默认 `2`，范围 `1–4`。
- `WEATHER_TIMEOUT_SECONDS`：天气 HTTP 请求超时，默认 `6` 秒，范围 `1–15` 秒。
- `ASSISTANT_TIMEZONE`：默认时区，默认 `Asia/Shanghai`。
- `TOOL_SYSTEM_PROMPT`：覆盖工具选择系统提示词。

## 测试

```bash
PYTHONPATH=. python scripts/function_calling_smoke_test.py
PYTHONPATH=. python scripts/read_only_tools_live_test.py
PYTHONPATH=. python scripts/function_calling_live_test.py
```

第一个测试不访问模型或天气网络；后两个分别验证真实工具数据和真实 Qwen Function Calling。
