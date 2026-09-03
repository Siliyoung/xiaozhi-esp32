"""Exercise real VAD -> STT -> LLM -> TTS through the public WSS endpoint."""

import asyncio
import json
import os
import sys
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
        "Device-Id": "conversation-smoke-test",
        "Client-Id": "conversation-smoke-test",
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
        hello = json.loads(await websocket.recv())
        assert hello["type"] == "hello"
        await websocket.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))

        for offset in range(0, len(pcm), input_bytes):
            chunk = pcm[offset : offset + input_bytes]
            if len(chunk) < input_bytes:
                chunk += b"\x00" * (input_bytes - len(chunk))
            await websocket.send(encoder.encode(chunk, input_samples))
            await asyncio.sleep(0.06)

        states: list[str] = []
        transcript = ""
        answer = ""
        output_frames = 0
        while "stop" not in states:
            message = await asyncio.wait_for(websocket.recv(), timeout=45)
            if isinstance(message, bytes):
                decoder.decode(message, 1440)
                output_frames += 1
                continue
            payload = json.loads(message)
            if payload.get("type") == "tts":
                states.append(payload["state"])
                if payload["state"] == "sentence_start":
                    answer = payload.get("text", "")
            elif payload.get("type") == "stt":
                transcript = payload.get("text", "")

        assert transcript
        assert answer
        assert output_frames > 0
        print(
            "conversation-ok",
            f"stt={transcript!r}",
            f"answer={answer!r}",
            f"opus_frames={output_frames}",
        )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
