"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_DISPATCH_API ?? "http://127.0.0.1:8787";

type Item = { id: string; kind: string; sender: string; subject: string; body: string; document?: string | null; status: string };
type Case = { id: string; item_id: string; owner_agent?: string; status: string; intent?: string; confidence?: number; reason?: string; pack?: any; result?: any; reviewer?: any[]; cost_usd: number; tokens_in: number; tokens_out: number };
type Action = { id: string; case_id: string; type: string; payload: any; amount?: number; counterparty?: string; policy_decision?: string; status: string; reviewer_verdict?: any; result?: any };
type Ev = { id: number; ts: number; case_id?: string; agent: string; kind: string; message: string };
type State = {
  items: Item[]; cases: Case[]; actions: Action[]; approvals: any[]; memories: any[]; calls: any[];
  worker: { running: boolean }; model: string; last_event_id: number;
  ledger: { handled: number; by_kind: Record<string, number>; awaiting_approval: number; executed: number; blocked: number; escalated: number; estimated_minutes: number; cost_usd: number; tokens: number };
};
type Settings = { provider: string; model: string; base_url: string; has_key: boolean; key_hint: string; anthropic_models: string[] };

const KIND: Record<string, string> = { supplier_invoice: "Supplier invoice", customer_email: "Customer email", scheduled: "Scheduled", question: "Question" };
const AGENT: Record<string, string> = { intake: "Intake → Accounts", customer: "Customer", followup: "Follow-up", analyst: "Analyst", escalate: "Escalated" };

