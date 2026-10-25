# Evaluation report — Ghost Data Still Exists

Reproduce every number below with:

```bash
python3 code/evaluation/main.py --all        # public samples + custom cases
python3 code/evaluation/main.py --all -v     # also prints each missing / extra step
```

## Metrics

All metrics are computed by `code/evaluation/main.py`:

| Metric | Definition |
|---|---|
| Status accuracy | `deletion_status` matches exactly |
| Step precision / recall / F1 | Multiset match of whole steps: (phase, system_id, resource_id, operation, state, evidence_id) |
| Resource F1 | Same, ignoring `evidence_id` (were the right records found and treated correctly?) |
| Field accuracy | Steps aligned by position; share of agreeing fields over max(len(pred), len(gold)) |
| Sequence accuracy | Whole `steps_json` array identical, order included, and status correct |

## Results (final pipeline)

> **These sample metrics are in-sample (the samples were used for tuning).** Every
> change in "Failures found and improvements made" was kept or dropped by scoring
> it on these 32 public samples, so they overstate accuracy on unseen cases. The
> 518 evaluation cases have no labels and are not scored here. The only cases
> whose expected answers were not tuned against are the synthetic ones below
> (case_9101–9103).

### Public samples, in-sample (samples used for tuning) (`dataset/sample_deletion_requests.csv`, 32 cases)

| Metric | Value |
|---|---|
| Case-level status accuracy | **100.0 %** (32/32) |
| Step precision | **100.0 %** |
| Step recall | 97.9 % |
| Step F1 | **0.990** |
| Resource F1 (ignoring evidence) | 0.990 |
| Field accuracy — phase / system / resource / operation / state / evidence | 97.9 / 93.8 / 93.8 / 94.8 / 94.8 / 97.4 % (mean 95.4 %) |
| Sequence accuracy (exact step arrays) | **87.5 %** (28/32) |

No predicted step is wrong (precision 100 %). The four imperfect cases miss one verification step each:

| Case | Missing step | Why |
|---|---|---|
| case_04 | `account_004` VERIFY_RECORD_ABSENT | `account_004` appears in no participant-visible file or activity row. |
| case_18 | `audit_record_6645` VERIFY_RECORD_PRESENT | Not present anywhere in participant data. |
| case_20 | `audit_record_5569` VERIFY_RECORD_PRESENT | Not present anywhere in participant data. |
| case_23 | `account_410` VERIFY_RECORD_ABSENT | Three requests in one deletion batch share three identical, identifier-free failed account submissions. This is a genuine 1-in-3 tie that the agent refuses to guess (see "Ambiguity policy"). |

### Custom cases on a synthetic dataset of our own (`code/evaluation/custom_dataset/`, 3 cases)

These three cases were designed from the problem statement and `deletion_rules.csv`, not from any sample or evaluation case. They run on a small dataset of our own:

- four fictional users (one of them a decoy);
- an identity audit log;
- seven activity rows;
- a service export and a snapshot bundle.

The run uses the real `dataset/deletion_rules.csv`. **The expected answers (`expected.csv`) and their reasoning (`custom_dataset/README.md`) were written before the evidence was built and before the agent was run.** All three matched on the first run, so nothing was adjusted afterwards. Their request texts are ic16–ic18 in `intent_cases.csv`, and Haiku labelled them in the keyed run.

| Case | What it tests | Expected (reasoned first) | Result |
|---|---|---|---|
| case_9101 | Subject with **two old emails**, writing from the oldest. The search document is reachable only through the middle address and the export only through the oldest. The account is gone (404 + catalog link). | INCOMPLETE: 2 findings (export, search), 2 deletes, 3 ABSENT checks | pass |
| case_9102 | **Sync recreates data after deletion.** The worker claims `search_9102` was deleted on 09-03, the connector `data_flow_9102` repopulates it on 09-05, and on 09-09 the document is back and the connector is enabled. | INCOMPLETE: search PRESENT + flow ACTIVE. DISABLE_DATA_FLOW comes **before** DELETE_RECORD, then 3 checks | pass |
| case_9103 | **Active legal hold.** A retained order record runs until 2031-12-31 (rule_13 RETAIN), the account is gone, and a decoy hold belongs to another user. | COMPLETE: account ABSENT (evidence_901), hold PRESENT (evidence_902), and no deletion of the hold | pass |

