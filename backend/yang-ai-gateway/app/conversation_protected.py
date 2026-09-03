"""Production-oriented multi-turn conversation endpoint with runtime guards."""

import asyncio
import json
import logging
import os
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute

from app import conversation_main as base
from app.dashscope_pipeline_protected import DashScopePipeline, NoSpeechRecognized
from app.main import compact_json, config, is_authorized, receive_device_hello
from app.runtime_guard import GuardConfig, SessionLimiter, UsageStore, device_key
from app.voice_activity import UtteranceCollector, VadConfig


logger = logging.getLogger("yang-ai-gateway.protected")
app = base.app
pipeline = DashScopePipeline()
guard_config = GuardConfig.from_environment()
usage_store = UsageStore(guard_config.usage_db_path, guard_config)
session_limiter = SessionLimiter(guard_config)
pipeline_slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_PIPELINES", "2")))

app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (isinstance(route, APIWebSocketRoute) and route.path == "/robot/ws/")
]


@app.get("/ready")
async def ready() -> dict:
    if not usage_store.check_ready():
        return {"status": "error", "service": "yang-ai-gateway"}
    return {"status": "ready", "service": "yang-ai-gateway"}


async def play_service_error(websocket: WebSocket, session_id: str) -> None:
    await websocket.send_text(base.control_message(session_id, "tts", state="start"))
    await base.stream_answer(websocket, session_id, base.error_text, base.error_frames)
    await websocket.send_text(base.control_message(session_id, "tts", state="stop"))


async def call_stage(stage: str, timeout: float, operation, *args):
    started = time.monotonic()
    try:
        async with pipeline_slots:
            result = await asyncio.wait_for(
                asyncio.to_thread(operation, *args), timeout=timeout
            )
        logger.info(
            "Stage timing stage=%s status=ok duration_ms=%d",
            stage,
            int((time.monotonic() - started) * 1000),
        )
        return result
    except Exception as exc:
        logger.error(
            "Stage timing stage=%s status=error duration_ms=%d error_type=%s",
            stage,
            int((time.monotonic() - started) * 1000),
            type(exc).__name__,
        )
        raise


@app.websocket("/robot/ws/")
async def robot_protected_websocket(websocket: WebSocket) -> None:
    if not is_authorized(websocket):
        logger.warning("WebSocket authentication rejected")
        await websocket.close(code=1008)
        return

    raw_device_id = websocket.headers.get("device-id", "unknown")
    raw_client_id = websocket.headers.get("client-id", "unknown")
    safe_device = device_key(raw_device_id, raw_client_id)
    acquired, reason = await session_limiter.acquire(safe_device)
    if not acquired:
        logger.warning("Session rejected device=%s reason=%s", safe_device, reason)
        await websocket.close(code=1013)
        return

    await websocket.accept()
    session_id = str(uuid.uuid4())
    session_started = time.monotonic()
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

    logger.info("Session opened device=%s session=%s", safe_device, session_id)
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

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if time.monotonic() - session_started >= guard_config.session_max_seconds:
                logger.info("Session expired device=%s session=%s", safe_device, session_id)
                await websocket.close(code=1000)
                return

            text = message.get("text")
            binary = message.get("bytes")
            if text is not None:
                payload = json.loads(text)
                if payload.get("type") == "listen" and payload.get("state") == "start":
                    awaiting_followup_start = False
                    collector.reset()
                    listen_deadline = time.monotonic() + followup_timeout
                elif payload.get("type") == "abort":
                    await websocket.close(code=1000)
                    return
                continue

            if binary is None:
                continue
            received_frames += 1
            if awaiting_followup_start:
                continue
            if time.monotonic() >= listen_deadline and collector.speech_ms == 0:
                await websocket.close(code=1000)
                return

            utterance = collector.feed_opus(binary)
            if utterance is None:
                continue

            decision = usage_store.reserve_turn(safe_device)
            if not decision.allowed:
                logger.warning(
                    "Turn rejected device=%s session=%s reason=%s",
                    safe_device,
                    session_id,
                    decision.reason,
                )
                await play_service_error(websocket, session_id)
                await websocket.close(code=1013)
                return
            logger.info(
                "Turn admitted device=%s session=%s device_daily=%d total_daily=%d",
                safe_device,
                session_id,
                decision.device_daily_turns,
                decision.total_daily_turns,
            )

            try:
                transcript = await call_stage(
                    "asr", float(os.getenv("ASR_STAGE_TIMEOUT_SECONDS", "45")),
                    pipeline.transcribe, utterance
                )
            except NoSpeechRecognized:
                empty_turns += 1
                collector.reset()
                if empty_turns >= max_empty_turns:
                    await websocket.close(code=1000)
                    return
                continue
            except Exception:
                await play_service_error(websocket, session_id)
                await websocket.close(code=1011)
                return

            empty_turns = 0
            await websocket.send_text(base.control_message(session_id, "tts", state="start"))
            await websocket.send_text(base.control_message(session_id, "stt", text=transcript))
            try:
                answer = await call_stage(
                    "llm", float(os.getenv("LLM_STAGE_TIMEOUT_SECONDS", "55")),
                    pipeline.generate_answer, transcript, history
                )
                pcm = await call_stage(
                    "tts", float(os.getenv("TTS_STAGE_TIMEOUT_SECONDS", "65")),
                    pipeline.synthesize, answer
                )
                frames = await asyncio.to_thread(base.encode_pcm_to_opus, pcm)
                await websocket.send_text(base.control_message(session_id, "llm", emotion="neutral"))
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
                    "Turn completed device=%s session=%s turn=%d input_chars=%d output_chars=%d",
                    safe_device,
                    session_id,
                    completed_turns,
                    len(transcript),
                    len(answer),
                )
            except Exception:
                await base.stream_answer(websocket, session_id, base.error_text, base.error_frames)
                await websocket.send_text(base.control_message(session_id, "tts", state="stop"))
                await websocket.close(code=1011)
                return

            if completed_turns >= guard_config.max_turns_per_session:
                await websocket.send_text(base.control_message(session_id, "tts", state="stop"))
                await websocket.close(code=1000)
                return
            awaiting_followup_start = True
            collector.reset()
            await websocket.send_text(base.control_message(session_id, "tts", state="stop"))
    except WebSocketDisconnect:
        pass
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Protocol rejected device=%s session=%s error_type=%s",
            safe_device,
            session_id,
            type(exc).__name__,
        )
        await websocket.close(code=1003)
    except Exception as exc:
        logger.error(
            "Session failed device=%s session=%s error_type=%s",
            safe_device,
            session_id,
            type(exc).__name__,
        )
    finally:
        await session_limiter.release(safe_device)
        logger.info(
            "Session closed device=%s session=%s frames=%d turns=%d duration_s=%d",
            safe_device,
            session_id,
            received_frames,
            completed_turns,
            int(time.monotonic() - session_started),
        )
