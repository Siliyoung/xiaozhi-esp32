"""Abort active playback, immediately upload a new question, and verify recovery."""

import asyncio
import json
import os
import sys
import time
import wave

import opuslib
from websockets.asyncio.client import connect


SERVICE_ERROR_MARKERS = ("语音服务暂时不可用", "请稍后再试")


def load_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16000
        pcm = wav_file.readframes(wav_file.getnframes())
    return pcm + b"\x00" * (16000 * 2 * 1200 // 1000)


async def send_pcm(websocket, encoder, pcm: bytes) -> None:
    samples = 16000 * 60 // 1000
    frame_bytes = samples * 2
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += b"\x00" * (frame_bytes - len(chunk))
        await websocket.send(encoder.encode(chunk, samples))
        await asyncio.sleep(0.06)


async def main(first_wav: str, second_wav: str) -> None:
    first_pcm = load_pcm(first_wav)
    second_pcm = load_pcm(second_wav)
    token = os.environ["DEVICE_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Device-Id": "barge-in-smoke-test",
        "Client-Id": "barge-in-smoke-test",
        "Protocol-Version": "1",
    }
    encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
    decoder = opuslib.Decoder(24000, 1)

    async with connect(
        "wss://ai.example.com/robot/ws/",
        additional_headers=headers,
        open_timeout=15,
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
        )
        assert json.loads(await websocket.recv())["type"] == "hello"
        await websocket.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
        await send_pcm(websocket, encoder, first_pcm)

        abort_sent_at = None
        first_stop_at = None
        frames_after_abort = 0
        stop_count = 0
        second_sender = None
        second_transcript = ""
        second_answers = []
        second_frames = 0

        while stop_count < 2:
            message = await asyncio.wait_for(websocket.recv(), timeout=90)
            now = time.monotonic()
            if isinstance(message, bytes):
                decoder.decode(message, 1440)
                if abort_sent_at is None:
                    abort_sent_at = now
                    await websocket.send(
                        json.dumps(
                            {"type": "abort", "reason": "wake_word_detected"}
                        )
                    )
                    await websocket.send(
                        json.dumps(
                            {"type": "listen", "state": "start", "mode": "auto"}
                        )
                    )
                    second_sender = asyncio.create_task(
                        send_pcm(websocket, encoder, second_pcm)
                    )
                elif stop_count == 0:
                    frames_after_abort += 1
                else:
                    second_frames += 1
                continue

            payload = json.loads(message)
            if payload.get("type") == "stt" and stop_count >= 1:
                second_transcript = payload.get("text", "")
            elif payload.get("type") == "tts":
                state = payload.get("state")
                if state == "sentence_start" and stop_count >= 1:
                    second_answers.append(payload.get("text", ""))
                elif state == "stop":
                    stop_count += 1
                    if stop_count == 1:
                        first_stop_at = now

        if second_sender is not None:
            await second_sender
        assert abort_sent_at is not None and first_stop_at is not None
        stop_latency_ms = int((first_stop_at - abort_sent_at) * 1000)
        assert stop_latency_ms < 1500, stop_latency_ms
        assert frames_after_abort <= 8, frames_after_abort
        assert second_transcript
        second_answer = "".join(second_answers)
        assert second_answer
        assert not any(marker in second_answer for marker in SERVICE_ERROR_MARKERS), second_answer
        assert second_frames > 0
        print(
            "barge-in-conversation-ok",
            f"stop_latency_ms={stop_latency_ms}",
            f"frames_after_abort={frames_after_abort}",
            f"followup_stt={second_transcript!r}",
            f"followup_answer={second_answer!r}",
            f"followup_frames={second_frames}",
        )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
