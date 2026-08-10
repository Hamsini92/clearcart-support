"""FastAPI layer wrapping agent/graph.py. Endpoints:
  POST /chat        -- customer chat, REST. Accepts an optional image (e.g.
                       damage-claim evidence) alongside the text message.
  WS   /ws/voice/{thread_id} -- push-to-talk voice: the OpenAI Realtime API
                       streams the customer's mic audio to a live,
                       VAD-segmented transcription (turn_detection's
                       create_response is explicitly False -- GPT never
                       generates a reply or calls a tool here, it's used
                       purely as a streaming STT/live-caption layer), then
                       the same agent, then a deterministic TTS call speaks
                       the reply back.
  WS   /ws/admin     -- live reasoning-log stream for the admin dashboard

Voice and image evidence are not separate implementations of the agent --
both call the exact same run_agent_turn() as plain text chat. Only the input
(transcribed audio, or a text+image content list instead of plain text) and
output (synthesized speech instead of plain text) differ. Image understanding
is Claude's own native vision capability -- no separate vision model.

Why not let the Realtime API's own model reason and reply during voice: that
would mean GPT, not Claude, deciding refunds on the voice path -- a second,
divergent decision-maker for the same job. Realtime is deliberately used only
as an audio I/O layer; run_agent_turn() (Claude) remains the sole reasoner
for both text and voice, and TTS output is a deterministic REST call rather
than the Realtime API's own generative voice, since a generative restate of
a refund decision could paraphrase away a policy citation like "SS10.1".

Log events are broadcast to any connected admin socket *while* a chat request
is being processed, not just returned at the end -- that's what makes the
admin dashboard real-time instead of a replay.
"""

import asyncio
import base64
import contextlib
import time
from typing import Set

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from agent.graph import build_graph  # noqa: E402

REALTIME_MODEL = "gpt-realtime-mini"
REALTIME_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"

# Voice guardrails -- the Realtime session is billed by connection time, and
# this endpoint takes raw client-controlled bytes (audio chunks, an inline
# base64 image) before any of it reaches the agent, so it's a real trust
# boundary, not a hypothetical one.
MAX_VOICE_SECONDS = 120  # hard cap per push-to-talk turn -- protects against a stuck/forgotten-open mic burning Realtime minutes
MAX_AUDIO_CHUNK_B64_BYTES = 65_536  # a real 4096-sample pcm16 frame is ~11KB base64; well past this is abuse, not audio
MAX_IMAGE_B64_BYTES = 8_000_000  # keeps the base64 image safely under Claude's 10MB raw-image limit after decoding

app = FastAPI(title="ClearCart Support")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

graph = build_graph()

_openai_client = None


def get_openai_client() -> AsyncOpenAI:
    """Lazy singleton -- only instantiated (and only requires OPENAI_API_KEY)
    when a voice request actually comes in. Text chat must work with no
    OpenAI key configured at all, since voice is an optional bonus feature,
    not a dependency of the core agent. Async client: a slow Realtime/TTS
    round-trip must not block the event loop that /chat and /ws/admin share."""
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI()
    return _openai_client


MAX_HISTORY = 300


class AdminBroadcaster:
    """Tiny in-process pub/sub. Single-instance only -- same limitation as
    MemorySaver, same fix later (a real pub/sub like Redis) if this scales
    past one server process. Keeps bounded history in memory and replays it
    as one batch to any newly connected dashboard, so tab-opening order never
    matters and a long testing session doesn't make replay slow."""

    def __init__(self):
        self._connections: Set[WebSocket] = set()
        self._history: list = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)
        if self._history:
            await ws.send_json({"type": "history", "events": self._history})

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)

    async def broadcast(self, event: dict):
        self._history.append(event)
        if len(self._history) > MAX_HISTORY:
            self._history = self._history[-MAX_HISTORY:]
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


admin_stream = AdminBroadcaster()


def _block_type(block):
    return getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")