Synthetic-case status accuracy is 100 % (3/3), step F1 is 1.00 and sequence accuracy is 100 %.

### Custom cases on the participant dataset (`code/evaluation/custom_cases.csv`, 3 cases)

These reuse real subjects and timestamps, so they are **not independent** of the dataset:

| Case | What it tests | Origin | Expected | Result |
|---|---|---|---|---|
| case_9001 | German access request ("Kopie meiner Daten … Konto soll aktiv bleiben") | Subject and timestamps of evaluation case_103, with a new text | NOT_APPLICABLE, `[]` | pass |
| case_9002 | Request sent from a **historical** email (`n.petrescu@yahoo.com`) | Sample case_10's subject and timestamps, from the subject's other old address. **The expected answer is sample case_10's answer**, so this case is sample-derived. | as case_10 | pass |
| case_9004 | **Temporal trap**: the account is FOUND on 08-31 but NOT_FOUND at the audit cutoff on 09-06 (`evidence_08`) | Subject and timestamps of evaluation case_86. The expected answer was derived by hand from the evidence. | COMPLETE: account ABSENT and audit record PRESENT, both citing `evidence_08` | pass |

Status accuracy is 100 % (3/3), step F1 is 1.00 and sequence accuracy is 100 %. Haiku never labelled these three texts, so the fallback rules decide their intent, correctly.

We removed two earlier custom cases, case_9003 and case_9005. They were Spanish and Korean translations of samples case_06 and case_24, with the samples' answers as expected output.

### Full evaluation set (518 cases, no labels)

The final run writes 355 INCOMPLETE, 153 COMPLETE and 10 NOT_APPLICABLE rows with 3,199 steps. Every row passes the structural contract checks:

- one row per request, sorted by case number (numeric: `case_33` … `case_548`), which is also the order of `dataset/deletion_requests.csv`;
- consecutive step numbers and correct phase order;
- evidence on every finding and on every COMPLETE verification;
- empty evidence on remediation and on post-remediation verification.

`python3 code/ghost/contract.py output.csv` reports `VALID … 0 violations` and no warnings. `main.py` runs the same validator before it writes, and refuses to write an output that violates the contract. Every INCOMPLETE row has at least one finding, so the pending-only rows described under "Latent engine bugs" do not occur in this data.

### Final output against the first version (v1)

v1 is the first output: fixed parameters and the hand-written media cache. A verified copy is kept in `backups/v1/`. The final output uses learned parameters, the engine fix, the catalog join and the API media extraction (prompt v2).

| | Rows changed | Status changes |
|---|---|---|
| v1 → final | **8 of 518** | **0** |

- **case_45** gains `audit_record_001`, `002`, `004`, `005` and `006` (VERIFY_RECORD_PRESENT). See "Linking rule for security audit records" below.
- **case_62, 312, 509 and 510** get a different account. v1's fixed submission window (−90 to +10 min) admitted submissions made −6 to +7 min from `reported_at`. Those belong to the next hour's batch: no identifier-linked example falls between −41 and +10 min. The learned window excludes them.
- **case_262, 290 and 308** gain an account. In v1 they had more than two candidates, and the agent does not guess in that situation. The learned window removes the spurious candidates and leaves a single one.

The public-sample metrics are identical for v1 and the final version.

### Final output against the previous submission (`code.zip` of 2026-10-24)

The final output.csv is **byte-identical** to the one shipped in the previous code.zip. Since then, three things changed:

- Intent now comes from `claude-haiku-5-5` instead of rules that contained phrases copied from evaluation requests. On all 518 requests, Haiku agrees with the previous rules.
- Policy now comes from `deletion_rules.csv` instead of a hand-written table. No rule condition is False in this data, so the actions are unchanged.
- Three latent engine bugs were fixed. None of them is triggered by this data.

