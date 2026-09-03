"""Exercise the public fixed-reply path and decode every returned Opus frame."""

import asyncio
import json
import os

import opuslib
from websockets.asyncio.client import connect


async def main() -> None:
    token = os.environ["DEVICE_TOKEN"]
    url = os.getenv("WSS_URL", "wss://ai.example.com/robot/ws/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Device-Id": "fixed-reply-smoke-test",
        "Client-Id": "fixed-reply-smoke-test",
        "Protocol-Version": "1",
    }
    async with connect(url, additional_headers=headers) as websocket:
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

        for _ in range(34):
            await websocket.send(b"test-input-frame")

        decoder = opuslib.Decoder(24000, 1)
        states: list[str] = []
        audio_frames = 0
        pcm_bytes = 0
        while "stop" not in states:
            message = await asyncio.wait_for(websocket.recv(), timeout=10)
            if isinstance(message, bytes):
                pcm = decoder.decode(message, 1440)
                audio_frames += 1
                pcm_bytes += len(pcm)
            else:
                payload = json.loads(message)
                if payload.get("type") == "tts":
                    states.append(payload["state"])

        assert states == ["start", "sentence_start", "sentence_end", "stop"]
        assert audio_frames > 0
        print(
            "fixed-reply-ok",
            f"states={','.join(states)}",
            f"opus_frames={audio_frames}",
            f"pcm_bytes={pcm_bytes}",
        )


if __name__ == "__main__":
    asyncio.run(main())
