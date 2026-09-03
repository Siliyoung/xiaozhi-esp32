from datetime import datetime
from http import HTTPStatus
from zoneinfo import ZoneInfo

from dashscope import Generation

from app.dashscope_pipeline import DashScopePipeline as BaseDashScopePipeline


class DashScopePipeline(BaseDashScopePipeline):
    """Add reliable Beijing date/time context to every LLM turn."""

    def generate_answer(self, transcript: str, history: list[dict[str, str]]) -> str:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        weekday = "一二三四五六日"[now.weekday()]
        time_context = now.strftime("当前北京时间：%Y年%m月%d日 %H:%M")
        time_context += f"，星期{weekday}。"
        messages = [
            {
                "role": "system",
                "content": f"{self.system_prompt}\n{time_context}",
            }
        ]
        messages.extend(history[-8:])
        messages.append({"role": "user", "content": transcript})
        response = Generation.call(
            model=self.llm_model,
            messages=messages,
            result_format="message",
            max_tokens=220,
            temperature=0.7,
            enable_thinking=False,
        )
        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(f"LLM failed: {response.code} {response.message}")
        answer = response.output.choices[0].message.content.strip()
        if not answer:
            raise RuntimeError("LLM returned empty text")
        return answer
