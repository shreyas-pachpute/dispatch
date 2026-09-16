"""The case graph: dispatch → specialist → reviewer → policy. Explicit edges, typed state, one run per item."""

from __future__ import annotations

import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import agents
from .agents import Runtime


class CaseState(TypedDict, total=False):
    case_id: str
    item: dict[str, Any]
    owner: str
    result: dict[str, Any]
    done: bool


def build_graph(rt: Runtime):
    async def n_dispatch(s: CaseState) -> CaseState:
        d = await agents.dispatcher(rt, s["case_id"], s["item"])
        if d.owner_agent == "escalate":
            rt.propose(s["case_id"], "escalate", {"reason": d.reason, "subject": s["item"]["subject"]}, None, None)
        return {"owner": d.owner_agent}

    async def n_intake(s: CaseState) -> CaseState:
        return {"result": await agents.intake(rt, s["case_id"], s["item"])}

    async def n_accounts(s: CaseState) -> CaseState:
        if s["result"].get("quote_failures") or s["result"]["extraction"].get("missing_required"):
            return {"result": s["result"]}
        return {"result": await agents.accounts(rt, s["case_id"], s["item"], s["result"])}

    async def n_customer(s: CaseState) -> CaseState:
        return {"result": await agents.customer(rt, s["case_id"], s["item"])}

    async def n_followup(s: CaseState) -> CaseState:
        return {"result": await agents.followup(rt, s["case_id"], s["item"])}

    async def n_analyst(s: CaseState) -> CaseState:
        return {"result": await agents.analyst(rt, s["case_id"], s["item"]), "done": True}

    async def n_reviewer(s: CaseState) -> CaseState:
        await agents.reviewer(rt, s["case_id"], s["item"])
        return {}

    async def n_policy(s: CaseState) -> CaseState:
        agents.apply_policy(rt, s["case_id"])
        return {"done": True}

    g = StateGraph(CaseState)
    g.add_node("dispatch", n_dispatch)
    g.add_node("intake", n_intake)
    g.add_node("accounts", n_accounts)
    g.add_node("customer", n_customer)
    g.add_node("followup", n_followup)
    g.add_node("analyst", n_analyst)
    g.add_node("reviewer", n_reviewer)
    g.add_node("policy", n_policy)

    g.add_edge(START, "dispatch")
    g.add_conditional_edges(
        "dispatch",
        lambda s: s["owner"],
        {"intake": "intake", "customer": "customer", "followup": "followup", "analyst": "analyst", "escalate": "reviewer"},
    )
    g.add_edge("intake", "accounts")
    g.add_edge("accounts", "reviewer")
    g.add_edge("customer", "reviewer")
    g.add_edge("followup", "reviewer")
    g.add_edge("analyst", END)
    g.add_edge("reviewer", "policy")
    g.add_edge("policy", END)
    return g.compile()


async def run_item(rt: Runtime, graph, item: dict[str, Any]) -> str:
    case_id = f"case-{item['id']}-{uuid.uuid4().hex[:4]}"
    rt.store.open_case(case_id, item["id"])
    try:
        await graph.ainvoke({"case_id": case_id, "item": item})
        pending = rt.store.q("SELECT 1 FROM actions WHERE case_id=%s AND status='awaiting_approval'", (case_id,))
        escalated = rt.store.q("SELECT 1 FROM actions WHERE case_id=%s AND type='escalate'", (case_id,))
        status = "escalated" if escalated else ("awaiting_approval" if pending else "closed")
        rt.store.close_case(case_id, status)
        if status == "closed":
            _remember(rt, case_id, item)
    except Exception as e:  # noqa: BLE001
        rt.say(case_id, "system", "error", f"{type(e).__name__}: {str(e)[:300]}")
        rt.store.close_case(case_id, "failed")
    return case_id


def _remember(rt: Runtime, case_id: str, item: dict[str, Any]) -> None:
    """Memory is written from outcomes only: an executed, unreverted action or a human decision."""
    acts = rt.store.q("SELECT type, policy_decision FROM actions WHERE case_id=%s AND status='executed'", (case_id,))
    if not acts:
        return
    summary = f"{item['kind']} “{item['subject']}” → " + ", ".join(f"{a['type']} ({a['policy_decision']})" for a in acts)
    rt.store.remember(f"mem-{case_id}", case_id, item["kind"], summary, "executed by policy")
