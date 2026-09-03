"""Verify that a duplicate session for the same device is rejected."""

import asyncio
import json
import os

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


def headers() -> dict[str, str]:
    token = os.environ["DEVICE_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Device-Id": "session-limit-smoke-test",
        "Client-Id": "session-limit-smoke-test",
        "Protocol-Version": "1",
    }


async def send_hello(websocket) -> None:
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
    response = json.loads(await websocket.recv())
    assert response["type"] == "hello"


async def main() -> None:
    url = "wss://ai.example.com/robot/ws/"
    async with connect(url, additional_headers=headers(), open_timeout=15) as first:
        await send_hello(first)
        rejected = False
        try:
            async with connect(
                url, additional_headers=headers(), open_timeout=15
            ) as second:
                try:
                    await send_hello(second)
                except ConnectionClosed as exc:
                    rejected = exc.code in {1008, 1013}
        except InvalidStatus as exc:
            rejected = exc.response.status_code in {403, 429, 503}
        assert rejected

    print("session-limit-ok duplicate_device_rejected=true")


if __name__ == "__main__":
    asyncio.run(main())
