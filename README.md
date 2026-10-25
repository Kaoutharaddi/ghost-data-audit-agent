# Ghost Data Audit Agent

**An AI-assisted agent that proves whether a user's personal data has really been deleted — or is still haunting your systems.**

Built for the [HackerRank Orchestrate (October 2026)](https://github.com/interviewstreet/hackerrank-orchestrate-october26) challenge *"Ghost Data Still Exists"*.

---

## The problem

A (fictional) online shop marked a customer's deletion request as **"Complete"**. Weeks later the customer tried to sign up again and got *"This email is already registered."*

The investigation found their personal data still sitting in secondary databases, support attachments, search indexes, analytics exports, logs, archives and backups. Worse, a sync job had **recreated** data that the main account service had successfully deleted.

The workflow's status was a claim, not proof. This agent checks the evidence instead.

For every deletion request it decides:

| Question | Output |
|---|---|
| Is the deletion really done? | `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE` (the person never asked for deletion) |
| What still exposes the user? | `FINDING` steps: remaining records, exposed PII, active data flows, restore risks |
| How do we fix it? | `REMEDIATION` steps, in a safe order: stop flows and block restores first, then delete / redact / anonymize |
| How do we prove it? | `VERIFICATION` steps, each citing the evidence it relies on |

It also has to **keep** what the policy says must be kept: security audit logs, legal holds and account tombstones are retained, but with direct identifiers stripped.

---

## Approach

**Deterministic core, AI only where the input is fuzzy.**

The evidence is mostly structured (CSVs, logs, JSON, SQL snapshots), and every step has to cite an exact file or event. So the core is plain Python rules: reproducible, auditable and free to run. LLMs are used only for the two inputs that rules can't read reliably:

| Task | Model | Why |
|---|---|---|
| Reading screenshots and PDFs (admin consoles, retention registers, support attachments) | `claude-opus-5-5` (vision) | Unstructured visual evidence |
| Classifying whether a free-text request is actually a deletion request (multilingual, with traps like *"correct my name without removing anything"*) | `claude-haiku-5-5` (batched) | Intent is fuzzy; a small model is enough and costs almost nothing |

All model outputs are **cached**, so the final run makes zero API calls and produces the same `output.csv` byte for byte, with or without an API key.

### Pipeline

```
deletion_requests.csv
   │
   ▼
1. Intent         ── is this a deletion request? (Haiku, cached; generic rule fallback)
   │
   ▼
2. Identity       ── current + historical emails, phones, email hashes (user_audit_log.csv)
   │
   ▼
3. Evidence       ── 94 files (6 log dialects, JSON, SQL run in SQLite, handover text,
   │                 vision extractions) normalised into one observation format
   ▼
4. Linking        ── which resources belong to this person: identifiers, catalog links,
   │                 restore/sync lineage, deletion-worker batches
   ▼
5. State at audit ── latest observation not after audit_at, from files captured by audit_at
   │
   ▼
6. Policy         ── deletion_rules.csv: rule, condition (retention, legal hold, exclusive
   │                 attachment, shared context…) and required treatment per record
   ▼
7. Plan           ── FINDING → REMEDIATION → VERIFICATION, ordered by the contract
   │
   ▼
8. Contract check ── output is validated before writing; any violation aborts the run
   │
   ▼
output.csv
```

### Key design decisions

- **Reported statuses are never proof.** A `SUCCESS` from the deletion workflow is only used to link resources, never as evidence of the final state.
- **Unknown is not compliant.** If there is no evidence about a backup's restore permission or a data flow's switch, the case cannot be `COMPLETE`.
- **`COMPLETE` requires evidence for every step.** Otherwise the case is `INCOMPLETE` with pending verifications.
- **Policy comes from the data, not the code.** Treatments and conditions are read from `deletion_rules.csv`. Changing one rule in the CSV (e.g. support messages from REDACT to DELETE) changes 155 output rows.
- **Batch parameters are learned, not hard-coded.** Timing gaps and windows used to match identifier-free records to requests are learned from the data on every run and validated with leave-one-out.
- **No guessing on real ambiguity.** Tie-breaks are limited to two candidates. With three or more, a wrong guess costs more in expectation than a missing step, and ordering heuristics tested at chance level.

---

## Results

On the 518 evaluation requests: **355 INCOMPLETE · 153 COMPLETE · 10 NOT_APPLICABLE**, with 0 contract violations.

On the 32 public sample cases:

| Metric | Score |
|---|---|
| Status accuracy | 100 % (32/32) |
| Step-level F1 | 0.990 |
| Exact sequence match | 87.5 % (28/32) |

> These are **in-sample** numbers: the samples were also used during development. Expect lower accuracy on unseen data.

The 4 imperfect sample cases each miss one verification step. Three of the missing records do not appear anywhere in the visible data. The fourth is a genuine three-way tie in a deletion batch, which the agent deliberately does not guess.

Additional checks:
- 3 **fully synthetic test cases** (fictional people, own mini-dataset, real policy file), with expected answers written *before* running the system: two historical emails, a sync that recreates data after deletion, and an active legal hold. 3/3 pass.
- Intent classifier: 32/32 on samples and 18/18 on hand-written multilingual edge cases.

### Cost

| Model | Use | Calls | Input / output tokens | Cost |
|---|---|---|---|---|
| `claude-opus-5-5` | Media extraction (11 images, 4 PDFs) | 15 | 48,039 / 8,550 | $0.36 |
| `claude-haiku-5-5` | Intent classification (416 texts, batched) | 22 | 42,893 / 7,967 | $0.008 |
| — | Final full run (all from cache) | 0 | 0 | $0.00 |

That is well under one cent per request. Full breakdown in [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

---

## Failures found and fixed

Most of the value came from finding what was wrong. An independent review (a fresh AI session that hadn't built the code) audited the solution against the problem statement. Among other things, it caught:

| Problem | Fix |
|---|---|
| Image/PDF extractions had been written by hand during development, and the docs claimed otherwise | Re-extracted through the API with `media.py`, logging real token counts; catalog cross-referencing moved into code |
| Batch timing constants (390 s / 449 s gaps, fixed windows) were fingerprints of this dataset | Learned per run from identifier-linked examples, with leave-one-out validation and margins |
| `deletion_rules.csv` was loaded but a hand-written table decided the policy | New `policy.py` evaluates each rule and its condition from the CSV |
| Intent rules contained phrases copied from evaluation requests (circular 10/10) | Replaced with a Haiku classifier whose prompt was written without reading evaluation texts; generic fallback rules |
| Engine treated "unknown" as compliant and allowed `COMPLETE` without evidence | Fixed, plus a contract validator that aborts invalid output |
| Custom test cases were translations of samples | Replaced with a synthetic dataset, expected outputs written before running |

Details in [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).

## Known limitations

- About 70 account links still come from two-way tie-breaks, and roughly half are likely wrong.
- Some learned parameters rest on few examples (e.g. one mechanism has only 4).
- Condition tests in `policy.py` are chosen by matching the condition text in the CSV, so rewording a rule may stop it from being recognised.
- Evidence observed before `requested_at` is ignored.
- Absence proofs assume database snapshots are complete.

---

## Running it

Requires **Python 3.9+** and the standard library only. The `anthropic` SDK is needed only to regenerate the cached model outputs.

The dataset is not included. Get it from the [official challenge repo](https://github.com/interviewstreet/hackerrank-orchestrate-october26) and place the `dataset/` folder at the repository root.

```bash
# Generate output.csv (uses cached model outputs, no API key needed)
python3 code/main.py

# Run the evaluation (samples + custom cases)
python3 code/evaluation/main.py --all

# Optional: regenerate model outputs (needs ANTHROPIC_API_KEY)
python3 code/ghost/media.py --out code/data/media_extractions.api_v2.json
python3 code/evaluation/intent_eval.py --eval-diff
```

## Project structure

```
code/
├── main.py                 # entry point → output.csv
├── ghost/
│   ├── intent.py           # deletion-intent classification (LLM + fallback rules)
│   ├── identity.py         # current and historical identifier resolution
│   ├── logparse.py         # parsers for the log dialects
│   ├── evidence.py         # evidence normalisation + catalog cross-referencing
│   ├── activity.py         # system_activity.csv handling
│   ├── batches.py          # deletion-worker batch matching
│   ├── engine.py           # linking, state at audit_at, step planning
│   ├── policy.py           # deletion_rules.csv evaluation
│   ├── contract.py         # output contract validator
│   ├── llm.py              # model client, caching, usage logging
│   └── media.py            # vision extraction of images and PDFs
├── data/                   # cached model outputs
└── evaluation/             # scorer, custom cases, synthetic dataset, reports
```

---

## About

Built by **Kaouthar Addi**, Computer Engineering (URV), working on data, AI, blockchain and fintech.

Developed with Claude Code as an AI pair programmer. I set the direction, reviewed each change against measured before/after metrics, and used an independent AI review to catch overfitting and inaccurate claims.
