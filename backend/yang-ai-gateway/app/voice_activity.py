from dataclasses import dataclass

import opuslib
import webrtcvad


@dataclass(frozen=True)
class VadConfig:
    sample_rate: int = 16000
    chunk_ms: int = 30
    silence_ms: int = 900
    min_speech_ms: int = 300
    max_utterance_ms: int = 15000
    aggressiveness: int = 2


class UtteranceCollector:
    """Decode device Opus packets and emit one PCM utterance after silence."""

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self.decoder = opuslib.Decoder(self.config.sample_rate, 1)
        self.vad = webrtcvad.Vad(self.config.aggressiveness)
        self.chunk_bytes = self.config.sample_rate * self.config.chunk_ms // 1000 * 2
        self.reset()

    def reset(self) -> None:
        self.pcm = bytearray()
        self.pending = bytearray()
        self.speech_ms = 0
        self.trailing_silence_ms = 0

    def feed_opus(self, packet: bytes, frame_duration_ms: int = 60) -> bytes | None:
        frame_samples = self.config.sample_rate * frame_duration_ms // 1000
        decoded = self.decoder.decode(packet, frame_samples)
        self.pending.extend(decoded)

        while len(self.pending) >= self.chunk_bytes:
            chunk = bytes(self.pending[: self.chunk_bytes])
            del self.pending[: self.chunk_bytes]
            self.pcm.extend(chunk)

            if self.vad.is_speech(chunk, self.config.sample_rate):
                self.speech_ms += self.config.chunk_ms
                self.trailing_silence_ms = 0
            elif self.speech_ms > 0:
                self.trailing_silence_ms += self.config.chunk_ms

            duration_ms = len(self.pcm) * 1000 // (self.config.sample_rate * 2)
            completed = (
                self.speech_ms >= self.config.min_speech_ms
                and self.trailing_silence_ms >= self.config.silence_ms
            ) or duration_ms >= self.config.max_utterance_ms

            if completed:
                utterance = bytes(self.pcm)
                self.reset()
                return utterance

        return None
