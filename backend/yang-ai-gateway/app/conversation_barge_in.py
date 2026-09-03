"""Streaming conversation with wake-word barge-in during playback."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute

from app import conversation_streaming as streaming
from app.clock_context import clock_context_from_weather
from app.dashscope_pipeline_v3 import NoSpeechRecognized
from app.device_context import (
    reset_current_device_key,
    set_current_device_key,
)
from app.emotion_selector import select_response_emotion
from app.interruptible_audio import stream_answer_interruptible
from app.location_context import (
    reset_client_public_ip,
    set_client_public_ip,
)
from app.main import compact_json, config, is_authorized, receive_device_hello
from app.pomodoro_intent import is_direct_cancel
from app.pomodoro_tools import control_pomodoro, has_active_pomodoro
from app.runtime_guard_v2 import device_key
from app.server_status_stream import ServerStatusStream
from app.tool_events import reset_tool_event_handler, set_tool_event_handler
from app.voice_activity import UtteranceCollector, VadConfig


logger = logging.getLogger("yang-ai-gateway.barge-in")
app = streaming.app

app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (isinstance(route, APIWebSocketRoute) and route.path == "/robot/ws/")
]


def _client_public_ip(websocket: WebSocket) -> str | None:
    """Return a validated public client address without logging it."""
    candidates = [
        websocket.headers.get("x-real-ip", ""),
        websocket.headers.get("x-forwarded-for", "").split(",", 1)[0].strip(),
        websocket.client.host if websocket.client else "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_global:
            return address.compressed
    return None

async def _single_sentence(text: str):
    yield text


def is_session_exit(transcript: str) -> bool:
    """Match explicit short commands that end the current voice session."""
    text = re.sub(r"[\s，。！？、,.!?;；:：'\"“”‘’（）()]+", "", transcript)
    if not text or len(text) > 24:
        return False
    patterns = (
        r"你?(?:(?:先)?退下|退一下)(?:吧|了)?",
        r"(?:结束|退出)(?:这次|当前)?(?:对话|会话)",
        r"(?:回到|进入)(?:待机|待命)(?:页面|状态)?",
        r"(?:不用|不要)(?:再|继续)?(?:听|聆听)(?:了|啦|吧)?",
    )
    return any(re.fullmatch(pattern, text) for pattern in patterns)


def validate_barge_in_metric(payload: dict) -> dict[str, int] | None:
    if payload.get("type") != "client_metric" or payload.get("name") != "barge_in":
        return None
    limits = {
        "metric_id": (1, 0xFFFFFFFF),
        "round_trip_ms": (0, 60000),
        "local_clear_ms": (0, 10000),
        "wifi_rssi_dbm": (-127, 0),
        "free_sram_bytes": (0, 32 * 1024 * 1024),
        "min_free_sram_bytes": (0, 32 * 1024 * 1024),
        "uplink_frames_dropped": (0, 10000),
    }
    metric = {}
    for name, (minimum, maximum) in limits.items():
        value = payload.get(name)
        if type(value) is not int or not minimum <= value <= maximum:
            return None
        metric[name] = value
    if metric["min_free_sram_bytes"] > metric["free_sram_bytes"]:
        return None
    return metric


async def receive_loop(
    websocket: WebSocket,
    incoming: asyncio.Queue,
    interrupt_event: asyncio.Event,
    turn_active: asyncio.Event,
    safe_device: str,
    session_id: str,
    interrupt_timing: dict,
) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                interrupt_event.set()
                await incoming.put(message)
                return

            text = message.get("text")
            if text is not None:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {}
                if payload.get("type") == "client_metric":
                    metric = validate_barge_in_metric(payload)
                    if metric is None:
                        logger.warning(
                            "Client metric rejected device=%s session=%s",
                            safe_device, session_id,
                        )
                    else:
                        logger.info(
                            "Barge-in device metric device=%s session=%s metric_id=%d round_trip_ms=%d local_clear_ms=%d wifi_rssi_dbm=%d free_sram_bytes=%d min_free_sram_bytes=%d uplink_frames_dropped=%d",
                            safe_device, session_id, metric["metric_id"],
                            metric["round_trip_ms"], metric["local_clear_ms"],
                            metric["wifi_rssi_dbm"], metric["free_sram_bytes"],
                            metric["min_free_sram_bytes"], metric["uplink_frames_dropped"],
                        )
                    continue
                is_abort = payload.get("type") == "abort"
                is_restart = (
                    payload.get("type") == "listen"
                    and payload.get("state") == "start"
                    and turn_active.is_set()
                )
                if is_abort or is_restart:
                    interrupt_event.set()
                if is_abort:
                    interrupt_timing["received_at"] = time.monotonic()
                    metric_id = payload.get("metric_id", 0)
                    interrupt_timing["metric_id"] = (
                        metric_id
                        if type(metric_id) is int and 0 <= metric_id <= 0xFFFFFFFF
                        else 0
                    )

            # During playback a misconfigured realtime client could continue
            # uploading audio. Drop excess binary packets but never control JSON.
            if incoming.full() and message.get("bytes") is not None:
                continue
            await incoming.put(message)
    except (WebSocketDisconnect, RuntimeError):
        interrupt_event.set()
        await incoming.put({"type": "websocket.disconnect", "code": 1000})


async def handle_turn(
    websocket: WebSocket,
    session_id: str,
    safe_device: str,
    client_ip: str | None,
    utterance: bytes,
    history: list[dict[str, str]],
    interrupt_event: asyncio.Event,
    dashboard: ServerStatusStream,
    interrupt_timing: dict,
) -> tuple[str, str | None, int]:
    """Return status, transcript, and number of fully played sentences."""
    turn_started = time.monotonic()
    transcript = await streaming.call_stage(
        streaming.asr_slots,
        "asr",
        float(os.getenv("ASR_STAGE_TIMEOUT_SECONDS", "45")),
        streaming.pipeline.transcribe,
        utterance,
    )
    if interrupt_event.is_set():
        return "interrupted", transcript, 0

    await websocket.send_text(
        streaming.base.control_message(session_id, "tts", state="start")
    )
    await websocket.send_text(
        streaming.base.control_message(session_id, "stt", text=transcript)
    )
    await websocket.send_text(
        streaming.base.control_message(session_id, "llm", emotion="thinking")
    )

    loop = asyncio.get_running_loop()

    def forward_tool_event(name: str, payload: dict) -> None:
        if not payload.get("ok"):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        if name == "get_current_weather" and data.get("found"):
            coroutine = websocket.send_text(
                json.dumps(
                    {"type": "clock_context", "data": clock_context_from_weather(data)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        elif name == "get_server_status":
            coroutine = dashboard.start(data)
        elif name in ("start_pomodoro", "control_pomodoro"):
            command = data.get("device_command")
            if not isinstance(command, dict):
                return
            coroutine = websocket.send_text(
                json.dumps({"type": "pomodoro", **command}, ensure_ascii=False, separators=(",", ":"))
            )
        elif name in (
            "create_reminder", "cancel_reminder", "create_todo",
            "complete_todo", "delete_todo",
        ):
            command = data.get("device_command")
            if not isinstance(command, dict):
                return
            coroutine = websocket.send_text(
                json.dumps({"type": "reminder", **command}, ensure_ascii=False, separators=(",", ":"))
            )
        else:
            return
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        future.result(timeout=3)

    answer_parts: list[str] = []
    end_session = is_session_exit(transcript)
    first_audio_logged = False
    response_emotion_sent = False
    handler_token = set_tool_event_handler(forward_tool_event)
    location_token = set_client_public_ip(client_ip)
    device_token = set_current_device_key(safe_device)
    try:
        if end_session:
            sentence_stream = _single_sentence("好的，我先退下了。")
            logger.info(
                "Session exit routed device=%s session=%s transcript_chars=%d",
                safe_device, session_id, len(transcript),
            )
        elif is_direct_cancel(transcript, has_active_pomodoro()):
            result = control_pomodoro({"action": "cancel"})
            command = result.get("device_command")
            if not isinstance(command, dict):
                raise RuntimeError("Pomodoro cancel did not produce a device command")
            await websocket.send_text(
                json.dumps(
                    {"type": "pomodoro", **command},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            sentence_stream = _single_sentence("番茄钟已取消。")
            logger.info(
                "Pomodoro direct cancel routed device=%s session=%s transcript_chars=%d",
                safe_device,
                session_id,
                len(transcript),
            )
        else:
            sentence_stream = streaming.iter_sentences(transcript, history)
        async for sentence in sentence_stream:
            if interrupt_event.is_set():
                break
            if not response_emotion_sent:
                response_emotion = select_response_emotion(transcript, sentence)
                await websocket.send_text(
                    streaming.base.control_message(
                        session_id, "llm", emotion=response_emotion
                    )
                )
                response_emotion_sent = True
                logger.info(
                    "Response emotion selected device=%s session=%s emotion=%s",
                    safe_device, session_id, response_emotion,
                )
            pcm = await streaming.call_stage(
                streaming.tts_slots,
                "tts",
                float(os.getenv("TTS_STAGE_TIMEOUT_SECONDS", "65")),
                streaming.pipeline.synthesize,
                sentence,
            )
            if interrupt_event.is_set():
                break
            frames = await asyncio.to_thread(streaming.base.encode_pcm_to_opus, pcm)
            if not first_audio_logged:
                first_audio_logged = True
                logger.info(
                    "First audio ready device=%s session=%s latency_ms=%d",
                    safe_device,
                    session_id,
                    int((time.monotonic() - turn_started) * 1000),
                )
            completed = await stream_answer_interruptible(
                websocket,
                session_id,
                sentence,
                frames,
                interrupt_event,
                streaming.base.control_message,
                streaming.base.output_frame_ms,
            )
            if not completed:
                break
            answer_parts.append(sentence)
    finally:
        reset_tool_event_handler(handler_token)
        reset_current_device_key(device_token)
        reset_client_public_ip(location_token)

    if interrupt_event.is_set():
        await websocket.send_text(
            streaming.base.control_message(session_id, "tts", state="stop")
        )
        stop_sent_at = time.monotonic()
        abort_received_at = interrupt_timing.pop("received_at", None)
        metric_id = interrupt_timing.pop("metric_id", 0)
        if abort_received_at is not None:
            logger.info(
                "Barge-in server metric device=%s session=%s metric_id=%d processing_ms=%d",
                safe_device, session_id, metric_id,
                max(0, int((stop_sent_at - abort_received_at) * 1000)),
            )
        logger.info(
            "Turn interrupted device=%s session=%s played_sentences=%d latency_ms=%d",
            safe_device,
            session_id,
            len(answer_parts),
            int((time.monotonic() - turn_started) * 1000),
        )
        return "interrupted", transcript, len(answer_parts)

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
    logger.info(
        "Turn completed device=%s session=%s sentences=%d input_chars=%d output_chars=%d total_ms=%d",
        safe_device,
        session_id,
        len(answer_parts),
        len(transcript),
        len(answer),
        int((time.monotonic() - turn_started) * 1000),
    )
    return "ended" if end_session else "completed", transcript, len(answer_parts)


@app.websocket("/robot/ws/")
async def robot_barge_in_websocket(websocket: WebSocket) -> None:
    if not is_authorized(websocket):
        await websocket.close(code=1008)
        return

    safe_device = device_key(
        websocket.headers.get("device-id", "unknown"),
        websocket.headers.get("client-id", "unknown"),
    )
    client_ip = _client_public_ip(websocket)
    acquired, reason = await streaming.session_limiter.acquire(safe_device)
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
    incoming: asyncio.Queue = asyncio.Queue(maxsize=512)
    interrupt_event = asyncio.Event()
    turn_active = asyncio.Event()
    interrupt_timing: dict = {}
    receiver_task = None
    dashboard = ServerStatusStream(websocket, session_id, safe_device)
    logger.info("Session opened device=%s session=%s barge_in=true", safe_device, session_id)

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
                        "frame_duration": streaming.base.output_frame_ms,
                    },
                }
            )
        )
        receiver_task = asyncio.create_task(
            receive_loop(
                websocket, incoming, interrupt_event, turn_active,
                safe_device, session_id, interrupt_timing,
            ),
            name=f"ws-receiver-{session_id}",
        )

        while True:
            message = await incoming.get()
            if message.get("type") == "websocket.disconnect":
                break
            if not dashboard.active and time.monotonic() - session_started >= streaming.guard_config.session_max_seconds:
                await websocket.close(code=1000)
                return

            text = message.get("text")
            binary = message.get("bytes")
            if text is not None:
                payload = json.loads(text)
                if payload.get("type") == "listen" and payload.get("state") == "start":
                    interrupt_event.clear()
                    awaiting_followup_start = False
                    collector.reset()
                    listen_deadline = time.monotonic() + followup_timeout
                    logger.info(
                        "Listening started device=%s session=%s mode=%s",
                        safe_device,
                        session_id,
                        payload.get("mode"),
                    )
                elif payload.get("type") == "abort":
                    if dashboard.active:
                        await dashboard.stop(notify_device=True)
                    awaiting_followup_start = True
                    collector.reset()
                    logger.info(
                        "Abort received device=%s session=%s reason=%s",
                        safe_device,
                        session_id,
                        payload.get("reason"),
                    )
                continue

            if binary is None:
                continue
            received_frames += 1
            if awaiting_followup_start:
                continue
            if not dashboard.active and time.monotonic() >= listen_deadline and collector.speech_ms == 0:
                await websocket.close(code=1000)
                return
            utterance = collector.feed_opus(binary)
            if utterance is None:
                continue

            decision = streaming.usage_store.reserve_turn(safe_device)
            if not decision.allowed:
                await streaming.play_service_error(websocket, session_id)
                await websocket.close(code=1013)
                return

            turn_active.set()
            try:
                status, _, _ = await handle_turn(
                    websocket,
                    session_id,
                    safe_device,
                    client_ip,
                    utterance,
                    history,
                    interrupt_event,
                    dashboard,
                    interrupt_timing,
                )
            except NoSpeechRecognized:
                empty_turns += 1
                collector.reset()
                if empty_turns >= max_empty_turns:
                    await websocket.close(code=1000)
                    return
                continue
            except Exception as exc:
                logger.error(
                    "Turn failed device=%s session=%s error_type=%s",
                    safe_device,
                    session_id,
                    type(exc).__name__,
                )
                if not interrupt_event.is_set():
                    await streaming.play_service_error(websocket, session_id)
                await websocket.close(code=1011)
                return
            finally:
                turn_active.clear()

            if status == "interrupted":
                awaiting_followup_start = True
                collector.reset()
                continue

            if status == "ended":
                await websocket.send_text(
                    streaming.base.control_message(session_id, "tts", state="stop")
                )
                await websocket.close(code=1000)
                return

            empty_turns = 0
            completed_turns += 1
            if not dashboard.active and completed_turns >= streaming.guard_config.max_turns_per_session:
                await websocket.send_text(
                    streaming.base.control_message(session_id, "tts", state="stop")
                )
                await websocket.close(code=1000)
                return
            awaiting_followup_start = True
            collector.reset()
            await websocket.send_text(
                streaming.base.control_message(session_id, "tts", state="stop")
            )
            if dashboard.active:
                await dashboard.enter_passive_mode()
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
        interrupt_event.set()
        await dashboard.stop()
        if receiver_task is not None:
            receiver_task.cancel()
            try:
                await receiver_task
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
        await streaming.session_limiter.release(safe_device)
        logger.info(
            "Session closed device=%s session=%s frames=%d turns=%d duration_s=%d",
            safe_device,
            session_id,
            received_frames,
            completed_turns,
            int(time.monotonic() - session_started),
        )
