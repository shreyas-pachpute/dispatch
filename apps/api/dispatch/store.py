"""Items, cases, actions, approvals, memory, events, the business records: all in one database (see db.py)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .db import DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, kind TEXT, source TEXT, sender TEXT, subject TEXT, body TEXT, document TEXT, received_at REAL, status TEXT DEFAULT 'queued');
CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, item_id TEXT, owner_agent TEXT, status TEXT, intent TEXT, confidence REAL, reason TEXT, pack TEXT, result TEXT, reviewer TEXT, cost_usd REAL DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, opened_at REAL, closed_at REAL);
CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, case_id TEXT, type TEXT, payload TEXT, amount REAL, counterparty TEXT, policy_decision TEXT, status TEXT, reviewer_verdict TEXT, created_at REAL, executed_at REAL, result TEXT);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, action_id TEXT, decision TEXT, note TEXT, decided_at REAL);
CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, case_id TEXT, kind TEXT, summary TEXT, decision TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS events (id {SERIAL}, ts REAL, case_id TEXT, agent TEXT, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS calls (id {SERIAL}, case_id TEXT, agent TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cached INTEGER, cost_usd REAL, ms INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, name TEXT, email TEXT, segment TEXT, sensitive INTEGER);
CREATE TABLE IF NOT EXISTS suppliers (id TEXT PRIMARY KEY, name TEXT, email TEXT, class TEXT, variance_tolerance REAL);
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, customer_id TEXT, placed TEXT, items TEXT, total REAL, status TEXT);
CREATE TABLE IF NOT EXISTS shipments (order_id TEXT PRIMARY KEY, carrier TEXT, tracking TEXT, shipped TEXT, eta TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders (id TEXT PRIMARY KEY, supplier_id TEXT, issued TEXT, lines TEXT, total REAL);
CREATE TABLE IF NOT EXISTS receipts (po_id TEXT PRIMARY KEY, received TEXT, lines TEXT);
CREATE TABLE IF NOT EXISTS invoices_out (id TEXT PRIMARY KEY, customer_id TEXT, issued TEXT, due TEXT, amount REAL, status TEXT, paid TEXT);
CREATE TABLE IF NOT EXISTS reminders_sent (invoice_id TEXT, stage INTEGER, sent TEXT);
"""

