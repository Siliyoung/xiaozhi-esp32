import asyncio
import json
import logging
import wave
from pathlib import Path

import opuslib
from fastapi import WebSocket


logger = logging.getLogger("yang-ai-gateway.fixed-reply")


def encode_wav_to_opus_frames(
    wav_path: Path, sample_rate: int, frame_duration_ms: int
) -> list[bytes]:
    with wave.open(str(wav_path), "rb") as wav_file:
        if wav_file.getnchannels() != 1:
            raise RuntimeError("fixed reply WAV must be mono")
        if wav_file.getsampwidth() != 2:
            raise RuntimeError("fixed reply WAV must use signed 16-bit PCM")
        if wav_file.getframerate() != sample_rate:
            raise RuntimeError(
                f"fixed reply WAV sample rate must be {sample_rate} Hz"
            )
        pcm = wav_file.readframes(wav_file.getnframes())

    frame_samples = sample_rate * frame_duration_ms // 1000
    frame_bytes = frame_samples * 2
    encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_AUDIO)
    encoder.bitrate = 32000

    frames: list[bytes] = []
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += b"\x00" * (frame_bytes - len(chunk))
        frames.append(encoder.encode(chunk, frame_samples))

    if not frames:
        raise RuntimeError("fixed reply WAV contains no audio")
    logger.info(
        "Encoded fixed reply path=%s frames=%d duration_ms=%d",
        wav_path,
        len(frames),
        len(frames) * frame_duration_ms,
    )
    return frames


async def send_fixed_reply(
    websocket: WebSocket,
    session_id: str,
    text: str,
    opus_frames: list[bytes],
    frame_duration_ms: int,
) -> None:
    def message(state: str, **extra: str) -> str:
        return json.dumps(
            {"session_id": session_id, "type": "tts", "state": state, **extra},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    await websocket.send_text(message("start"))
    await websocket.send_text(message("sentence_start", text=text))
    logger.info("Sending fixed reply session=%s frames=%d", session_id, len(opus_frames))

    for frame in opus_frames:
        await websocket.send_bytes(frame)
        await asyncio.sleep(frame_duration_ms / 1000)

    await websocket.send_text(message("sentence_end", text=text))
    await websocket.send_text(message("stop"))
    logger.info("Fixed reply completed session=%s", session_id)
