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
- Infer `condition` for check_refund_policy from what the customer says about
  the item ("unopened", "opened", "damaged", or "customer_damaged"). Default
  to "unopened" only if they say nothing suggesting otherwise. Infer
  has_damage_evidence from whether they mention providing photos.
- check_refund_policy's decision is authoritative. Always cite the exact
  clause it returns. Never state a different outcome or amount than what it
  returned.
- If the decision is "escalate", tell the customer this needs manual review --
  do not approve or deny it yourself.
- If the customer pushes back on a denial or escalation (repeated requests,
  frustration, asking for an exception), do not change the decision. You may
  explain that policy does not allow overrides for customer pressure, and
  offer that they can request supervisor review.
- Only call process_refund after check_refund_policy returns decision="approve".
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
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "process_refund",
        "description": "Execute an approved refund. Only call after check_refund_policy returns decision='approve'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["order_id", "amount"],
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


def tools_node(state: AgentState) -> dict:
    last_message = state["messages"][-1]
    result_blocks = []
    log_events = []
    updates = {}

    for block in last_message["content"]:
        if block.type != "tool_use":
            continue
        name, tool_input, tool_id = block.name, block.input, block.id
        result = TOOL_FUNCTIONS[name](**tool_input)
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


def verify_node(state: AgentState) -> dict:
    decision = state.get("last_decision")
    last_message = state["messages"][-1]
    text = "".join(b.text for b in last_message["content"] if b.type == "text")
    response_snippet = _truncate(text)
    customer_snippet = _last_customer_text(state["messages"])

    if not decision or not decision.get("clause"):
        return {"log_events": [{
            "node": "verify",
            "result": "no decision to verify, passed through",
            "customer_message": customer_snippet,
            "response_snippet": response_snippet,
        }]}

    if decision["clause"] in text:
        return {"log_events": [{
            "node": "verify",
            "result": "clause citation matches tool output, passed",
            "customer_message": customer_snippet,
            "response_snippet": response_snippet,
        }]}

    # Escalations intentionally don't need to expose the internal trigger
    # clause to the customer (e.g. an abuse/fraud flag) -- discretion is
    # appropriate there. Only approve/deny require the citation, since
    # explaining the specific rule is the point of a defensible decision.
    if decision.get("decision") == "escalate":
        return {"log_events": [{
            "node": "verify",
            "result": "escalation confirmed -- clause citation not required in customer-facing text",
            "customer_message": customer_snippet,
            "response_snippet": response_snippet,
        }]}

    return {
        "log_events": [{
            "node": "verify",
            "result": "MISMATCH: response did not cite the clause check_refund_policy returned",
            "expected_clause": decision["clause"],
            "customer_message": customer_snippet,
            "response_snippet": response_snippet,
        }],
        "verify_retry_count": state.get("verify_retry_count", 0) + 1,
        "messages": [{
            "role": "user",
            "content": (
                f"Your last response must cite clause {decision['clause']} exactly, "
                f"as returned by check_refund_policy, and must not state a different "
                f"outcome or amount. Revise your response."
            ),
        }],
    }


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
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": (
            "I'm not able to resolve this automatically -- escalating to a "
            "human teammate to take it from here."
        )}],
    }
    return {
        "messages": [message],
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
