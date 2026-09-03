"""One wake-up, one answer; ambient noise ends silently instead of looping."""

import asyncio
import json
import logging
import os
import uuid

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute

from app import conversation_main as base
from app.dashscope_pipeline_v3 import DashScopePipeline, NoSpeechRecognized
from app.main import compact_json, config, is_authorized, receive_device_hello
from app.voice_activity import UtteranceCollector, VadConfig


logger = logging.getLogger("yang-ai-gateway.single-turn")
app = base.app
pipeline = DashScopePipeline()

app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (isinstance(route, APIWebSocketRoute) and route.path == "/robot/ws/")
]


async def finish_session(websocket: WebSocket, session_id: str) -> None:
    await websocket.send_text(
        base.control_message(session_id, "tts", state="stop")
    )
    await asyncio.sleep(0.1)
    await websocket.close(code=1000)


@app.websocket("/robot/ws/")
async def robot_single_turn_websocket(websocket: WebSocket) -> None:
    if not is_authorized(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    device_id = websocket.headers.get("device-id", "unknown")
    session_id = str(uuid.uuid4())
    collector = UtteranceCollector(
        VadConfig(
            silence_ms=int(os.getenv("VAD_SILENCE_MS", "900")),
            min_speech_ms=int(os.getenv("VAD_MIN_SPEECH_MS", "450")),
            max_utterance_ms=int(os.getenv("MAX_UTTERANCE_MS", "15000")),
            aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "3")),
        )
    )
    received_frames = 0
    logger.info("Device connected device=%s session=%s", device_id, session_id)

    try:
        hello = await receive_device_hello(websocket)
        if hello.get("audio_params", {}).get("sample_rate") != 16000:
            raise ValueError("conversation mode requires 16000 Hz device audio")
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
                        "frame_duration": base.output_frame_ms,
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
                payload = json.loads(text)
                if payload.get("type") == "listen":
                    logger.info(
                        "Listen event session=%s state=%s mode=%s",
                        session_id,
                        payload.get("state"),
                        payload.get("mode"),
                    )
                elif payload.get("type") == "abort":
                    logger.info("Abort session=%s", session_id)
                    await websocket.close(code=1000)
                    return
                continue

            if binary is None:
                continue
            received_frames += 1
            utterance = collector.feed_opus(binary)
            if utterance is None:
                continue

            duration_ms = len(utterance) * 1000 // (16000 * 2)
            logger.info(
                "Utterance completed session=%s duration_ms=%d",
                session_id,
                duration_ms,
            )

            try:
                transcript = await asyncio.to_thread(pipeline.transcribe, utterance)
            except NoSpeechRecognized:
                logger.info("No speech recognized; ending silently session=%s", session_id)
                await websocket.close(code=1000)
                return

            await websocket.send_text(
                base.control_message(session_id, "tts", state="start")
            )
            await websocket.send_text(
                base.control_message(session_id, "stt", text=transcript)
            )

            try:
                answer = await asyncio.to_thread(
                    pipeline.generate_answer, transcript, []
                )
                pcm = await asyncio.to_thread(pipeline.synthesize, answer)
                frames = await asyncio.to_thread(base.encode_pcm_to_opus, pcm)
                await websocket.send_text(
                    base.control_message(session_id, "llm", emotion="neutral")
                )
                await base.stream_answer(websocket, session_id, answer, frames)
                logger.info(
                    "Conversation turn completed session=%s input_chars=%d output_chars=%d",
                    session_id,
                    len(transcript),
                    len(answer),
                )
            except Exception:
                logger.exception("Conversation pipeline failed session=%s", session_id)
                await base.stream_answer(
                    websocket, session_id, base.error_text, base.error_frames
                )

            await finish_session(websocket, session_id)
            return
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket session failed session=%s", session_id)
    finally:
        logger.info(
            "Device disconnected device=%s session=%s received_frames=%d",
            device_id,
            session_id,
            received_frames,
        )
