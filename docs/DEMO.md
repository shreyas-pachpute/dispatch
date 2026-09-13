# Demo scenario: Northwind Supplies

A fictional 40-person distributor of office and warehouse supplies. Everything about it is synthetic and generated for this repo; any resemblance to a real company is accidental.

## The business

- 220 customers, 35 suppliers, 6 employees who touch paperwork
- Orders come by email and a web form; invoices from suppliers arrive as PDF attachments; customer invoices go out from an accounting system
- Records live in three CSV-backed systems for the demo: `crm.csv` (customers, contacts, orders), `accounting.csv` (invoices, POs, receipts, payments), `contracts/` (PDF supplier agreements)
- Policies: a payment approval limit of $2,000; a 2% price variance tolerance for preferred suppliers, 0% for others; follow-ups at 7, 14 and 30 days overdue; quiet hours 19:00 to 08:00; two customers flagged as sensitive accounts

## The dataset

| Set            | Count | Notes                                                                            |
| -------------- | ----- | -------------------------------------------------------------------------------- |
| Supplier invoices | 25 | 18 clean, 3 price variances, 2 quantity mismatches, 1 duplicate, 1 scanned sideways |
| Purchase orders   | 30 | Including 5 with no invoice yet                                                  |
| Receipts          | 22 |                                                                                  |
| Customer emails   | 40 | Order status, returns, complaints, quote requests, one legal-sounding thread, two injection attempts hidden in signatures |
| Contracts         | 6  | Two expiring within 30 days                                                      |
| Knowledge docs    | 15 | Return policy, shipping terms, product FAQs, internal SOPs                       |

Labels for every item (expected class, fields, owner agent, expected action and policy decision) live next to the data and drive the evaluation suites.

## A day, step by step

1. `make demo` loads the dataset and replays the overnight inbox in order.
2. The queue fills; the Dispatcher routes each item. The control room shows owners and confidence.
3. Intake extracts the 25 invoices. The sideways scan is OCR'd and rotated; the field view shows each value highlighted on its page.
4. Accounts matches invoices to POs and receipts. 18 queue for payment automatically (under the limit, no variance). 3 variances within tolerance for preferred suppliers pass; the others hold with a drafted supplier query. The duplicate is held with a reason.
5. Customer drafts replies. "Where is order #4821" cites the shipment row. The legal-sounding thread escalates with a summary. The two injection attempts are caught by the Reviewer and shown in the control room with the offending text highlighted.
6. Follow-up schedules reminders for overdue invoices; the one over $2,000 waits for approval; nothing goes to the sensitive accounts without approval.
7. The owner asks the Analyst "which suppliers had variances this month" and gets a table, the SQL and a bar chart.
8. The ledger shows items handled by type, approvals outstanding, spend, and a time estimate per item type (an estimate you configure, shown as one).

## What the demo is for

It is the evaluation dataset, the onboarding tutorial and the regression suite at once. If the demo day runs clean, the system works; if it does not, the failing step names the layer that broke.
