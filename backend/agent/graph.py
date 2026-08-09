"""The agent: a ReAct loop (agent <-> tools) over the 5 refund tools, gated by
a deterministic verify step before any response goes out. Transport-agnostic
-- nothing here knows about FastAPI, WebSockets, or the terminal. Whoever
calls build_graph().invoke(...) just needs a thread_id and a message.
"""

import json
import operator
import os
from typing import Annotated, Optional, TypedDict

import anthropic
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from tools import refund_tools

MODEL_NAME = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "8"))
MAX_VERIFY_RETRIES = 1

SYSTEM_PROMPT = """You are Loopp's refund support agent.

You have 5 tools: get_customer, get_order, check_refund_policy, process_refund,
escalate_to_human. Use them to gather real information -- never guess or invent
a customer, order, policy outcome, or refund amount.

Rules:
- Identify the customer (get_customer) and the order (get_order) before checking policy.
- get_order can take an exact order_id, or a customer_id + description if the
  customer doesn't know their order number.
  - If it returns zero matches: tell the customer and ask for more detail.
  - If it returns more than one match: list them and ask which one. Do not
    guess, and do not call check_refund_policy or process_refund until the
    order is uniquely identified.
  - If a tool result contains an "error" about the lookup/order service
    itself being unavailable (this is different from a normal "not found") --
    do not guess, retry it yourself, or proceed as if you had the record.
    Tell the customer you're temporarily unable to access the required
    account/order information, and call escalate_to_human rather than making
    a decision without it.
- Infer `condition` for check_refund_policy from what the customer actually
  says about the item ("unopened", "opened", "damaged", or
  "customer_damaged"). Never guess or default this silently -- if condition
  could change the outcome and the customer hasn't said, ask them directly
  before calling check_refund_policy.
- If the item is opened electronics, also ask whether it's fully functional
  and whether all original accessories/parts are present before calling
  check_refund_policy -- pass accessories_present / is_functional based on
  what they actually say, not an assumption.
- If a customer claims damage, ask them to send a photo. If an image is
  included in their message, look at it yourself and judge whether it
  genuinely shows damage or a defect on a physical item. Only pass
  has_damage_evidence=true if the image actually supports the claim -- an
  image that is blank, unrelated, or shows an undamaged item is NOT evidence.
  If the photo doesn't support the claim, say so plainly and ask for a
  clearer or more relevant photo rather than proceeding. Do not treat the
  mere presence of an uploaded image as automatic proof.
- check_refund_policy's decision is authoritative. Never state a different
  outcome or amount than what it returned. Every customer-facing message
  about this decision must include the exact clause it returned (e.g.
  "approved under policy §10.1") -- not just your first message explaining
  the decision, but also the final confirmation after process_refund runs.
  Work it in naturally (e.g. "Your refund of $129.99 has been processed
  under policy §10.1..."); don't drop it just because you already mentioned
  it earlier in the conversation.
- If the decision is "escalate", tell the customer this needs manual review --
  do not approve or deny it yourself.
- If the customer pushes back on a denial or escalation (repeated requests,
  frustration, asking for an exception), do not change the decision. You may
  explain that policy does not allow overrides for customer pressure, and
  offer that they can request supervisor review.
- Only call process_refund after check_refund_policy returns decision="approve".
  process_refund takes only order_id -- the refund amount is never something
  you decide or supply; the backend uses the amount check_refund_policy
  already calculated for that order.
"""

