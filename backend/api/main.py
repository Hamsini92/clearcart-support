"""FastAPI layer wrapping agent/graph.py. Two endpoints:
  POST /chat      -- customer chat, REST
  WS   /ws/admin  -- live reasoning-log stream for the admin dashboard

Log events are broadcast to any connected admin socket *while* a chat request
is being processed, not just returned at the end -- that's what makes the
admin dashboard real-time instead of a replay.
"""

from typing import Set

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

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


class ChatRequest(BaseModel):
    thread_id: str
    message: str


def _block_type(block):
    return getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")


@app.post("/chat")
async def chat(req: ChatRequest):
    config = {"configurable": {"thread_id": req.thread_id}}
    input_state = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": req.message}]}],
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
                event = {"thread_id": req.thread_id, "node": node_name, **ev}
                collected_events.append(event)
                await admin_stream.broadcast(event)

    final_state = graph.get_state(config).values
    last_message = final_state["messages"][-1]
    reply = "".join(b.text for b in last_message["content"] if _block_type(b) == "text")

    return {"thread_id": req.thread_id, "reply": reply, "log_events": collected_events}


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