## Failures found and improvements made

We ran the scorer on the public samples after each change. Step F1 and sequence accuracy are on the samples:

| # | Change | Step F1 | Seq. acc. |
|---|---|---|---|
| 0 | Baseline: identity resolution + identifier linking + latest in-window state + policy table | 0.917 | 47 % |
| 1 | **Global batch solver**. Accounts and anonymized analytics carry no identifiers after deletion. They are recovered from the deletion worker's batch for the same `reported_at`: the account submission's result mirrors the request's `reported_status`, and one request's policy actions form a fixed-cadence chain. | 0.968 | 66 % |
| 2 | Linking may use an observation whose own timestamp is ≤ audit_at even if its file was exported later. **Citations** only use files captured ≤ audit_at. This fixed missed security-audit records. | — | — |
| 3 | Workflow messages (`… Target resource: X`) near `reported_at`. Empty profile collections in snapshot bundles count as proof of absence. | 0.984 | 81 % |
| 4 | Absence proof cites the **latest** full-collection snapshot before the audit and prefers dedicated profile snapshots over broad inventories. This fixed the wrong evidence on case_31. | 0.990 | 88 % |
| 5 | Replica snapshots (`customer_data_*`) allowed as absence proof. This fixed 13 eval COMPLETE cases whose account verification had no evidence. | 0.990 | 88 % |
| 6 | **Near-audit preference**: readback exports are captured right at the audit time of the cases they serve. This fixed a wrong analytics assignment on case_19. | 0.990 | 88 % |
| 7 | Learned batch parameters replace the hand-written constants (see below) | 0.990 | 88 % |
| 8 | Engine fix: an unknown state is no longer treated as compliant (see below) | 0.990 | 88 % |
| 9 | Media facts from the metered Claude vision run (prompt v2) + catalog join in code (see below) | 0.990 | 88 % |

### Problem 1 — dataset-specific constants (overfitting) → parameters learned on every run

v1 hard-coded several values read off this dataset:

- the policy-chain cadence (`CHAIN_GAPS = (390, 449)` s, with ±1 s tolerance and a 600 s cut-off);
- the status ↔ result mapping;
- the account-submission window (−90 / +10 min) and the policy window (−35 / 0 min);
- the ±10 min window for `Target resource:` messages;
- a 48 h "near audit" horizon.

These would not transfer to another dataset.

`code/ghost/learn.py` now re-learns all of them on every run. It is self-supervised: the training examples are only the resources each request already reaches through its own identifiers, so no batch assumption is involved.

- Windows are the 2–98 % quantiles of the example offsets, widened on each side by `max(10 % of span, span/(n−1))`. The quantiles drop label noise, such as identifier-linked accounts deleted in another batch. `span/(n−1)` is the expected gap between neighbouring order statistics, so it covers what one unseen example adds beyond the observed extreme. The 10 % floor stops large samples from collapsing onto their exact extremes.
- The cadence is the set of gap values with at least 5 occurrences. The status mapping is kept only if it holds in at least 90 % of examples.
- Every window is checked by leave-one-out. A mechanism is enabled with ≥ 5 examples, or with 4 examples if leave-one-out is perfect. Otherwise it is disabled.

What the data gives:

| Parameter | v1 (hard-coded) | Learned | Examples | Leave-one-out |
|---|---|---|---|---|
| Chain cadence | 390 / 449 s | 390 / 449 s (37 and 34 occurrences) | 73 gaps | — |
| Status ↔ result | fixed table | same table (131/132, 67/67, 49/49) | 248 | — |
| Account submission window | −90 / +10 min | −77.7 / −41.4 min | 248 | 246/248 |
| Policy window | −35 / 0 min | −31.0 / −4.9 min | 121 | 120/121 |
| `Target resource:` window | ±10 min | ±757 s | **4** | **4/4** |
| Near-audit horizon | 48 h | 28.27 h | 536 | 524/536 |

