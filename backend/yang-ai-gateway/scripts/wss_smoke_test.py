"""Verify the public WebSocket route without printing the device token."""

import asyncio
import json
import os

from websockets.asyncio.client import connect


async def main() -> None:
    token = os.environ["DEVICE_TOKEN"]
    url = os.getenv("WSS_URL", "wss://ai.example.com/robot/ws/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Device-Id": "deployment-smoke-test",
        "Client-Id": "deployment-smoke-test",
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
        response = json.loads(await websocket.recv())
        assert response["type"] == "hello"
        assert response["transport"] == "websocket"
        print(
            "wss-ok",
            f"session_id={response['session_id']}",
            f"sample_rate={response['audio_params']['sample_rate']}",
        )


if __name__ == "__main__":
    asyncio.run(main())
