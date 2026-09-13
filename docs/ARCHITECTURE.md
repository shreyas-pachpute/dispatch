# Architecture

Dispatch is one deployment per business: a small set of services around one Postgres database. Everything an agent does is a job on a queue, everything a job does is a trace, and everything a trace produced is stored with a citation.

## Components

| Service          | Runtime            | Responsibility                                                                                 |
| ---------------- | ------------------ | ---------------------------------------------------------------------------------------------- |
| `api`            | FastAPI            | Inbound webhooks and uploads, control-room API, approvals, policy evaluation                    |
| `worker`         | Python, LangGraph  | Runs agent graphs from the queue; one process, many concurrent jobs                             |
| `control-room`   | Next.js            | Queue, item detail, approvals, knowledge search, ledger, traces                                  |
| `mcp-*`          | Python MCP servers | One per integration: mail, files, sheets, CRM, accounting. Reference implementations are CSV-backed |
| `postgres`       | Postgres 16 + pgvector | Operational store, knowledge base, queue, memory, evals                                      |
| `langfuse`       | Langfuse           | Trace storage and viewing (OpenTelemetry in)                                                    |
| `vllm` (optional) | vLLM              | Self-hosted open-weight model behind an OpenAI-compatible endpoint                              |

## Data flow

1. **Inbound.** Mail is polled through `mcp-mail`; files are watched through `mcp-files`; anything else arrives through the API. Each arrival becomes an `item` (immutable) and a `job` on the queue with an idempotency key (hash of source and content).
2. **Dispatch.** The worker runs the Dispatcher graph: intent classification, owner selection, context pack assembly. The pack is stored with the job so the exact model input is reproducible.
3. **Specialist.** The owner agent runs its graph. Tool calls go through MCP. Every retrieval returns citations; every extracted field carries one.
4. **Review.** Proposed outbound actions are handed to the Reviewer graph, which reads the same evidence and the policy, and returns pass / amend / block with a reason.
5. **Policy.** The approval matrix maps `(action type, amount, counterparty class)` to `auto`, `notify` or `approve`. `auto` executes and notifies; `notify` executes and flags; `approve` waits in the queue.
6. **Execute.** Approved actions run through MCP. Results are written back as `actions` with the tool response attached.
7. **Remember.** A resolved case (and the human's decision, if any) is written to case memory; new facts about a customer or supplier are written to entity memory with provenance.
8. **Report.** The control room reads the ledger: items by type, time estimate per type (configurable, shown as an estimate), model spend per item, approvals outstanding.

## Data model (v1)

```
items        id, source, external_id, received_at, kind, raw_ref, idempotency_key
documents    id, item_id, sha256, mime, pages, text_ref, ocr_used
chunks       id, document_id, page, bbox, text, tsv (full text), embedding (vector)
cases        id, item_id, owner_agent, status, context_pack_ref, trace_id, opened_at, closed_at
extractions  id, document_id, schema, version, fields (jsonb: value + citation per field), confidence
actions      id, case_id, type, payload, policy_decision, reviewer_verdict, status, executed_at, result
approvals    id, action_id, decided_by, decision, note, decided_at
entities     id, kind (customer|supplier|contract), key, facts (jsonb with provenance)
memories     id, case_id, summary, decision, embedding, created_at
evals        id, suite, commit, metric, value, run_at
```

A citation is `{document_id, page, span: [start, end], quote}` and is validated on write: the quote must be a substring of the page text.

## Queue

A `jobs` table with `FOR UPDATE SKIP LOCKED` polling. Jobs carry `attempt`, `max_attempts`, `not_before`, `dead_at`. Retries use exponential backoff; a job that fails `max_attempts` times moves to a dead-letter state visible in the control room. No Redis in v1; Postgres is enough at the scale a single business produces, and one fewer thing to run.

## Model gateway

One module wraps model access so agents never import an SDK directly:

- Claude through the official Anthropic SDK: adaptive thinking on, effort set per agent (Dispatcher and Reviewer high, Intake medium, classification low), structured outputs for anything that must parse, prompt caching on the stable prefix (system prompt, policies, tool schemas), server-side fallbacks enabled.
- An OpenAI-compatible adapter for vLLM with the same interface, minus features the endpoint does not support (the gateway degrades explicitly, never silently).
- Every call records input, cached and output tokens, cost and latency onto the trace.

## Context packs

The unit of work an agent receives. Built once per job, stored, and replayable. Layers, budgets and ordering are specified in [`CONTEXT_ENGINEERING.md`](CONTEXT_ENGINEERING.md).

## MCP servers

Each integration is a standalone MCP server exposing tools with strict JSON schemas and a short description written for the model. Reference implementations are CSV-backed so the whole system runs from a folder of files; a real system (Gmail, Google Drive, HubSpot, QuickBooks) is a new server with the same tool names. Agents load only the tools their role needs; the rest are deferred.

## Observability

OpenTelemetry spans for job, graph node, model call and tool call, exported to Langfuse. Span attributes include token counts, cost, cache hits, policy decision and reviewer verdict. PII is redacted at the exporter in Phase 6.

## Deployment

`docker-compose.yml` for a single VM. Environment variables for model keys and integration credentials. Nightly Postgres dump. No Kubernetes in v1.

## Security posture (v1)

- Agents cannot execute anything outside MCP tools; there is no shell tool.
- Outbound actions always pass the Reviewer and the policy matrix.
- The Analyst runs against a read-only role with statement timeouts and row limits.
- Prompt injection defence: retrieved content and email bodies are wrapped as untrusted data blocks with explicit instructions; the Reviewer checks for instructions that originated in evidence rather than policy.