**The near-audit horizon is brittle, and harmless.** It is the 98th percentile of each request's shortest gap between evidence capture and audit. Almost all requests have evidence captured within 0.93 h of the audit, and about 12 have gaps of 14–135 h. The 98th percentile therefore falls on the 12th-largest value, which sits right at the edge of that tail, so adding or removing one example moves it:

| Media facts | Tail examples | Learned horizon |
|---|---|---|
| API extraction **without** the catalog join (early development runs) | one fewer | 1.02 h |
| Hand-written reference cache | — | 15.85 h |
| API v2 **with** the catalog join (final) | + case_61 (26 h, via `object_19`) | 28.27 h |

The value is deterministic for given inputs: two consecutive final runs are byte-identical. The final output.csv is also unchanged for any horizon between 1.02 h and 48 h, because no assignment in this data depends on it. A rule less sensitive to one tail example (for example the largest gap inside the dense cluster) would be more robust on other data; it is listed under limitations.

**`Target resource:` with only 4 examples.** It was first disabled under a plain minimum-of-5 rule. That cost the seven handcrafted sample cases their account: step F1 fell from 0.990 to 0.971, and the custom cases fell from 1.00 to 0.938. It was **not** re-enabled because it improved the samples. It was re-enabled because it passes leave-one-out: for each of the 4 examples, a window learned from the other 3 covers it.

| Example left out | Window from the other 3 | Covered |
|---|---|---|
| 61 s | [0, 714 s] | yes |
| 322 s | [0, 844 s] | yes |
| 481 s | [0, 844 s] | yes |
| 583 s | [0, 691 s] | yes |

Caveats:

- The `span/(n−1)` term of the margin rule was written after these four offsets had been seen. Under the earlier "10 % of span" rule, the 583 s example would have failed (window 529 s).
- With 4 examples this mechanism remains the least supported one. It is listed under limitations.

### Problem 2 — media facts written by hand → metered API extraction, prompt corrected against the hand cache

v1 read screenshot and PDF facts from `code/data/media_extractions.json`. The coding agent wrote that file by hand during the development session, with no API call. Its `_about` note originally claimed that `ghost/media.py` had produced it. That claim was false and was corrected.

The final pipeline uses `code/data/media_extractions.api_v2.json`, produced by `python3 code/ghost/media.py` (`claude-opus-5-5`). The participant ran it in their own terminal with their own key, and every call is metered in `media_api_usage.json`. Each API run was compared field by field with the hand cache using `code/ghost/compare_media.py --impact`.

**v1 prompt (first API run)** — field agreement with the cache was 84.6 %, sample step F1 0.955, and 6 eval cases changed. The fair baseline is 0.971, not 0.990: at that point the learned parameters had temporarily switched off the `Target resource:` mechanism, and the hand cache scored 0.971 / 68.8 % under the same configuration (`media_comparison.md`). The comparison exposed five failure types. Each was fixed with a **general rule**, not by copying values from the cache:

| Failure exposed by the comparison | General fix |
|---|---|
| `support_001`, an uploaded correction document, labelled `SUPPORT_MESSAGE` (sample case_07 expects DELETE) | Prompt rule: a file uploaded or attached to a ticket, or an artifact that shows file properties, is `SUPPORT_ATTACHMENT`. |
| `export_1842`, `object_19` and `tombstone_17` missing: the artifacts show `NST-2026-0718-1842`, `delivery_confirmation_19.pdf` and `user_17`, and only the data catalog maps these to resource ids | The model now lists every visible reference. The **code** (`EvidenceStore.join_catalog`) resolves catalog-mapped references, attaching each catalog link to the most recent artifact captured before it that shows the reference. The artifact's visible subject identifiers decide whether the record is exposed or redacted. |
| `support_9538` reported as exposed, although the screenshot only shows it as a link in the customer panel | Prompt rule: a resource only referenced inside another record's panel is a reference, not a record. |
| `backup_9287` `restore_allowed: null`, so the case became COMPLETE without proof | Prompt rule for archives (false only if a block / deny / hold is shown; true if listed as restorable with no block). Plus the engine fix below. |
| `status: IDENTIFIABLE` on every record, including accounts and audit logs | Prompt rule: status is filled only for analytics, support messages and tombstones; a `user_N` reference is not a direct identifier. |

