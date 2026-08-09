"""Tool failure/retry behavior at the dispatch layer (agent/graph.py's
_dispatch_tool_with_retry). No LLM calls -- these test the deterministic
boundary: does a transient failure get retried, does a persistent one fail
gracefully instead of crashing, and does a genuine business error (order not
found) get left alone instead of being retried pointlessly.

The retry wrapper itself is tool-agnostic -- it wraps the whole dispatch,
not get_order specifically. Only repository.get_order currently has a
simulated failure hook wired into it (matching the demo scenario), but
test_retry_wraps_any_tool_dispatch_not_just_get_order proves the mechanism
applies uniformly, including to the ownership-check lookup embedded inside
check_refund_policy's own dispatch.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
os.environ["MOCK_TODAY"] = "2026-08-12"

import pytest  # noqa: E402

from agent.graph import _dispatch_tool_with_retry  # noqa: E402
from data_access import repository  # noqa: E402


@pytest.fixture(autouse=True)
def clean_simulated_failure_state():
    """Simulated-failure config is read fresh from env vars on every call
    (see repository._simulated_failure_config), and _simulated_failures_used
    is process-global mutable state -- both must be reset around each test
    so these don't leak into each other or into the rest of the suite."""
    for key in ("SIMULATE_TOOL_FAILURE_ORDER_ID", "SIMULATE_TOOL_FAILURE_MODE", "DEMO_FAIL_FIRST_ORDER_LOOKUP"):
        os.environ.pop(key, None)
    repository._simulated_failures_used.clear()
    yield
    for key in ("SIMULATE_TOOL_FAILURE_ORDER_ID", "SIMULATE_TOOL_FAILURE_MODE", "DEMO_FAIL_FIRST_ORDER_LOOKUP"):
        os.environ.pop(key, None)
    repository._simulated_failures_used.clear()


# ---------------------------------------------------------------------------
# Case 1: transient failure, then recovery -- the demo scenario.
# ---------------------------------------------------------------------------

def test_transient_failure_then_recovery():
    os.environ["SIMULATE_TOOL_FAILURE_ORDER_ID"] = "ORD-1001"
    log_events = []
    state = {"customer_id": "CUST-001", "last_decision": None}

    result = _dispatch_tool_with_retry("get_order", {"order_id": "ORD-1001"}, state, {}, log_events)

    assert result["matches"][0]["order_id"] == "ORD-1001"
    statuses = [e.get("status") for e in log_events]
    assert statuses == ["failed, retrying", "recovered on retry"]


def test_demo_env_alias_matches_explicit_order_id_flag():
    """DEMO_FAIL_FIRST_ORDER_LOOKUP=true is shorthand for the exact demo
    scenario (ORD-1001, fail once) without remembering the longer flag name."""
    os.environ["DEMO_FAIL_FIRST_ORDER_LOOKUP"] = "true"
    log_events = []
    state = {"customer_id": "CUST-001", "last_decision": None}

    result = _dispatch_tool_with_retry("get_order", {"order_id": "ORD-1001"}, state, {}, log_events)

    assert result["matches"][0]["order_id"] == "ORD-1001"
    assert [e.get("status") for e in log_events] == ["failed, retrying", "recovered on retry"]


# ---------------------------------------------------------------------------
# Case 2: persistent failure -- must not crash, must not fabricate a result.
# ---------------------------------------------------------------------------

def test_persistent_failure_fails_gracefully_not_crash():
    os.environ["SIMULATE_TOOL_FAILURE_ORDER_ID"] = "ORD-1001"
    os.environ["SIMULATE_TOOL_FAILURE_MODE"] = "always"
    log_events = []
    state = {"customer_id": "CUST-001", "last_decision": None}

    result = _dispatch_tool_with_retry("get_order", {"order_id": "ORD-1001"}, state, {}, log_events)

    assert "error" in result
    assert [e.get("status") for e in log_events] == ["failed, retrying", "failed again, giving up"]


def test_persistent_failure_never_produces_a_policy_decision():
    """Same persistent failure, but through check_refund_policy -- the point
    where a refund amount could get fabricated if this weren't handled. Must
    come back as a dispatch-level error, never as a decision (approve/deny/
    escalate) computed from data that was never actually retrieved."""
    os.environ["SIMULATE_TOOL_FAILURE_ORDER_ID"] = "ORD-1001"
    os.environ["SIMULATE_TOOL_FAILURE_MODE"] = "always"
    log_events = []
    state = {"customer_id": "CUST-001", "last_decision": None}

    result = _dispatch_tool_with_retry(
        "check_refund_policy", {"order_id": "ORD-1001", "condition": "unopened"}, state, {}, log_events
    )

    assert "error" in result
    assert "decision" not in result
    assert [e.get("status") for e in log_events] == ["failed, retrying", "failed again, giving up"]


# ---------------------------------------------------------------------------
# Case 3: a genuine business error (order not found) is not a transient
# failure and must never be retried.
# ---------------------------------------------------------------------------

def test_order_not_found_is_never_retried():
    log_events = []
    state = {"customer_id": "CUST-001", "last_decision": None}

    result = _dispatch_tool_with_retry("get_order", {"order_id": "ORD-9999"}, state, {}, log_events)

    assert result["matches"] == []
    assert log_events == []  # resolved on the first attempt -- no failure/retry events at all


def test_wrong_customer_ownership_error_is_never_retried():
    """Another business-layer error (not infrastructure) -- must not retry."""
    log_events = []
    state = {"customer_id": "CUST-002", "last_decision": None}  # ORD-1001 belongs to CUST-001

    result = _dispatch_tool_with_retry(
        "check_refund_policy", {"order_id": "ORD-1001", "condition": "unopened"}, state, {}, log_events
    )

    assert result["decision"] == "error"
    assert log_events == []


# ---------------------------------------------------------------------------
# The retry wrapper is not get_order-specific.
# ---------------------------------------------------------------------------

def test_retry_wraps_any_tool_dispatch_not_just_get_order():
    """The same transient failure, but hit via check_refund_policy's own
    internal ownership-check lookup rather than a direct get_order call --
    proves the bounded retry applies to the whole dispatch layer, not one
    hardcoded tool."""
    os.environ["SIMULATE_TOOL_FAILURE_ORDER_ID"] = "ORD-1001"
    log_events = []
    state = {"customer_id": "CUST-001", "last_decision": None}

    result = _dispatch_tool_with_retry(
        "check_refund_policy", {"order_id": "ORD-1001", "condition": "unopened"}, state, {}, log_events
    )

    assert result["decision"] == "approve"
    assert [e.get("status") for e in log_events] == ["failed, retrying", "recovered on retry"]
