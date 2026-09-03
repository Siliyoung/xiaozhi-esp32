"""Exercise real Qwen Function Calling without ASR or TTS."""

from app.dashscope_pipeline_tools import DashScopeToolsPipeline


pipeline = DashScopeToolsPipeline()
queries = [
    "现在北京时间几点，今天星期几？",
    "深圳现在天气怎么样？",
    "我的AI网关服务器现在运行正常吗？",
]
for query in queries:
    answer = "".join(pipeline.iter_answer_deltas(query, []))
    assert answer.strip(), query
    print(f"query={query!r} answer={answer!r}")
print("function-calling-live-ok queries=3")