async def run_agent_turn(thread_id: str, content: list) -> tuple[str, list]:
    """Runs one message through the agent graph, broadcasting reasoning
    events to the admin dashboard as they happen. Shared by /chat and /voice
    so neither voice nor image evidence is a second implementation of the
    agent -- only the I/O around this call differs. `content` is an Anthropic
    content-block list -- a plain text block, or text + image blocks."""
    config = {"configurable": {"thread_id": thread_id}}
    input_state = {
        "messages": [{"role": "user", "content": content}],
        # Reset per-turn state explicitly -- none of this may leak from a previous turn.
        # last_decision/verify_retry_count: verify would check this turn's reply against a
        # stale decision (e.g. a plain "you're welcome" flagged for not citing an old clause).
        # step_count: MAX_AGENT_STEPS is a runaway-loop guard for ONE turn, not a lifetime
        # budget for the whole thread -- without this reset it accumulates across every turn
        # ever sent on this thread_id and eventually trips safety_stop on a perfectly valid
        # new request.
        "last_decision": None,
        "verify_retry_count": 0,
        "step_count": 0,
    }

    collected_events = []
    async for step in graph.astream(input_state, config, stream_mode="updates"):
        for node_name, update in step.items():
            for ev in update.get("log_events", []):
                event = {"thread_id": thread_id, "node": node_name, **ev}
                collected_events.append(event)
                await admin_stream.broadcast(event)

    final_state = graph.get_state(config).values
    last_message = final_state["messages"][-1]
    reply = "".join(b.text for b in last_message["content"] if _block_type(b) == "text")
    return reply, collected_events


@app.post("/chat")
async def chat(thread_id: str = Form(...), message: str = Form(...), image: UploadFile = File(None)):
    content = [{"type": "text", "text": message}]
    if image is not None:
        image_bytes = await image.read()
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.content_type or "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            },
        })

    reply, collected_events = await run_agent_turn(thread_id, content)
    return {"thread_id": thread_id, "reply": reply, "log_events": collected_events}


