"""Synthesize a short PCM sample with the configured TTS model and voice."""

from app.dashscope_pipeline_tools import DashScopeToolsPipeline


pipeline = DashScopeToolsPipeline()
audio = pipeline.synthesize("语音模型切换测试成功。")
if len(audio) < 4800:
    raise RuntimeError("TTS smoke test returned too little PCM audio")
print(
    f"tts-model-smoke-ok model={pipeline.tts_model} "
    f"voice={pipeline.tts_voice} pcm_bytes={len(audio)}"
)
