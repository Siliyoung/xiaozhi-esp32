import asyncio
import hmac
import ipaddress
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.clock_context import build_clock_context


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("yang-ai-gateway")


@dataclass(frozen=True)
class Config:
    public_base_url: str
    device_token: str
    websocket_version: int
    output_sample_rate: int
    hello_timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "Config":
        public_base_url = os.getenv(
            "PUBLIC_BASE_URL", "https://ai.example.com"
        ).rstrip("/")
        device_token = os.getenv("DEVICE_TOKEN", "")
        if len(device_token) < 32:
            raise RuntimeError("DEVICE_TOKEN must contain at least 32 characters")

        return cls(
            public_base_url=public_base_url,
            device_token=device_token,
            websocket_version=int(os.getenv("WEBSOCKET_VERSION", "1")),
            output_sample_rate=int(os.getenv("OUTPUT_SAMPLE_RATE", "24000")),
            hello_timeout_seconds=float(os.getenv("HELLO_TIMEOUT_SECONDS", "10")),
        )

    @property
    def websocket_url(self) -> str:
        if self.public_base_url.startswith("https://"):
            base = "wss://" + self.public_base_url.removeprefix("https://")
        elif self.public_base_url.startswith("http://"):
            base = "ws://" + self.public_base_url.removeprefix("http://")
        else:
            raise RuntimeError("PUBLIC_BASE_URL must start with http:// or https://")
        return f"{base}/robot/ws/"


config = Config.from_environment()
app = FastAPI(title="Yang AI Gateway", version="0.1.0")


def _request_public_ip(request: Request) -> str | None:
    candidates = [
        request.headers.get("x-real-ip", ""),
        request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip(),
        request.client.host if request.client else "",
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


def compact_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def is_authorized(websocket: WebSocket) -> bool:
    provided = websocket.headers.get("authorization", "")
    expected = f"Bearer {config.device_token}"
    return hmac.compare_digest(provided, expected)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "yang-ai-gateway"}


@app.get("/robot/clock/")
async def clock(request: Request) -> dict:
    """Return refreshed standby-clock weather without invoking any AI model."""
    public_ip = _request_public_ip(request)
    try:
        return await asyncio.to_thread(build_clock_context, public_ip)
    except RuntimeError as exc:
        # A non-200 response makes the device retain its last valid weather
        # instead of replacing the standby clock with placeholder values.
        raise HTTPException(status_code=503, detail="device location is unavailable") from exc


@app.api_route("/robot/ota/", methods=["GET", "POST"])
async def ota(request: Request) -> dict:
    # The ESP32 sends board/system information in the POST body. Only log its
    # size here so future payload changes cannot accidentally leak credentials.
    body = await request.body() if request.method == "POST" else b""
    device_id = request.headers.get("device-id", "unknown")
    client_id = request.headers.get("client-id", "unknown")
    logger.info(
        "OTA request device=%s client=%s body_bytes=%d",
        device_id,
        client_id,
        len(body),
    )

    response = {
        "websocket": {
            "url": config.websocket_url,
            "token": config.device_token,
            "version": config.websocket_version,
        },
        "server_time": {
            "timestamp": int(time.time() * 1000),
            "timezone_offset": 480,
        },
    }
    public_ip = _request_public_ip(request)
    if public_ip:
        try:
            response["clock"] = await asyncio.to_thread(build_clock_context, public_ip)
        except Exception as exc:
            logger.warning(
                "Standby clock context unavailable device=%s error_type=%s",
                device_id,
                type(exc).__name__,
            )
    return response


async def receive_device_hello(websocket: WebSocket) -> dict:
    message = await asyncio.wait_for(
        websocket.receive(), timeout=config.hello_timeout_seconds
    )
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))

    text = message.get("text")
    if text is None:
        raise ValueError("the first WebSocket message must be a JSON hello")

    payload = json.loads(text)
    if payload.get("type") != "hello":
        raise ValueError("the first WebSocket message must have type=hello")
    if payload.get("transport") != "websocket":
        raise ValueError("unsupported transport")
    return payload


@app.websocket("/robot/ws/")
async def robot_websocket(websocket: WebSocket) -> None:
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
    session_id = str(uuid.uuid4())
    audio_frames = 0
    audio_bytes = 0

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
                        "frame_duration": 60,
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
                    logger.info(
                        "Device JSON session=%s type=%s", session_id, message_type
                    )
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
    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        logger.warning("Device hello timeout session=%s", session_id)
        await websocket.close(code=1008)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Protocol error session=%s error=%s", session_id, exc)
        await websocket.close(code=1003)
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
