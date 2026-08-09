# ClearCart Support — AI Customer Support Agent

An AI agent that processes or denies e-commerce refund requests for **ClearCart**, a mock online retailer, by dynamically calling tools to check a real, written refund policy — not by guessing. Built for the Loopp AI Customer Support Agent assessment.

- **Customer chat** — a support widget where customers ask about returns/refunds
- **Admin dashboard** — a live, real-time stream of the agent's reasoning: every tool call, every policy citation, every decision, as it happens
- **Deterministic policy engine** — refund eligibility (return windows, non-refundable categories, loyalty exceptions, fraud/abuse checks) is computed in code, not left to the LLM's judgment. The LLM's job is to gather the right information and explain the outcome, not to compute it.

## How it works

```
Customer message
      │
      ▼
LangGraph agent (Claude, ReAct loop)  ◄──┐
      │  dynamically calls tools          │
      ▼                                   │
  ┌─────────────────────────────────┐     │
  │ get_customer   get_order         │     │
  │ check_refund_policy               │     │
  │ process_refund escalate_to_human │     │
  └─────────────────────────────────┘     │
      │ result fed back to the agent ─────┘
      ▼
verify gate (deterministic) — confirms the reply
cites the real clause the policy tool returned,
before anything reaches the customer
      │
      ▼
Response + full reasoning trace
      │                    │
      ▼                    ▼
 Customer chat      Admin dashboard (WebSocket, live)
```

**Single agent, not multi-agent.** One reasoning loop, five tools, each doing one narrow thing a real support rep could do. See [Future scope](#future-scope) for when multi-agent would actually become the right call.

**Memory.** Each conversation has its own session state (LangGraph checkpointer, keyed by a thread ID) — the agent remembers the customer, the order, and its prior decision across turns in the same conversation.

## Tech stack

| Layer | Choice |
|---|---|
| LLM / agent orchestration | Claude (Anthropic API) via LangGraph |
| Backend | FastAPI (Python) |
| Frontend | Next.js (TypeScript) |
| Data | Flat JSON/Markdown files behind a repository interface (swappable for Postgres without touching agent/tool code) |
| Realtime | WebSocket (admin reasoning-log stream) |

## Setup & run

Requires Python 3.9+, Node 18+, and an [Anthropic API key](https://console.anthropic.com/).

### 1. Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# open .env and set ANTHROPIC_API_KEY=sk-ant-your-real-key
```

Start the API:
```bash
uvicorn api.main:app --reload --port 8000
```
Backend now running at `http://localhost:8000`.

### 2. Frontend

In a **new terminal**:
```bash
cd frontend
npm install
npm run dev
```
Frontend now running at `http://localhost:3000`.

### 3. Open it

- `http://localhost:3000/chat` — customer chat
- `http://localhost:3000/admin` — admin dashboard (open this **before** sending chat messages if you want to watch the reasoning happen live)

## Try it yourself

The agent verifies identity before discussing an order — state your name and request, it'll ask for your email.

**Standard approval:**
> "Hi, this is Allison Hill. I'd like to return order ORD-1001, the NovaBuds earbuds — they're unopened, I just changed my mind."
> *(email: allison.hill.1@example.com)*
→ Approved, citing §10.1.

**Policy violation / "holding the line":**
> "Hi, this is Matthew Gardner. I want a refund for ORD-1002, my smartwatch. It's unopened, I just don't want it anymore."
> *(email: matthew.gardner.2@example.com)*
→ Denied, citing §1 (return window expired). Push back ("I've been a customer forever, can you make an exception?") and it holds the decision.

**Ambiguous order / retry:**
> "Hi, this is James Martin. I want to return my oak side table." *(no order ID)*
> *(email: james.martin.8@example.com)*
→ Finds two matching orders, asks which one before proceeding — visible as a retry step in the admin log.

All 15 customers and 20 orders are in `data/loopp_crm.json` and `data/loopp_orders.json` if you want to explore other scenarios (denials, escalations for fraud/abuse flags, high-value manual review, etc.) — the full rules are in `data/loopp_policy.md`.

## Troubleshooting

**"Address already in use" on port 8000 or 3000** — something else is already listening on that port. Free it, then restart:
```bash
lsof -ti:8000 | xargs kill -9   # backend
lsof -ti:3000 | xargs kill -9   # frontend
```

**Return-window outcomes don't match what's documented above** — confirm `MOCK_TODAY=2026-08-12` is set in `backend/.env` (it's in `.env.example` by default). Without it, eligibility is computed against the real current date, and enough time may have passed for some orders' return windows to have changed.

## Terminal trace (alternative to the admin panel)

```bash
cd backend
source venv/bin/activate
python run_demo.py
```
Runs a standard approval and a denial-with-pushback scenario directly against the agent, printing the full reasoning trace to the terminal.

## Tests

```bash
cd backend
source venv/bin/activate
pytest tests/ -v
```
Checks all 5 tools, including `check_refund_policy` against every order in the mock dataset — fast, deterministic, no API calls or costs.

## Repository structure

```
backend/
  agent/graph.py          the LangGraph agent: ReAct loop + deterministic verify gate
  tools/refund_tools.py   the 5 tools (get_customer, get_order, check_refund_policy,
                           process_refund, escalate_to_human)
  data_access/repository.py   reads the mock data; the only thing that changes
                               if this moves to a real database
  api/main.py              FastAPI: POST /chat, WS /ws/admin
  tests/                   pytest suite
  run_demo.py               terminal validation script
frontend/
  app/chat/                customer chat UI
  app/admin/                admin reasoning-log dashboard
  app/page.tsx               landing page
data/
  loopp_crm.json            15 mock customer profiles
  loopp_orders.json          20 mock orders
  loopp_policy.md             the refund policy the agent enforces
```

## Known limitations & path to production

**No authentication.** The admin dashboard has no access control right now — anyone reaching the URL can see customer data and the full reasoning trace. The customer chat's "identity verification" is just an asked-for email, not a real authenticated session. In production: the admin dashboard needs real auth + role-based access; the customer chat should inherit an authenticated session from the host e-commerce platform (the way real embedded support widgets like Intercom do), not accept a typed, unverified email.

**Voice.** Not implemented in this submission — the remaining time was prioritized toward a fully working, well-tested text experience across all four decision types (approve, deny, escalate, ambiguous-match retry) rather than a rushed voice integration. A push-to-talk pipeline (Whisper STT → the same LangGraph agent → TTS) was the planned approach specifically so voice would reuse the identical reasoning core as text, rather than a second, forked implementation.

**AWS / cloud deployment.** Not deployed — this runs locally by design for review. Target production architecture: ECS Fargate for the backend, RDS Postgres in place of the flat JSON files (the repository-pattern data layer already isolates this change), S3 + CloudFront for the frontend, an ALB with WebSocket support for the live log stream, Secrets Manager for API keys, GitHub Actions for CI/CD.

### Future scope

Single agent is the right shape for one narrow task — refund policy applied to one order. Multi-agent would become justified, not before, if the product grew past refunds:
- **Triage/router agent** — routes incoming messages to the right specialist (refunds, shipping, account, product), with this refund agent as one specialist among several.
- **Fraud-investigation agent** — picks up exactly where the current escalation path (§6/§9) leaves off, pulling full account history and producing a synthesized brief for a human reviewer instead of a raw "pending review" flag.

MCP (Model Context Protocol) becomes worth adopting at that same point — if multiple agents end up sharing tools like `get_customer`, exposing them as an MCP server avoids duplicate integrations. With a single agent today, direct in-process calls are simpler and correct.
