"""Encode a speech WAV like the ESP32 and verify server-side end-of-speech VAD."""

import sys
import wave

import opuslib

from app.voice_activity import UtteranceCollector


def main(path: str) -> None:
    with wave.open(path, "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16000
        speech = wav_file.readframes(wav_file.getnframes())

    pcm = b"\x00" * (16000 * 2 * 300 // 1000)
    pcm += speech
    pcm += b"\x00" * (16000 * 2 * 1200 // 1000)
    frame_samples = 16000 * 60 // 1000
    frame_bytes = frame_samples * 2
    encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
    collector = UtteranceCollector()

    utterance = None
    packets = 0
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += b"\x00" * (frame_bytes - len(chunk))
        packet = encoder.encode(chunk, frame_samples)
        packets += 1
        utterance = collector.feed_opus(packet)
        if utterance is not None:
            break

    assert utterance is not None
    duration_ms = len(utterance) * 1000 // (16000 * 2)
    print("vad-ok", f"input_packets={packets}", f"utterance_ms={duration_ms}")


if __name__ == "__main__":
    main(sys.argv[1])
