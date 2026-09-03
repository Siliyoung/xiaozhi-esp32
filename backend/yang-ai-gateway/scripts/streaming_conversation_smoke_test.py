"""Measure first-audio latency and sentence count through the public WSS."""

import asyncio
import json
import os
import sys
import time
import wave

import opuslib
from websockets.asyncio.client import connect


async def main(wav_path: str) -> None:
    with wave.open(wav_path, "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16000
        pcm = wav_file.readframes(wav_file.getnframes())
    pcm += b"\x00" * (16000 * 2 * 1200 // 1000)

    token = os.environ["DEVICE_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Device-Id": "streaming-conversation-smoke-test",
        "Client-Id": "streaming-conversation-smoke-test",
        "Protocol-Version": "1",
    }
    encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
    decoder = opuslib.Decoder(24000, 1)
    input_samples = 16000 * 60 // 1000
    input_bytes = input_samples * 2

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

        for offset in range(0, len(pcm), input_bytes):
            chunk = pcm[offset : offset + input_bytes]
            if len(chunk) < input_bytes:
                chunk += b"\x00" * (input_bytes - len(chunk))
            await websocket.send(encoder.encode(chunk, input_samples))
            await asyncio.sleep(0.06)
        input_finished = time.monotonic()

        transcript = ""
        answers = []
        sentence_starts = []
        first_audio_at = None
        output_frames = 0
        while True:
            message = await asyncio.wait_for(websocket.recv(), timeout=90)
            now = time.monotonic()
            if isinstance(message, bytes):
                decoder.decode(message, 1440)
                output_frames += 1
                if first_audio_at is None:
                    first_audio_at = now
                continue
            payload = json.loads(message)
            if payload.get("type") == "stt":
                transcript = payload.get("text", "")
            elif payload.get("type") == "tts":
                if payload.get("state") == "sentence_start":
                    answers.append(payload.get("text", ""))
                    sentence_starts.append(now)
                elif payload.get("state") == "stop":
                    stopped_at = now
                    break

        assert transcript
        assert first_audio_at is not None
        assert output_frames > 0
        assert len(answers) >= 2, answers
        print(
            "streaming-conversation-ok",
            f"sentences={len(answers)}",
            f"opus_frames={output_frames}",
            f"first_sentence_ms={int((sentence_starts[0] - input_finished) * 1000)}",
            f"first_audio_ms={int((first_audio_at - input_finished) * 1000)}",
            f"total_ms={int((stopped_at - input_finished) * 1000)}",
        )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
