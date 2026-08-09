"""FastAPI layer wrapping agent/graph.py. Three endpoints:
  POST /chat      -- customer chat, REST. Accepts an optional image (e.g.
                     damage-claim evidence) alongside the text message.
  POST /voice     -- push-to-talk: Whisper STT -> the same agent -> TTS
  WS   /ws/admin  -- live reasoning-log stream for the admin dashboard

Voice and image evidence are not separate implementations of the agent --
both call the exact same run_agent_turn() as plain text chat. Only the input
(transcribed audio, or a text+image content list instead of plain text) and
output (synthesized speech instead of plain text) differ. Image understanding
is Claude's own native vision capability -- no separate vision model.

Log events are broadcast to any connected admin socket *while* a chat request
is being processed, not just returned at the end -- that's what makes the
admin dashboard real-time instead of a replay.
"""

import base64
from typing import Set

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from openai import OpenAI  # noqa: E402

from agent.graph import build_graph  # noqa: E402

app = FastAPI(title="ClearCart Support")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

graph = build_graph()
openai_client = OpenAI()


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
        # Reset per-turn state explicitly -- last_decision/verify_retry_count must not leak
        # from a previous turn, or verify ends up checking this turn's reply against a stale
        # decision (e.g. a plain "you're welcome" gets flagged for not citing an old clause).
        "last_decision": None,
        "verify_retry_count": 0,
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


@app.post("/voice")
async def voice(
    audio: UploadFile = File(...),
    thread_id: str = Form(...),
    image: UploadFile = File(None),
):
    audio_bytes = await audio.read()

    transcription = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=(audio.filename or "recording.webm", audio_bytes),
    )
    transcript = transcription.text

    content = [{"type": "text", "text": transcript}]
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

    speech = openai_client.audio.speech.create(
        model="tts-1",
        voice="alloy",
        input=reply,
    )
    audio_base64 = base64.b64encode(speech.content).decode("utf-8")

    return {
        "thread_id": thread_id,
        "transcript": transcript,
        "reply": reply,
        "audio_base64": audio_base64,
        "log_events": collected_events,
    }


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
