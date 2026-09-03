"""Verify that one public WebSocket session can complete two voice turns."""

import asyncio
import json
import os
import sys
import wave

import opuslib
from websockets.asyncio.client import connect


def load_pcm(wav_path: str) -> bytes:
    with wave.open(wav_path, "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16000
        pcm = wav_file.readframes(wav_file.getnframes())
    return pcm + b"\x00" * (16000 * 2 * 1200 // 1000)


async def send_utterance(websocket, encoder, pcm: bytes) -> None:
    input_samples = 16000 * 60 // 1000
    input_bytes = input_samples * 2
    await websocket.send(
        json.dumps({"type": "listen", "state": "start", "mode": "auto"})
    )
    for offset in range(0, len(pcm), input_bytes):
        chunk = pcm[offset : offset + input_bytes]
        if len(chunk) < input_bytes:
            chunk += b"\x00" * (input_bytes - len(chunk))
        await websocket.send(encoder.encode(chunk, input_samples))
        await asyncio.sleep(0.06)


async def receive_turn(websocket, decoder) -> tuple[str, str, int]:
    transcript = ""
    answer = ""
    output_frames = 0
    while True:
        message = await asyncio.wait_for(websocket.recv(), timeout=60)
        if isinstance(message, bytes):
            decoder.decode(message, 1440)
            output_frames += 1
            continue
        payload = json.loads(message)
        if payload.get("type") == "stt":
            transcript = payload.get("text", "")
        elif payload.get("type") == "tts":
            if payload.get("state") == "sentence_start":
                answer = payload.get("text", "")
            elif payload.get("state") == "stop":
                break
    assert transcript
    assert answer
    assert output_frames > 0
    return transcript, answer, output_frames


async def main(wav_path: str) -> None:
    pcm = load_pcm(wav_path)
    token = os.environ["DEVICE_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Device-Id": "multi-turn-smoke-test",
        "Client-Id": "multi-turn-smoke-test",
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
        hello = json.loads(await websocket.recv())
        assert hello["type"] == "hello"

        results = []
        for _ in range(2):
            await send_utterance(websocket, encoder, pcm)
            results.append(await receive_turn(websocket, decoder))

        for turn, (transcript, answer, output_frames) in enumerate(results, 1):
            print(
                f"turn-{turn}-ok",
                f"stt={transcript!r}",
                f"answer={answer!r}",
                f"opus_frames={output_frames}",
            )
        print("multi-turn-ok same_websocket=true turns=2")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
