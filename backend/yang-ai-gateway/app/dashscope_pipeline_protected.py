"""DashScope pipeline with bounded output, request timeouts, and safe retries."""

import logging
import os
import tempfile
import time
import wave
from datetime import datetime
from http import HTTPStatus
from zoneinfo import ZoneInfo

from dashscope import Generation
from dashscope.audio.asr import Recognition
from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

from app.dashscope_pipeline import DashScopePipeline as BasePipeline
from app.dashscope_pipeline_v3 import NoSpeechRecognized


logger = logging.getLogger("yang-ai-gateway.upstream")


class UpstreamError(RuntimeError):
    def __init__(self, stage: str, status: int | None = None) -> None:
        super().__init__(f"{stage} upstream request failed")
        self.stage = stage
        self.status = status


class DashScopePipeline(BasePipeline):
    def __init__(self) -> None:
        super().__init__()
        self.max_attempts = max(1, min(int(os.getenv("UPSTREAM_MAX_ATTEMPTS", "2")), 3))
        self.retry_delay = max(0.0, float(os.getenv("UPSTREAM_RETRY_DELAY_SECONDS", "0.5")))
        self.asr_timeout = max(5, int(os.getenv("ASR_REQUEST_TIMEOUT_SECONDS", "20")))
        self.llm_timeout = max(5, int(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "25")))
        self.tts_timeout_ms = max(5000, int(os.getenv("TTS_REQUEST_TIMEOUT_MS", "30000")))
        self.llm_max_tokens = max(32, min(int(os.getenv("LLM_MAX_TOKENS", "180")), 512))
        self.max_transcript_chars = max(32, int(os.getenv("MAX_TRANSCRIPT_CHARS", "500")))
        self.max_answer_chars = max(32, int(os.getenv("MAX_ANSWER_CHARS", "300")))
        self.system_prompt = os.getenv(
            "LLM_SYSTEM_PROMPT",
            "你是运行在桌面智能音箱里的中文助手。回答自然、准确、简短，通常不超过三句话。"
            "不要使用 Markdown、列表符号或表情，因为回答会被直接朗读。",
        )

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, UpstreamError):
            return exc.status in {408, 429, 500, 502, 503, 504}
        name = f"{type(exc).__module__}.{type(exc).__name__}".lower()
        return "timeout" in name or "connection" in name or "websocket" in name

    def _run(self, stage: str, operation):
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation()
            except NoSpeechRecognized:
                raise
            except Exception as exc:
                if attempt >= self.max_attempts or not self._retryable(exc):
                    raise
                logger.warning(
                    "Upstream retry stage=%s attempt=%d error_type=%s",
                    stage,
                    attempt,
                    type(exc).__name__,
                )
                time.sleep(self.retry_delay * attempt)

    def transcribe(self, pcm_16000: bytes) -> str:
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                path = temp_file.name
            with wave.open(path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(pcm_16000)

            def recognize():
                recognition = Recognition(
                    model=self.asr_model,
                    format="wav",
                    sample_rate=16000,
                    language_hints=["zh", "en"],
                    semantic_punctuation_enabled=False,
                    callback=None,
                )
                result = recognition.call(
                    path, request_timeout=self.asr_timeout
                )
                if result.status_code != HTTPStatus.OK:
                    raise UpstreamError("asr", int(result.status_code))
                return result

            result = self._run("asr", recognize)
            transcript = "".join(
                sentence.get("text", "") for sentence in (result.get_sentence() or [])
            ).strip()
            if not transcript:
                raise NoSpeechRecognized("ASR returned no recognized speech")
            transcript = transcript[: self.max_transcript_chars]
            logger.info("Stage completed stage=asr chars=%d", len(transcript))
            return transcript
        finally:
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    def generate_answer(self, transcript: str, history: list[dict[str, str]]) -> str:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        weekday = "一二三四五六日"[now.weekday()]
        time_context = now.strftime("当前北京时间：%Y年%m月%d日 %H:%M")
        time_context += f"，星期{weekday}。"
        messages = [
            {"role": "system", "content": f"{self.system_prompt}\n{time_context}"},
            *history[-8:],
            {"role": "user", "content": transcript},
        ]

        def generate():
            response = Generation.call(
                model=self.llm_model,
                messages=messages,
                result_format="message",
                max_tokens=self.llm_max_tokens,
                temperature=0.7,
                enable_thinking=False,
                request_timeout=self.llm_timeout,
            )
            if response.status_code != HTTPStatus.OK:
                raise UpstreamError("llm", int(response.status_code))
            return response

        response = self._run("llm", generate)
        answer = response.output.choices[0].message.content.strip()
        if not answer:
            raise UpstreamError("llm")
        answer = answer[: self.max_answer_chars]
        logger.info("Stage completed stage=llm chars=%d", len(answer))
        return answer

    def synthesize(self, text: str) -> bytes:
        def synthesize_audio():
            synthesizer = SpeechSynthesizer(
                model=self.tts_model,
                voice=self.tts_voice,
                format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            )
            audio = synthesizer.call(text, timeout_millis=self.tts_timeout_ms)
            if not audio:
                raise UpstreamError("tts")
            return audio

        audio = self._run("tts", synthesize_audio)
        logger.info("Stage completed stage=tts pcm_bytes=%d", len(audio))
        return audio
