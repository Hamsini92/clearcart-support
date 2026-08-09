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


def get_order(order_id: str) -> Optional[dict]:
    for order in load_orders():
        if order["order_id"] == order_id:
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
