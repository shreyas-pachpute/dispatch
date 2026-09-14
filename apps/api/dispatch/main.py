"""The API the control room talks to. One process: HTTP, the queue worker, and server-sent events."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import agents
from .agents import Runtime
from .gateway import Gateway, Settings
from .graph import build_graph, run_item
from .knowledge import Knowledge
from .packs import PackBuilder
from .policy import Policy
from .store import Store

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(os.environ.get("DISPATCH_DATA", ROOT / "data" / "northwind"))
DB = Path(os.environ.get("DISPATCH_DB", ROOT / "data" / "runtime" / "dispatch.sqlite"))
DB.parent.mkdir(parents=True, exist_ok=True)

store = Store(DB)
knowledge = Knowledge(DATA / "knowledge.json")
policy = Policy(DATA / "policy.yaml")
gateway = Gateway(store)
packs = PackBuilder(store, knowledge, policy)
rt = Runtime(store, gateway, packs, policy)
graph = build_graph(rt)

app = FastAPI(title="Dispatch API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

worker_state: dict[str, Any] = {"running": False, "task": None, "started_at": None}


# ------------------------------------------------------------------ worker

async def worker_loop() -> None:
    worker_state["running"] = True
    worker_state["started_at"] = time.time()
    store.event(None, "system", "system", f"Worker started · model: {gateway.settings.label}")
    try:
        while True:
            item = store.one("SELECT * FROM items WHERE status='queued' ORDER BY received_at LIMIT 1")
            if not item:
                break
            it = {**item, "from": item["sender"]}
            await run_item(rt, graph, it)
    finally:
        worker_state["running"] = False
        store.event(None, "system", "system", "Queue drained")


# ------------------------------------------------------------------ models

class SettingsIn(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class Decision(BaseModel):
    decision: str  # approve | reject
    note: str = ""


class Question(BaseModel):
    question: str


# ------------------------------------------------------------------ routes

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "model": gateway.settings.label, "worker": worker_state["running"]}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return gateway.settings.public()


@app.post("/api/settings")
def set_settings(s: SettingsIn) -> dict[str, Any]:
    gateway.configure(**{k: v for k, v in s.model_dump().items() if v is not None})
    store.event(None, "system", "system", f"Model changed to {gateway.settings.label}")
    return gateway.settings.public()


@app.post("/api/settings/check")
async def check_settings() -> dict[str, Any]:
    return await gateway.check()


@app.get("/api/state")
def state() -> dict[str, Any]:
    snap = store.snapshot()
    snap["worker"] = {"running": worker_state["running"]}
    snap["model"] = gateway.settings.label
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
    cost = sum(c["cost_usd"] or 0 for c in snap["calls"])
    tokens = sum((c["tokens_in"] or 0) + (c["tokens_out"] or 0) for c in snap["calls"])
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
async def events(after: int = 0):
    async def gen():
        last = after
        while True:
            for e in store.events_since(last):
                last = e["id"]
                yield {"event": "log", "id": str(e["id"]), "data": json.dumps(e)}
            await asyncio.sleep(0.4)

    return EventSourceResponse(gen())


@app.post("/api/demo/reset")
def demo_reset() -> dict[str, Any]:
    if worker_state["running"]:
        raise HTTPException(409, "worker is running")
    store.reset(DATA)
    store.event(None, "system", "system", "Northwind dataset loaded: 5 customers, 3 suppliers, 4 orders, 3 purchase orders, 6 invoices")
    return {"ok": True}


@app.post("/api/demo/run")
async def demo_run() -> dict[str, Any]:
    if worker_state["running"]:
        return {"ok": True, "already": True}
    items = json.loads((DATA / "items.json").read_text(encoding="utf8"))
    existing = {i["id"] for i in store.q("SELECT id FROM items")}
    store.enqueue_items([i for i in items if i["id"] not in existing])
    store.event(None, "system", "system", f"{len(items)} overnight items queued")
    worker_state["task"] = asyncio.create_task(worker_loop())
    return {"ok": True}


@app.post("/api/actions/{action_id}/decide")
def decide(action_id: str, d: Decision) -> dict[str, Any]:
    a = store.one("SELECT * FROM actions WHERE id=?", (action_id,))
    if not a:
        raise HTTPException(404, "no such action")
    if a["status"] != "awaiting_approval":
        raise HTTPException(409, f"action is {a['status']}")
    payload = json.loads(a["payload"])
    store.x("INSERT INTO approvals (id, action_id, decision, note, decided_at) VALUES (?,?,?,?,?)", (f"apr-{uuid.uuid4().hex[:8]}", action_id, d.decision, d.note, time.time()))
    if d.decision == "approve":
        agents.execute(rt, a, payload)
        store.set_action(action_id, status="executed")
        store.event(a["case_id"], "you", "human", f"Approved {a['type']}" + (f": {d.note}" if d.note else ""))
    else:
        store.set_action(action_id, status="rejected")
        store.event(a["case_id"], "you", "human", f"Rejected {a['type']}" + (f": {d.note}" if d.note else ""))
    # memory is written from human decisions
    case = store.one("SELECT * FROM cases WHERE id=?", (a["case_id"],)) or {}
    item = store.one("SELECT * FROM items WHERE id=?", (case.get("item_id"),)) or {}
    store.remember(f"mem-{action_id}", a["case_id"], item.get("kind", "unknown"), f"{item.get('kind')} “{item.get('subject')}”: {a['type']} for {a['counterparty']} ({a['amount']})", f"{d.decision} by owner" + (f": {d.note}" if d.note else ""))
    if not store.q("SELECT 1 FROM actions WHERE case_id=? AND status='awaiting_approval'", (a["case_id"],)):
        store.close_case(a["case_id"], "closed")
    return {"ok": True}


@app.post("/api/ask")
async def ask(q: Question) -> dict[str, Any]:
    item = {"id": f"q-{uuid.uuid4().hex[:6]}", "kind": "question", "source": "control-room", "from": "owner@northwind.example", "subject": q.question[:80], "body": q.question}
    store.enqueue_items([item])
    if not worker_state["running"]:
        worker_state["task"] = asyncio.create_task(worker_loop())
    return {"ok": True, "item_id": item["id"]}


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str) -> dict[str, Any]:
    snap = store.snapshot()
    c = next((c for c in snap["cases"] if c["id"] == case_id), None)
    if not c:
        raise HTTPException(404)
    return {"case": c, "actions": [a for a in snap["actions"] if a["case_id"] == case_id], "events": store.q("SELECT * FROM events WHERE case_id=? ORDER BY id", (case_id,))}


def run() -> None:
    import uvicorn

    uvicorn.run("dispatch.main:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8787")), reload=False)


if __name__ == "__main__":
    run()
