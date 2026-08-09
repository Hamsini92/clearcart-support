"""Adversarial cases -- each one is a way a customer (or a jailbroken/careless
LLM) could try to get a refund the policy doesn't allow, or get one twice.
These hit the same deterministic layer as test_refund_tools.py (no LLM calls)
so they're fast and reproducible, but specifically target the server-side
guardrails in agent/graph.py's tools_node dispatch -- the things that must
hold even if a prompt injection or a model mistake tries to skip them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
os.environ["MOCK_TODAY"] = "2026-08-12"

from agent.graph import _dispatch_tool  # noqa: E402
from tools.refund_tools import check_refund_policy, process_refund  # noqa: E402


# ---------------------------------------------------------------------------
# Ownership: an order_id alone is not enough -- it must belong to the
# customer the conversation has actually identified.
# ---------------------------------------------------------------------------

def test_ownership_check_blocks_wrong_customer():
    """ORD-1001 belongs to CUST-001, not CUST-002. A customer (or a model
    that guesses/borrows an order number) must not be able to evaluate or
    refund someone else's order just by knowing its ID."""
    state = {"customer_id": "CUST-002", "last_decision": None}
    result = _dispatch_tool("check_refund_policy", {"order_id": "ORD-1001", "condition": "unopened"}, state, {})
    assert result["decision"] == "error"
    assert "does not belong" in result["reason"]


def test_ownership_check_requires_customer_identified():
    """No get_customer call yet -> check_refund_policy must refuse, not
    silently evaluate an anonymous order lookup."""
    state = {"customer_id": None, "last_decision": None}
    result = _dispatch_tool("check_refund_policy", {"order_id": "ORD-1001"}, state, {})
    assert result["decision"] == "error"
    assert "not yet identified" in result["reason"]


# ---------------------------------------------------------------------------
# Refund amount: never trust what the LLM's tool_use block says. Only the
# amount check_refund_policy already calculated is ever paid out.
# ---------------------------------------------------------------------------

def test_process_refund_ignores_llm_supplied_amount():
    """Even if a tool call somehow still carried an 'amount' field (e.g. an
    older/different client, or a manipulated request), the backend must
    ignore it and use the amount from the held policy decision instead."""
    state = {
        "customer_id": "CUST-005",
        "last_decision": {"decision": "approve", "order_id": "ORD-1005", "refund_amount": 96.0, "clause": "§10.1"},
    }
    result = _dispatch_tool("process_refund", {"order_id": "ORD-1005", "amount": 999999.00}, state, {})
    assert result["success"] is True
    assert result["entry"]["amount"] == 96.0


def test_process_refund_blocked_without_prior_approval():
    """No check_refund_policy decision on record at all -- must refuse."""
    state = {"customer_id": "CUST-006", "last_decision": None}
    result = _dispatch_tool("process_refund", {"order_id": "ORD-1006"}, state, {})
    assert result["success"] is False


def test_process_refund_blocked_for_mismatched_order():
    """Decision on record approved ORD-1001, but the refund call targets a
    different order (ORD-1016) the same customer also owns -- an approval
    for one order must not be reusable to pay out another."""
    state = {
        "customer_id": "CUST-001",
        "last_decision": {"decision": "approve", "order_id": "ORD-1001", "refund_amount": 129.99, "clause": "§10.1"},
    }
    result = _dispatch_tool("process_refund", {"order_id": "ORD-1016"}, state, {})
    assert result["success"] is False


def test_duplicate_refund_blocked():
    """Same order refunded twice -- second call must be rejected, not
    silently double-pay."""
    first = process_refund("ORD-1004", 118.00)
    assert first["success"] is True
    second = process_refund("ORD-1004", 118.00)
    assert second["success"] is False
    assert "already refunded" in second["reason"]


# ---------------------------------------------------------------------------
# Policy edge cases the review specifically flagged.
# ---------------------------------------------------------------------------

def test_final_sale_denial_is_stable_under_repeated_pressure():
    """Simulates a customer repeatedly asking for an exception -- the
    deterministic decision must not drift or soften on repeat evaluation."""
    for _ in range(3):
        result = check_refund_policy("ORD-1003")
        assert result["decision"] == "deny"
        assert result["clause"] == "§2.3"


def test_digital_goods_denied_even_with_damage_claim():
    """Digital goods have no damage exception -- unlike physical
    non-refundable categories (personal_care, §2.2), a 'damaged' claim must
    not unlock a refund for a non-physical product."""
    result = check_refund_policy("ORD-1012", condition="damaged", has_damage_evidence=True)
    assert result["decision"] == "deny"
    assert result["clause"] == "§2.1"


def test_opened_electronics_missing_accessories_escalates():
    """Opened electronics with accessories missing must not silently qualify
    for the 85% reduced-rate refund -- it needs a human look."""
    result = check_refund_policy("ORD-1001", condition="opened", accessories_present=False)
    assert result["decision"] == "escalate"
    assert result["clause"] == "§3.2"


def test_opened_electronics_non_functional_escalates():
    result = check_refund_policy("ORD-1001", condition="opened", is_functional=False)
    assert result["decision"] == "escalate"
    assert result["clause"] == "§3.2"


def test_opened_electronics_fully_eligible_gets_reduced_rate():
    """The 85% rate still works for the clean case -- functional, all
    accessories present."""
    result = check_refund_policy("ORD-1001", condition="opened", accessories_present=True, is_functional=True)
    assert result["decision"] == "approve"
    assert result["refund_amount"] == round(129.99 * 0.85, 2)


def test_fraud_flag_escalates_not_approves():
    result = check_refund_policy("ORD-1010")
    assert result["decision"] == "escalate"
    assert result["clause"] == "§6.2"


def test_high_value_order_escalates_not_auto_approved():
    result = check_refund_policy("ORD-1011")
    assert result["decision"] == "escalate"
    assert result["clause"] == "§9.1"


def test_checks_list_reflects_the_failing_clause():
    result = check_refund_policy("ORD-1002")  # return window expired, §1
    assert result["decision"] == "deny"
    assert isinstance(result["checks"], list) and result["checks"]
    assert result["checks"][-1]["clause"] == "§1"
    assert result["checks"][-1]["passed"] is False
