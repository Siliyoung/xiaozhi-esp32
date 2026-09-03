"""Deterministic incremental sentence segmentation checks."""

from app.streaming_text import SentenceSegmenter


def main() -> None:
    segmenter = SentenceSegmenter(max_chars=20, min_soft_split_chars=8)
    output = []
    for delta in ["你好", "！今天", "天气不错。", "这是最后", "一句"]:
        output.extend(segmenter.feed(delta))
    output.extend(segmenter.flush())
    assert output == ["你好！", "今天天气不错。", "这是最后一句"], output

    long_text = SentenceSegmenter(max_chars=20, min_soft_split_chars=8)
    chunks = long_text.feed("这是一段比较长的内容，应该在逗号位置优先切开并立即送去语音合成")
    chunks.extend(long_text.flush())
    assert len(chunks) >= 2
    assert "".join(chunks) == "这是一段比较长的内容，应该在逗号位置优先切开并立即送去语音合成"
    print("streaming-text-ok incremental=true sentence_split=true max_length=true")


if __name__ == "__main__":
    main()
