"""Stage 1.5 entry point: reply once with a bundled Opus voice message."""

import json
import logging
import os

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute

from app.fixed_reply import encode_wav_to_opus_frames, send_fixed_reply
from app.main import app, compact_json, config, is_authorized, receive_device_hello


logger = logging.getLogger("yang-ai-gateway.fixed-reply-main")
reply_text = os.getenv("FIXED_REPLY_TEXT", "你好，我已经连接到你的服务器。")
reply_after_frames = int(os.getenv("FIXED_REPLY_AFTER_FRAMES", "34"))
reply_frame_duration_ms = 60
reply_wav = os.getenv(
    "FIXED_REPLY_WAV",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fixed_reply.wav"),
)
reply_frames = encode_wav_to_opus_frames(
    reply_wav,
    config.output_sample_rate,
    reply_frame_duration_ms,
)

# Replace the base gateway's receive-only WebSocket route while retaining its
# health and OTA routes. Keeping this as a separate entry point makes rollback
# to app.main immediate.
app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (isinstance(route, APIWebSocketRoute) and route.path == "/robot/ws/")
]


@app.websocket("/robot/ws/")
async def robot_websocket_with_fixed_reply(websocket: WebSocket) -> None:
    if not is_authorized(websocket):
        logger.warning(
            "WebSocket authentication rejected device=%s",
            websocket.headers.get("device-id", "unknown"),
        )
        await websocket.close(code=1008)
        return

    await websocket.accept()
    device_id = websocket.headers.get("device-id", "unknown")
    client_id = websocket.headers.get("client-id", "unknown")
    protocol_version = websocket.headers.get("protocol-version", "1")

    import uuid

    session_id = str(uuid.uuid4())
    audio_frames = 0
    audio_bytes = 0
    reply_sent = False

    logger.info(
        "Device connected device=%s client=%s protocol=%s session=%s",
        device_id,
        client_id,
        protocol_version,
        session_id,
    )

    try:
        hello = await receive_device_hello(websocket)
        logger.info(
            "Device hello session=%s audio_params=%s features=%s",
            session_id,
            hello.get("audio_params"),
            hello.get("features"),
        )
        await websocket.send_text(
            compact_json(
                {
                    "type": "hello",
                    "transport": "websocket",
                    "session_id": session_id,
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": config.output_sample_rate,
                        "channels": 1,
                        "frame_duration": reply_frame_duration_ms,
                    },
                }
            )
        )
        logger.info("Handshake completed session=%s", session_id)

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            text = message.get("text")
            binary = message.get("bytes")
            if text is not None:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON session=%s", session_id)
                    continue
                message_type = payload.get("type")
                if message_type == "listen":
                    logger.info(
                        "Listen event session=%s state=%s mode=%s text=%s",
                        session_id,
                        payload.get("state"),
                        payload.get("mode"),
                        payload.get("text"),
                    )
                elif message_type == "abort":
                    logger.info(
                        "Abort session=%s reason=%s",
                        session_id,
                        payload.get("reason"),
                    )
                elif message_type == "mcp":
                    logger.info("MCP message session=%s", session_id)
                else:
                    logger.info("Device JSON session=%s type=%s", session_id, message_type)
            elif binary is not None:
                audio_frames += 1
                audio_bytes += len(binary)
                if audio_frames % 20 == 0:
                    logger.info(
                        "Opus received session=%s frames=%d bytes=%d",
                        session_id,
                        audio_frames,
                        audio_bytes,
                    )
                if not reply_sent and audio_frames >= reply_after_frames:
                    reply_sent = True
                    await send_fixed_reply(
                        websocket,
                        session_id,
                        reply_text,
                        reply_frames,
                        reply_frame_duration_ms,
                    )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Unhandled WebSocket error session=%s", session_id)
    finally:
        logger.info(
            "Device disconnected device=%s session=%s frames=%d bytes=%d",
            device_id,
            session_id,
            audio_frames,
            audio_bytes,
        )
