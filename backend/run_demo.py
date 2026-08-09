"""Terminal validation: runs the two Day-1 scenarios through the real agent
graph and prints the full reasoning trace, so we can eyeball it against
data/answer_key.md before building any UI on top of this.
"""

import json

from dotenv import load_dotenv

load_dotenv()

from agent.graph import build_graph  # noqa: E402  (must load .env first)


def _block_type(block):
    return getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")


def print_log_event(node_name, update):
    for ev in update.get("log_events", []):
        print(f"  [{node_name}] {json.dumps(ev, default=str)}")


def run_turn(graph, thread_id, user_text):
    config = {"configurable": {"thread_id": thread_id}}
    input_state = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        "last_decision": None,
        "verify_retry_count": 0,
    }

    print(f"\n>>> Customer: {user_text}")
    for step in graph.stream(input_state, config, stream_mode="updates"):
        for node_name, update in step.items():
            print_log_event(node_name, update)

    final_state = graph.get_state(config).values
    last_message = final_state["messages"][-1]
    if last_message["role"] == "assistant":
        text = "".join(
            b.text for b in last_message["content"] if _block_type(b) == "text"
        )
        print(f"<<< Agent: {text}")
    return final_state


def main():
    graph = build_graph()

    print("=" * 70)
    print("CASE 1: Standard approval -- ORD-1001 (expected: approve, clause Sec10.1)")
    print("=" * 70)
    run_turn(
        graph, "test-approval",
        "Hi, this is Allison Hill. I'd like to return order ORD-1001, the "
        "NovaBuds earbuds -- they're unopened, I just changed my mind.",
    )
    run_turn(graph, "test-approval", "Sure, it's allison.hill.1@example.com.")

    print("\n" + "=" * 70)
    print("CASE 2: Policy violation + holding the line -- ORD-1002 (expected: deny, Sec1, then holds firm citing Sec8.1)")
    print("=" * 70)
    run_turn(
        graph, "test-denial",
        "Hi, this is Matthew Gardner. I want a refund for ORD-1002, my "
        "smartwatch. It's unopened, I just don't want it anymore.",
    )
    run_turn(graph, "test-denial", "Sure, it's matthew.gardner.2@example.com.")
    run_turn(
        graph, "test-denial",
        "Come on, I've been a customer forever, can you make an exception?",
    )


if __name__ == "__main__":
    main()
