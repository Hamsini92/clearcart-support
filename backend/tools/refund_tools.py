"""The 5 tools the agent can call. Each wraps data_access.repository and does
one narrow thing a real support rep would be allowed to do. Eligibility math
in check_refund_policy is deterministic code, not LLM judgment -- the LLM's
job is to call these correctly and explain the result, not compute it."""

from datetime import date
from typing import Optional

from data_access import repository

RETURN_WINDOWS_DAYS = {
    "electronics": 15,
    "apparel": 30,
    "home_furniture": 7,
    "general": 30,
}
NON_REFUNDABLE_CATEGORIES = {"digital_goods", "personal_care"}
LOYALTY_EXTENSION_DAYS = {"gold": 7, "platinum": 7}
LOYALTY_APPLIES_TO_CATEGORIES = {"apparel", "general"}
HIGH_VALUE_THRESHOLD = 500.00
ABUSE_REFUND_COUNT_THRESHOLD = 3
DAMAGE_REPORTING_DEADLINE_DAYS = 7
OPENED_ELECTRONICS_REFUND_RATE = 0.85

# In-memory mock ledger. Resets each process run -- fine for a demo, would
# become a real refunds table in production.
_refund_ledger = []


def get_customer(email: str) -> dict:
    """Look up a customer by email."""
    customer = repository.get_customer_by_email(email)
    if customer is None:
        return {"found": False}
    return {"found": True, "customer": customer}


def get_order(
    order_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Look up an order by exact order_id, OR search a customer's orders by a
    text description (e.g. "oak side table"). The description form can return
    more than one match -- if it does, the caller must ask which order before
    proceeding, not guess."""
    if order_id:
        order = repository.get_order(order_id)
        return {"matches": [order] if order else []}
    if customer_id and description:
        return {"matches": repository.find_orders_by_product(customer_id, description)}
    return {"matches": [], "error": "provide either order_id, or customer_id + description"}


def check_refund_policy(
    order_id: str,
    condition: str = "unopened",
    has_damage_evidence: bool = False,
) -> dict:
    """Evaluate one order against the refund policy. `condition` is whatever
    the customer says about the item's state: "unopened", "opened",
    "damaged", or "customer_damaged" (misuse/accidental damage after
    delivery). Returns a decision, the policy clause(s) it rests on, and the
    refund amount if approved. Never invents outcomes outside these rules."""
    order = repository.get_order(order_id)
    if order is None:
        return {"decision": "error", "reason": "order not found"}
    customer = repository.get_customer(order["customer_id"])
    category = order["category"]
    today = repository.get_today()
    delivery = date.fromisoformat(order["delivery_date"])
    days_since_delivery = (today - delivery).days

    # 1. Non-refundable status and product condition (policy §2-3)
    if order.get("final_sale"):
        return _deny("§2.3", "item is marked final sale")

    if category in NON_REFUNDABLE_CATEGORIES:
        if condition != "damaged":
            return _deny("§2.1/§2.2", f"{category} is non-refundable unless damaged or defective on arrival")
        # damaged claim on a normally non-refundable item falls through to
        # the evidence/window checks below, per §2.2 and §3.3

    if condition == "customer_damaged":
        return _deny("§3.4", "damage was customer-caused after delivery, not refundable")

    # 2. Return window (policy §1, plus §5 loyalty extension)
    base_window = RETURN_WINDOWS_DAYS.get(category, 30)
    extension = 0
    if category in LOYALTY_APPLIES_TO_CATEGORIES:
        extension = LOYALTY_EXTENSION_DAYS.get(customer["tier"], 0)
    window = base_window + extension
    if days_since_delivery > window:
        return _deny("§1", f"return window expired ({window} days, {days_since_delivery} since delivery)")

    # 3. Evidence requirements for damage claims (policy §4)
    if condition == "damaged":
        if days_since_delivery > DAMAGE_REPORTING_DEADLINE_DAYS:
            return _deny("§4.1", "damage must be reported within 7 days of delivery")
        if not has_damage_evidence:
            return _deny("§4.2", "photo evidence required before a damage-based refund can be approved; customer may resubmit while the window remains open")

    # 4. Refund amount (policy §10)
    if condition == "opened" and category == "electronics":
        amount = round(order["price"] * OPENED_ELECTRONICS_REFUND_RATE, 2)
        amount_clause = "§3.2/§10.2"
    else:
        amount = order["price"]
        amount_clause = "§10.1"

    # 5. Abuse/fraud and high-value review (policy §6, §9) -- these
    # escalate, they do not deny; a human makes the final call.
    if customer.get("fraud_flag"):
        return _escalate("§6.2", "account has an active fraud flag")
    if customer.get("chargeback_flag"):
        return _escalate("§6.2", "account has an active chargeback flag")
    if customer.get("refunds_last_90_days", 0) >= ABUSE_REFUND_COUNT_THRESHOLD:
        return _escalate("§6.1", "3 or more refunds in the last 90 days")
    if amount > HIGH_VALUE_THRESHOLD:
        return _escalate("§9.1", f"refund amount ${amount:.2f} exceeds ${HIGH_VALUE_THRESHOLD:.2f} manual review threshold")

    return {
        "decision": "approve",
        "clause": amount_clause,
        "refund_amount": amount,
        "reason": f"eligible under {amount_clause}, within {window}-day window",
    }


def process_refund(order_id: str, amount: float) -> dict:
    """Execute an approved refund. Only call this after check_refund_policy
    returns decision="approve". Mock ledger write -- would be a real payment
    provider call in production."""
    order = repository.get_order(order_id)
    if order is None:
        return {"success": False, "reason": "order not found"}
    entry = {
        "order_id": order_id,
        "customer_id": order["customer_id"],
        "amount": amount,
        "payment_method": order["payment_method"],
        "processed_at": repository.get_today().isoformat(),
    }
    _refund_ledger.append(entry)
    return {"success": True, "entry": entry}


def escalate_to_human(reason: str, order_id: Optional[str] = None) -> dict:
    """Flag a case for manual review instead of deciding it. Does not contact
    a real person -- marks the case's status for the admin dashboard."""
    return {"status": "escalated", "reason": reason, "order_id": order_id}


def _deny(clause: str, reason: str) -> dict:
    return {"decision": "deny", "clause": clause, "reason": reason}


def _escalate(clause: str, reason: str) -> dict:
    return {"decision": "escalate", "clause": clause, "reason": reason}
