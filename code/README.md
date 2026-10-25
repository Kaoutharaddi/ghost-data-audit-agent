# Ghost Data Still Exists — deletion-audit agent

For each deletion request, the agent decides whether the subject's personal data is really gone at `audit_at`. It then emits the findings, ordered remediation steps and verification checks required by the challenge contract.

## Setup and run

The agent needs Python 3.9 or later. It uses only the standard library; `sqlite3` is built in.

```bash
# from the repository root (the folder that contains dataset/)
python3 code/main.py                       # writes ./output.csv (518 rows)
python3 code/main.py --trace traces.jsonl  # also writes per-case reasoning traces
python3 code/evaluation/main.py --all      # scores the public samples, the custom cases and the synthetic custom dataset
python3 code/ghost/contract.py output.csv   # checks output.csv against the output contract
```

Optional extras need `pip install anthropic` and `ANTHROPIC_API_KEY` set in your environment. Never commit the key.

- `python3 code/ghost/media.py --dry-run` lists the 15 screenshots and PDFs and how each would be sent; no key needed.
- `python3 code/ghost/media.py --out <file>` extracts the media facts with Claude vision.
  - The final output uses `code/data/media_extractions.api_v2.json`, which this command produced; it is committed, so `main.py` needs no key.
  - It never overwrites the hand-written reference `code/data/media_extractions.json`.
  - It appends the real tokens of every call to `code/evaluation/media_api_usage.json`.
- `python3 code/ghost/compare_media.py --api <file> --impact` compares an API extraction with the hand-written reference, field by field. It also lists the evaluation cases whose output would change and shows the sample metrics with each file.
- `python3 code/main.py --media <file>` runs the agent with a different media facts file.
- Intent: `main.py` first looks up every request text in `code/data/llm_cache.json`, which `claude-haiku-5-5` filled in one keyed run.
  - With a key, uncached texts are sent to Haiku in batches of 20, and every call is metered in `code/evaluation/llm_usage_log.json`.
  - Without a key, uncached texts fall back to generic rules.
  - The committed cache covers every evaluation and sample request, so the final output needs no key.
- `python3 code/evaluation/intent_eval.py [--eval-diff]` scores Haiku and the fallback rules on the samples and on 18 invented requests. `--eval-diff` also lists evaluation requests where Haiku disagrees with the v1 rules.
- To change models, set `GHOST_INTENT_MODEL` (default `claude-haiku-5-5`) or `GHOST_VISION_MODEL` (default `claude-opus-5-5`).

## Architecture

```
dataset/ ──► ghost/evidence.py   every file becomes uniform Observations
             (logparse.py: 6 log dialects; JSON bundles/exports; SQLite snapshots incl.
              primary vs replica tables; handover text; Claude-vision media facts,
              with catalog-mapped references resolved in code)
         ──► ghost/identity.py   request email → user_id + all current/historical emails,
                                 phones, email hashes (sha256 of every email)
         ──► ghost/activity.py   system_activity index: catalog links, restore/sync lineage,
                                 deletion-worker ops (claims, never proof of state)
         ──► ghost/learn.py      learns the batch timing from identifier-proven links
                                 (windows, cadence, status mapping; leave-one-out checked)
         ──► ghost/batches.py    global pass: splits each deletion-worker batch between the
                                 requests that share it (recovers identifier-free accounts
                                 and anonymized analytics)
         ──► ghost/intent.py     does the text authorize deletion? (claude-haiku-5-5 labels, cached;
                                 generic multilingual rules only for uncached texts)
         ──► ghost/policy.py     deletion_rules.csv: category → rule, condition test, required action
         ──► ghost/engine.py     link → state at audit_at → policy → ordered steps
         ──► ghost/contract.py   output-contract validator; main.py refuses to write a violating output
```

### Investigation

1. **Intent.** A `claude-haiku-5-5` label decides whether the request authorizes deletion (DELETION vs ACCESS / CORRECTION / EXPORT / MARKETING_OPT_OUT / OTHER). Generic rules are used only for texts with no cached label. Access, export, correction, marketing opt-out and login-help requests, and explicit refusals ("do not close the account"), become `NOT_APPLICABLE` with `[]`.
2. **Identity.** The request email may be current or historical. It resolves to the stable `user_id` and every identifier the subject ever had.
3. **Linking.** Only observations between `requested_at` and `audit_at` are used. A resource belongs to the subject if any of these hold:
   - An observation carries one of the subject's identifiers, including lookup keys inside the same trace.
   - A data-catalog link connects it to the subject.
   - It is a search document derived from the subject's account.
   - Restore or connector lineage connects it to the subject.
   - The deletion batch assigns it (see below).
