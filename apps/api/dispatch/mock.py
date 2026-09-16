"""Deterministic stand-ins for the model, derived from the same evidence the model would see.
They exist so the demo and the tests run without a key. They are labelled 'mock' everywhere in the UI."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from . import schemas as S


def _find(pattern: str, text: str) -> tuple[str, str]:
    m = re.search(pattern, text, re.M)
    if not m:
        return "", ""
    return m.group(1).strip(), m.group(0).strip()


def respond(role: str, schema: type[BaseModel], ctx: dict[str, Any]) -> Any:
    item = ctx.get("item", {})
    text = (item.get("subject") or "") + "\n" + (item.get("body") or "")
    doc = item.get("document") or ""

    if schema is S.DispatchDecision:
        kind = item.get("kind")
        owner = {"supplier_invoice": "intake", "customer_email": "customer", "scheduled": "followup", "question": "analyst"}.get(kind, "escalate")
        intent = {"intake": "supplier invoice to process", "customer": "customer email needing a reply", "followup": "scheduled receivables check", "analyst": "question about the business"}.get(owner, "unclear")
        return S.DispatchDecision(owner_agent=owner, intent=intent, confidence=0.93 if owner != "escalate" else 0.3, reason=f"{kind} from {item.get('sender')} matches the {owner} agent's remit.")

    if schema is S.InvoiceExtraction:
        supplier, q_s = doc.split("\n", 1)[0], doc.split("\n", 1)[0]
        inv, q_inv = _find(r"^Invoice number: (.+)$", doc)
        po, q_po = _find(r"^Purchase order: (.+)$", doc)
        date, q_date = _find(r"^Invoice date: (.+)$", doc)
        total, q_total = _find(r"^Total due: [A-Z]{3} ([\d,\.]+)$", doc)
        lines = []
        for m in re.finditer(r"^(.+?) \| (\d+) \| ([\d\.]+) \| ([\d,\.]+)$", doc, re.M):
            lines.append(S.InvoiceLine(description=m.group(1), quantity=float(m.group(2)), unit_price=float(m.group(3)), amount=float(m.group(4).replace(",", "")), quote=m.group(0)))
        missing = [k for k, v in {"invoice_number": inv, "po_number": po, "total": total}.items() if not v]
        return S.InvoiceExtraction(
            document_type="invoice",
            supplier_name=S.CitedField(value=supplier.title(), quote=q_s),
            invoice_number=S.CitedField(value=inv, quote=q_inv),
            po_number=S.CitedField(value=po, quote=q_po),
            invoice_date=S.CitedField(value=date, quote=q_date),
            total=S.CitedField(value=total.replace(",", ""), quote=q_total),
            currency="USD",
            lines=lines,
            missing_required=missing,
        )

    if schema is S.MatchNarrative:
        m = ctx.get("match", {})
        verdict = m.get("verdict")
        po = m.get("po_id", "the PO")
        if verdict == "matched":
            return S.MatchNarrative(explanation=f"Invoice {m.get('invoice_number')} matches {po} line for line and the goods receipt for {po} confirms delivery. Nothing to query.")
        if verdict == "duplicate":
            return S.MatchNarrative(explanation=f"Invoice number {m.get('invoice_number')} was already received and processed against {po}. Held so it cannot be paid twice.")
        diffs = "; ".join(m.get("differences", [])) or "quantities or prices differ from the purchase order"
        return S.MatchNarrative(
            explanation=f"Invoice {m.get('invoice_number')} does not match {po}: {diffs}. The goods receipt for {po} confirms delivery, so the question is price, not quantity.",
            supplier_query=S.SupplierQuery(
                subject=f"Query on invoice {m.get('invoice_number')} against {po}",
                body=f"Hello,\n\nWe received invoice {m.get('invoice_number')} against our purchase order {po}. It differs from the order: {diffs}. Could you confirm the agreed price or send a corrected invoice?\n\nThank you,\nNorthwind Supplies accounts",
            ),
        )

    if schema is S.CustomerReply:
        rec = ctx.get("records", {})
        injected = bool(re.search(r"ignore your policy|SYSTEM NOTE", text, re.I))
        hostile = bool(re.search(r"unacceptable|refund|legal", text, re.I))
        ship = (rec.get("shipments") or [None])[0]
        order = (rec.get("orders") or [None])[0]
        cust = rec.get("customer") or {}
        name = (item.get("body") or "").strip().split("\n")[-1].split(",")[0].strip() if not injected else ""
        if hostile or injected:
            return S.CustomerReply(intent="complaint", sentiment="negative", escalate=True, escalation_reason="Customer demands a refund and threatens to leave; refunds need the operations manager. " + ("The email also contains instructions aimed at the assistant." if injected else ""), subject=f"Re: {item.get('subject')}", body="", citations=[c for c in ["kb-returns-1", "kb-shipping-2"]], injection_suspected=injected)
        if ship and order:
            body = f"Hi {name or 'there'},\n\nOrder #{order['id']} ({order['items']}) shipped on {ship['shipped']} with {ship['carrier']}, tracking {ship['tracking']}. The carrier's estimate is {ship['eta']}.\n\nIf anything changes we will let you know.\n\nNorthwind Supplies"
            return S.CustomerReply(intent="informational", sentiment="neutral", escalate=False, subject=f"Re: {item.get('subject')}", body=body, citations=[f"order:{order['id']}", f"shipment:{order['id']}", *[k for k in ctx.get("knowledge_ids", []) if k.startswith("kb-shipping")][:1]], injection_suspected=False)
        return S.CustomerReply(intent="other", sentiment="neutral", escalate=True, escalation_reason="No order or shipment record answers this email.", subject=f"Re: {item.get('subject')}", body="", citations=[], injection_suspected=False)

    if schema is S.ReminderDraft:
        inv = ctx.get("invoice", {})
        cust = ctx.get("customer", {})
        return S.ReminderDraft(subject=f"Invoice {inv.get('id')} is past due", body=f"Hi {cust.get('name')},\n\nA quick reminder that invoice {inv.get('id')} for USD {inv.get('amount'):,.2f} was due on {inv.get('due')}. If it has already been paid, thank you and please ignore this. If not, could you let us know when to expect it?\n\nNorthwind Supplies")

    if schema is S.AnalystQuery:
        return S.AnalystQuery(
            sql="SELECT c.name AS customer, ROUND(CAST(SUM(i.amount) AS NUMERIC),2) AS invoiced, SUM(CASE WHEN i.status='open' THEN 1 ELSE 0 END) AS open_invoices, ROUND(CAST(SUM(CASE WHEN i.status='open' THEN i.amount ELSE 0 END) AS NUMERIC),2) AS open_amount FROM invoices_out i JOIN customers c ON c.id=i.customer_id WHERE i.issued BETWEEN '2026-08-01' AND '2026-08-31' GROUP BY c.name ORDER BY invoiced DESC",
            explanation="Sums August invoices per customer and counts how many are still open.",
        )

    if schema is S.AnalystAnswer:
        rows = ctx.get("rows", [])
        if not rows:
            return S.AnalystAnswer(answer="The query returned no rows for August 2026.")
        top = rows[0]
        total = sum(r.get("invoiced", 0) for r in rows)
        open_amt = sum(r.get("open_amount", 0) for r in rows)
        return S.AnalystAnswer(answer=f"August invoicing totalled USD {total:,.2f} across {len(rows)} customers, led by {top.get('customer')} at USD {top.get('invoiced'):,.2f}. USD {open_amt:,.2f} of it is still open.")

    if schema is S.ReviewerVerdict:
        action = ctx.get("action", {})
        payload = action.get("payload", {})
        body = (payload.get("body") or "") + " " + (payload.get("explanation") or "")
        if ctx.get("injection_suspected"):
            return S.ReviewerVerdict(verdict="block", reason="The proposed action follows instructions that came from the email, not from policy.", injection_detected=True)
        if action.get("type") == "customer_reply" and re.search(r"refund|guarantee|by (mon|tue|wed|thu|fri)", body, re.I):
            return S.ReviewerVerdict(verdict="block", reason="The draft promises something no record supports.", unsupported_claims=["refund or delivery promise"])
        return S.ReviewerVerdict(verdict="pass", reason="Every fact in the action traces to a record or the policy; tone matches the voice guide.")

    raise ValueError(f"mock has no handler for {schema.__name__}")
