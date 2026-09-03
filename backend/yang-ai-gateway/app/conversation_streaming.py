"""Multi-turn conversation with incremental LLM and sentence-level TTS."""

import asyncio
import json
import logging
import os
import threading
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute

from app import conversation_main as base
from app.dashscope_pipeline_tools import DashScopeStreamingPipeline
from app.dashscope_pipeline_v3 import NoSpeechRecognized
from app.email_alerting import EmailAlertConfig, EmailAlertManager
from app.main import compact_json, config, is_authorized, receive_device_hello
from app.runtime_guard_email import UsageStore
from app.runtime_guard_v2 import GuardConfig, SessionLimiter, device_key
from app.streaming_text import SentenceSegmenter
from app.voice_activity import UtteranceCollector, VadConfig


logger = logging.getLogger("yang-ai-gateway.streaming")
app = base.app
pipeline = DashScopeStreamingPipeline()
guard_config = GuardConfig.from_environment()
email_alert_manager = EmailAlertManager(
    guard_config.usage_db_path, EmailAlertConfig.from_environment()
)
usage_store = UsageStore(
    guard_config.usage_db_path, guard_config, email_alert_manager
)
session_limiter = SessionLimiter(guard_config)
asr_slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_ASR", "2")))
llm_slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_LLM", "2")))
tts_slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_TTS", "2")))

app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (isinstance(route, APIWebSocketRoute) and route.path == "/robot/ws/")
]


@app.get("/ready")
async def ready() -> dict:
    return {
        "status": "ready" if usage_store.check_ready() else "error",
        "service": "yang-ai-gateway",
        "mode": "streaming-function-calling",
    }


app.router.add_event_handler("startup", email_alert_manager.start)
app.router.add_event_handler("shutdown", email_alert_manager.stop)


async def play_service_error(websocket: WebSocket, session_id: str) -> None:
    await websocket.send_text(base.control_message(session_id, "tts", state="start"))
    await base.stream_answer(websocket, session_id, base.error_text, base.error_frames)
    await websocket.send_text(base.control_message(session_id, "tts", state="stop"))


async def call_stage(semaphore, stage: str, timeout: float, operation, *args):
    started = time.monotonic()
    try:
        async with semaphore:
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


async def iter_llm_deltas(transcript: str, history: list[dict[str, str]]):
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    stop_event = threading.Event()
    deadline = time.monotonic() + float(
        os.getenv("LLM_STREAM_TIMEOUT_SECONDS", "60")
    )

    def publish(item) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def produce() -> None:
        try:
            for delta in pipeline.iter_answer_deltas(transcript, history):
                if stop_event.is_set():
                    break
                publish(("delta", delta))
        except Exception as exc:
            publish(("error", exc))
        finally:
            publish(("done", None))

    async with llm_slots:
        producer_task = asyncio.create_task(asyncio.to_thread(produce))
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError("LLM stream timed out")
                kind, value = await asyncio.wait_for(queue.get(), timeout=remaining)
                if kind == "delta":
                    yield value
                elif kind == "error":
                    raise value
                else:
                    break
        finally:
            stop_event.set()
            try:
                await asyncio.wait_for(producer_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                producer_task.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )


async def iter_sentences(transcript: str, history: list[dict[str, str]]):
    segmenter = SentenceSegmenter(
        max_chars=int(os.getenv("STREAM_SENTENCE_MAX_CHARS", "80"))
    )
    async for delta in iter_llm_deltas(transcript, history):
        for sentence in segmenter.feed(delta):
            yield sentence
    for sentence in segmenter.flush():
        yield sentence


@app.websocket("/robot/ws/")
async def robot_streaming_websocket(websocket: WebSocket) -> None:
    if not is_authorized(websocket):
        await websocket.close(code=1008)
        return

    safe_device = device_key(
        websocket.headers.get("device-id", "unknown"),
        websocket.headers.get("client-id", "unknown"),
    )
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
    completed_turns = 0
    empty_turns = 0
    received_frames = 0
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

            turn_started = time.monotonic()
            decision = usage_store.reserve_turn(safe_device)
            if not decision.allowed:
                logger.warning(
                    "Turn rejected device=%s reason=%s", safe_device, decision.reason
                )
                await play_service_error(websocket, session_id)
                await websocket.close(code=1013)
                return

            try:
                transcript = await call_stage(
                    asr_slots,
                    "asr",
                    float(os.getenv("ASR_STAGE_TIMEOUT_SECONDS", "45")),
                    pipeline.transcribe,
                    utterance,
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
            await websocket.send_text(base.control_message(session_id, "llm", emotion="neutral"))

            answer_parts: list[str] = []
            first_audio_logged = False
            try:
                async for sentence in iter_sentences(transcript, history):
                    pcm = await call_stage(
                        tts_slots,
                        "tts",
                        float(os.getenv("TTS_STAGE_TIMEOUT_SECONDS", "65")),
                        pipeline.synthesize,
                        sentence,
                    )
                    frames = await asyncio.to_thread(base.encode_pcm_to_opus, pcm)
                    if not first_audio_logged:
                        first_audio_logged = True
                        logger.info(
                            "First audio ready device=%s session=%s latency_ms=%d",
                            safe_device,
                            session_id,
                            int((time.monotonic() - turn_started) * 1000),
                        )
                    await base.stream_answer(websocket, session_id, sentence, frames)
                    answer_parts.append(sentence)

                if not answer_parts:
                    raise RuntimeError("LLM stream returned no sentences")
                answer = "".join(answer_parts)
                history.extend(
                    [
                        {"role": "user", "content": transcript},
                        {"role": "assistant", "content": answer},
                    ]
                )
                del history[:-8]
                completed_turns += 1
                logger.info(
                    "Streaming turn completed device=%s session=%s turn=%d sentences=%d input_chars=%d output_chars=%d total_ms=%d",
                    safe_device,
                    session_id,
                    completed_turns,
                    len(answer_parts),
                    len(transcript),
                    len(answer),
                    int((time.monotonic() - turn_started) * 1000),
                )
            except Exception as exc:
                logger.error(
                    "Streaming turn failed device=%s session=%s played_sentences=%d error_type=%s",
                    safe_device,
                    session_id,
                    len(answer_parts),
                    type(exc).__name__,
                )
                if not answer_parts:
                    await base.stream_answer(
                        websocket, session_id, base.error_text, base.error_frames
                    )
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
