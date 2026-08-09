export type LogEvent = {
  thread_id?: string;
  node: string;
  tool?: string;
  input?: Record<string, unknown>;
  output?: { decision?: string; clause?: string; reason?: string; refund_amount?: number; found?: boolean; matches?: unknown[]; [key: string]: unknown };
  result?: string;
  reason?: string;
  [key: string]: unknown;
};

export type Decision = "approve" | "deny" | "escalate" | undefined;

export function decisionOf(ev: LogEvent): Decision {
  const d = ev.output?.decision;
  return d === "approve" || d === "deny" || d === "escalate" ? d : undefined;
}

/** True for low-signal steps (agent chose not to call a tool, verify had
 * nothing to check) -- shown, but visually de-emphasized so real tool calls
 * and decisions stand out. Mismatches stay full-weight; they're the point. */
export function isMinor(ev: LogEvent): boolean {
  if (ev.node === "agent") return ((ev.tool_calls_requested as string[] | undefined) ?? []).length === 0;
  if (ev.node === "verify") {
    const result = ev.result ?? "";
    return !result.startsWith("MISMATCH") && !result.includes("clause citation matches") && !result.includes("escalation confirmed");
  }
  return false;
}

/** Turns a raw log event into a short, human-readable summary line. */
export function summarize(ev: LogEvent): string {
  if (ev.node === "agent") {
    const tools = (ev.tool_calls_requested as string[] | undefined) ?? [];
    return tools.length ? `Deciding what to check -- calling ${tools.join(", ")}` : "Ready to respond";
  }

  if (ev.node === "tools") {
    switch (ev.tool) {
      case "get_customer": {
        const found = ev.output?.found;
        const email = ev.input?.email as string | undefined;
        return found ? `Verified customer -- ${email}` : `No customer found for ${email}`;
      }
      case "get_order": {
        const matches = (ev.output?.matches as unknown[] | undefined) ?? [];
        if (matches.length === 0) return "No matching order found";
        if (matches.length === 1) return "Order identified";
        return `Ambiguous -- ${matches.length} orders matched, asking customer to clarify`;
      }
      case "check_refund_policy": {
        const decision = ev.output?.decision;
        const clause = ev.output?.clause;
        return `Policy check -- ${String(decision).toUpperCase()} (${clause}) -- ${ev.output?.reason ?? ""}`;
      }
      case "process_refund": {
        const entry = ev.output?.entry as { amount?: number } | undefined;
        return `Refund processed -- $${entry?.amount?.toFixed(2) ?? "?"}`;
      }
      case "escalate_to_human":
        return `Escalated for manual review -- ${ev.output?.reason ?? ev.reason ?? ""}`;
      default:
        return `${ev.tool} called`;
    }
  }

  if (ev.node === "verify") {
    const result = (ev.result as string) ?? "Verified";
    if (result.startsWith("MISMATCH")) {
      const expected = ev.expected_clause as string | undefined;
      const reply = ev.response_snippet as string | undefined;
      return `MISMATCH -- expected ${expected ?? "a clause"} in the reply, got: "${reply ?? "?"}"`;
    }
    return result;
  }
  if (ev.node === "safety_stop") return ev.reason ?? "Safety stop triggered";

  return JSON.stringify(ev);
}
