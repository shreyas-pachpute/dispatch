"""The API the control room talks to. Stateless per request so it runs as a serverless function:
the database holds the queue, the cases and the transcript; the UI drives the worker with `tick` calls
and polls `events` and `state`. Model settings arrive as request headers and are never stored."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import agents
from .agents import Runtime
from .db import connect
from .gateway import ANTHROPIC_MODELS, Gateway, Settings
from .graph import build_graph, run_item
from .knowledge import Knowledge
from .packs import PackBuilder
from .policy import Policy
from .store import Store

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(os.environ.get("DISPATCH_DATA", ROOT / "data" / "northwind"))

store = Store(connect(ROOT / "data" / "runtime" / "dispatch.sqlite", "dispatch"))
knowledge = Knowledge(DATA / "knowledge.json")
policy = Policy(DATA / "policy.yaml")

app = FastAPI(title="Dispatch API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Vercel functions run at most 300s; leave headroom for the response
TICK_BUDGET_S = float(os.environ.get("DISPATCH_TICK_BUDGET", "240"))


def runtime(request: Request) -> Runtime:
    gw = Gateway(store, Settings.from_headers(request.headers))
    return Runtime(store, gw, PackBuilder(store, knowledge, policy), policy)


class Decision(BaseModel):
    decision: str  # approve | reject
    note: str = ""


class Question(BaseModel):
    question: str


# ------------------------------------------------------------------ routes

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "db": store.db.label, "queued": store.queued_count(), "working": store.working_count()}


@app.get("/api/settings")
def get_settings(request: Request) -> dict[str, Any]:
    s = Settings.from_headers(request.headers)
    return {"provider": s.provider, "model": s.model, "base_url": s.base_url, "has_key": bool(s.api_key), "label": s.label, "anthropic_models": ANTHROPIC_MODELS, "server_default": Settings.from_env().label}


@app.post("/api/settings/check")
async def check_settings(request: Request) -> dict[str, Any]:
    return await Gateway(store, Settings.from_headers(request.headers)).check()


@app.get("/api/state")
def state() -> dict[str, Any]:
    snap = store.snapshot()
    snap["worker"] = {"queued": store.queued_count(), "working": store.working_count()}
    snap["db"] = store.db.label
    snap["ledger"] = ledger(snap)
    snap["last_event_id"] = (store.one("SELECT MAX(id) AS m FROM events") or {}).get("m") or 0
    return snap


def ledger(snap: dict[str, Any]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    minutes = 0
    for it in snap["items"]:
        if it["status"] in ("done", "escalated"):
            by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
            minutes += policy.time_estimates.get(it["kind"], 10)
    cost = sum(float(c["cost_usd"] or 0) for c in snap["calls"])
    tokens = sum(int(c["tokens_in"] or 0) + int(c["tokens_out"] or 0) for c in snap["calls"])
    return {
        "handled": sum(by_kind.values()),
        "by_kind": by_kind,
        "awaiting_approval": sum(1 for a in snap["actions"] if a["status"] == "awaiting_approval"),
        "executed": sum(1 for a in snap["actions"] if a["status"] == "executed"),
        "blocked": sum(1 for a in snap["actions"] if a["status"] == "blocked"),
        "escalated": sum(1 for c in snap["cases"] if c["status"] == "escalated"),
        "estimated_minutes": minutes,
        "cost_usd": round(cost, 4),
        "tokens": tokens,
        "time_estimates": policy.time_estimates,
    }


@app.get("/api/events")
def events(after: int = 0) -> dict[str, Any]:
    rows = store.events_since(after)
    return {"events": rows, "last_id": rows[-1]["id"] if rows else after}


@app.post("/api/demo/reset")
def demo_reset() -> dict[str, Any]:
    store.reset(DATA)
    store.event(None, "system", "system", "Northwind dataset loaded: 5 customers, 3 suppliers, 4 orders, 3 purchase orders, 6 invoices")
    return {"ok": True}


@app.post("/api/demo/run")
def demo_run(request: Request) -> dict[str, Any]:
    """Queue the overnight inbox. The UI then calls /api/worker/tick until the queue is drained."""
    items = json.loads((DATA / "items.json").read_text(encoding="utf8"))
    store.enqueue_items(items)
    store.event(None, "system", "system", f"{len(items)} overnight items queued · model: {Settings.from_headers(request.headers).label}")
    return {"ok": True, "queued": store.queued_count()}


@app.post("/api/worker/tick")
async def worker_tick(request: Request) -> dict[str, Any]:
    """Process queued items until the queue is empty or the time budget is spent. Idempotent and safe to call repeatedly."""
    rt = runtime(request)
    graph = build_graph(rt)
    t0 = time.time()
    done = 0
    while time.time() - t0 < TICK_BUDGET_S:
        item = store.claim_next()
        if not item:
            break
        await run_item(rt, graph, {**item, "from": item["sender"]})
        done += 1
        if rt.gw.settings.provider != "mock":
            break  # one real-model item per request keeps well inside the function limit
    remaining = store.queued_count()
    if remaining == 0 and done:
        store.event(None, "system", "system", "Queue drained")
    return {"ok": True, "processed": done, "remaining": remaining}


@app.post("/api/actions/{action_id}/decide")
def decide(action_id: str, d: Decision, request: Request) -> dict[str, Any]:
    a = store.one("SELECT * FROM actions WHERE id=%s", (action_id,))
    if not a:
        raise HTTPException(404, "no such action")
    if a["status"] != "awaiting_approval":
        raise HTTPException(409, f"action is {a['status']}")
    payload = json.loads(a["payload"])
    rt = runtime(request)
    store.x("INSERT INTO approvals (id, action_id, decision, note, decided_at) VALUES (%s,%s,%s,%s,%s)", (f"apr-{uuid.uuid4().hex[:8]}", action_id, d.decision, d.note, time.time()))
    if d.decision == "approve":
        agents.execute(rt, a, payload)
        store.set_action(action_id, status="executed")
        store.event(a["case_id"], "you", "human", f"Approved {a['type']}" + (f": {d.note}" if d.note else ""))
    else:
        store.set_action(action_id, status="rejected")
        store.event(a["case_id"], "you", "human", f"Rejected {a['type']}" + (f": {d.note}" if d.note else ""))
    case = store.one("SELECT * FROM cases WHERE id=%s", (a["case_id"],)) or {}
    item = store.one("SELECT * FROM items WHERE id=%s", (case.get("item_id"),)) or {}
    store.remember(f"mem-{action_id}", a["case_id"], item.get("kind", "unknown"), f"{item.get('kind')} “{item.get('subject')}”: {a['type']} for {a['counterparty']} ({a['amount']})", f"{d.decision} by owner" + (f": {d.note}" if d.note else ""))
    if not store.q("SELECT 1 FROM actions WHERE case_id=%s AND status='awaiting_approval'", (a["case_id"],)):
        store.close_case(a["case_id"], "closed")
    return {"ok": True}


@app.post("/api/ask")
def ask(q: Question) -> dict[str, Any]:
    item = {"id": f"q-{uuid.uuid4().hex[:6]}", "kind": "question", "source": "control-room", "from": "owner@northwind.example", "subject": q.question[:80], "body": q.question}
    store.enqueue_items([item])
    return {"ok": True, "item_id": item["id"]}


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str) -> dict[str, Any]:
    snap = store.snapshot()
    c = next((c for c in snap["cases"] if c["id"] == case_id), None)
    if not c:
        raise HTTPException(404)
    return {"case": c, "actions": [a for a in snap["actions"] if a["case_id"] == case_id], "events": store.q("SELECT * FROM events WHERE case_id=%s ORDER BY id", (case_id,))}


def run() -> None:
    import uvicorn

    uvicorn.run("dispatch.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8787")), reload=False)


if __name__ == "__main__":
    run()
