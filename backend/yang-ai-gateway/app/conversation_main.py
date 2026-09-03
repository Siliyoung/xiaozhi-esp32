"""Production conversation entry point: VAD -> STT -> LLM -> TTS."""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import opuslib
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIWebSocketRoute

from app.dashscope_pipeline import DashScopePipeline
from app.fixed_reply import encode_wav_to_opus_frames
from app.main import app, compact_json, config, is_authorized, receive_device_hello
from app.voice_activity import UtteranceCollector, VadConfig


logger = logging.getLogger("yang-ai-gateway.conversation")
output_sample_rate = config.output_sample_rate
output_frame_ms = 60
pipeline = DashScopePipeline()

error_text = "语音服务暂时不可用，请稍后再试。"
error_wav = Path(__file__).resolve().parent.parent / "assets" / "service_error.wav"
error_frames = encode_wav_to_opus_frames(
    error_wav, output_sample_rate, output_frame_ms
)


def encode_pcm_to_opus(pcm: bytes) -> list[bytes]:
    frame_samples = output_sample_rate * output_frame_ms // 1000
    frame_bytes = frame_samples * 2
    encoder = opuslib.Encoder(output_sample_rate, 1, opuslib.APPLICATION_AUDIO)
    encoder.bitrate = 32000
    frames: list[bytes] = []
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += b"\x00" * (frame_bytes - len(chunk))
        frames.append(encoder.encode(chunk, frame_samples))
    if not frames:
        raise RuntimeError("cannot encode an empty TTS response")
    return frames


def control_message(session_id: str, message_type: str, **fields: object) -> str:
    return compact_json(
        {"session_id": session_id, "type": message_type, **fields}
    )


async def stream_answer(
    websocket: WebSocket,
    session_id: str,
    text: str,
    frames: list[bytes],
) -> None:
    await websocket.send_text(
        control_message(session_id, "tts", state="sentence_start", text=text)
    )
    for frame in frames:
        await websocket.send_bytes(frame)
        await asyncio.sleep(output_frame_ms / 1000)
    await websocket.send_text(
        control_message(session_id, "tts", state="sentence_end", text=text)
    )


# Retain the base OTA and health routes, replacing only its receive-only WS route.
app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (isinstance(route, APIWebSocketRoute) and route.path == "/robot/ws/")
]


@app.websocket("/robot/ws/")
async def robot_conversation_websocket(websocket: WebSocket) -> None:
    if not is_authorized(websocket):
        logger.warning(
            "WebSocket authentication rejected device=%s",
            websocket.headers.get("device-id", "unknown"),
        )
        await websocket.close(code=1008)
        return

    await websocket.accept()
    device_id = websocket.headers.get("device-id", "unknown")
    session_id = str(uuid.uuid4())
    collector = UtteranceCollector(
        VadConfig(
            silence_ms=int(os.getenv("VAD_SILENCE_MS", "900")),
            min_speech_ms=int(os.getenv("VAD_MIN_SPEECH_MS", "300")),
            max_utterance_ms=int(os.getenv("MAX_UTTERANCE_MS", "15000")),
            aggressiveness=int(os.getenv("VAD_AGGRESSIVENESS", "2")),
        )
    )
    history: list[dict[str, str]] = []
    received_frames = 0

    logger.info("Device connected device=%s session=%s", device_id, session_id)
    try:
        hello = await receive_device_hello(websocket)
        input_audio = hello.get("audio_params", {})
        if input_audio.get("sample_rate") != 16000:
            raise ValueError("conversation mode requires 16000 Hz device audio")

        await websocket.send_text(
            compact_json(
                {
                    "type": "hello",
                    "transport": "websocket",
                    "session_id": session_id,
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": output_sample_rate,
                        "channels": 1,
                        "frame_duration": output_frame_ms,
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
                        "Listen event session=%s state=%s mode=%s",
                        session_id,
                        payload.get("state"),
                        payload.get("mode"),
                    )
                    if payload.get("state") == "stop":
                        collector.reset()
                elif message_type == "abort":
                    collector.reset()
                    logger.info("Abort session=%s", session_id)
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
            await websocket.send_text(
                control_message(session_id, "tts", state="start")
            )

            try:
                result = await asyncio.to_thread(pipeline.process, utterance, history)
                await websocket.send_text(
                    control_message(session_id, "stt", text=result.transcript)
                )
                await websocket.send_text(
                    control_message(session_id, "llm", emotion="neutral")
                )
                frames = await asyncio.to_thread(encode_pcm_to_opus, result.pcm_24000)
                await stream_answer(
                    websocket, session_id, result.answer, frames
                )
                history.extend(
                    [
                        {"role": "user", "content": result.transcript},
                        {"role": "assistant", "content": result.answer},
                    ]
                )
                del history[:-8]
                logger.info(
                    "Conversation turn completed session=%s input_chars=%d output_chars=%d",
                    session_id,
                    len(result.transcript),
                    len(result.answer),
                )
            except Exception:
                logger.exception("Conversation pipeline failed session=%s", session_id)
                await stream_answer(
                    websocket, session_id, error_text, error_frames
                )
            finally:
                await websocket.send_text(
                    control_message(session_id, "tts", state="stop")
                )
                collector.reset()
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
