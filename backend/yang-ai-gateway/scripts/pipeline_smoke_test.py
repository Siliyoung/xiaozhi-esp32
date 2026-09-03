"""Call real DashScope STT, LLM, and TTS without exposing the API key."""

import sys
import wave

from app.dashscope_pipeline import DashScopePipeline


def main(path: str) -> None:
    with wave.open(path, "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16000
        pcm = wav_file.readframes(wav_file.getnframes())

    pipeline = DashScopePipeline()
    transcript = pipeline.transcribe(pcm)
    print("stt-ok", repr(transcript))
    answer = pipeline.generate_answer(transcript, [])
    print("llm-ok", repr(answer))
    audio = pipeline.synthesize(answer)
    assert len(audio) > 24000
    print("tts-ok", f"pcm_bytes={len(audio)}")


if __name__ == "__main__":
    main(sys.argv[1])
