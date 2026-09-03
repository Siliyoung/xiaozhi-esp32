"""Stable systemd entry point for the current conversation pipeline."""

from app import conversation_main
from app.dashscope_pipeline_v2 import DashScopePipeline


conversation_main.pipeline = DashScopePipeline()
app = conversation_main.app