TOOL_SCHEMAS = [
    {
        "name": "get_customer",
        "description": "Look up a customer by email.",
        "input_schema": {
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
        },
    },
    {
        "name": "get_order",
        "description": (
            "Look up an order by exact order_id, OR search a customer's orders "
            "by a text description (e.g. 'oak side table') using customer_id + "
            "description. The description form can return more than one match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "customer_id": {"type": "string"},
                "description": {"type": "string"},
            },
        },
    },
    {
        "name": "check_refund_policy",
        "description": "Evaluate one order against the refund policy. Authoritative -- always cite its clause and amount exactly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "condition": {
                    "type": "string",
                    "enum": ["unopened", "opened", "damaged", "customer_damaged"],
                },
                "has_damage_evidence": {"type": "boolean"},
                "accessories_present": {"type": "boolean"},
                "is_functional": {"type": "boolean"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "process_refund",
        "description": (
            "Execute a refund for an order check_refund_policy has already approved. "
            "Takes only order_id -- the refund amount is never supplied by you; the "
            "backend uses the amount check_refund_policy already calculated and holds "
            "for this exact order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Flag a case for manual review instead of deciding it yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "order_id": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]

TOOL_FUNCTIONS = {
    "get_customer": refund_tools.get_customer,
    "get_order": refund_tools.get_order,
    "check_refund_policy": refund_tools.check_refund_policy,
    "process_refund": refund_tools.process_refund,
    "escalate_to_human": refund_tools.escalate_to_human,
}


class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    customer_id: Optional[str]
    order_id: Optional[str]
    candidate_orders: list
    last_decision: Optional[dict]
    log_events: Annotated[list, operator.add]
    step_count: int
    verify_retry_count: int


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def agent_node(state: AgentState) -> dict:
    response = _client().messages.create(
        model=MODEL_NAME,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=state["messages"],
        tools=TOOL_SCHEMAS,
    )
    assistant_message = {"role": "assistant", "content": response.content}
    tool_calls = [b.name for b in response.content if b.type == "tool_use"]
    log_event = {
        "node": "agent",
        "stop_reason": response.stop_reason,
        "tool_calls_requested": tool_calls,
    }
    return {
        "messages": [assistant_message],
        "log_events": [log_event],
        "step_count": state.get("step_count", 0) + 1,
    }


def _resolve_authorized_amount(order_id: str, state: "AgentState", updates: dict):
    """The refund amount process_refund actually uses -- pulled from a held
    check_refund_policy decision, never from the LLM's tool_use input (which
    no longer even carries an amount field). Also refuses to pay out a
    decision that was approved for a different order_id."""
    decision = updates.get("last_decision", state.get("last_decision"))
    if not decision:
        return None, "no policy decision on record for this case -- call check_refund_policy first"
    if decision.get("decision") != "approve":
        return None, f"the decision on record is '{decision.get('decision')}', not an approval"
    if decision.get("order_id") != order_id:
        return None, "the approved decision on record is for a different order_id"
    return decision.get("refund_amount"), None


def _dispatch_tool(name: str, tool_input: dict, state: AgentState, updates: dict) -> dict:
    """The actual tool call, after the server-side checks that don't trust
    the LLM's input alone (customer-order ownership, authorized refund
    amount). May raise repository.TransientLookupError -- the caller
    (tools_node) is responsible for retrying that, not this function."""
    if name == "check_refund_policy":
        order_id = tool_input.get("order_id")
        known_customer = updates.get("customer_id", state.get("customer_id"))
        if not known_customer:
            return {"decision": "error", "order_id": order_id, "reason": "customer not yet identified -- call get_customer first"}
        lookup = refund_tools.get_order(order_id=order_id)
        matched = lookup.get("matches") or []
        if not matched:
            return {"decision": "error", "order_id": order_id, "reason": "order not found"}
        if matched[0]["customer_id"] != known_customer:
            return {"decision": "error", "order_id": order_id, "reason": "this order does not belong to the identified customer"}
        return refund_tools.check_refund_policy(**tool_input)

    if name == "process_refund":
        order_id = tool_input.get("order_id")
        amount, error = _resolve_authorized_amount(order_id, state, updates)
        if error:
            return {"success": False, "reason": error}
        return refund_tools.process_refund(order_id, amount)

    return TOOL_FUNCTIONS[name](**tool_input)


def _dispatch_tool_with_retry(name: str, tool_input: dict, state: AgentState, updates: dict, log_events: list) -> dict:
    """Wraps _dispatch_tool with one automatic retry on a transient lookup
    failure -- the kind of thing a real CRM/order-service call can throw on a
    network blip. Logs failed / retrying / recovered as distinct admin
    events so the retry is visible, not silently swallowed."""
    try:
        return _dispatch_tool(name, tool_input, state, updates)
    except refund_tools.repository.TransientLookupError as e:
        log_events.append({
            "node": "tools", "tool": name, "input": tool_input,
            "status": "failed, retrying", "error": str(e),
        })
        try:
            result = _dispatch_tool(name, tool_input, state, updates)
            log_events.append({
                "node": "tools", "tool": name, "input": tool_input,
                "status": "recovered on retry",
            })
            return result
        except refund_tools.repository.TransientLookupError as e2:
            log_events.append({
                "node": "tools", "tool": name, "input": tool_input,
                "status": "failed again, giving up", "error": str(e2),
            })
            return {"error": "lookup service unavailable after retry, please try again shortly"}


def tools_node(state: AgentState) -> dict:
    last_message = state["messages"][-1]
    result_blocks = []
    log_events = []
    updates = {}

    for block in last_message["content"]:
        if block.type != "tool_use":
            continue
        name, tool_input, tool_id = block.name, block.input, block.id

        result = _dispatch_tool_with_retry(name, tool_input, state, updates, log_events)
        result_blocks.append({
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": json.dumps(result),
        })
        log_events.append({"node": "tools", "tool": name, "input": tool_input, "output": result})

        if name == "get_customer" and result.get("found"):
            updates["customer_id"] = result["customer"]["customer_id"]
        if name == "get_order":
            matches = result.get("matches", [])
            if len(matches) == 1:
                updates["order_id"] = matches[0]["order_id"]
                updates["candidate_orders"] = []
            elif len(matches) > 1:
                updates["order_id"] = None
                updates["candidate_orders"] = matches
        if name == "check_refund_policy":
            updates["last_decision"] = result

    updates["messages"] = [{"role": "user", "content": result_blocks}]
    updates["log_events"] = log_events
    return updates


def _truncate(text: str, max_len: int = 80) -> str:
    return text if len(text) <= max_len else text[:max_len] + "..."


def _last_customer_text(messages: list, max_len: int = 80):
    """Most recent actual customer message, as opposed to a tool_result
    message -- both are stored with role="user", so this filters by content
    block type rather than role alone."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                return _truncate(" ".join(texts), max_len)
    return None


# Lexical signals used only to catch a response contradicting the decision
# type it's supposed to be reporting (e.g. narrating a refund on a denial).
# Deliberately narrow phrases chosen to avoid false positives on ordinary
# empathetic language ("I understand your frustration" etc).
_DENIAL_SIGNALS = ["denied", "not eligible", "unable to approve", "cannot approve", "can't approve", "not able to refund", "won't be able to refund"]
_APPROVAL_CONFIRMATION_SIGNALS = ["refund has been processed", "refund is being processed", "issued the refund", "refund of $", "we've refunded", "we have refunded", "refund has been issued"]


def verify_node(state: AgentState) -> dict:
    decision = state.get("last_decision")
    last_message = state["messages"][-1]
    text = "".join(b.text for b in last_message["content"] if b.type == "text")
    text_lower = text.lower()
    response_snippet = _truncate(text)
    customer_snippet = _last_customer_text(state["messages"])

    def _pass(result: str) -> dict:
        return {"log_events": [{
            "node": "verify",
            "result": result,
            "customer_message": customer_snippet,
            "response_snippet": response_snippet,
        }]}

    def _fail(result: str, instruction: str) -> dict:
        return {
            "log_events": [{
                "node": "verify",
                "result": result,
                "customer_message": customer_snippet,
                "response_snippet": response_snippet,
            }],
            "verify_retry_count": state.get("verify_retry_count", 0) + 1,
            "messages": [{"role": "user", "content": instruction}],
        }

    if not decision or not decision.get("clause"):
        return _pass("no decision to verify, passed through")

    clause_ok = decision["clause"] in text
    decision_type = decision.get("decision")

    if decision_type == "approve":
        amount = decision.get("refund_amount")
        amount_ok = amount is None or f"{amount:.2f}" in text
        contradicts_denial = any(s in text_lower for s in _DENIAL_SIGNALS)
        if clause_ok and amount_ok and not contradicts_denial:
            return _pass("clause and refund amount match the approved decision, passed")
        problems = []
        if not clause_ok:
            problems.append(f"cite clause {decision['clause']} exactly")
        if not amount_ok:
            problems.append(f"state the exact approved amount (${amount:.2f})")
        if contradicts_denial:
            problems.append("not use denial language -- this order was approved")
        return _fail(
            f"MISMATCH: approved decision not accurately reflected ({'; '.join(problems)})",
            f"Your last response must {', and '.join(problems)}, exactly as check_refund_policy "
            f"returned. Revise your response.",
        )

    if decision_type == "deny":
        contradicts_approval = any(s in text_lower for s in _APPROVAL_CONFIRMATION_SIGNALS)
        if clause_ok and not contradicts_approval:
            return _pass("clause citation matches the denial, passed")
        problems = []
        if not clause_ok:
            problems.append(f"cite clause {decision['clause']} exactly")
        if contradicts_approval:
            problems.append("not claim a refund was issued -- this order was denied")
        return _fail(
            f"MISMATCH: denial not accurately reflected ({'; '.join(problems)})",
            f"Your last response must {', and '.join(problems)}, and must not state a "
            f"different outcome than check_refund_policy returned. Revise your response.",
        )

    if decision_type == "escalate":
        # Escalations don't need to expose the internal trigger clause to the
        # customer (e.g. an abuse/fraud flag) -- discretion is appropriate
        # there. But the response still must not claim an approval/refund
        # that check_refund_policy never granted.
        contradicts_approval = any(s in text_lower for s in _APPROVAL_CONFIRMATION_SIGNALS)
        if not contradicts_approval:
            return _pass("escalation confirmed -- clause citation not required in customer-facing text")
        return _fail(
            "MISMATCH: response claims a refund was issued, but this case was escalated, not approved",
            "This case was escalated for manual review, not approved. Your response must not "
            "claim a refund was issued or that the case was approved. Revise your response.",
        )

    return _pass("no decision to verify, passed through")


def route_after_agent(state: AgentState) -> str:
    last_message = state["messages"][-1]
    has_tool_calls = any(b.type == "tool_use" for b in last_message["content"])
    if has_tool_calls and state.get("step_count", 0) < MAX_AGENT_STEPS:
        return "tools"
    if has_tool_calls:
        return "safety_stop"
    return "verify"


def route_after_verify(state: AgentState) -> str:
    last_event = state["log_events"][-1]
    if last_event.get("result", "").startswith("MISMATCH") and state.get("verify_retry_count", 0) <= MAX_VERIFY_RETRIES:
        return "agent"
    return END


def safety_stop_node(state: AgentState) -> dict:
    """Reached when the agent still wants to call tools after MAX_AGENT_STEPS.
    The last message has pending tool_use blocks that tools_node never ran --
    those must get a synthetic tool_result before anything else is appended.
    Anthropic's API requires every tool_use to be immediately followed by its
    tool_result, and since MemorySaver keeps this thread's full history
    forever, an unresolved one wouldn't just break this turn -- it would
    break every future turn on this thread with a 400 error."""
    last_message = state["messages"][-1]
    pending_tool_uses = [b for b in last_message["content"] if getattr(b, "type", None) == "tool_use"]

    messages = []
    if pending_tool_uses:
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"error": f"stopped -- exceeded {MAX_AGENT_STEPS}-step limit for this turn"}),
                }
                for block in pending_tool_uses
            ],
        })

    messages.append({
        "role": "assistant",
        "content": [{"type": "text", "text": (
            "I'm not able to resolve this automatically -- escalating to a "
            "human teammate to take it from here."
        )}],
    })

    return {
        "messages": messages,
        "log_events": [{"node": "safety_stop", "reason": f"exceeded {MAX_AGENT_STEPS} tool-call steps"}],
    }


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("verify", verify_node)
    graph.add_node("safety_stop", safety_stop_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {
        "tools": "tools",
        "verify": "verify",
        "safety_stop": "safety_stop",
    })
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges("verify", route_after_verify, {
        "agent": "agent",
        END: END,
    })
    graph.add_edge("safety_stop", END)

    return graph.compile(checkpointer=MemorySaver())
