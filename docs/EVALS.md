# Evaluation

A change that lowers a score does not merge. That is the whole strategy; the rest is mechanics.

## Suites

| Suite            | What it measures                                                             | Grader                                 | Gate (v1 target) |
| ---------------- | ---------------------------------------------------------------------------- | -------------------------------------- | ---------------- |
| `retrieval`      | recall@5, citation precision over labelled questions                         | Deterministic                          | recall@5 ≥ 0.90  |
| `intake`         | Classification accuracy; field accuracy by type; fabricated fields           | Deterministic (normalised comparison)  | ≥ 0.97 / ≥ 0.95 / 0 |
| `routing`        | Dispatcher owner accuracy; escalation precision                              | Deterministic                          | ≥ 0.95           |
| `policy`         | Policy violations that reached the approval queue                            | Deterministic                          | 0                |
| `reviewer`       | Catch rate on seeded bad actions; false-block rate on good actions           | Deterministic                          | 100% / ≤ 5%      |
| `accounts`       | Three-way match verdicts on seeded discrepancies                             | Deterministic                          | ≥ 0.95           |
| `customer`       | Faithfulness (claims supported by citations); tone; escalation precision     | LLM judge with rubric + deterministic  | faithfulness ≥ 0.98 |
| `followup`       | Idempotency under replay; schedule correctness; opt-out respect              | Deterministic                          | 100%             |
| `analyst`        | Result-set equality; SQL safety                                              | Deterministic                          | ≥ 0.90 / 100%    |
| `context`        | Context contract: the pack contains the fact the answer depends on           | Deterministic                          | ≥ 0.95           |
| `cost`           | Tokens and dollars per item by type; cache hit rate                          | Measured                               | Reported, alerted on +20% |

## Datasets

All synthetic, all in the repo, all versioned. The Northwind corpus (documents, emails, records) plus labelled sets per suite. Labels live next to the data as JSON with a `version` and a `notes` field explaining tricky cases. Adding a case is a pull request like any other.

## Graders

- **Deterministic first.** Exact or normalised comparison wherever the answer is a value, a class, a set or a boolean. Numbers are compared after currency and thousands-separator normalisation; dates after parsing.
- **LLM judge only where language is the output.** Faithfulness and tone use a rubric, a fixed judge model and version, and a calibration set of 30 human-graded examples that the judge must agree with at ≥ 0.9 before its scores count.
- **No self-grading.** The judge never runs on the same model version as the agent under test in the same run.

## Running

```
make eval            # all suites, prints a scorecard, writes evals/reports/<commit>.json
make eval-intake     # one suite
make eval-diff BASE=main   # scorecard delta against a base commit
```

CI runs the full set on every pull request against the Northwind data. Model calls in CI use a recorded-response cache keyed on the exact request, so a PR that does not change prompts or packs costs nothing and runs in minutes; changed requests hit the model and the cache is updated by a maintainer.

## Reporting

Each run writes a scorecard: suite, metric, value, gate, pass/fail, cost, duration, commit. The control room shows the last scorecard and the trend. Regressions are the first thing on the page, above features.

## What gets an eval before it gets a feature

New agent behaviour is added in this order: labelled cases, a failing test, the behaviour, a passing test, the pull request. It is slower for the first week and faster every week after.