**v2 prompt (final)** — field agreement 97.1 %, sample step F1 0.990 and sequence 87.5 %, identical to the hand cache. One eval case changes (case_45, below). The remaining differences from the cache are by design:

- three catalog-mapped ids are now resolved in code instead of the extraction;
- seven extra records belong to other people shown on the same screens and do not link to any request;
- the `user_03` reference is stored on `audit_record_003`, which has no effect for a retained record.

### Engine fix — unknown is not compliant

The API comparison also exposed two engine flaws that the hand cache had hidden:

- A data flow or backup whose switch or restore flag is not shown was treated as compliant.
- A COMPLETE case could contain a verification step without evidence.

Both are fixed in `ghost/engine.py`:

- Missing flags give the state UNKNOWN.
- A case is COMPLETE only if every required state is proven by citable evidence. Otherwise it stays INCOMPLETE, with the checks that must pass listed as pending verification.

The change has no effect on the samples or on the final eval output, because no unproven state remains.

### Linking rule for security audit records (case_45)

The API extraction lists six signed audit events for `user_03` (`audit_record_001`–`006`) in `evidence_37`. The hand cache had kept only `audit_record_003`, the one a data-catalog row names.

To decide which rule to use, we checked the public samples. In all 22 samples that have an observable audit record, the official answer verifies exactly the security audit records that carry the subject's identifier inside the request window, and uses no catalog link (22/22). The final agent applies that same identifier rule, so case_45 verifies all six records as retained.

The samples contain no subject with more than one audit record. The rule is therefore the one used everywhere else, not one confirmed on a multi-record example.

### Other failures investigated

- **Ordering heuristics for ties.** On the samples, pairing requests and candidates in sorted order looked perfect. We tested it on 46 eval batches where identifiers already fix the mapping, using participant data only. Sorted-order pairing was right 21 times and wrong 25 times, which is chance. Submission time, requested_at, audit_at and user id did no better.
- **Ambiguity policy.** A wrong guess adds a false positive and breaks a case that might otherwise be exact. With k equally plausible options, a guess is right with probability 1/k. That is break-even at k = 2 and a net loss at k ≥ 3. The agent therefore breaks ties only between two options. This cost two "lucky" sample hits (case_21 and case_23). We accepted that because it is the better expected policy on the hidden set.
- **Request-window lower bound.** Evidence before `requested_at` is ignored. Without this, sample case_07 inherited an attachment that belonged to the subject's February request.
- **Intent rules copied evaluation phrases.** A review found that the first intent rules contained phrases taken verbatim from evaluation requests, for example "marketing lists only", "stop the promotional" and "reset(ting) the login". Those rules were replaced:
  - `claude-haiku-5-5` now labels every request in batches of 20, with structured JSON output and cached answers.
  - Its prompt is written from the statement's categories (deletion vs access, correction, export, marketing opt-out, other).
  - Generic multilingual rules remain only for texts with no cached label.
  - Haiku scores 32/32 on the samples and 18/18 on our 18 invented requests, which include traps such as "No borréis mi cuenta, solo…". The fallback rules also score 32/32 and 18/18; two generic grammar fixes, found with the invented set, are included.
  - On the 518 evaluation requests, Haiku and the previous rules disagree on **0** (`intent_eval.py --eval-diff`).
  - Caveat: the prompt and the rules were written without re-reading the evaluation texts, but those texts had been read earlier in the same session.
- **case_537 ("correct my surname without removing any of my records").** This is a correction. The rule rewrite had made it a deletion, because the negation "without … removing" was not recognised. It also exposed a vacuous answer: an authorized request whose subject is unresolved, or with no linked record, returned COMPLETE `[]`. Two generic fixes:
  - the refusal cue accepts "without" and verb stems;
  - the engine returns INCOMPLETE (unproven) instead of a vacuous COMPLETE.
  
  `ghost/contract.py` now validates every output before it is written. `main.py` refuses to write an output that violates the contract; a forced test confirmed it.
