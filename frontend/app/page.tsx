import Link from "next/link";

export default function Home() {
  return (
    <div>
      <section className="hero-band">
        <div className="hero-band-inner">
          <div>
            <div className="hero-badge">
              <span className="dot" />
              Agent online
            </div>
            <h1>Refunds, decided on policy — not guesswork.</h1>
            <p className="hero-sub">
              A tool-calling agent looks up the customer and order, checks real
              policy rules, and approves, denies, or escalates — with every
              step logged for review, live.
            </p>
            <div className="hero-cta-row">
              <Link href="/chat" className="btn-primary">
                Open Customer Chat →
              </Link>
              <Link href="/admin" className="btn-secondary">
                View Admin Dashboard
              </Link>
            </div>
          </div>

          <div className="trace-preview">
            <div className="trace-title">Live reasoning trace</div>
            <div className="trace-line">
              <span className="trace-node">get_customer</span>
              <span className="trace-text">verified allison.hill.1@example.com</span>
            </div>
            <div className="trace-line">
              <span className="trace-node">get_order</span>
              <span className="trace-text">ORD-1001 · NovaBuds Earbuds</span>
            </div>
            <div className="trace-line">
              <span className="trace-node">check_refund_policy</span>
              <span className="trace-text">§10.1</span>
              <span className="trace-pill approve">approve</span>
            </div>
            <div className="trace-line">
              <span className="trace-node">process_refund</span>
              <span className="trace-text">$129.99 refunded</span>
            </div>
          </div>
        </div>
      </section>

      <div className="page-narrow">
        <div className="section-label">What&rsquo;s actually running</div>
        <div className="feature-strip">
          <div className="feature-item">
            <svg className="feature-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 0 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 0 1 0-4h.09a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34H9a1.7 1.7 0 0 0 1-1.55V3a2 2 0 0 1 4 0v.09a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87V9a1.7 1.7 0 0 0 1.55 1H21a2 2 0 0 1 0 4h-.09a1.7 1.7 0 0 0-1.55 1z" />
            </svg>
            <div className="feature-label">Backend</div>
            <div className="feature-value">Dynamic tool orchestration</div>
          </div>
          <div className="feature-item">
            <svg className="feature-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3z" />
              <path d="m9 12 2 2 4-4" />
            </svg>
            <div className="feature-label">Policy</div>
            <div className="feature-value">Deterministic eligibility engine</div>
          </div>
          <div className="feature-item">
            <svg className="feature-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
            <div className="feature-label">Oversight</div>
            <div className="feature-value">Live reasoning &amp; escalation logs</div>
          </div>
          <div className="feature-item">
            <svg className="feature-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 7v5l3 3" />
            </svg>
            <div className="feature-label">Memory</div>
            <div className="feature-value">Per-conversation session state</div>
          </div>
        </div>
      </div>
    </div>
  );
}
