"""Generate a temporary 16 kHz spoken prompt for end-to-end streaming QA."""

import audioop
import sys
import wave

from app.dashscope_pipeline_protected import DashScopePipeline


def main(output_path: str) -> None:
    pipeline = DashScopePipeline()
    pcm_24000 = pipeline.synthesize(
        "请用三句话介绍一下你自己，每句话都说得简短一点。"
    )
    pcm_16000, _ = audioop.ratecv(pcm_24000, 2, 1, 24000, 16000, None)
    with wave.open(output_path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(pcm_16000)
    print(f"streaming-test-wav-ok path={output_path} pcm_bytes={len(pcm_16000)}")


if __name__ == "__main__":
    main(sys.argv[1])
