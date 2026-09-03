"""Ensure a successful ASR response with no text is classified as silence."""

from app.dashscope_pipeline_v3 import DashScopePipeline, NoSpeechRecognized


def main() -> None:
    pipeline = DashScopePipeline()
    silence = b"\x00" * (16000 * 2 * 2)
    try:
        pipeline.transcribe(silence)
    except NoSpeechRecognized:
        print("no-speech-ok")
        return
    raise AssertionError("silence unexpectedly produced a transcript")


if __name__ == "__main__":
    main()
