"""Output-contract validator (problem statement / AGENTS.md section 6).

    python3 code/ghost/contract.py output.csv [--requests dataset/deletion_requests.csv]

main.py calls validate() before writing output.csv and aborts on any
violation, so a run can never emit, for example, a COMPLETE case with no
verification steps.  Checks:
  * header, one row per request, no duplicates, rows sorted by case number;
  * status in COMPLETE / INCOMPLETE / NOT_APPLICABLE;
  * NOT_APPLICABLE <=> steps_json == []; COMPLETE needs steps;
  * COMPLETE holds only VERIFICATION steps;
  * exactly the seven step fields, consecutive step_no from 1, phase order,
    each operation in its phase with its contract state;
  * findings and COMPLETE verifications cite a known file_id / activity_id /
    audit_id that is not later than the case's audit_at; remediation and
    post-remediation verification of INCOMPLETE cases have an empty evidence_id.

warnings() lists rows that are valid but weak: an INCOMPLETE case with no
steps (unresolved subject, no linked record) or with no finding (only pending
verifications, e.g. a retained record that is gone, for which the contract
has no finding operation).  They are reported, not fatal.
"""
import csv
import json
import os
import sys

HEADER = ["case_id", "deletion_status", "steps_json"]
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
FIELDS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
PHASE_OPS = {
    "FINDING": {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
                "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE"},
    "REMEDIATION": {"DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
                    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED"},
    "VERIFICATION": {"VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
                     "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
                     "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED"},
}
PHASE_RANK = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}


class ContractError(Exception):
    pass


def case_num(cid):
    return int(cid.split("_")[1])


def evidence_times(dataset_dir):
    """Time of every citable id: file capture, activity event, audit change."""
    out = {}
    for name, idcol, tcol in (("evidence_files.csv", "file_id", "captured_at"),
                              ("system_activity.csv", "activity_id", "event_time"),
                              ("user_audit_log.csv", "audit_id", "event_time")):
        with open(os.path.join(dataset_dir, name), encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out[r[idcol]] = r[tcol]
    return out


def validate(rows, requests, ev_times):
    """rows: [(case_id, status, steps list)]; requests: {case_id: request row}.
    Returns a list of violation strings (empty when the output is valid)."""
    errs = []
    ids = [r[0] for r in rows]
    if len(ids) != len(set(ids)):
        errs.append("duplicate case_id rows")
    if set(ids) != set(requests):
        missing, extra = set(requests) - set(ids), set(ids) - set(requests)
        errs.append(f"rows do not match the requests (missing {sorted(missing)[:5]}, extra {sorted(extra)[:5]})")
    if ids != sorted(ids, key=case_num):
        errs.append("rows are not sorted by case number")
    for cid, status, steps in rows:
        def bad(msg):
            errs.append(f"{cid}: {msg}")
        if status not in STATUSES:
            bad(f"invalid status {status!r}")
            continue
        if not isinstance(steps, list):
            bad("steps_json is not an array")
            continue
        if status == "NOT_APPLICABLE":
            if steps:
                bad("NOT_APPLICABLE with steps")
            continue
        if not steps:
            if status == "COMPLETE":
                bad("COMPLETE with empty steps_json")
            continue
        phases = [s.get("phase") for s in steps]
        if status == "COMPLETE" and any(p != "VERIFICATION" for p in phases):
            bad("COMPLETE with a non-verification step")
        audit_at = requests.get(cid, {}).get("audit_at", "")
        last = -1
        for i, s in enumerate(steps, 1):
            if set(s) != FIELDS:
                bad(f"step {i}: fields {sorted(s)}")
                continue
            if s["step_no"] != i:
                bad(f"step {i}: step_no {s['step_no']}")
            ops = PHASE_OPS.get(s["phase"])
            if ops is None or ops.get(s["operation"]) != s["state"]:
                bad(f"step {i}: {s['phase']}/{s['operation']}/{s['state']} not allowed")
                continue
            rank = PHASE_RANK[s["phase"]]
            if rank < last:
                bad(f"step {i}: phase order")
            last = rank
            ev = s["evidence_id"]
            if s["phase"] == "FINDING" or status == "COMPLETE":
                if not ev:
                    bad(f"step {i}: missing evidence_id")
                elif ev not in ev_times:
                    bad(f"step {i}: unknown evidence_id {ev}")
                elif audit_at and ev_times[ev] > audit_at:
                    bad(f"step {i}: {ev} is later than audit_at")
            elif ev:
                bad(f"step {i}: future step cites {ev}")
    return errs


def warnings(rows):
    """Valid but weak INCOMPLETE rows (see module docstring)."""
    out = []
    for cid, status, steps in rows:
        if status != "INCOMPLETE" or not isinstance(steps, list):
            continue
        if not steps:
            out.append(f"{cid}: INCOMPLETE with no steps")
        elif not any(s.get("phase") == "FINDING" for s in steps):
            out.append(f"{cid}: INCOMPLETE with only pending verifications (no finding)")
    return out


def load_output(path):
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != HEADER:
            raise ContractError(f"header {header} != {HEADER}")
        rows = []
        for r in reader:
            try:
                steps = json.loads(r[2])
            except (IndexError, json.JSONDecodeError) as e:
                raise ContractError(f"{r[:1]}: invalid steps_json ({e})")
            rows.append((r[0], r[1], steps))
    return rows


def main():
    import argparse
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    ap = argparse.ArgumentParser()
    ap.add_argument("output")
    ap.add_argument("--requests", default=os.path.join(root, "dataset", "deletion_requests.csv"))
    ap.add_argument("--dataset", default=os.path.join(root, "dataset"))
    a = ap.parse_args()
    with open(a.requests, encoding="utf-8") as fh:
        requests = {r["case_id"]: r for r in csv.DictReader(fh)}
    try:
        rows = load_output(a.output)
    except ContractError as e:
        sys.exit(f"INVALID: {e}")
    errs = validate(rows, requests, evidence_times(a.dataset))
    for e in errs[:50]:
        print(e)
    for w in warnings(rows):
        print("warning: " + w)
    counts = {}
    for _, st, _ in rows:
        counts[st] = counts.get(st, 0) + 1
    print(f"{'INVALID' if errs else 'VALID'}: {len(rows)} rows {counts}, {len(errs)} violations")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
