"""Controlled multi-turn conversation with silence and stale-audio protection."""

import asyncio
import json
import logging
import os
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute

from app import conversation_main as base
from app.dashscope_pipeline_v3 import DashScopePipeline, NoSpeechRecognized
from app.main import compact_json, config, is_authorized, receive_device_hello
from app.voice_activity import UtteranceCollector, VadConfig


logger = logging.getLogger("yang-ai-gateway.multi-turn")
app = base.app
pipeline = DashScopePipeline()

app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (isinstance(route, APIWebSocketRoute) and route.path == "/robot/ws/")
]


@app.websocket("/robot/ws/")
async def robot_multi_turn_websocket(websocket: WebSocket) -> None:
    if not is_authorized(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    device_id = websocket.headers.get("device-id", "unknown")
    session_id = str(uuid.uuid4())
    followup_timeout = float(os.getenv("FOLLOWUP_TIMEOUT_SECONDS", "20"))
    max_empty_turns = int(os.getenv("MAX_EMPTY_TURNS", "3"))
    collector = UtteranceCollector(
        VadConfig(
            silence_ms=int(os.getenv("VAD_SILENCE_MS", "900")),
            min_speech_ms=int(os.getenv("VAD_MIN_SPEECH_MS", "300")),
            max_utterance_ms=int(os.getenv("MAX_UTTERANCE_MS", "15000")),
            aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "3")),
        )
    )
    history: list[dict[str, str]] = []
    received_frames = 0
    completed_turns = 0
    empty_turns = 0
    awaiting_followup_start = False
    listen_deadline = time.monotonic() + followup_timeout

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
                message_type = payload.get("type")
                if message_type == "listen":
                    logger.info(
                        "Listen event session=%s state=%s mode=%s turn=%d",
                        session_id,
                        payload.get("state"),
                        payload.get("mode"),
                        completed_turns,
                    )
                    if payload.get("state") == "start":
                        awaiting_followup_start = False
                        collector.reset()
                        listen_deadline = time.monotonic() + followup_timeout
                elif message_type == "abort":
                    logger.info("Abort session=%s", session_id)
                    await websocket.close(code=1000)
                    return
                continue

            if binary is None:
                continue
            received_frames += 1

            # Frames queued while STT/LLM/TTS was running belong to the previous
            # turn. Wait for the device's fresh listen:start before accepting more.
            if awaiting_followup_start:
                continue

            if (
                time.monotonic() >= listen_deadline
                and collector.speech_ms == 0
            ):
                logger.info(
                    "Follow-up timeout session=%s turns=%d",
                    session_id,
                    completed_turns,
                )
                await websocket.close(code=1000)
                return

            utterance = collector.feed_opus(binary)
            if utterance is None:
                continue

            duration_ms = len(utterance) * 1000 // (16000 * 2)
            logger.info(
                "Utterance completed session=%s duration_ms=%d turn=%d",
                session_id,
                duration_ms,
                completed_turns + 1,
            )

            try:
                transcript = await asyncio.to_thread(pipeline.transcribe, utterance)
            except NoSpeechRecognized:
                empty_turns += 1
                collector.reset()
                logger.info(
                    "No speech recognized session=%s empty_turns=%d",
                    session_id,
                    empty_turns,
                )
                if empty_turns >= max_empty_turns:
                    await websocket.close(code=1000)
                    return
                continue
            except Exception:
                logger.exception("ASR failed session=%s", session_id)
                await websocket.send_text(
                    base.control_message(session_id, "tts", state="start")
                )
                await base.stream_answer(
                    websocket, session_id, base.error_text, base.error_frames
                )
                await websocket.send_text(
                    base.control_message(session_id, "tts", state="stop")
                )
                await websocket.close(code=1011)
                return

            empty_turns = 0
            await websocket.send_text(
                base.control_message(session_id, "tts", state="start")
            )
            await websocket.send_text(
                base.control_message(session_id, "stt", text=transcript)
            )

            try:
                answer = await asyncio.to_thread(
                    pipeline.generate_answer, transcript, history
                )
                pcm = await asyncio.to_thread(pipeline.synthesize, answer)
                frames = await asyncio.to_thread(base.encode_pcm_to_opus, pcm)
                await websocket.send_text(
                    base.control_message(session_id, "llm", emotion="neutral")
                )
                await base.stream_answer(websocket, session_id, answer, frames)
                history.extend(
                    [
                        {"role": "user", "content": transcript},
                        {"role": "assistant", "content": answer},
                    ]
                )
                del history[:-8]
                completed_turns += 1
                logger.info(
                    "Conversation turn completed session=%s turn=%d input_chars=%d output_chars=%d",
                    session_id,
                    completed_turns,
                    len(transcript),
                    len(answer),
                )
            except Exception:
                logger.exception("Conversation pipeline failed session=%s", session_id)
                await base.stream_answer(
                    websocket, session_id, base.error_text, base.error_frames
                )
                await websocket.send_text(
                    base.control_message(session_id, "tts", state="stop")
                )
                await websocket.close(code=1011)
                return

            awaiting_followup_start = True
            collector.reset()
            await websocket.send_text(
                base.control_message(session_id, "tts", state="stop")
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket session failed session=%s", session_id)
    finally:
        logger.info(
            "Device disconnected device=%s session=%s frames=%d turns=%d",
            device_id,
            session_id,
            received_frames,
            completed_turns,
        )
