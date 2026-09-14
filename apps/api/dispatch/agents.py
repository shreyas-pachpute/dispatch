"""The team. Each agent is an async function over a Case; they read the pack, call the gateway with a schema,
validate what came back against the evidence, and propose actions. They never execute anything."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from typing import Any

from . import schemas as S
from .gateway import Gateway
from .packs import ROLES, PackBuilder
from .policy import Policy
from .store import Store

TODAY = date(2026, 9, 14)
REMINDER_STAGES = [7, 14, 30]


class Runtime:
    def __init__(self, store: Store, gateway: Gateway, packs: PackBuilder, policy: Policy):
        self.store, self.gw, self.packs, self.policy = store, gateway, packs, policy

    def say(self, case_id: str, agent: str, kind: str, msg: str) -> None:
        self.store.event(case_id, agent, kind, msg)

    def propose(self, case_id: str, type_: str, payload: dict[str, Any], amount: float | None, counterparty: str | None) -> str:
        aid = f"act-{uuid.uuid4().hex[:8]}"
        self.store.add_action(aid, case_id, type_, payload, amount, counterparty)
        return aid


# ---------------------------------------------------------------- helpers

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def validate_quotes(extraction: S.InvoiceExtraction, document: str) -> list[str]:
    """A citation is only a citation if the quote is in the document. Returns the fields that fail."""
    doc = _norm(document)
    bad = []
    for name in ("supplier_name", "invoice_number", "po_number", "invoice_date", "total"):
        f: S.CitedField = getattr(extraction, name)
        if f.value and _norm(f.quote) not in doc:
            bad.append(name)
    for i, line in enumerate(extraction.lines):
        if _norm(line.quote) not in doc:
            bad.append(f"lines[{i}]")
    return bad


def three_way_match(store: Store, ex: S.InvoiceExtraction) -> dict[str, Any]:
    """Deterministic: invoice vs purchase order vs receipt. The model only narrates the result."""
    inv_no = ex.invoice_number.value
    po_id = ex.po_number.value
    dup = store.one(
        "SELECT c.id FROM cases c JOIN actions a ON a.case_id=c.id WHERE a.type IN ('queue_payment','hold_invoice','supplier_query') AND a.payload LIKE ? LIMIT 1",
        (f'%"invoice_number": "{inv_no}"%',),
    )
    if dup:
        return {"verdict": "duplicate", "invoice_number": inv_no, "po_id": po_id, "differences": [f"invoice {inv_no} already processed in case {dup['id']}"], "total": float(ex.total.value or 0)}
    po = store.one("SELECT * FROM purchase_orders WHERE id=?", (po_id,))
    if not po:
        return {"verdict": "unmatched", "invoice_number": inv_no, "po_id": po_id, "differences": [f"no purchase order {po_id}"], "total": float(ex.total.value or 0)}
    po_lines = json.loads(po["lines"])
    rcpt = store.one("SELECT * FROM receipts WHERE po_id=?", (po_id,))
    rcpt_lines = json.loads(rcpt["lines"]) if rcpt else []
    supplier = store.one("SELECT * FROM suppliers WHERE id=?", (po["supplier_id"],)) or {}
    tol = float(supplier.get("variance_tolerance") or 0)
    diffs: list[str] = []
    for line in ex.lines:
        pl = next((p for p in po_lines if _norm(p["description"]) == _norm(line.description)), None)
        if not pl:
            diffs.append(f"line '{line.description}' is not on {po_id}")
            continue
        if abs(line.quantity - pl["quantity"]) > 1e-6:
            diffs.append(f"quantity {line.quantity:g} vs {pl['quantity']:g} on {po_id} for '{line.description}'")
        if pl["unit_price"] and abs(line.unit_price - pl["unit_price"]) / pl["unit_price"] > tol + 1e-9:
            pct = (line.unit_price - pl["unit_price"]) / pl["unit_price"] * 100
            diffs.append(f"unit price {line.unit_price:.2f} vs {pl['unit_price']:.2f} on {po_id} ({pct:+.1f}%, tolerance {tol*100:.0f}%) for '{line.description}'")
        rl = next((r for r in rcpt_lines if _norm(r["description"]) == _norm(line.description)), None)
        if rcpt and (not rl or abs(rl["quantity"] - line.quantity) > 1e-6):
            diffs.append(f"receipt for {po_id} shows {rl['quantity'] if rl else 0:g} received for '{line.description}'")
    if not rcpt:
        diffs.append(f"no goods receipt for {po_id}")
    verdict = "matched" if not diffs else "variance"
    return {
        "verdict": verdict,
        "invoice_number": inv_no,
        "po_id": po_id,
        "supplier": supplier.get("name"),
        "supplier_class": supplier.get("class"),
        "supplier_email": supplier.get("email"),
        "receipt": rcpt["received"] if rcpt else None,
        "differences": diffs,
        "total": float(ex.total.value or 0),
    }


# ---------------------------------------------------------------- agents

async def dispatcher(rt: Runtime, case_id: str, item: dict[str, Any]) -> S.DispatchDecision:
    pack = rt.packs.build("dispatcher", item, [])
    rt.store.update_case(case_id, pack=pack)
    rt.say(case_id, "dispatcher", "turn", f"Reading {item['kind'].replace('_',' ')} from {item['sender']}: “{item['subject']}”")
    out = await rt.gw.complete(role="dispatcher", schema=S.DispatchDecision, system=pack["system"], user=PackBuilder.render(pack, "Decide which specialist owns this item."), case_id=case_id, context={"item": item})
    rt.store.update_case(case_id, owner_agent=out.owner_agent, intent=out.intent, confidence=out.confidence, reason=out.reason)
    rt.say(case_id, "dispatcher", "decision", f"→ {out.owner_agent} ({out.confidence:.0%}): {out.reason}")
    return out


async def intake(rt: Runtime, case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    pack = rt.packs.build("intake", item, ["queue_payment", "hold_invoice", "supplier_query"])
    rt.say(case_id, "intake", "turn", "Extracting fields from the attached document, one quote per field")
    ex = await rt.gw.complete(role="intake", schema=S.InvoiceExtraction, system=pack["system"], user=PackBuilder.render(pack, "Extract the invoice fields with verbatim quotes."), case_id=case_id, context={"item": item})
    bad = validate_quotes(ex, item.get("document") or "")
    if bad:
        rt.say(case_id, "intake", "warning", f"Quote check failed for {', '.join(bad)}; retrying once")
        ex = await rt.gw.complete(role="intake", schema=S.InvoiceExtraction, system=pack["system"], user=PackBuilder.render(pack, f"Extract again. These fields had quotes that were not verbatim: {bad}. Copy exact characters."), case_id=case_id, context={"item": item})
        bad = validate_quotes(ex, item.get("document") or "")
    result = {"extraction": ex.model_dump(), "quote_failures": bad}
    rt.store.update_case(case_id, result=result, pack=pack)
    fields = [f"{k}={getattr(ex, k).value}" for k in ("invoice_number", "po_number", "total")]
    rt.say(case_id, "intake", "result", f"Extracted {', '.join(fields)}; {len(ex.lines)} lines; every quote verified" if not bad else f"Extracted with {len(bad)} unverifiable quotes: escalating")
    if ex.missing_required or bad:
        rt.propose(case_id, "escalate", {"reason": f"missing {ex.missing_required} / unverifiable {bad}", "invoice_number": ex.invoice_number.value}, None, None)
    return result


async def accounts(rt: Runtime, case_id: str, item: dict[str, Any], intake_result: dict[str, Any]) -> dict[str, Any]:
    ex = S.InvoiceExtraction.model_validate(intake_result["extraction"])
    match = three_way_match(rt.store, ex)
    rt.say(case_id, "accounts", "turn", f"Three-way match against {match['po_id']}: {match['verdict']}" + (f" ({len(match['differences'])} difference{'s' if len(match['differences'])!=1 else ''})" if match["differences"] else ""))
    pack = rt.packs.build("accounts", item, ["queue_payment", "hold_invoice", "supplier_query"], extra={"match": match})
    narrative = await rt.gw.complete(role="accounts", schema=S.MatchNarrative, system=pack["system"], user=PackBuilder.render(pack, "Explain the match verdict; draft a supplier query only if there is a variance."), case_id=case_id, context={"item": item, "match": match})
    total = match["total"]
    supplier = match.get("supplier") or ex.supplier_name.value
    if match["verdict"] == "matched":
        rt.propose(case_id, "queue_payment", {"invoice_number": match["invoice_number"], "po_id": match["po_id"], "supplier": supplier, "amount": total, "explanation": narrative.explanation}, total, supplier)
    elif match["verdict"] == "duplicate":
        rt.propose(case_id, "hold_invoice", {"invoice_number": match["invoice_number"], "po_id": match["po_id"], "supplier": supplier, "reason": "duplicate invoice number", "explanation": narrative.explanation}, total, supplier)
    else:
        rt.propose(case_id, "hold_invoice", {"invoice_number": match["invoice_number"], "po_id": match["po_id"], "supplier": supplier, "reason": "; ".join(match["differences"]), "explanation": narrative.explanation}, total, supplier)
        if narrative.supplier_query:
            rt.propose(case_id, "supplier_query", {"to": match.get("supplier_email"), "invoice_number": match["invoice_number"], "po_id": match["po_id"], "subject": narrative.supplier_query.subject, "body": narrative.supplier_query.body}, total, supplier)
    result = {**intake_result, "match": match, "narrative": narrative.model_dump()}
    rt.store.update_case(case_id, result=result)
    rt.say(case_id, "accounts", "result", narrative.explanation)
    return result


async def customer(rt: Runtime, case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    pack = rt.packs.build("customer", item, ["customer_reply", "escalate"])
    rt.store.update_case(case_id, pack=pack)
    rt.say(case_id, "customer", "turn", "Reading the thread against order, shipment and policy records")
    out = await rt.gw.complete(role="customer", schema=S.CustomerReply, system=pack["system"], user=PackBuilder.render(pack, "Draft the reply, or escalate with a reason."), case_id=case_id, context={"item": item, "records": pack["evidence"]["records"], "knowledge_ids": [c["id"] for c in pack["evidence"]["knowledge"]]})
    # citations must point at things that exist in the pack
    valid_ids = {c["id"] for c in pack["evidence"]["knowledge"]}
    for o in pack["evidence"]["records"].get("orders", []):
        valid_ids.add(f"order:{o['id']}")
    for s in pack["evidence"]["records"].get("shipments", []):
        valid_ids.add(f"shipment:{s['order_id']}")
    dangling = [c for c in out.citations if c not in valid_ids]
    result = {"reply": out.model_dump(), "dangling_citations": dangling}
    rt.store.update_case(case_id, result=result)
    cust = pack["evidence"]["records"].get("customer") or {}
    if out.escalate or dangling:
        why = out.escalation_reason or f"citations not in evidence: {dangling}"
        rt.say(case_id, "customer", "result", f"Escalating: {why}")
        rt.propose(case_id, "escalate", {"reason": why, "customer": cust.get("name"), "injection_suspected": out.injection_suspected, "subject": item["subject"]}, None, cust.get("name"))
    else:
        rt.say(case_id, "customer", "result", f"Drafted a {out.intent} reply with {len(out.citations)} citations")
        rt.propose(case_id, "customer_reply", {"to": item["sender"], "subject": out.subject, "body": out.body, "citations": out.citations, "intent": out.intent}, None, cust.get("name"))
    return result


async def followup(rt: Runtime, case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    rt.say(case_id, "followup", "turn", f"Checking open receivables as of {TODAY.isoformat()}")
    invoices = rt.store.q("SELECT i.*, c.name AS customer_name, c.email AS customer_email, c.sensitive FROM invoices_out i JOIN customers c ON c.id=i.customer_id WHERE i.status='open'")
    sent = rt.store.q("SELECT * FROM reminders_sent")
    plan = []
    for inv in invoices:
        overdue = (TODAY - date.fromisoformat(inv["due"])).days
        if overdue <= 0:
            continue
        stage = sum(1 for d in REMINDER_STAGES if overdue >= d)
        done = {r["stage"] for r in sent if r["invoice_id"] == inv["id"]}
        if stage == 0 or stage in done:
            plan.append({"invoice": inv["id"], "overdue_days": overdue, "stage": stage, "skip": "already sent" if stage in done else "not due"})
            continue
        plan.append({"invoice": inv["id"], "overdue_days": overdue, "stage": stage, "amount": inv["amount"], "customer": inv["customer_name"], "sensitive": bool(inv["sensitive"])})
    pack = rt.packs.build("followup", item, ["schedule_reminder"], extra={"plan": plan})
    rt.store.update_case(case_id, pack=pack)
    drafts = []
    for p in plan:
        if p.get("skip"):
            continue
        inv = next(i for i in invoices if i["id"] == p["invoice"])
        draft = await rt.gw.complete(role="followup", schema=S.ReminderDraft, system=pack["system"], user=PackBuilder.render(pack, f"Write the stage-{p['stage']} reminder for invoice {inv['id']} ({inv['amount']:.2f} USD, due {inv['due']}) to {inv['customer_name']}."), case_id=case_id, context={"invoice": inv, "customer": {"name": inv["customer_name"]}})
        drafts.append({"invoice": inv["id"], "stage": p["stage"], **draft.model_dump()})
        rt.propose(case_id, "schedule_reminder", {"to": inv["customer_email"], "customer": inv["customer_name"], "invoice_number": inv["id"], "stage": p["stage"], "amount": inv["amount"], "overdue_days": p["overdue_days"], "subject": draft.subject, "body": draft.body, "sensitive": p["sensitive"], "idempotency_key": f"{inv['id']}:{p['stage']}"}, inv["amount"], inv["customer_name"])
    rt.store.update_case(case_id, result={"plan": plan, "drafts": drafts})
    n = len(drafts)
    rt.say(case_id, "followup", "result", f"{n} reminder{'s' if n != 1 else ''} to schedule; {len(plan) - n} skipped (already sent or not due)")
    return {"plan": plan, "drafts": drafts}


SCHEMA_CARD = """tables:
  customers(id TEXT, name TEXT, email TEXT, segment TEXT, sensitive INTEGER)
  orders(id TEXT, customer_id TEXT, placed TEXT, items TEXT, total REAL, status TEXT)
  shipments(order_id TEXT, carrier TEXT, tracking TEXT, shipped TEXT, eta TEXT, status TEXT)
  suppliers(id TEXT, name TEXT, email TEXT, class TEXT, variance_tolerance REAL)
  purchase_orders(id TEXT, supplier_id TEXT, issued TEXT, lines TEXT(json), total REAL)
  invoices_out(id TEXT, customer_id TEXT, issued TEXT, due TEXT, amount REAL, status TEXT('open'|'paid'), paid TEXT)
  reminders_sent(invoice_id TEXT, stage INTEGER, sent TEXT)