4. **Batch recovery.** After deletion, accounts and anonymized analytics carry no identifier. The deletion worker processes all requests reported at the same time in one batch, and two facts identify each request's share:
   - An account submission's result mirrors the request's `reported_status`.
   - One request's policy actions form a chain with a fixed cadence.

   Neither the timing windows, the cadence nor the status mapping is hard-coded. `ghost/learn.py` measures them on every run from the links that identifiers already prove, checks each window by leave-one-out, and switches off any mechanism with too little support. On this dataset it recovers a 390 s / 449 s cadence. Candidates read back by the request's own audit evidence are preferred. Ties are broken only between two options.
5. **State at `audit_at`.** The agent takes the latest observation that is not after `audit_at` and comes from a file captured by `audit_at`. Workflow results are never treated as proof. An absence with no direct readback is proven by the latest full-collection snapshot that does not list the record. A state the evidence does not show (for example an archive with no visible restore flag) stays unknown. A case is `COMPLETE` only if every required state is proven by citable evidence.
6. **Policy.** The data category comes from the evidence (record type, attachment or contact hints) or from the action the workflow attempted, otherwise from the resource family. `ghost/policy.py` reads each category's rule, condition and action from `deletion_rules.csv`. A condition is tested against the evidence (retention or hold dates, catalog exclusivity, shared context), and the test is chosen by the condition's wording. With the current CSV, the required treatments are:
   - Profiles, search entries, exports, attachments and marketing profiles must be **deleted**.
   - Support messages and account tombstones must be **redacted**: the tombstone keeps only `user_id`.
   - Analytics must be **anonymized**.
   - Security audit records and legal holds are **retained**.
   - Backups must have restore **blocked**.
   - Connectors must be **disabled**.

### Remediation planning

- Steps are ordered by phase (FINDING, then REMEDIATION, then VERIFICATION), then by the operation order in the contract, then by `system_id` and `resource_id`.
- Inside remediation this order disables data flows and blocks restores before any record is deleted, redacted or anonymized, so nothing can be recreated mid-remediation.
- Every linked resource gets a verification of its required final state. That includes records that were already compliant and retained records (VERIFY_RECORD_PRESENT).
- `COMPLETE` cases cite the evidence that proves each state.
- In `INCOMPLETE` cases, remediation steps and the checks after remediation carry an empty `evidence_id`.
- Sometimes a record the policy keeps (retained, redacted, anonymized or tombstone) is found absent. The contract has no operation to restore it, so its check stays pending and the case is `INCOMPLETE`. An absence never counts as proof of the kept state.
- Output rows are sorted by case number (`case_33` … `case_548`, numeric order). This is also the order of `dataset/deletion_requests.csv`.

## Files

| Path | Purpose |
|---|---|
| `main.py` | Entry point (writes `output.csv`) |
| `ghost/` | Agent modules (see architecture) |
| `data/media_extractions.api_v2.json` | Media facts used by the agent, from the metered Claude vision run (prompt v2) |
| `data/media_extractions.json` | Hand-written reference extraction (development, no API), used only for comparison |
| `data/media_extractions.api.json` | First API run (prompt v1), kept for the comparison history |
| `data/llm_cache.json` | Cached `claude-haiku-5-5` intent labels (416 texts) |
| `evaluation/main.py` | Scorer: status, step P/R/F1, field and sequence accuracy |
| `evaluation/custom_cases.csv`, `custom_expected.csv` | Three custom cases on the participant dataset |
| `evaluation/custom_dataset/` | Synthetic dataset of our own with three more cases; expected answers written before the run (see its README) |
| `evaluation/intent_eval.py`, `intent_cases.csv` | Intent classifier check: samples + 18 invented requests |
| `evaluation/evaluation_report.md` | Results, failures found, improvements |
| `evaluation/usage_report.md` | Model calls, tokens and cost of the final run and of development |
| `evaluation/media_api_usage.json`, `llm_usage_log.json` | Cumulative per-call usage logs |
| `evaluation/media_comparison*.md` | API extraction vs hand-written reference, per prompt version |
