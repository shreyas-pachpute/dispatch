# Dispatch

**An AI operations team for businesses that run on email, PDFs and spreadsheets.**

Forward Dispatch your inbox, your documents and a connection to the tools you already use. A team of specialist AI agents triages what comes in, extracts what matters, reconciles it against your records, drafts what needs a reply, chases what is overdue and reports what changed. It asks a human only when your policy says it must, shows every step it took, and proves every number back to the source document.

> **Status: building in public.** Phase 0 of 6. Nothing here is a demo of a finished product yet; it is a plan being executed in the open. The plan, the architecture and the evaluation strategy are in [`docs/`](docs/). Progress is tracked in the [issues](../../issues) and the [roadmap](#roadmap).

Built by [Shreyas Pachpute](https://shreyaspachpute.in), one person, end to end. MIT licensed.

---

## Why this exists

Most businesses do not have a data problem. They have a *paperwork* problem. Invoices arrive as PDFs, purchase orders live in a spreadsheet, customers ask questions by email, contracts sit in a shared drive, and a handful of people spend their week moving facts from one of those places to another. Every one of those steps is slow, error-prone and interrupts the people who should be doing something else.

I have spent the last two years building AI into products that do exactly this kind of work: agents inside an ERP-to-CRM integration platform, and the AI backend of a tax-prep platform that turns a shoebox of client documents into a first-reviewed return. Dispatch is what I would build for a business from scratch, with everything I learned, in the open:

- **Grounded, or it does not say it.** Every extracted number, every claim in a drafted reply, links back to the source document or record. If the evidence is not there, the agent says so and escalates.
- **Context engineering, not prompt stuffing.** Each agent gets a purpose-built *context pack* under a token budget: the task, the policies that apply, the evidence that matters, what the business decided in similar cases before, and only the tools it needs. See [`docs/CONTEXT_ENGINEERING.md`](docs/CONTEXT_ENGINEERING.md).
- **Human in the loop by policy, not by vibes.** An approval matrix decides what runs automatically, what only notifies, and what waits for a person. Every outbound action passes an independent Reviewer agent before it reaches the queue.
- **Evaluated in CI.** Golden cases for extraction, routing, policy compliance and reply quality. A change that lowers the score does not merge. See [`docs/EVALS.md`](docs/EVALS.md).
- **Observable.** A control room shows what the team is doing, what is waiting on you, what each decision cost, and the ledger of work it did for you.
- **Your models, your data.** Claude by default. Any OpenAI-compatible endpoint, including self-hosted open-weight models on vLLM, when cost, privacy or control matter more.
- **Integrations via MCP.** Mail, files, sheets, CRM and accounting are Model Context Protocol servers. Add your own system by writing one.

## A day with Dispatch

The demo company is *Northwind Supplies*, a fictional 40-person distributor. The dataset is synthetic and ships with the repo. See [`docs/DEMO.md`](docs/DEMO.md).

| Time  | What happened                                                                                                  | Who handled it                                   |
| ----- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 07:00 | 38 emails and 12 attachments arrived overnight                                                                 | **Dispatcher** routes each one with a context pack |
| 07:02 | 9 supplier invoices extracted into structured fields, each value traced to a page and box on the PDF            | **Intake**                                       |
| 07:04 | 7 invoices match their purchase orders and are queued for payment; 2 do not, and a query to the supplier is drafted | **Accounts**, then **Reviewer**                  |
| 07:05 | A customer asks where order #4821 is; the reply cites the shipment record and the carrier's tracking page        | **Customer**                                     |
| 07:06 | 3 invoices are 30+ days overdue; reminders are scheduled, the one above $2,000 is held for approval              | **Follow-up**                                    |
| 07:10 | "How did we do last week?" is answered with a table and a chart, from SQL the agent wrote and showed              | **Analyst**                                      |
| 09:00 | The owner opens the control room: 31 items handled, 4 waiting for approval, 2 escalated with a reason           | **Control room**                                 |

Every row above is a planned behaviour with an evaluation case behind it, not a marketing claim. When the phase that delivers it ships, the row links to the recorded run.

## The team

| Agent          | Job                                                                                                                   | Asks a human when                                              |
| -------------- | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| **Dispatcher** | Reads every incoming item, decides which specialist owns it, assembles that specialist's context pack                  | Nothing fits, or confidence is low                             |
| **Intake**     | Classifies documents and extracts structured fields against schemas (invoice, PO, contract, form), with source tracing | A required field cannot be found in the document               |
| **Accounts**   | Matches invoices to POs and receipts, flags discrepancies, prepares payment runs, drafts supplier queries               | Amount over the policy limit, or a mismatch it cannot explain  |
| **Customer**   | Drafts replies grounded in order history, policies and the knowledge base                                              | The answer is not in the evidence, or the customer is unhappy |
| **Follow-up**  | Chases overdue invoices, unanswered quotes and expiring contracts on a schedule, never twice for the same thing         | Policy says the recipient or amount needs sign-off             |
| **Analyst**    | Answers business questions with SQL over the operational store, shows the query and a chart                            | Never sends anything; read-only                                |
| **Reviewer**   | Independently checks every outbound action against policy and evidence before it enters the approval queue            | Always: it is the last gate before a person                    |

Full specifications, tools and evaluation cases per agent: [`docs/AGENTS.md`](docs/AGENTS.md).

## How it works

```mermaid
flowchart LR
  subgraph In["Inbound"]
    M[Mail]:::src
    F[Files / drive]:::src
    W[Webhooks / uploads]:::src
  end
  subgraph Core["Dispatch core"]
    Q[(Queue)]
    D[Dispatcher]
    CP[Context pack builder]
    KB[(Knowledge base<br/>Postgres + pgvector)]
    MEM[(Case & entity memory)]
    A1[Intake]
    A2[Accounts]
    A3[Customer]
    A4[Follow-up]
    A5[Analyst]
    R[Reviewer]
    P{Approval policy}
  end
  subgraph Out["Outbound via MCP"]
    MS[Mail]:::mcp
    SH[Sheets / accounting]:::mcp
    CRM[CRM]:::mcp
  end
  CR[Control room]
  M --> Q
  F --> Q
  W --> Q
  Q --> D --> CP
  KB --> CP
  MEM --> CP
  CP --> A1 & A2 & A3 & A4 & A5
  A1 & A2 & A3 & A4 --> R --> P
  P -->|auto| MS & SH & CRM
  P -->|approve| CR
  CR -->|approved| MS & SH & CRM
  CR -->|decision| MEM
  A5 --> CR
  classDef src fill:#eef,stroke:#88a
  classDef mcp fill:#efe,stroke:#8a8
```

The full component and data-flow description, schemas and deployment layout: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Stack

| Layer          | Choice                                                            | Why                                                                                       |
| -------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Agents         | Python, [LangGraph](https://github.com/langchain-ai/langgraph)    | Explicit, inspectable state machines; a debuggable agent is a shippable agent             |
| Models         | Claude (Opus 5 for judgment, Sonnet 5 for volume) via the Anthropic SDK; vLLM for self-hosted open-weight models | Best reasoning by default, a private path when the business needs one |
| Retrieval      | Postgres + pgvector, hybrid BM25 + vector, reranked, cited        | One database for everything; citations are a first-class column, not an afterthought     |
| Integrations   | MCP servers (mail, files, sheets, CRM, accounting)                | Standard interface; swap a CSV-backed server for a real system without touching agents    |
| API and queue  | FastAPI, Postgres-backed job queue                                | Boring, observable, no extra infrastructure to run                                        |
| Control room   | Next.js, TypeScript                                               | Live queue, approvals, traces, ledger                                                     |
| Observability  | OpenTelemetry traces to Langfuse                                  | Every decision has a trace, a cost and a duration                                         |
| Evals          | pytest suites, deterministic checks plus LLM-as-judge, in CI      | Quality is a number that must not go down                                                 |

Decisions and the alternatives considered: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Roadmap

Detailed plan with definition-of-done per phase: [`docs/PLAN.md`](docs/PLAN.md).

- [ ] **Phase 0 · Foundations** — repo, compose stack, API, queue, control room shell, tracing, CI
- [ ] **Phase 1 · Knowledge** — ingestion, chunking, hybrid retrieval with citations, retrieval evals
- [ ] **Phase 2 · Intake** — document classification and schema extraction with source tracing, extraction evals
- [ ] **Phase 3 · Dispatcher, context packs, memory, Reviewer, approval policy**
- [ ] **Phase 4 · Accounts, Customer and Follow-up agents; MCP servers**
- [ ] **Phase 5 · Analyst agent and the control room**
- [ ] **Phase 6 · Hardening** — evals as CI gate, cost controls, PII redaction, vLLM path, demo recording

## Running it

Not yet. Phase 0 delivers `docker compose up` and `make demo`. Until then the repo is documentation.

## Contributing

It is early and the shape is still moving. Issues and questions are welcome; pull requests after Phase 1 when the interfaces settle.

## License

MIT. See [`LICENSE`](LICENSE).
