"""Generate a 16 kHz WAV prompt for end-to-end tool-call QA."""

import audioop
import sys
import wave

from app.dashscope_pipeline_protected import DashScopePipeline


output_path = sys.argv[1]
text = sys.argv[2]
pipeline = DashScopePipeline()
pcm_24000 = pipeline.synthesize(text)
pcm_16000, _ = audioop.ratecv(pcm_24000, 2, 1, 24000, 16000, None)
with wave.open(output_path, "wb") as wav_file:
    wav_file.setnchannels(1)
    wav_file.setsampwidth(2)
    wav_file.setframerate(16000)
    wav_file.writeframes(pcm_16000)
print(f"tool-test-wav-ok path={output_path} text={text!r}")
