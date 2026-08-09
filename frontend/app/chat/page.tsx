"use client";

import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { API_BASE } from "@/lib/config";

type Message = { role: "customer" | "agent"; text: string; at: Date };

const SUGGESTIONS = [
  "I'd like to return order ORD-1001, it's unopened",
  "I want a refund for my smartwatch, order ORD-1002",
  "I want to return my oak side table",
];

function timeLabel(d: Date) {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function AgentIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="16" height="16">
      <path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

export default function ChatPage() {
  const threadId = useRef(crypto.randomUUID());
  const sending = useRef(false); // synchronous guard -- React state updates are async and can't
  // reliably block a fast double-submit (double-click, or Enter + button both firing) on their own.
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || sending.current) return;
    sending.current = true;

    setMessages((prev) => [...prev, { role: "customer", text: trimmed, at: new Date() }]);
    setInput("");
    setPending(true);

    try {
      const res = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thread_id: threadId.current, message: trimmed }),
      });
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      const data = await res.json();
      setMessages((prev) => [...prev, { role: "agent", text: data.reply, at: new Date() }]);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "agent", text: "Sorry, something went wrong reaching the agent. Please try again.", at: new Date() },
      ]);
    } finally {
      setPending(false);
      sending.current = false;
    }
  }

  return (
    <div className="chat-shell">
      <div className="page-narrow">
        <h1>Customer Chat</h1>
        <p className="page-sub">Ask about a return or refund -- the agent will look up your account and order.</p>

        <div className="chat-panel">
          <div className="chat-widget-header">
            <div className="chat-widget-avatar">
              <AgentIcon />
            </div>
            <div>
              <div className="chat-widget-title">ClearCart Support</div>
              <div className="chat-widget-status">
                <span className="dot" />
                Online now
              </div>
            </div>
          </div>

          <div className="chat-thread">
            {messages.length === 0 && (
              <div className="chat-empty">
                <div className="chat-empty-icon">
                  <AgentIcon />
                </div>
                <h3>Start a conversation</h3>
                <p>Try one of these, or type your own request below.</p>
                <div className="suggestion-chips">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} className="chip" onClick={() => setInput(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((m, i) => (
              <div key={i} className={`message-row ${m.role}`}>
                <div className={`avatar ${m.role}`}>
                  {m.role === "customer" ? "C" : <AgentIcon />}
                </div>
                <div className="bubble-col">
                  <div className={`bubble ${m.role}`}>
                    {m.role === "agent" ? <ReactMarkdown>{m.text}</ReactMarkdown> : m.text}
                  </div>
                  <span className="timestamp">{timeLabel(m.at)}</span>
                </div>
              </div>
            ))}

            {pending && (
              <div className="message-row agent">
                <div className="avatar agent">
                  <AgentIcon />
                </div>
                <div className="bubble-col">
                  <div className="bubble pending">Agent is thinking…</div>
                </div>
              </div>
            )}
          </div>

          <form
            className="chat-input"
            onSubmit={(e) => {
              e.preventDefault();
              send(input);
            }}
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Type a message…"
              disabled={pending}
            />
            <button type="submit" disabled={pending}>
              Send
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
