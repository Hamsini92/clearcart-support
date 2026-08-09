"""Reads crm.json, orders.json, and policy.md. No writes except the mock refund ledger."""

import json
import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CRM_PATH = DATA_DIR / "loopp_crm.json"
ORDERS_PATH = DATA_DIR / "loopp_orders.json"
POLICY_PATH = DATA_DIR / "loopp_policy.md"


class TransientLookupError(Exception):
    """Simulates a real-world flaky dependency (a CRM/order-service call that
    times out) -- the retryable half of the failure taxonomy. A genuine
    business outcome like "order not found" is never raised as an exception;
    it's a normal return value, precisely so retry logic (agent/graph.py's
    _dispatch_tool_with_retry) can't accidentally retry it. graph.py is what
    actually retries and logs this."""


_simulated_failures_used: set = set()


def _simulated_failure_config():
    """Read fresh on every call (same pattern as MOCK_TODAY in get_today())
    so tests can flip these env vars per-test rather than being stuck with
    whatever was set at import time.
      SIMULATE_TOOL_FAILURE_ORDER_ID -- which order_id's lookup fails
      SIMULATE_TOOL_FAILURE_MODE     -- "once" (default: fails first call,
                                         recovers after) or "always" (every
                                         call fails -- for testing/demoing
                                         the exhausted-retries path)
      DEMO_FAIL_FIRST_ORDER_LOOKUP=true -- shorthand alias for ORD-1001/once,
                                            used unless an explicit order_id
                                            is already set above
    """
    order_id = os.environ.get("SIMULATE_TOOL_FAILURE_ORDER_ID")
    if not order_id and os.environ.get("DEMO_FAIL_FIRST_ORDER_LOOKUP", "").lower() == "true":
        order_id = "ORD-1001"
    mode = os.environ.get("SIMULATE_TOOL_FAILURE_MODE", "once")
    return order_id, mode


def get_today() -> date:
    """Live clock by default. MOCK_TODAY (YYYY-MM-DD) overrides it for demos/tests."""
    override = os.environ.get("MOCK_TODAY")
    if override:
        return date.fromisoformat(override)
    return date.today()


@lru_cache(maxsize=1)
def load_customers() -> dict:
    with open(CRM_PATH) as f:
        customers = json.load(f)
    return {c["customer_id"]: c for c in customers}


@lru_cache(maxsize=1)
def load_orders() -> list:
    with open(ORDERS_PATH) as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_policy_text() -> str:
    with open(POLICY_PATH) as f:
        return f.read()


def get_customer(customer_id: str) -> Optional[dict]:
    return load_customers().get(customer_id)


def get_customer_by_email(email: str) -> Optional[dict]:
    email = email.strip().lower()
    for customer in load_customers().values():
        if customer["email"].lower() == email:
            return customer
    return None


def _normalize_order_id(raw: str) -> str:
    """Normalizes order ID formats so "1001", "1,001", "ord1001", "ORD 1001"
    all resolve the same as "ORD-1001". Voice in particular tends to produce
    bare numbers ("1001") rather than the spelled-out "ORD-1001" -- this is
    a deterministic fix at the data layer, not something left to the LLM to
    get right by reformatting what it heard."""
    cleaned = raw.strip().upper().replace(",", "").replace(" ", "").replace("-", "")
    if cleaned.isdigit():
        return f"ORD-{cleaned}"
    if cleaned.startswith("ORD") and cleaned[3:].isdigit():
        return f"ORD-{cleaned[3:]}"
    return raw.strip().upper()


def get_order(order_id: str) -> Optional[dict]:
    target = _normalize_order_id(order_id)
    sim_order_id, sim_mode = _simulated_failure_config()
    if sim_order_id and target == _normalize_order_id(sim_order_id):
        if sim_mode == "always" or target not in _simulated_failures_used:
            _simulated_failures_used.add(target)
            raise TransientLookupError(f"order lookup service timed out looking up {target}")
    for order in load_orders():
        if _normalize_order_id(order["order_id"]) == target:
            return order
    return None


def get_orders_by_customer(customer_id: str) -> list:
    return [o for o in load_orders() if o["customer_id"] == customer_id]


def find_orders_by_product(customer_id: str, query: str) -> list:
    """Substring match on product name, scoped to one customer. Used for the
    ambiguous-match case where a customer describes an item without an order ID."""
    query = query.strip().lower()
    return [
        o for o in get_orders_by_customer(customer_id)
        if query in o["product_name"].lower()
    ]
