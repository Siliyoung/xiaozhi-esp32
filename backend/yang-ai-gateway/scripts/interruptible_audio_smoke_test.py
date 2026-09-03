"""Verify that frame delivery stops promptly when an abort event arrives."""

import asyncio
import time

from app.interruptible_audio import stream_answer_interruptible


class FakeWebSocket:
    def __init__(self) -> None:
        self.text = []
        self.frames = []

    async def send_text(self, value: str) -> None:
        self.text.append(value)

    async def send_bytes(self, value: bytes) -> None:
        self.frames.append(value)


def message(session_id: str, message_type: str, **fields) -> str:
    return f"{session_id}:{message_type}:{fields.get('state')}"


async def main() -> None:
    websocket = FakeWebSocket()
    interrupted = asyncio.Event()

    async def trigger() -> None:
        await asyncio.sleep(0.13)
        interrupted.set()

    trigger_task = asyncio.create_task(trigger())
    started = time.monotonic()
    completed = await stream_answer_interruptible(
        websocket,
        "session",
        "一段很长的测试语音。",
        [b"opus"] * 100,
        interrupted,
        message,
        60,
    )
    await trigger_task
    elapsed_ms = int((time.monotonic() - started) * 1000)
    assert not completed
    assert len(websocket.frames) <= 3
    assert elapsed_ms < 250
    print(
        "interruptible-audio-ok",
        f"frames_before_stop={len(websocket.frames)}",
        f"elapsed_ms={elapsed_ms}",
    )


if __name__ == "__main__":
    asyncio.run(main())