RECORD_TABLES = ["customers", "suppliers", "orders", "shipments", "purchase_orders", "receipts", "invoices_out", "reminders_sent"]
ALL_TABLES = ["items", "cases", "actions", "approvals", "memories", "events", "calls", *RECORD_TABLES]


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _parse(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    for k in keys:
        if row.get(k):
            try:
                row[k] = json.loads(row[k])
            except (TypeError, ValueError):
                pass
    return row


class Store:
    def __init__(self, db: DB):
        self.db = db
        db.script(SCHEMA)

    # ---- passthrough
    def q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return self.db.q(sql, args)

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        return self.db.one(sql, args)

    def x(self, sql: str, args: tuple = ()) -> None:
        self.db.x(sql, args)

    # ---- dataset
    def reset(self, data_dir: Path) -> None:
        for t in ALL_TABLES:
            self.x(f"DELETE FROM {t}")
        records = json.loads((data_dir / "records.json").read_text(encoding="utf8"))
        for table, rows in records.items():
            for r in rows:
                cols = list(r.keys())
                vals = [_j(v) if isinstance(v, (list, dict)) else (int(v) if isinstance(v, bool) else v) for v in r.values()]
                self.x(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})", tuple(vals))

    def enqueue_items(self, items: list[dict[str, Any]]) -> None:
        now = time.time()
        for i, it in enumerate(items):
            self.x(
                "INSERT INTO items (id, kind, source, sender, subject, body, document, received_at, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'queued') "
                "ON CONFLICT (id) DO NOTHING",
                (it["id"], it["kind"], it["source"], it["from"], it["subject"], it["body"], it.get("document"), now + i * 0.01),
            )

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically take one queued item. Safe with concurrent workers on Postgres."""
        rows = self.q(
            "UPDATE items SET status='working' WHERE id = (SELECT id FROM items WHERE status='queued' ORDER BY received_at LIMIT 1" + self.db.lock_hint() + ") RETURNING *"
        )
        return rows[0] if rows else None

    def queued_count(self) -> int:
        return (self.one("SELECT COUNT(*) AS n FROM items WHERE status='queued'") or {}).get("n", 0)

    def working_count(self) -> int:
        return (self.one("SELECT COUNT(*) AS n FROM items WHERE status='working'") or {}).get("n", 0)

    # ---- events (the transcript the control room polls)
    def event(self, case_id: str | None, agent: str, kind: str, message: str) -> None:
        self.x("INSERT INTO events (ts, case_id, agent, kind, message) VALUES (%s,%s,%s,%s,%s)", (time.time(), case_id, agent, kind, message))

    def events_since(self, last_id: int, limit: int = 300) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM events WHERE id > %s ORDER BY id LIMIT %s", (last_id, limit))

    # ---- cases
    def open_case(self, case_id: str, item_id: str) -> None:
        self.x("INSERT INTO cases (id, item_id, status, opened_at) VALUES (%s,%s,'open',%s) ON CONFLICT (id) DO NOTHING", (case_id, item_id, time.time()))

    def update_case(self, case_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=%s" for k in fields)
        vals = [_j(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.x(f"UPDATE cases SET {sets} WHERE id=%s", (*vals, case_id))

    def close_case(self, case_id: str, status: str) -> None:
        c = self.one("SELECT item_id FROM cases WHERE id=%s", (case_id,))
        self.x("UPDATE cases SET status=%s, closed_at=%s WHERE id=%s", (status, time.time(), case_id))
        if c:
            self.x("UPDATE items SET status=%s WHERE id=%s", ("escalated" if status == "escalated" else "done", c["item_id"]))

    def record_call(self, case_id: str, agent: str, model: str, tokens_in: int, tokens_out: int, cached: int, cost: float, ms: int) -> None:
        self.x(
            "INSERT INTO calls (case_id, agent, model, tokens_in, tokens_out, cached, cost_usd, ms, ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (case_id, agent, model, tokens_in, tokens_out, cached, cost, ms, time.time()),
        )
        self.x("UPDATE cases SET cost_usd = cost_usd + %s, tokens_in = tokens_in + %s, tokens_out = tokens_out + %s WHERE id=%s", (cost, tokens_in, tokens_out, case_id))

    # ---- actions
    def add_action(self, action_id: str, case_id: str, type_: str, payload: dict[str, Any], amount: float | None, counterparty: str | None) -> None:
        self.x(
            "INSERT INTO actions (id, case_id, type, payload, amount, counterparty, status, created_at) VALUES (%s,%s,%s,%s,%s,%s,'proposed',%s)",
            (action_id, case_id, type_, _j(payload), amount, counterparty, time.time()),
        )

    def set_action(self, action_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=%s" for k in fields)
        vals = [_j(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.x(f"UPDATE actions SET {sets} WHERE id=%s", (*vals, action_id))

    # ---- memory
    def remember(self, mem_id: str, case_id: str, kind: str, summary: str, decision: str) -> None:
        self.x(
            "INSERT INTO memories (id, case_id, kind, summary, decision, created_at) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET summary=excluded.summary, decision=excluded.decision, created_at=excluded.created_at",
            (mem_id, case_id, kind, summary, decision, time.time()),
        )

    # ---- state snapshot for the control room
    def snapshot(self) -> dict[str, Any]:
        items = self.q("SELECT * FROM items ORDER BY received_at")
        cases = [_parse(c, "pack", "result", "reviewer") for c in self.q("SELECT * FROM cases ORDER BY opened_at")]
        actions = [_parse(a, "payload", "reviewer_verdict", "result") for a in self.q("SELECT * FROM actions ORDER BY created_at")]
        approvals = self.q("SELECT * FROM approvals ORDER BY decided_at")
        memories = self.q("SELECT * FROM memories ORDER BY created_at")
        calls = self.q("SELECT agent, model, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, SUM(cached) AS cached, SUM(cost_usd) AS cost_usd, COUNT(*) AS n FROM calls GROUP BY agent, model")
        return {"items": items, "cases": cases, "actions": actions, "approvals": approvals, "memories": memories, "calls": calls}