- **Intent answers and API keys.** The intent client used to skip the answer cache when no key was set, so a run without a key could differ from one with a key. Now the cache is always consulted first. The final output takes every intent from the cache, which `claude-haiku-5-5` filled, so keyed and key-less runs give the same output.csv.
- **Usage accounting.** The first usage file was overwritten on every run, and a refused call would not have been counted. One real intent call made by `claude-opus-5-5` (case_538, the earlier fallback design) therefore lost its token counts. Both usage logs now accumulate, and API errors, refusals and truncated answers are metered.

### Latent engine bugs (fixed; none triggered by this data)

Each fix was checked with synthetic resources. The output for this data is unchanged.

| Bug | Before | After |
|---|---|---|
| A retained record (legal hold, security audit log) is found absent | `VERIFY_RECORD_PRESENT` cited the evidence that showed it **absent**, and the case could be COMPLETE | State MISSING: the check stays pending with no evidence, and the case is INCOMPLETE |
| A record the policy requires to be redacted or anonymized (tombstone, support message, analytics) is found absent | COMPLIANT: `VERIFY_RECORD_REDACTED` / `ANONYMIZED` cited the absence as proof | State MISSING, as above. Over-deletion is not proof of the required state |
| An INCOMPLETE row with no finding (only pending checks), or an unresolved subject | The new contract guard rejected it and aborted the **whole** run | Such rows are written and reported as contract **warnings**. The contract has no finding operation for a missing kept record, so pending checks are the most honest answer |

## Known limitations

- **In-sample tuning.** The sample metrics are in-sample (see the note under Results). Several heuristics were chosen by looking at sample cases and are not learned:
  - the shared-context pattern (order / agent / delivery);
  - the near-audit preference for readback exports;
  - preferring dedicated profile snapshots over inventories;
  - the two-way tie-break limit;
  - the ±1 s cadence tolerance.

  "Learned on every run" applies only to the batch timing parameters listed above.
- **Custom cases on the participant dataset.** case_9001 and case_9004 reuse the subjects and timestamps of evaluation cases case_103 and case_86. case_9002 reuses sample case_10, and its expected answer is that sample's answer. Only the three synthetic cases are independent.
- **Rows that are valid but weak.** When a kept record is missing, the contract offers no finding operation, so such a case is INCOMPLETE with only pending checks.

- **Tie-breaks.** 73 account links and 28 policy-chain links in the eval set come from a two-way tie-break, so roughly half of them are expected to be wrong.
- **Unobservable resources.** Some expected resources do not appear in participant data at all (for example `audit_record_6645` and `account_004` in the samples), so the agent cannot recover them.
- **`Target resource:` support.** This mechanism rests on 4 labelled examples, validated only by leave-one-out.
- **Near-audit horizon.** The learned horizon sits on the edge of a small tail, so it moves (1.02 / 15.85 / 28.27 h) when one example is added or removed. It has no effect on this output, but it would need a more robust rule on other data.
- **Policy condition tests are chosen by condition text.** `ghost/policy.py` reads each rule's category, condition and action from `deletion_rules.csv`. However, the test that evaluates a condition (retention period, active legal hold, exclusive attachment, shared support context, and so on) is selected by matching the condition's wording. A reworded condition with no matching pattern is reported as untestable, and its rule is applied. A missing fact (unknown) also applies the rule.
- **Catalog join scope.** The join matches references literally. A reference shown in a different format (for example with different spacing) would not be resolved.
- **Media reproducibility.** Media facts come from one model run. A rerun of `media.py` can differ, so `compare_media.py --impact` should be run before replacing the file.
- **Hand-written cache.** `code/data/media_extractions.json` is kept only as a comparison reference. The coding agent (Claude Code, model `claude-opus-5-5`) wrote it by hand on 2026-10-24 during the development session: it viewed each PNG with the session's image-reading tool and read each PDF via `pdftotext -layout`. No Anthropic API call was made, `ghost/media.py` was not used to produce it, and its token usage was not measured.
