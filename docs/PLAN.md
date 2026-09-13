# Build plan

Seven phases, each with a definition of done that a stranger could verify by running one command. Phases are sequential because each one is the foundation of the next; the estimates assume evenings and weekends, one person.

Progress is tracked as GitHub issues, one per phase, with the checklist below copied into each issue.

## Phase 0 · Foundations

**Goal:** a running skeleton that does nothing useful and is fully observable.

- [ ] Monorepo layout: `apps/api` (FastAPI), `apps/worker` (LangGraph runners), `apps/control-room` (Next.js), `packages/core` (schemas, policies, context packs), `packages/mcp-*` (one package per MCP server), `evals/`
- [ ] `docker compose up` brings up Postgres 16 with pgvector, the API, the worker, the control room and Langfuse
- [ ] Postgres-backed job queue (`FOR UPDATE SKIP LOCKED`), with retries, dead-letter and idempotency keys
- [ ] Data model v1 migrated: `items`, `cases`, `actions`, `approvals`, `documents`, `chunks`, `entities`, `memories`, `traces`
- [ ] Model gateway: Anthropic SDK client with adaptive thinking, prompt caching on the stable prefix, cost accounting per call; OpenAI-compatible adapter for vLLM behind the same interface
- [ ] OpenTelemetry tracing from API and worker into Langfuse; every job has a trace id visible in the control room
- [ ] Control room shell: queue view, item detail, trace link
- [ ] CI: lint, type-check, unit tests, compose smoke test
- [ ] `make demo` loads the Northwind dataset (documents and emails only; no agents yet)

**Done when:** a fresh clone runs `docker compose up && make demo`, 50 items appear in the queue, each with a trace.

## Phase 1 · Knowledge

**Goal:** the company's documents become citable evidence.

- [ ] Ingestion pipeline: PDF (text and scanned via OCR), DOCX, email bodies, CSV rows; content-addressed storage; dedupe
- [ ] Structure-aware chunking (headings, tables, page boundaries) with page and bounding-box provenance where available
- [ ] Hybrid retrieval: BM25 (Postgres full-text) plus vector (pgvector), reciprocal rank fusion, cross-encoder rerank
- [ ] Citation objects: `{document_id, page, span, quote}` returned with every hit; a quote must appear verbatim in the source
- [ ] Retrieval evals: 60 question/answer pairs over the Northwind corpus; recall@5 and citation precision reported in CI
- [ ] Knowledge view in the control room: search, hit list, highlighted source

**Done when:** `make eval-retrieval` prints recall@5 ≥ 0.9 on the Northwind set and every hit renders its highlighted source.

## Phase 2 · Intake

**Goal:** any document in any condition becomes structured, verifiable data.

- [ ] Document classifier (invoice, purchase order, receipt, contract, form, correspondence, other) with confidence
- [ ] Extraction against typed schemas (Pydantic) using structured outputs; every field carries a source citation
- [ ] Required-field policy: missing required fields escalate instead of guessing
- [ ] Update flow: a corrected document re-runs extraction and diffs against the previous version
- [ ] Extraction evals: 120 labelled documents, field-level accuracy by document type; confusion matrix for classification
- [ ] Control room: document view with field-to-source highlighting

**Done when:** `make eval-intake` reports classification accuracy ≥ 0.97 and invoice field accuracy ≥ 0.95, with zero fabricated fields (a field with no citation counts as fabricated).

## Phase 3 · Dispatcher, context packs, memory, Reviewer, policy

**Goal:** the core of the team: routing, context, judgment and the gate.

- [ ] Dispatcher graph: classify intent, choose owner agent, assemble context pack, hand off; low confidence escalates
- [ ] Context pack builder with per-agent budgets and layers (task, policies, evidence, memory, tools), deterministic ordering for prompt caching
- [ ] Case memory: resolved cases embedded and retrievable ("what did we do last time"); entity memory: facts per customer and supplier with provenance
- [ ] Memory write-back only after a human decision or an automatic action that was not reverted
- [ ] Approval policy as data: YAML matrix by action type, amount and counterparty → `auto` / `notify` / `approve`
- [ ] Reviewer graph: independent check of every outbound action for policy compliance, evidence support and tone; can block, amend or pass
- [ ] Routing evals (200 items), policy compliance evals (0 violations tolerated), Reviewer catch-rate on seeded bad actions

**Done when:** every seeded policy violation is caught before the approval queue, and routing accuracy ≥ 0.95.

## Phase 4 · Accounts, Customer, Follow-up, MCP servers

**Goal:** the agents that do the work, and the hands they use.

- [ ] MCP servers: `mcp-mail` (IMAP/SMTP, Gmail later), `mcp-files` (local and Drive), `mcp-sheets` (CSV and Google Sheets), `mcp-crm` (CSV-backed reference implementation), `mcp-accounting` (CSV-backed reference implementation)
- [ ] Accounts agent: three-way match (invoice, PO, receipt), discrepancy explanation, payment run proposal, supplier query drafts
- [ ] Customer agent: grounded replies with citations, sentiment-aware escalation, never invents order facts
- [ ] Follow-up agent: schedules from policy, idempotent (never chases the same thing twice), respects quiet hours and opt-outs
- [ ] Agent evals: match accuracy on seeded discrepancies, reply faithfulness (judge), follow-up idempotency

**Done when:** the Northwind day in the README runs end to end with approvals in the control room and no unsupported claim in any draft.

## Phase 5 · Analyst and the control room

**Goal:** the owner's view.

- [ ] Analyst agent: text-to-SQL over a read-only replica with a schema card, query shown with the answer, chart generation
- [ ] Control room: live queue, approvals with one-click approve/amend/reject, per-item trace and cost, the work ledger (items handled, time estimate per item type, spend)
- [ ] Notifications: daily digest email, escalation alerts
- [ ] Analyst evals: 50 questions with expected result sets; SQL safety checks (no writes, row limits)

**Done when:** the owner can run a week of Northwind, approve from the control room, and read the ledger.

## Phase 6 · Hardening

**Goal:** something a business could actually run.

- [ ] Evals as a CI gate with thresholds per suite; scorecards published per commit
- [ ] Cost controls: per-item and per-day budgets, model routing by task class, cache hit-rate monitoring
- [ ] PII redaction in traces and logs; secrets via environment only
- [ ] vLLM path validated end to end with an open-weight model; documented accuracy delta versus Claude
- [ ] Backups, migrations, upgrade notes
- [ ] Demo recording and a written walkthrough

**Done when:** a stranger can deploy it on one VM with the README alone.

## Out of scope for v1

- Multi-tenant SaaS: one deployment per business
- A visual workflow builder: policies are YAML and code on purpose
- Voice, chat widgets, browsing agents

## Principles that outrank the plan

1. Nothing ships without an eval that would catch its regression.
2. A number without a citation is a bug.
3. When in doubt, escalate to a person and remember the answer.
