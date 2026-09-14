"""One SQLite file for everything: items, cases, actions, approvals, memory, events, and the business records."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, kind TEXT, source TEXT, sender TEXT, subject TEXT, body TEXT, document TEXT, received_at REAL, status TEXT DEFAULT 'queued');
CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, item_id TEXT, owner_agent TEXT, status TEXT, intent TEXT, confidence REAL, reason TEXT, pack TEXT, result TEXT, reviewer TEXT, cost_usd REAL DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, opened_at REAL, closed_at REAL);
CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, case_id TEXT, type TEXT, payload TEXT, amount REAL, counterparty TEXT, policy_decision TEXT, status TEXT, reviewer_verdict TEXT, created_at REAL, executed_at REAL, result TEXT);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, action_id TEXT, decision TEXT, note TEXT, decided_at REAL);
CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, case_id TEXT, kind TEXT, summary TEXT, decision TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, case_id TEXT, agent TEXT, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, agent TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cached INTEGER, cost_usd REAL, ms INTEGER, ts REAL);
"""

RECORD_TABLES = {
    "customers": "CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, name TEXT, email TEXT, segment TEXT, sensitive INTEGER)",
    "suppliers": "CREATE TABLE IF NOT EXISTS suppliers (id TEXT PRIMARY KEY, name TEXT, email TEXT, class TEXT, variance_tolerance REAL)",
    "orders": "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, customer_id TEXT, placed TEXT, items TEXT, total REAL, status TEXT)",
    "shipments": "CREATE TABLE IF NOT EXISTS shipments (order_id TEXT PRIMARY KEY, carrier TEXT, tracking TEXT, shipped TEXT, eta TEXT, status TEXT)",
    "purchase_orders": "CREATE TABLE IF NOT EXISTS purchase_orders (id TEXT PRIMARY KEY, supplier_id TEXT, issued TEXT, lines TEXT, total REAL)",
    "receipts": "CREATE TABLE IF NOT EXISTS receipts (po_id TEXT PRIMARY KEY, received TEXT, lines TEXT)",
    "invoices_out": "CREATE TABLE IF NOT EXISTS invoices_out (id TEXT PRIMARY KEY, customer_id TEXT, issued TEXT, due TEXT, amount REAL, status TEXT, paid TEXT)",
    "reminders_sent": "CREATE TABLE IF NOT EXISTS reminders_sent (invoice_id TEXT, stage INTEGER, sent TEXT)",
}


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            for ddl in RECORD_TABLES.values():
                self.conn.execute(ddl)
            self.conn.commit()

    # ---- generic helpers
    def q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, args)
            self.conn.commit()

    # ---- dataset
    def reset(self, data_dir: Path) -> None:
        with self._lock:
            for t in ["items", "cases", "actions", "approvals", "memories", "events", "calls", *RECORD_TABLES]:
                self.conn.execute(f"DELETE FROM {t}")
            records = json.loads((data_dir / "records.json").read_text(encoding="utf8"))
            for table, rows in records.items():
                for r in rows:
                    cols = list(r.keys())
                    vals = [_j(v) if isinstance(v, (list, dict)) else (int(v) if isinstance(v, bool) else v) for v in r.values()]
                    self.conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            self.conn.commit()

    def enqueue_items(self, items: list[dict[str, Any]]) -> None:
        now = time.time()
        for i, it in enumerate(items):
            self.x(
                "INSERT OR REPLACE INTO items (id, kind, source, sender, subject, body, document, received_at, status) VALUES (?,?,?,?,?,?,?,?,'queued')",
                (it["id"], it["kind"], it["source"], it["from"], it["subject"], it["body"], it.get("document"), now + i * 0.01),
            )

    # ---- events (the transcript the control room streams)
    def event(self, case_id: str | None, agent: str, kind: str, message: str) -> None:
        self.x("INSERT INTO events (ts, case_id, agent, kind, message) VALUES (?,?,?,?,?)", (time.time(), case_id, agent, kind, message))

    def events_since(self, last_id: int) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM events WHERE id > ? ORDER BY id", (last_id,))

    # ---- cases
    def open_case(self, case_id: str, item_id: str) -> None:
        self.x("INSERT OR REPLACE INTO cases (id, item_id, status, opened_at) VALUES (?,?,'open',?)", (case_id, item_id, time.time()))
        self.x("UPDATE items SET status='working' WHERE id=?", (item_id,))

    def update_case(self, case_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [_j(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.x(f"UPDATE cases SET {sets} WHERE id=?", (*vals, case_id))

    def close_case(self, case_id: str, status: str) -> None:
        c = self.one("SELECT item_id FROM cases WHERE id=?", (case_id,))
        self.x("UPDATE cases SET status=?, closed_at=? WHERE id=?", (status, time.time(), case_id))
        if c:
            self.x("UPDATE items SET status=? WHERE id=?", ("done" if status != "escalated" else "escalated", c["item_id"]))

    def record_call(self, case_id: str, agent: str, model: str, tokens_in: int, tokens_out: int, cached: int, cost: float, ms: int) -> None:
        self.x(
            "INSERT INTO calls (case_id, agent, model, tokens_in, tokens_out, cached, cost_usd, ms, ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (case_id, agent, model, tokens_in, tokens_out, cached, cost, ms, time.time()),
        )
        self.x(
            "UPDATE cases SET cost_usd = cost_usd + ?, tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE id=?",
            (cost, tokens_in, tokens_out, case_id),
        )

    # ---- actions
    def add_action(self, action_id: str, case_id: str, type_: str, payload: dict[str, Any], amount: float | None, counterparty: str | None) -> None:
        self.x(
            "INSERT INTO actions (id, case_id, type, payload, amount, counterparty, status, created_at) VALUES (?,?,?,?,?,?,'proposed',?)",
            (action_id, case_id, type_, _j(payload), amount, counterparty, time.time()),
        )

    def set_action(self, action_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [_j(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.x(f"UPDATE actions SET {sets} WHERE id=?", (*vals, action_id))

    # ---- memory
    def remember(self, mem_id: str, case_id: str, kind: str, summary: str, decision: str) -> None:
        self.x("INSERT OR REPLACE INTO memories (id, case_id, kind, summary, decision, created_at) VALUES (?,?,?,?,?,?)", (mem_id, case_id, kind, summary, decision, time.time()))

    # ---- state snapshot for the control room
    def snapshot(self) -> dict[str, Any]:
        def parse(row: dict[str, Any], *keys: str) -> dict[str, Any]:
            for k in keys:
                if row.get(k):
                    try:
                        row[k] = json.loads(row[k])
                    except (TypeError, ValueError):
                        pass
            return row

        items = self.q("SELECT * FROM items ORDER BY received_at")
        cases = [parse(c, "pack", "result", "reviewer") for c in self.q("SELECT * FROM cases ORDER BY opened_at")]
        actions = [parse(a, "payload", "reviewer_verdict", "result") for a in self.q("SELECT * FROM actions ORDER BY created_at")]
        approvals = self.q("SELECT * FROM approvals ORDER BY decided_at")
        memories = self.q("SELECT * FROM memories ORDER BY created_at")
        calls = self.q("SELECT agent, model, SUM(tokens_in) tokens_in, SUM(tokens_out) tokens_out, SUM(cached) cached, SUM(cost_usd) cost_usd, COUNT(*) n FROM calls GROUP BY agent, model")
        return {"items": items, "cases": cases, "actions": actions, "approvals": approvals, "memories": memories, "calls": calls}
