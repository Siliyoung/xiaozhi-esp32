import logging
import os
import tempfile
import wave
from http import HTTPStatus

from dashscope.audio.asr import Recognition

from app.dashscope_pipeline_v2 import DashScopePipeline as TimeAwarePipeline


logger = logging.getLogger("yang-ai-gateway.dashscope-v3")


class NoSpeechRecognized(RuntimeError):
    """The cloud ASR call succeeded but did not recognize useful speech."""


class DashScopePipeline(TimeAwarePipeline):
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
            sentences = result.get_sentence() or []
            transcript = "".join(
                sentence.get("text", "") for sentence in sentences
            ).strip()
            if not transcript:
                raise NoSpeechRecognized("ASR returned no recognized speech")
            logger.info("ASR completed chars=%d", len(transcript))
            return transcript
        finally:
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
