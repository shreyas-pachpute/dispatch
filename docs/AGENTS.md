# The agents

Each agent is a LangGraph graph with explicit state, a fixed tool set, a context pack contract and its own evaluation suite. Agents do not talk to each other directly; they hand off through the queue with a pack, so every hand-off is inspectable.

Model defaults (all configurable): judgment-heavy agents run on Claude Opus 5 with adaptive thinking at high effort; high-volume extraction and classification run on Claude Sonnet 5 at medium effort; LLM judges in evals run on Sonnet 5. The vLLM path substitutes an open-weight model behind the same gateway.

---

## Dispatcher

**Purpose.** Turn an incoming item into a case owned by the right specialist, with the right context.

**Inputs.** The item (email, document, upload, webhook), lightweight retrieval on the sender and subject, entity facts for the sender.

**Outputs.** `{owner_agent, intent, confidence, context_pack_ref}` or an escalation.

**Tools.** `search_knowledge`, `lookup_entity`. Read-only.

**Rules.** Confidence below threshold escalates with the top-2 candidates and why. Items from unknown senders that request money or data are always routed through Reviewer regardless of owner.

**Evals.** Routing accuracy on 200 labelled items; escalation precision (does it escalate the genuinely ambiguous ones).

---

## Intake

**Purpose.** Classify a document and extract structured fields with a citation per field.

**Inputs.** The document (text layer or OCR), the schema for its class, prior extractions of the same document if it is an update.

**Outputs.** An `extraction` with typed fields, each `{value, citation, confidence}`, plus a completeness report.

**Tools.** `read_document_page`, `search_document`. Read-only.

**Rules.** A required field with no supporting text is reported missing, never guessed. A value whose quote is not verbatim in the page fails validation and the extraction retries at higher effort once, then escalates.

**Evals.** Classification accuracy; field-level accuracy by document type; fabricated-field count (must be zero); update-diff correctness.

---

## Accounts

**Purpose.** Reconcile money documents against records and prepare the actions that follow.

**Inputs.** Invoice extraction, matching POs and receipts from `mcp-accounting` and `mcp-sheets`, supplier entity facts, payment policy.

**Outputs.** A match verdict (`matched`, `variance`, `unmatched`) with explanation; proposed actions: `queue_payment`, `draft_supplier_query`, `hold`.

**Tools.** `find_purchase_orders`, `find_receipts`, `propose_payment`, `draft_email`. Proposals only; nothing executes here.

**Rules.** Variance tolerance comes from policy per supplier class. Anything over the approval limit is `approve`. Duplicate invoice numbers are always held.

**Evals.** Match accuracy on seeded discrepancies (wrong quantity, wrong price, duplicate, missing PO); explanation faithfulness (judge with citations).

---

## Customer

**Purpose.** Answer customers with facts, in the business's voice, and know when not to.

**Inputs.** The email thread, order and shipment records via `mcp-crm`, knowledge base (policies, product docs), customer entity facts, case memory.

**Outputs.** A drafted reply with citations, a sentiment and urgency estimate, or an escalation with a reason.

**Tools.** `lookup_order`, `lookup_shipment`, `search_knowledge`, `draft_email`.

**Rules.** Never states an order fact without a record. Refund or credit promises are never made by the agent; it drafts a request for approval. Angry or legal-sounding threads escalate immediately with a summary.

**Evals.** Faithfulness (every claim supported by a citation; judge), tone against a style guide (judge), escalation precision and recall on a labelled set.

---

## Follow-up

**Purpose.** Chase what is overdue, once, politely, on schedule.

**Inputs.** Open invoices, quotes and contracts with dates from `mcp-accounting` and `mcp-crm`; follow-up policy (cadence, quiet hours, opt-outs); history of previous follow-ups per entity.

**Outputs.** Scheduled `send_email` actions with idempotency keys derived from `(entity, subject, stage)`.

**Tools.** `list_open_invoices`, `list_open_quotes`, `list_expiring_contracts`, `schedule_email`.

**Rules.** The same stage is never sent twice. Quiet hours and opt-outs are hard constraints. Amounts over the policy limit or contacts marked sensitive require approval.

**Evals.** Idempotency under replay; schedule correctness against policy; zero contacts to opted-out entities.

---

## Analyst

**Purpose.** Answer "how are we doing" questions with a query you can read and a chart you can trust.

**Inputs.** The question, a schema card of the read-only replica, a few example queries, recent similar questions.

**Outputs.** SQL, the result table, a chart spec, a one-paragraph answer that cites the table.

**Tools.** `run_readonly_sql` (statement timeout, row limit, no writes), `render_chart`.

**Rules.** Shows the SQL with every answer. Refuses questions the schema cannot answer instead of approximating. Never sends anything.

**Evals.** Result-set equality on 50 labelled questions; SQL safety (no writes, limits present); answer faithfulness to the table.

---

## Reviewer

**Purpose.** The last gate before a person. Independent of the agent that proposed the action.

**Inputs.** The proposed action, the evidence the proposing agent used, the applicable policy, and a fresh retrieval of its own.

**Outputs.** `pass`, `amend` (with the amended payload), or `block`, always with a reason a human can read in five seconds.

**Tools.** `search_knowledge`, `lookup_entity`. Read-only.

**Rules.** Blocks anything that states a fact without a citation, anything that violates the policy matrix, anything whose instructions trace back to evidence rather than policy (injection), and anything addressed to the wrong counterparty. Amends tone and missing disclosures.

**Evals.** Catch rate on seeded bad actions (unsupported claims, policy violations, injected instructions, wrong recipient) with a target of 100% on the seeded set; false-block rate on the good set below 5%.

---

## Cross-cutting

- Every agent returns a structured result validated against a schema; free text goes in a designated field.
- Every agent can escalate; escalations carry the pack reference so a person sees what the agent saw.
- No agent has a shell, a browser or write access outside MCP tools.
