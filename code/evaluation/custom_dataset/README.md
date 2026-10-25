# Synthetic custom dataset (case_9101 – case_9103)

A small dataset of our own. It was designed from the problem statement and
`deletion_rules.csv`, not from any sample or evaluation case. Every person,
identifier and record here is fictional and appears nowhere in `dataset/`.
`code/evaluation/main.py --all` runs the agent on it as a separate suite. At run
time it copies `dataset/deletion_rules.csv` next to these files, so the policy
is the real one.

**Order of work.** The three expected answers in `expected.csv` and the reasoning
below were written first. The evidence files were built to match the scenario.
Only then was the agent run. Results are in `evaluation_report.md`.

## Shared setup

- `requested_at` 2026-09-01T09:00Z, `reported_at` 2026-09-03T10:00Z (every
  workflow reports `COMPLETE`), `audit_at` 2026-09-10T12:00Z.
- `evidence_901`: one service export captured 2026-09-09T08:00Z. It covers all
  three subjects and one unrelated user (`user_904`), who acts as a decoy.
- `evidence_902`: one snapshot bundle captured 2026-09-09T08:30Z with the legal
  service's `retained_order_records`, including a decoy hold for `user_904`.
- The data catalog (`LINK_RESOURCE_IDENTITY`, 2026-09-02) links each deleted
  account to its owner. A 404 profile lookup carries no identifier, so without
  this link nothing could connect the account to the subject.
- Within one operation, steps are ordered by `system_id`, then `resource_id`.
  That is the convention documented in the README. The statement fixes only the
  phase order and that flows are disabled before dependent records change.

## case_9101: two old email addresses

Ilka Brandt (`user_901`) changed their email twice: `ilka.brandt@web.de` →
`i.brandt.home@gmx.de` (2025-11-14), then → `ilka.brandt@posteo.de`
(2026-03-02). They write from the **oldest** address.

- `account_9101`: the profile lookup returns 404, and the catalog links the
  account to her current address. The record is ABSENT, as rule_01 requires.
- `search_9101`: the index query for the **middle** address still returns a
  document with that email. rule_03 says DELETE, and the record is PRESENT.
- `export_9101`: the export owned by the **oldest** address is still listed.
  rule_09 says DELETE, and the record is PRESENT.

The search document is reachable only through the middle address and the export
only through the oldest one, so both historical emails must be resolved. The
reported COMPLETE is false.

Expected: **INCOMPLETE**
1. FINDING `export_9101` REPORT_REMAINING_RECORD (evidence_901)
2. FINDING `search_9101` REPORT_REMAINING_RECORD (evidence_901)
3. REMEDIATION `export_9101` DELETE_RECORD
4. REMEDIATION `search_9101` DELETE_RECORD
5. VERIFICATION `account_9101` VERIFY_RECORD_ABSENT
6. VERIFICATION `export_9101` VERIFY_RECORD_ABSENT
7. VERIFICATION `search_9101` VERIFY_RECORD_ABSENT

Remediation and verification carry an empty `evidence_id`, because the case is
INCOMPLETE.

## case_9102: a sync recreates data after the deletion

Tomás Echeverría (`user_902`, `tomas.echeverria@fastmail.com`).

- The deletion worker reports `DELETE_RECORD` for `search_9102` on 2026-09-03.
  That is a claim, not proof.
- On 2026-09-05 connector `data_flow_9102` "repopulated downstream resource
  search_9102". The search document is back.
- In `evidence_901` the index query for his email returns `search_9102` again
  (PRESENT, rule_03 DELETE), and connector `data_flow_9102` is
  `enabled: true` (ACTIVE).
- `account_9102`: 404 plus a catalog link, so it is ABSENT.

Deleting the search document again is useless while the connector runs, so the
flow is disabled first (statement: disable data flows before changing dependent
records).

Expected: **INCOMPLETE**
1. FINDING `search_9102` REPORT_REMAINING_RECORD (evidence_901)
2. FINDING `data_flow_9102` REPORT_ACTIVE_DATA_FLOW (evidence_901)
3. REMEDIATION `data_flow_9102` DISABLE_DATA_FLOW
4. REMEDIATION `search_9102` DELETE_RECORD
5. VERIFICATION `account_9102` VERIFY_RECORD_ABSENT
6. VERIFICATION `search_9102` VERIFY_RECORD_ABSENT
7. VERIFICATION `data_flow_9102` VERIFY_DATA_FLOW_DISABLED

## case_9103: an active legal hold

Noor Al-Khatib (`user_903`, `noor.alkhatib@outlook.com`).

- `account_9103`: 404 plus a catalog link, so it is ABSENT (rule_01).
- `legal_hold_9103`: a retained order record for `user_903` with
  `retained_until` 2031-12-31, which is after `audit_at`. rule_13 (active legal
  hold) says RETAIN, so it must **not** be deleted, and the record is PRESENT.
- `legal_hold_9104` belongs to `user_904` and must not be linked.

Every required state is proven by evidence captured before `audit_at`.

Expected: **COMPLETE**
1. VERIFICATION `account_9103` VERIFY_RECORD_ABSENT (evidence_901)
2. VERIFICATION `legal_hold_9103` VERIFY_RECORD_PRESENT (evidence_902)
