# Decisions

Short architecture decision records. Each one names the alternatives so future-me can tell whether the reason still holds.

## ADR-001 · Agents as explicit graphs (LangGraph)

**Decision.** Every agent is a LangGraph state machine with named nodes and typed state.
**Alternatives.** Free-form tool-calling loops; a hosted agent platform.
**Why.** A debuggable agent is a shippable agent. Explicit state makes replay, tracing and unit-testing single nodes possible, and makes the hand-offs between agents inspectable. I learned this building agents into a product with paying customers: if a teammate cannot debug it, it is not done.

## ADR-002 · One Postgres for everything

**Decision.** Postgres 16 with pgvector holds the operational store, knowledge base, queue, memory and eval results.
**Alternatives.** A separate vector database; Redis for the queue; an object store for documents.
**Why.** One business, one VM, one backup. Hybrid search (full text plus vector) in one query with a join to the citation is simpler and more correct than stitching two stores. Documents themselves live on disk or S3-compatible storage by reference.

## ADR-003 · Integrations are MCP servers

**Decision.** Mail, files, sheets, CRM and accounting are Model Context Protocol servers with strict tool schemas. Reference implementations are CSV-backed.
**Alternatives.** Direct SDK calls inside agents; a plugin interface of my own.
**Why.** A standard interface means a business can swap the CSV reference for its real system without touching agent code, and tools can be loaded per role and deferred. It also keeps agents from importing integration code, which is where prompt-injection turns into damage.

## ADR-004 · Claude by default, vLLM as the private path

**Decision.** Claude Opus 5 for judgment-heavy agents (Dispatcher, Accounts, Customer, Reviewer) with adaptive thinking; Claude Sonnet 5 for high-volume extraction and for LLM judges; Haiku 4.5 for cheap classification where evals show no loss. An OpenAI-compatible adapter serves self-hosted open-weight models on vLLM behind the same gateway.
**Alternatives.** One model everywhere; open-weight only.
**Why.** Reasoning quality is the product for the gate and the money paths; volume paths are where a cheaper model earns its keep, and the evals decide, not taste. Some businesses cannot send documents to an API at all; I have served models on a company's own GPUs before and the path has to exist from day one, not as a retrofit.

## ADR-005 · Approval policy is data

**Decision.** The approval matrix is YAML: action type × amount band × counterparty class → `auto` | `notify` | `approve`. Evaluated in code, never by the model.
**Alternatives.** Let the agent decide when to ask; a visual rule builder.
**Why.** An owner must be able to read the rules in one screen and change them without a deploy. A model deciding when to ask a human is the failure mode this project exists to avoid.

## ADR-006 · Evals are a merge gate

**Decision.** CI runs every suite on every pull request with recorded model responses; thresholds block merges.
**Alternatives.** Manual QA; evals as a dashboard only.
**Why.** Agent systems regress silently. A number that must not go down is the only thing that has ever kept mine honest.

## ADR-007 · Memory is written only from outcomes

**Decision.** Case and entity memory are written from executed, unreverted actions and human decisions, never from an agent's intermediate beliefs.
**Alternatives.** Let agents write notes to memory as they work.
**Why.** Self-written memory compounds mistakes. Outcome-written memory compounds the business's judgment.

## ADR-008 · No shell, no browser

**Decision.** Agents act only through MCP tools with schemas. There is no shell tool, no browsing agent.
**Alternatives.** General computer-use agents.
**Why.** The blast radius of a back-office agent has to be enumerable. Every action type appears in the policy matrix; a shell cannot.