async function api(path: string, init?: RequestInit) {
  const r = await fetch(API + path, { ...init, headers: { "content-type": "application/json", ...(init?.headers ?? {}) } });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export default function ControlRoom() {
  const [state, setState] = useState<State | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [offline, setOffline] = useState(false);
  const lastEvent = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const s = await api("/api/state");
      setState(s);
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    api("/api/settings").then(setSettings).catch(() => {});
    const es = new EventSource(`${API}/api/events?after=0`);
    let t: ReturnType<typeof setTimeout> | null = null;
    es.addEventListener("log", (e) => {
      const ev = JSON.parse((e as MessageEvent).data) as Ev;
      lastEvent.current = ev.id;
      setEvents((prev) => (prev.some((p) => p.id === ev.id) ? prev : [...prev.slice(-400), ev]));
      if (!t) t = setTimeout(() => { t = null; refresh(); }, 250);
    });
    es.onerror = () => setOffline(true);
    const poll = setInterval(refresh, 4000);
    return () => { es.close(); clearInterval(poll); };
  }, [refresh]);

  const cases = state?.cases ?? [];
  const byItem = useMemo(() => Object.fromEntries(cases.map((c) => [c.item_id, c])), [cases]);
  const selCase = selected ? byItem[selected] : undefined;
  const selItem = state?.items.find((i) => i.id === selected);
  const selActions = useMemo(() => (state?.actions ?? []).filter((a) => a.case_id === selCase?.id), [state, selCase]);
  const selEvents = useMemo(() => events.filter((e) => e.case_id === selCase?.id), [events, selCase]);
  const pending = (state?.actions ?? []).filter((a) => a.status === "awaiting_approval");

  useEffect(() => {
    if (!selected && state?.items.length) setSelected(state.items[0].id);
  }, [state, selected]);

  const run = async () => { await api("/api/demo/run", { method: "POST" }); refresh(); };
  const reset = async () => { await api("/api/demo/reset", { method: "POST" }); setEvents([]); setSelected(null); refresh(); };
  const decide = async (id: string, decision: "approve" | "reject") => { await api(`/api/actions/${id}/decide`, { method: "POST", body: JSON.stringify({ decision }) }); refresh(); };

  return (
    <>
      <header className="top">
        <div className="brand">
          <span className="name">Dispatch</span>
          <span className="mono dim">control room · Northwind Supplies</span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span className={`badge ${state?.worker.running ? "accent" : ""}`}>
            <span className={`dot ${state?.worker.running ? "pulse" : ""}`} /> {state?.worker.running ? "team working" : offline ? "api offline" : "idle"}
          </span>
          <button className="btn sm" onClick={() => setShowSettings((s) => !s)} title={state?.model}>
            {settings ? modelLabel(settings) : "model"}
          </button>
          <button className="btn sm" onClick={reset} disabled={state?.worker.running}>Reset</button>
          <button className="btn primary sm" onClick={run} disabled={state?.worker.running}>Run the overnight inbox →</button>
        </div>
      </header>

      {showSettings && settings ? <SettingsPanel settings={settings} onChange={(s) => { setSettings(s); refresh(); }} onClose={() => setShowSettings(false)} /> : null}

      <main className="grid">
        <section className="col">
          <div className="card">
            <h2>Queue · {state?.items.length ?? 0} items</h2>
            {!state?.items.length ? <div className="empty">Press “Run the overnight inbox” to load 8 items: 4 supplier invoices, 2 customer emails, a scheduled receivables check and a question from the owner.</div> : null}
            {state?.items.map((it) => {
              const c = byItem[it.id];
              return (
                <button key={it.id} className="item" aria-selected={selected === it.id} onClick={() => setSelected(it.id)}>
                  <span className="subject">{it.subject}</span>
                  <span className="meta">
                    <span className="badge">{KIND[it.kind] ?? it.kind}</span>
                    {c?.owner_agent ? <span className="badge accent">{AGENT[c.owner_agent] ?? c.owner_agent}</span> : null}
                    <StatusBadge status={c?.status ?? it.status} />
                  </span>
                </button>
              );
            })}
          </div>
          <div className="card">
            <h2>Live transcript</h2>
            <Transcript events={events.slice(-14)} />
          </div>
        </section>

        <section className="col">
          {selItem ? <Detail item={selItem} c={selCase} actions={selActions} events={selEvents} onDecide={decide} /> : <div className="card empty">Select an item</div>}
        </section>

        <aside className="col">
          <div className="card">
            <h2>Waiting for you · {pending.length}</h2>
            {pending.length === 0 ? <div className="note">Nothing needs your decision.</div> : null}
            {pending.map((a) => (
              <div key={a.id} className="action" style={{ marginBottom: 8 }}>
                <div className="head">
                  <strong>{label(a.type)}</strong>
                  {a.amount ? <span className="badge warn">USD {a.amount.toLocaleString()}</span> : null}
                </div>
                <div className="ink2" style={{ fontSize: 12.5 }}>{summary(a)}</div>
                <div style={{ display: "flex", gap: 6 }}>
                  <button className="btn ok sm" onClick={() => decide(a.id, "approve")}>Approve</button>
                  <button className="btn bad sm" onClick={() => decide(a.id, "reject")}>Reject</button>
                  <button className="btn sm" onClick={() => setSelected(cases.find((c) => c.id === a.case_id)?.item_id ?? null)}>Open</button>
                </div>
              </div>
            ))}
          </div>
          <div className="card">
            <h2>Ledger</h2>
            {state ? (
              <div className="stats">
                <div className="stat"><span className="v">{state.ledger.handled}</span><span className="l">items handled</span></div>
                <div className="stat"><span className="v">{state.ledger.executed}</span><span className="l">actions executed</span></div>
                <div className="stat"><span className="v">{state.ledger.awaiting_approval}</span><span className="l">waiting for you</span></div>
                <div className="stat"><span className="v">{state.ledger.escalated}</span><span className="l">escalated with a reason</span></div>
                <div className="stat"><span className="v">{Math.round(state.ledger.estimated_minutes / 60 * 10) / 10}h</span><span className="l">estimated time, configurable</span></div>
                <div className="stat"><span className="v">${state.ledger.cost_usd.toFixed(2)}</span><span className="l">model spend · {state.ledger.tokens.toLocaleString()} tokens</span></div>
              </div>
            ) : null}
          </div>
          <div className="card">
            <h2>Ask the Analyst</h2>
            <Ask onAsk={async (q) => { const r = await api("/api/ask", { method: "POST", body: JSON.stringify({ question: q }) }); setSelected(r.item_id); refresh(); }} />
            <div className="note" style={{ marginTop: 8 }}>Read-only SQL over the operational store. The query is shown with the answer.</div>
          </div>
          <div className="card">
            <h2>Memory · {state?.memories.length ?? 0}</h2>
            {(state?.memories ?? []).slice(-4).reverse().map((m) => (
              <div key={m.id} style={{ fontSize: 12.5, marginBottom: 8 }}>
                <div className="ink2">{m.summary}</div>
                <div className="dim">{m.decision}</div>
              </div>
            ))}
            {!state?.memories.length ? <div className="note">Written only from executed actions and your decisions.</div> : null}
          </div>
        </aside>
      </main>
    </>
  );
}

function modelLabel(s: Settings) {
  if (s.provider === "anthropic") return `Anthropic · ${s.model === "auto" ? "Opus 5 + Sonnet 5" : s.model}`;
  if (s.provider === "openai") return `${s.model} @ ${new URL(s.base_url).host}`;
  return "Mock · no key";
}

