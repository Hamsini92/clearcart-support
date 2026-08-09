"""Tests all 5 tools directly (no LLM calls -- fast, free, deterministic).
check_refund_policy is checked against every order in data/answer_key.md;
the other 4 get targeted cases covering their real branches.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
os.environ["MOCK_TODAY"] = "2026-08-12"

from tools.refund_tools import (  # noqa: E402
    check_refund_policy,
    escalate_to_human,
    get_customer,
    get_order,
    process_refund,
)

# order_id -> (expected decision, expected clause substring)
# mirrors data/answer_key.md, pinned to MOCK_TODAY=2026-08-12
EXPECTED_OUTCOMES = {
    "ORD-1001": ("approve", "§10.1"),
    "ORD-1002": ("deny", "§1"),
    "ORD-1003": ("deny", "§2.3"),
    "ORD-1004": ("approve", "§10.1"),
    "ORD-1005": ("approve", "§10.1"),
    "ORD-1006": ("escalate", "§6.1"),
    "ORD-1007": ("approve", "§10.1"),
    "ORD-1008": ("approve", "§10.1"),
    "ORD-1009": ("deny", "§2"),
    "ORD-1010": ("escalate", "§6.2"),
    "ORD-1011": ("escalate", "§9.1"),
    "ORD-1012": ("deny", "§2"),
    "ORD-1013": ("approve", "§10.1"),
    "ORD-1014": ("approve", "§10.1"),
    "ORD-1015": ("escalate", "§6.2"),
    "ORD-1016": ("approve", "§10.1"),
    "ORD-1017": ("deny", "§1"),
    "ORD-1018": ("approve", "§10.1"),
    "ORD-1019": ("approve", "§10.1"),
    "ORD-1020": ("deny", "§1"),
}


def test_check_refund_policy_matches_answer_key():
    failures = []
    for order_id, (expected_decision, expected_clause) in EXPECTED_OUTCOMES.items():
        result = check_refund_policy(order_id)
        if result["decision"] != expected_decision or expected_clause not in result.get("clause", ""):
            failures.append(f"{order_id}: expected ({expected_decision}, {expected_clause}), got {result}")
    assert not failures, "\n" + "\n".join(failures)


def test_check_refund_policy_unknown_order():
    result = check_refund_policy("ORD-9999")
    assert result["decision"] == "error"


def test_get_customer_found_and_not_found():
    found = get_customer("allison.hill.1@example.com")
    assert found["found"] is True
    assert found["customer"]["customer_id"] == "CUST-001"

    not_found = get_customer("nobody@example.com")
    assert not_found["found"] is False


def test_get_order_exact_id():
    result = get_order(order_id="ORD-1001")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["order_id"] == "ORD-1001"


def test_get_order_unknown_id():
    result = get_order(order_id="ORD-9999")
    assert result["matches"] == []


def test_get_order_ambiguous_description_match():
    result = get_order(customer_id="CUST-008", description="oak side table")
    assert len(result["matches"]) == 2
    assert {m["order_id"] for m in result["matches"]} == {"ORD-1008", "ORD-1018"}


def test_get_order_unique_description_match():
    result = get_order(customer_id="CUST-001", description="earbuds")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["order_id"] == "ORD-1001"


def test_process_refund_success():
    result = process_refund("ORD-1001", 129.99)
    assert result["success"] is True
    assert result["entry"]["amount"] == 129.99


def test_process_refund_unknown_order():
    result = process_refund("ORD-9999", 50.00)
    assert result["success"] is False


def test_escalate_to_human():
    result = escalate_to_human(reason="fraud flag", order_id="ORD-1010")
    assert result["status"] == "escalated"
    assert result["reason"] == "fraud flag"
    assert result["order_id"] == "ORD-1010"