notes: dates are ISO 'YYYY-MM-DD' strings; amounts are USD."""


async def analyst(rt: Runtime, case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    pack = rt.packs.build("analyst", item, [], extra={"schema_card": SCHEMA_CARD})
    rt.store.update_case(case_id, pack=pack)
    rt.say(case_id, "analyst", "turn", "Writing a read-only query over the operational store")
    q = await rt.gw.complete(role="analyst", schema=S.AnalystQuery, system=pack["system"], user=PackBuilder.render(pack, f"Question: {item['body']}"), case_id=case_id, context={"item": item})
    sql = q.sql.strip().rstrip(";")
    if not re.match(r"^\s*select\b", sql, re.I) or re.search(r"\b(insert|update|delete|drop|alter|create|attach|pragma)\b", sql, re.I):
        rt.say(case_id, "analyst", "warning", "Query rejected: not a plain SELECT")
        rt.store.update_case(case_id, result={"sql": sql, "error": "rejected: not read-only"})
        return {"sql": sql, "error": "rejected"}
    try:
        rows = rt.store.q(sql + " LIMIT 50")
        error = None
    except Exception as e:  # noqa: BLE001
        rows, error = [], str(e)
    rt.say(case_id, "analyst", "result", f"Query returned {len(rows)} rows" if not error else f"Query failed: {error}")
    ans = await rt.gw.complete(role="analyst_answer", schema=S.AnalystAnswer, system=ROLES["analyst_answer"], user=f"Question: {item['body']}\n\nResult table (JSON):\n{json.dumps(rows, default=str)}", case_id=case_id, context={"rows": rows}) if not error else S.AnalystAnswer(answer=f"The query could not run: {error}")
    result = {"sql": sql, "explanation": q.explanation, "rows": rows, "error": error, "answer": ans.answer}
    rt.store.update_case(case_id, result=result)
    rt.say(case_id, "analyst", "answer", ans.answer)
    return result


async def reviewer(rt: Runtime, case_id: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    """Independent check of every outbound action before policy sees it."""
    actions = rt.store.q("SELECT * FROM actions WHERE case_id=? AND status='proposed'", (case_id,))
    case = rt.store.one("SELECT * FROM cases WHERE id=?", (case_id,)) or {}
    result = json.loads(case["result"]) if case.get("result") else {}
    injection = bool(result.get("reply", {}).get("injection_suspected"))
    verdicts = []
    for a in actions:
        payload = json.loads(a["payload"])
        if a["type"] in ("escalate",):
            rt.store.set_action(a["id"], reviewer_verdict={"verdict": "pass", "reason": "escalations always reach a person"})
            continue
        pack = rt.packs.build("reviewer", item, [a["type"]], extra={"proposed_action": {"type": a["type"], **payload}})
        v = await rt.gw.complete(role="reviewer", schema=S.ReviewerVerdict, system=pack["system"], user=PackBuilder.render(pack, "Review the proposed action."), case_id=case_id, context={"item": item, "action": {"type": a["type"], "payload": payload}, "injection_suspected": injection})
        rt.store.set_action(a["id"], reviewer_verdict=v.model_dump())
        if v.verdict == "amend" and v.amended_body and "body" in payload:
            payload["body"] = v.amended_body
            rt.store.set_action(a["id"], payload=payload)
        if v.verdict == "block":
            rt.store.set_action(a["id"], status="blocked", policy_decision="blocked")
        verdicts.append({"action": a["id"], "type": a["type"], **v.model_dump()})
        rt.say(case_id, "reviewer", "verdict", f"{a['type']}: {v.verdict.upper()} — {v.reason}")
    rt.store.update_case(case_id, reviewer=verdicts)
    return verdicts


def apply_policy(rt: Runtime, case_id: str) -> None:
    """Auto / notify / approve for what the Reviewer let through. Executes auto and notify through the (mock) MCP tools."""
    for a in rt.store.q("SELECT * FROM actions WHERE case_id=? AND status='proposed'", (case_id,)):
        payload = json.loads(a["payload"])
        flags = {"sensitive": payload.get("sensitive"), "intent": payload.get("intent")}
        decision, rule = rt.policy.decide(a["type"], a["amount"], flags)
        if decision in ("auto", "notify"):
            execute(rt, a, payload)
            rt.store.set_action(a["id"], policy_decision=decision, status="executed")
            rt.say(case_id, "policy", "policy", f"{a['type']}: {decision} ({rule}) → executed")
        else:
            rt.store.set_action(a["id"], policy_decision="approve", status="awaiting_approval")
            rt.say(case_id, "policy", "policy", f"{a['type']}: waiting for you ({rule})")


def execute(rt: Runtime, action: dict[str, Any], payload: dict[str, Any]) -> None:
    """The MCP tool calls, v0: the reference servers are CSV/JSON-backed, so 'sending' is a recorded write."""
    import time as _t

    t = action["type"]
    result: dict[str, Any]
    if t == "schedule_reminder":
        rt.store.x("INSERT INTO reminders_sent (invoice_id, stage, sent) VALUES (?,?,?)", (payload["invoice_number"], payload["stage"], TODAY.isoformat()))
        result = {"tool": "mcp-mail.schedule_email", "to": payload["to"], "when": "next send window (08:00)"}
    elif t == "customer_reply":
        result = {"tool": "mcp-mail.send_email", "to": payload["to"], "subject": payload["subject"]}
    elif t == "supplier_query":
        result = {"tool": "mcp-mail.send_email", "to": payload.get("to"), "subject": payload["subject"]}
    elif t == "queue_payment":
        result = {"tool": "mcp-accounting.queue_payment", "invoice": payload["invoice_number"], "amount": payload["amount"]}
    elif t == "hold_invoice":
        result = {"tool": "mcp-accounting.hold_invoice", "invoice": payload["invoice_number"], "reason": payload.get("reason")}
    else:
        result = {"tool": "none"}
    rt.store.set_action(action["id"], executed_at=_t.time(), result=result)