function StatusBadge({ status }: { status: string }) {
  const cls = status === "closed" || status === "done" ? "ok" : status === "awaiting_approval" ? "warn" : status === "escalated" || status === "failed" ? "bad" : status === "open" || status === "working" ? "accent" : "";
  const text = { closed: "done", done: "done", awaiting_approval: "waiting for you", escalated: "escalated", open: "working", working: "working", queued: "queued", failed: "failed" }[status] ?? status;
  return <span className={`badge ${cls}`}>{status === "open" || status === "working" ? <span className="dot pulse" /> : null}{text}</span>;
}

function label(t: string) {
  return { queue_payment: "Queue payment", hold_invoice: "Hold invoice", supplier_query: "Query the supplier", customer_reply: "Reply to customer", schedule_reminder: "Schedule reminder", escalate: "Escalation" }[t] ?? t;
}

function summary(a: Action) {
  const p = a.payload ?? {};
  switch (a.type) {
    case "queue_payment": return `${p.supplier} · invoice ${p.invoice_number} against ${p.po_id}`;
    case "hold_invoice": return `${p.supplier} · ${p.invoice_number}: ${p.reason}`;
    case "supplier_query": return `To ${p.to}: “${p.subject}”`;
    case "customer_reply": return `To ${p.to}: “${p.subject}”`;
    case "schedule_reminder": return `${p.customer} · ${p.invoice_number} · stage ${p.stage} · ${p.overdue_days} days overdue${p.sensitive ? " · sensitive account" : ""}`;
    case "escalate": return p.reason;
    default: return JSON.stringify(p).slice(0, 120);
  }
}

function Transcript({ events }: { events: Ev[] }) {
  if (!events.length) return <div className="note">The team's turns appear here as they happen.</div>;
  return (
    <ul className="trace">
      {events.map((e) => (
        <li key={e.id} data-kind={e.kind}><span className="who">{e.agent}</span><span>{e.message}</span></li>
      ))}
    </ul>
  );
}

