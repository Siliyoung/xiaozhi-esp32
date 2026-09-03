"""Probe the installed DashScope SDK's real incremental generation behavior."""

import time

from app.dashscope_pipeline_streaming import DashScopeStreamingPipeline
from app.streaming_text import SentenceSegmenter


def main() -> None:
    pipeline = DashScopeStreamingPipeline()
    segmenter = SentenceSegmenter(max_chars=80)
    started = time.monotonic()
    first_delta_ms = None
    chunks = []
    sentences = []
    for delta in pipeline.iter_answer_deltas(
        "请用三句很短的话介绍你自己，每句话都用句号结尾。", []
    ):
        if first_delta_ms is None:
            first_delta_ms = int((time.monotonic() - started) * 1000)
        chunks.append(delta)
        sentences.extend(segmenter.feed(delta))
    sentences.extend(segmenter.flush())
    answer = "".join(chunks)
    assert answer
    assert len(chunks) > 1
    assert first_delta_ms is not None
    assert sentences
    print(
        "streaming-llm-ok",
        f"chunks={len(chunks)}",
        f"sentences={len(sentences)}",
        f"chars={len(answer)}",
        f"first_delta_ms={first_delta_ms}",
        f"total_ms={int((time.monotonic() - started) * 1000)}",
    )


if __name__ == "__main__":
    main()
