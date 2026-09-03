"""Incremental text segmentation for sentence-by-sentence speech synthesis."""


class SentenceSegmenter:
    hard_endings = frozenset("。！？!?；;\n")
    soft_endings = frozenset("，,、：:")
    trailing_marks = frozenset("”’\"'）)]】》」』")

    def __init__(self, max_chars: int = 80, min_soft_split_chars: int = 16) -> None:
        if max_chars < 16:
            raise ValueError("max_chars must be at least 16")
        self.max_chars = max_chars
        self.min_soft_split_chars = min(min_soft_split_chars, max_chars - 1)
        self.buffer = ""

    def feed(self, delta: str) -> list[str]:
        if delta:
            self.buffer += delta
        sentences: list[str] = []
        while self.buffer:
            hard_index = next(
                (
                    index
                    for index, character in enumerate(self.buffer)
                    if character in self.hard_endings
                ),
                -1,
            )
            if hard_index >= 0:
                split_at = hard_index + 1
                while (
                    split_at < len(self.buffer)
                    and self.buffer[split_at] in self.trailing_marks
                ):
                    split_at += 1
                self._take(split_at, sentences)
                continue

            if len(self.buffer) < self.max_chars:
                break

            window = self.buffer[: self.max_chars]
            soft_index = max(
                (window.rfind(mark) for mark in self.soft_endings), default=-1
            )
            split_at = (
                soft_index + 1
                if soft_index >= self.min_soft_split_chars
                else self.max_chars
            )
            self._take(split_at, sentences)
        return sentences

    def flush(self) -> list[str]:
        if not self.buffer.strip():
            self.buffer = ""
            return []
        sentence = self.buffer.strip()
        self.buffer = ""
        return [sentence]

    def _take(self, split_at: int, sentences: list[str]) -> None:
        sentence = self.buffer[:split_at].strip()
        self.buffer = self.buffer[split_at:]
        if sentence:
            sentences.append(sentence)