function Detail({ item, c, actions, events, onDecide }: { item: Item; c?: Case; actions: Action[]; events: Ev[]; onDecide: (id: string, d: "approve" | "reject") => void }) {
  const r = c?.result ?? {};
  const ex = r.extraction;
  const quotes: string[] = ex ? [ex.supplier_name?.quote, ex.invoice_number?.quote, ex.po_number?.quote, ex.invoice_date?.quote, ex.total?.quote, ...(ex.lines ?? []).map((l: any) => l.quote)].filter(Boolean) : [];
  return (
    <>
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
          <div>
            <div className="mono dim">{KIND[item.kind] ?? item.kind} · {item.sender}</div>
            <h3 className="serif" style={{ fontSize: 24, margin: "4px 0 6px" }}>{item.subject}</h3>
          </div>
          <StatusBadge status={c?.status ?? item.status} />
        </div>
        <div className="draft" style={{ marginTop: 8 }}>{item.body}</div>
        {c?.owner_agent ? (
          <dl className="kv" style={{ marginTop: 12 }}>
            <dt>Dispatcher</dt><dd><strong>{AGENT[c.owner_agent] ?? c.owner_agent}</strong> · {Math.round((c.confidence ?? 0) * 100)}% · {c.reason}</dd>
            <dt>Context pack</dt><dd className="ink2">{c.pack ? `${c.pack.evidence?.knowledge?.length ?? 0} knowledge chunks · ${Object.keys(c.pack.evidence?.records ?? {}).length} record sets · ${c.pack.memory?.length ?? 0} memories · policy: ${String(c.pack.policy).split("\n")[0]}` : "—"}</dd>
            <dt>Cost</dt><dd className="ink2">${(c.cost_usd ?? 0).toFixed(4)} · {(c.tokens_in ?? 0) + (c.tokens_out ?? 0)} tokens</dd>
          </dl>
        ) : null}
      </div>

      {item.document ? (
        <div className="card">
          <h2>Document{ex ? " · every value traces to a highlighted quote" : ""}</h2>
          <Highlighted text={item.document} quotes={quotes} />
          {ex ? (
            <dl className="kv" style={{ marginTop: 12 }}>
              {(["supplier_name", "invoice_number", "po_number", "invoice_date", "total"] as const).map((k) => (
                <FieldRow key={k} name={k} f={ex[k]} />
              ))}
              <dt>Lines</dt><dd>{ex.lines.map((l: any, i: number) => <div key={i}>{l.description} · {l.quantity} × {l.unit_price} = {l.amount}</div>)}</dd>
              {r.quote_failures?.length ? <><dt>Quote check</dt><dd style={{ color: "var(--bad)" }}>failed for {r.quote_failures.join(", ")}</dd></> : <><dt>Quote check</dt><dd style={{ color: "var(--ok)" }}>every quote found verbatim in the document</dd></>}
            </dl>
          ) : null}
        </div>
      ) : null}

      {r.match ? (
        <div className="card">
          <h2>Three-way match · {r.match.verdict}</h2>
          <div className="ink2">{r.narrative?.explanation}</div>
          {r.match.differences?.length ? <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 13 }}>{r.match.differences.map((d: string) => <li key={d}>{d}</li>)}</ul> : null}
          <dl className="kv" style={{ marginTop: 10 }}>
            <dt>Purchase order</dt><dd>{r.match.po_id}</dd>
            <dt>Goods receipt</dt><dd>{r.match.receipt ?? "none"}</dd>
            <dt>Supplier</dt><dd>{r.match.supplier} · {r.match.supplier_class}</dd>
          </dl>
        </div>
      ) : null}

      {r.reply ? (
        <div className="card">
          <h2>Customer agent · {r.reply.intent} · {r.reply.sentiment}{r.reply.injection_suspected ? " · injection suspected" : ""}</h2>
          {r.reply.escalate ? <div className="ink2"><strong>Escalated:</strong> {r.reply.escalation_reason}</div> : <div className="draft"><strong>{r.reply.subject}</strong>{"\n\n"}{r.reply.body}</div>}
          {r.reply.citations?.length ? <div className="note" style={{ marginTop: 8 }}>Citations: {r.reply.citations.join(", ")}{r.dangling_citations?.length ? ` · not in evidence: ${r.dangling_citations.join(", ")}` : ""}</div> : null}
        </div>
      ) : null}

      {r.plan ? (
        <div className="card">
          <h2>Follow-up plan</h2>
          <table className="rows"><thead><tr><th>Invoice</th><th>Overdue</th><th>Stage</th><th>Outcome</th></tr></thead>
            <tbody>{r.plan.map((p: any) => <tr key={p.invoice}><td>{p.invoice}</td><td>{p.overdue_days} d</td><td>{p.stage}</td><td>{p.skip ?? `reminder to ${p.customer}${p.sensitive ? " (sensitive)" : ""}`}</td></tr>)}</tbody></table>
        </div>
      ) : null}

      {r.sql ? (
        <div className="card">
          <h2>Analyst</h2>
          <div className="ink2" style={{ marginBottom: 8 }}>{r.answer}</div>
          <pre className="doc">{r.sql}</pre>
          {r.rows?.length ? (
            <table className="rows" style={{ marginTop: 8 }}>
              <thead><tr>{Object.keys(r.rows[0]).map((k) => <th key={k}>{k}</th>)}</tr></thead>
              <tbody>{r.rows.map((row: any, i: number) => <tr key={i}>{Object.values(row).map((v: any, j) => <td key={j}>{String(v)}</td>)}</tr>)}</tbody>
            </table>
          ) : null}
        </div>
      ) : null}

      {actions.length ? (
        <div className="card">
          <h2>Actions · Reviewer, then policy</h2>
          <div style={{ display: "grid", gap: 8 }}>
            {actions.map((a) => (
              <div key={a.id} className="action">
                <div className="head">
                  <strong>{label(a.type)}</strong>
                  <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {a.reviewer_verdict ? <span className={`badge ${a.reviewer_verdict.verdict === "pass" ? "ok" : a.reviewer_verdict.verdict === "block" ? "bad" : "warn"}`}>reviewer: {a.reviewer_verdict.verdict}</span> : null}
                    {a.policy_decision ? <span className={`badge ${a.policy_decision === "approve" ? "warn" : a.policy_decision === "blocked" ? "bad" : "blue"}`}>policy: {a.policy_decision}</span> : null}
                    <StatusBadge status={a.status} />
                  </span>
                </div>
                <div className="ink2" style={{ fontSize: 12.5 }}>{summary(a)}</div>
                {a.reviewer_verdict?.reason ? <div className="note">Reviewer: {a.reviewer_verdict.reason}{a.reviewer_verdict.injection_detected ? " · instructions in the email were ignored" : ""}</div> : null}
                {a.payload?.body ? <div className="draft" style={{ fontSize: 12.5 }}>{a.payload.body}</div> : null}
                {a.result?.tool ? <div className="note">Executed via {a.result.tool}</div> : null}
                {a.status === "awaiting_approval" ? (
                  <div style={{ display: "flex", gap: 6 }}>
                    <button className="btn ok sm" onClick={() => onDecide(a.id, "approve")}>Approve</button>
                    <button className="btn bad sm" onClick={() => onDecide(a.id, "reject")}>Reject</button>
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="card">
        <h2>Trace</h2>
        <Transcript events={events} />
      </div>
    </>
  );
}

function FieldRow({ name, f }: { name: string; f: any }) {
  return (
    <>
      <dt>{name.replace("_", " ")}</dt>
      <dd>{f?.value || <span className="dim">missing</span>}{f?.quote ? <span className="quote">“{f.quote}”</span> : null}</dd>
    </>
  );
}

function Highlighted({ text, quotes }: { text: string; quotes: string[] }) {
  const parts = useMemo(() => {
    if (!quotes.length) return [{ t: text, m: false }];
    const spans: Array<[number, number]> = [];
    for (const q of quotes) {
      const i = text.indexOf(q);
      if (i >= 0) spans.push([i, i + q.length]);
    }
    spans.sort((a, b) => a[0] - b[0]);
    const out: { t: string; m: boolean }[] = [];
    let pos = 0;
    for (const [s, e] of spans) {
      if (s < pos) continue;
      if (s > pos) out.push({ t: text.slice(pos, s), m: false });
      out.push({ t: text.slice(s, e), m: true });
      pos = e;
    }
    if (pos < text.length) out.push({ t: text.slice(pos), m: false });
    return out;
  }, [text, quotes]);
  return <pre className="doc">{parts.map((p, i) => (p.m ? <mark key={i}>{p.t}</mark> : <span key={i}>{p.t}</span>))}</pre>;
}

function Ask({ onAsk }: { onAsk: (q: string) => Promise<void> }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form className="ask" onSubmit={async (e) => { e.preventDefault(); if (!q.trim()) return; setBusy(true); try { await onAsk(q.trim()); setQ(""); } finally { setBusy(false); } }}>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Which customers are overdue right now?" />
      <button className="btn primary sm" disabled={busy}>Ask</button>
    </form>
  );
}

function SettingsPanel({ settings, onChange, onClose }: { settings: Settings; onChange: (s: Settings) => void; onClose: () => void }) {
  const [provider, setProvider] = useState(settings.provider);
  const [model, setModel] = useState(settings.model);
  const [baseUrl, setBaseUrl] = useState(settings.base_url);
  const [key, setKey] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const s = await api("/api/settings", { method: "POST", body: JSON.stringify({ provider, model: provider === "anthropic" && !settings.anthropic_models.includes(model) ? "auto" : model, base_url: baseUrl, api_key: key || undefined }) });
      onChange(s);
      setStatus("Saved. Checking…");
      const c = await api("/api/settings/check", { method: "POST" });
      setStatus((c.ok ? "✓ " : "✗ ") + c.detail);
    } catch (e: any) {
      setStatus("✗ " + String(e.message ?? e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card" style={{ margin: "16px 20px 0", display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "0 20px" }}>
      <div style={{ gridColumn: "1 / -1", display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h2>Model · bring your own key</h2>
        <button className="btn sm" onClick={onClose}>Close</button>
      </div>
      <div className="field">
        <label>Provider</label>
        <select value={provider} onChange={(e) => { setProvider(e.target.value); setModel(e.target.value === "anthropic" ? "auto" : e.target.value === "openai" ? "gpt-4o" : "mock"); }}>
          <option value="mock">Mock · deterministic, no key</option>
          <option value="anthropic">Anthropic · Claude</option>
          <option value="openai">OpenAI-compatible · OpenAI, vLLM, Ollama, …</option>
        </select>
      </div>
      {provider === "anthropic" ? (
        <div className="field">
          <label>Model</label>
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            {settings.anthropic_models.map((m) => <option key={m} value={m}>{m === "auto" ? "auto · Opus 5 for judgment, Sonnet 5 for volume" : m}</option>)}
          </select>
        </div>
      ) : null}
      {provider === "openai" ? (
        <>
          <div className="field"><label>Model</label><input value={model} onChange={(e) => setModel(e.target.value)} placeholder="gpt-4o, llama-3.3-70b, …" /></div>
          <div className="field"><label>Base URL</label><input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.openai.com/v1 or http://localhost:8000/v1" /></div>
        </>
      ) : null}
      {provider !== "mock" ? (
        <div className="field">
          <label>API key {settings.has_key ? `(current: ${settings.key_hint})` : ""}</label>
          <input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={settings.has_key ? "leave blank to keep the current key" : "paste your key"} />
        </div>
      ) : null}
      <div style={{ gridColumn: "1 / -1", display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn primary sm" onClick={save} disabled={busy}>Save and test</button>
        <span className="note">{status ?? "The key stays in the local server's memory for this session. It is never written to disk or sent anywhere but the provider you chose."}</span>
      </div>
    </div>
  );
}
