"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { WS_BASE } from "@/lib/config";
import { decisionOf, isMinor, summarize, type LogEvent } from "@/lib/formatEvent";

const ICONS = {
  conversations: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="3" y="5" width="18" height="12" rx="3" />
      <path d="M8 17 L8 21 L12 17 Z" fill="currentColor" stroke="none" />
    </svg>
  ),
  approve: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12.5 2.5 2.5 5-5" />
    </svg>
  ),
  deny: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="9" />
      <path d="m9 9 6 6M15 9l-6 6" />
    </svg>
  ),
  escalate: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 3 2 20h20L12 3z" />
      <path d="M12 10v4M12 17h.01" />
    </svg>
  ),
  pulse: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M3 12h4l2 7 4-14 2 7h6" />
    </svg>
  ),
};

export default function AdminPage() {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [selectedThread, setSelectedThread] = useState("all");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const socket = new WebSocket(`${WS_BASE}/ws/admin`);
    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onmessage = (msg) => {
      const parsed = JSON.parse(msg.data);
      if (parsed.type === "history") {
        setEvents((prev) => [...prev, ...(parsed.events as LogEvent[])]);
      } else {
        setEvents((prev) => [...prev, parsed as LogEvent]);
      }
    };
    return () => socket.close();
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events, selectedThread]);

  const threads = useMemo(
    () => Array.from(new Set(events.map((e) => e.thread_id).filter(Boolean))) as string[],
    [events]
  );

  const stats = useMemo(() => {
    const policyEvents = events.filter((e) => e.tool === "check_refund_policy");
    return {
      conversations: threads.length,
      approve: policyEvents.filter((e) => decisionOf(e) === "approve").length,
      deny: policyEvents.filter((e) => decisionOf(e) === "deny").length,
      escalate: policyEvents.filter((e) => decisionOf(e) === "escalate").length,
    };
  }, [events, threads]);

  const visibleEvents =
    selectedThread === "all" ? events : events.filter((e) => e.thread_id === selectedThread);

  return (
    <div className="page-wide" style={{ paddingTop: "0.5rem" }}>
      <div className="dashboard-header">
        <div>
          <h1>Admin Dashboard</h1>
          <p className="page-sub" style={{ margin: 0 }}>
            Live reasoning trace — every tool call, decision, and clause citation as it happens.
          </p>
        </div>
        <span className="status-badge">
          <span className={`status-dot ${connected ? "connected" : "disconnected"}`} />
          {connected ? "Connected — live" : "Disconnected"}
        </span>
      </div>

      <div className="stats-row" style={{ marginTop: "2rem" }}>
        <div className="stat-tile">
          <div className="stat-icon">{ICONS.conversations}</div>
          <div className="stat-label">Conversations</div>
          <div className="stat-value">{stats.conversations}</div>
        </div>
        <div className="stat-tile approve">
          <div className="stat-icon">{ICONS.approve}</div>
          <div className="stat-label">Approved</div>
          <div className="stat-value">{stats.approve}</div>
        </div>
        <div className="stat-tile deny">
          <div className="stat-icon">{ICONS.deny}</div>
          <div className="stat-label">Denied</div>
          <div className="stat-value">{stats.deny}</div>
        </div>
        <div className="stat-tile escalate">
          <div className="stat-icon">{ICONS.escalate}</div>
          <div className="stat-label">Escalated</div>
          <div className="stat-value">{stats.escalate}</div>
        </div>
      </div>

      <div className="admin-toolbar">
        <span className="section-label" style={{ margin: 0 }}>
          Reasoning log
        </span>
        {threads.length > 0 && (
          <select
            className="thread-filter"
            value={selectedThread}
            onChange={(e) => setSelectedThread(e.target.value)}
          >
            <option value="all">All conversations ({threads.length})</option>
            {threads.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="log-stream">
        {visibleEvents.length === 0 && (
          <div className="log-empty">
            <div className="pulse-icon">{ICONS.pulse}</div>
            <h3>Waiting for activity</h3>
            <p>Send a message from the customer chat and watch the agent reason through it here, live.</p>
          </div>
        )}

        {visibleEvents.map((ev, i) => {
          const decision = decisionOf(ev);
          const minor = isMinor(ev);
          return (
            <div key={i} className={`log-card ${decision ? `decision-${decision}` : ""} ${minor ? "minor" : ""}`}>
              <div className="log-head">
                <span className="log-node-tag">{ev.node}{ev.tool ? ` · ${ev.tool}` : ""}</span>
                {decision && <span className={`pill ${decision}`}>{decision}</span>}
                {ev.thread_id && <span className="log-thread">{ev.thread_id}</span>}
              </div>
              <div className="log-summary">{summarize(ev)}</div>
              <details className="log-details">
                <summary>View raw event</summary>
                <pre>{JSON.stringify(ev, null, 2)}</pre>
              </details>
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