@app.websocket("/ws/voice/{thread_id}")
async def ws_voice(websocket: WebSocket, thread_id: str):
    """Push-to-talk over a WebSocket. Protocol (JSON frames):

    client -> server:
      {"type": "audio", "audio": "<base64 pcm16, 24kHz mono>"}   repeated while recording
      {"type": "stop"}                                           mic released, entering review
      {"type": "finalize", "image": "<base64>"|null, "image_type": "..."|null}   send it
      {"type": "cancel"}                                         discarded, never sent

    server -> client:
      {"type": "ready"}
      {"type": "speech_started"}
      {"type": "transcript_delta", "text": "..."}   one per VAD-segmented utterance
      {"type": "error", "stage": "...", "message": "..."}
      {"type": "final", "transcript": "...", "reply": "...", "audio_base64": "..."|null}
    """
    await websocket.accept()
    client = get_openai_client()
    transcript_parts: list[str] = []
    last_client_msg: dict = {}
    start_time = time.monotonic()
    duration_limit_hit = False
    audio_chunk_count = 0

    try:
        async with client.realtime.connect(model=REALTIME_MODEL) as conn:
            await conn.send({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "output_modalities": ["text"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            # language pinned rather than left to auto-detect --
                            # short utterances can otherwise get misidentified
                            # and transcribed in the wrong script/language entirely.
                            "transcription": {"model": REALTIME_TRANSCRIBE_MODEL, "language": "en"},
                            "turn_detection": {
                                "type": "server_vad",
                                "create_response": False,
                                "silence_duration_ms": 800,
                            },
                        },
                    },
                },
            })
            await websocket.send_json({"type": "ready"})

            async def pump_openai_events():
                async for event in conn:
                    if event.type == "input_audio_buffer.speech_started":
                        await websocket.send_json({"type": "speech_started"})
                        await admin_stream.broadcast({
                            "thread_id": thread_id, "node": "voice", "stage": "speech_started", "status": "info",
                        })
                    elif event.type == "input_audio_buffer.speech_stopped":
                        await admin_stream.broadcast({
                            "thread_id": thread_id, "node": "voice", "stage": "speech_stopped", "status": "info",
                        })
                    elif event.type == "conversation.item.input_audio_transcription.completed":
                        transcript_parts.append(event.transcript)
                        await websocket.send_json({"type": "transcript_delta", "text": event.transcript})
                        await admin_stream.broadcast({
                            "thread_id": thread_id, "node": "voice", "stage": "transcribed",
                            "status": "info", "transcript": event.transcript,
                        })
                    elif event.type == "conversation.item.input_audio_transcription.failed":
                        await websocket.send_json({
                            "type": "error", "stage": "transcribe",
                            "message": event.error.message or "Transcription failed.",
                        })
                        await admin_stream.broadcast({
                            "thread_id": thread_id, "node": "voice", "stage": "transcribe",
                            "status": "failed", "error": event.error.message,
                        })
                    elif event.type == "error":
                        # A session-level rejection (e.g. a malformed session.update) --
                        # otherwise silent, since it doesn't fall under either
                        # transcription-event case above.
                        await admin_stream.broadcast({
                            "thread_id": thread_id, "node": "voice", "stage": "session",
                            "status": "failed", "error": event.error.message,
                        })
                        await websocket.send_json({
                            "type": "error", "stage": "connect",
                            "message": "The voice session hit an unexpected error. Please try again.",
                        })

            pump_task = asyncio.create_task(pump_openai_events())
            try:
                while True:
                    msg = await websocket.receive_json()
                    mtype = msg.get("type")
                    if mtype == "audio":
                        if duration_limit_hit:
                            continue  # already warned client; keep draining the queue without forwarding more audio
                        if time.monotonic() - start_time > MAX_VOICE_SECONDS:
                            duration_limit_hit = True
                            await websocket.send_json({
                                "type": "error", "stage": "guardrail",
                                "message": f"Recording limit reached ({MAX_VOICE_SECONDS}s). Stop and review, or discard and try again.",
                            })
                            continue
                        audio_b64 = msg.get("audio", "")
                        if len(audio_b64) > MAX_AUDIO_CHUNK_B64_BYTES:
                            await websocket.send_json({
                                "type": "error", "stage": "guardrail",
                                "message": "Audio chunk rejected -- unexpected size.",
                            })
                            continue
                        audio_chunk_count += 1
                        await conn.send({"type": "input_audio_buffer.append", "audio": audio_b64})
                    elif mtype in ("stop", "finalize", "cancel"):
                        last_client_msg = msg
                        if mtype != "stop":
                            break
            finally:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task
    except WebSocketDisconnect:
        return
    except Exception as e:
        await admin_stream.broadcast({
            "thread_id": thread_id, "node": "voice", "stage": "connect",
            "status": "failed", "error": str(e),
        })
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "stage": "connect", "message": "Couldn't reach the voice service. Please try again."})
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    if last_client_msg.get("type") != "finalize":
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    transcript = " ".join(p.strip() for p in transcript_parts if p.strip())
    if not transcript:
        # audio_chunk_count tells us whether any mic audio reached this endpoint
        # at all, vs. reaching OpenAI but never being recognized as speech --
        # otherwise this failure mode is a black box.
        await admin_stream.broadcast({
            "thread_id": thread_id, "node": "voice", "stage": "transcribe",
            "status": "empty", "audio_chunks_received": audio_chunk_count,
        })
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "stage": "transcribe", "message": "Didn't catch that -- no speech detected. Please try again."})
            await websocket.close()
        return

    content = [{"type": "text", "text": transcript}]
    image_b64 = last_client_msg.get("image")
    if image_b64 and len(image_b64) > MAX_IMAGE_B64_BYTES:
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "stage": "guardrail", "message": "Attached photo is too large. Please try a smaller image."})
            await websocket.close()
        return
    if image_b64:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": last_client_msg.get("image_type") or "image/jpeg",
                "data": image_b64,
            },
        })

    reply, _collected_events = await run_agent_turn(thread_id, content)

    # audio_base64 stays None on a synthesis failure -- the "final" message below
    # still carries the reply text either way, so a TTS hiccup never drops the
    # agent's actual answer, just the spoken version of it.
    audio_base64 = None
    try:
        speech = await client.audio.speech.create(model="tts-1", voice="alloy", input=reply)
        audio_base64 = base64.b64encode(speech.content).decode("utf-8")
    except Exception as e:
        await admin_stream.broadcast({
            "thread_id": thread_id, "node": "voice", "stage": "synthesize",
            "status": "failed", "error": str(e),
        })

    with contextlib.suppress(Exception):
        await websocket.send_json({
            "type": "final", "transcript": transcript, "reply": reply, "audio_base64": audio_base64,
        })
    with contextlib.suppress(Exception):
        await websocket.close()


@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket):
    await admin_stream.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        admin_stream.disconnect(websocket)


@app.get("/health")
async def health():
    return {"status": "ok"}
