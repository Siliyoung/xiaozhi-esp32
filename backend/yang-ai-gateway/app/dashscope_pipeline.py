import logging
import os
import tempfile
import wave
from dataclasses import dataclass
from http import HTTPStatus

import dashscope
from dashscope import Generation
from dashscope.audio.asr import Recognition
from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer


logger = logging.getLogger("yang-ai-gateway.dashscope")


@dataclass(frozen=True)
class PipelineResult:
    transcript: str
    answer: str
    pcm_24000: bytes


class DashScopePipeline:
    def __init__(self) -> None:
        api_key = os.getenv("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for conversation mode")
        dashscope.api_key = api_key

        self.asr_model = os.getenv("ASR_MODEL", "paraformer-realtime-v2")
        self.llm_model = os.getenv("LLM_MODEL", "qwen-plus")
        self.tts_model = os.getenv("TTS_MODEL", "cosyvoice-v2")
        self.tts_voice = os.getenv("TTS_VOICE", "longxiaochun_v2")
        self.system_prompt = os.getenv(
            "LLM_SYSTEM_PROMPT",
            "你是一个运行在桌面智能音箱里的中文助手。回答自然、准确、简短，"
            "通常不超过三句话。不要使用Markdown、列表符号或表情，因为回答会被直接朗读。",
        )

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

            recognition = Recognition(
                model=self.asr_model,
                format="wav",
                sample_rate=16000,
                language_hints=["zh", "en"],
                semantic_punctuation_enabled=False,
                callback=None,
            )
            result = recognition.call(path)
            if result.status_code != HTTPStatus.OK:
                raise RuntimeError(f"ASR failed: {result.code} {result.message}")
            transcript = "".join(
                sentence.get("text", "") for sentence in result.get_sentence()
            ).strip()
            if not transcript:
                raise RuntimeError("ASR returned empty text")
            logger.info("ASR completed chars=%d", len(transcript))
            return transcript
        finally:
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    def generate_answer(self, transcript: str, history: list[dict[str, str]]) -> str:
        messages = [{"role": "system", "content": self.system_prompt}]
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
        logger.info("LLM completed chars=%d", len(answer))
        return answer

    def synthesize(self, text: str) -> bytes:
        synthesizer = SpeechSynthesizer(
            model=self.tts_model,
            voice=self.tts_voice,
            format=AudioFormat.PCM_24000HZ_MONO_16BIT,
        )
        audio = synthesizer.call(text)
        if not audio:
            raise RuntimeError("TTS returned empty audio")
        logger.info("TTS completed pcm_bytes=%d", len(audio))
        return audio

    def process(
        self, pcm_16000: bytes, history: list[dict[str, str]]
    ) -> PipelineResult:
        transcript = self.transcribe(pcm_16000)
        answer = self.generate_answer(transcript, history)
        pcm_24000 = self.synthesize(answer)
        return PipelineResult(transcript, answer, pcm_24000)
