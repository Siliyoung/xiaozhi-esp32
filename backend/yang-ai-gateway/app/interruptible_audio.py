"""Low-latency interruptible Opus frame delivery."""

import asyncio


async def stream_answer_interruptible(
    websocket,
    session_id: str,
    text: str,
    frames: list[bytes],
    interrupt_event: asyncio.Event,
    message_factory,
    frame_duration_ms: int = 60,
) -> bool:
    """Return False when playback was interrupted before sentence completion."""
    if interrupt_event.is_set():
        return False
    await websocket.send_text(
        message_factory(session_id, "tts", state="sentence_start", text=text)
    )
    for frame in frames:
        if interrupt_event.is_set():
            return False
        await websocket.send_bytes(frame)
        try:
            await asyncio.wait_for(
                interrupt_event.wait(), timeout=frame_duration_ms / 1000
            )
            return False
        except asyncio.TimeoutError:
            pass
    if interrupt_event.is_set():
        return False
    await websocket.send_text(
        message_factory(session_id, "tts", state="sentence_end", text=text)
    )
    return True
