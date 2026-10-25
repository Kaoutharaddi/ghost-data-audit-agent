"""Score predictions against labelled answers.

    python3 code/evaluation/main.py --all          # run the agent on the public samples, on
                                                   # evaluation/custom_cases.csv and on the synthetic
                                                   # evaluation/custom_dataset/, then score each
    python3 code/evaluation/main.py --pred P.csv --gold G.csv [-v]

Metrics
  status accuracy   - deletion_status exact match per case
  step P/R/F1       - multiset match of full steps (phase, system, resource, operation, state, evidence)
  resource P/R/F1   - same, ignoring evidence_id (did we find the right records and actions?)
  field accuracy    - position-aligned per-field agreement over max(len(pred), len(gold)) steps
  sequence accuracy - whole steps_json array identical (order included)
"""
import argparse
import csv
import json
from collections import Counter

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path):
    with open(path, encoding="utf-8") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}


def key(s, with_evidence=True):
    return tuple(s[f] for f in FIELDS if with_evidence or f != "evidence_id")


def prf(tp, np_, ng):
    p = tp / np_ if np_ else 1.0
    r = tp / ng if ng else 1.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def score(pred, gold, verbose=False):
    m = Counter()
    field_hits = Counter()
    field_total = 0
    for cid, (gs, gsteps) in gold.items():
        ps, psteps = pred.get(cid, ("MISSING", []))
        m["cases"] += 1
        m["status_ok"] += ps == gs
        for we, tag in ((True, "step"), (False, "res")):
            pc, gc = Counter(key(s, we) for s in psteps), Counter(key(s, we) for s in gsteps)
            m[tag + "_tp"] += sum((pc & gc).values())
            m[tag + "_np"] += sum(pc.values())
            m[tag + "_ng"] += sum(gc.values())
        n = max(len(psteps), len(gsteps))
        field_total += n
        for i in range(n):
            for f in FIELDS:
                if i < len(psteps) and i < len(gsteps) and psteps[i][f] == gsteps[i][f]:
                    field_hits[f] += 1
        exact = [key(s) for s in psteps] == [key(s) for s in gsteps]
        m["seq_ok"] += exact and ps == gs
        if verbose and not (exact and ps == gs):
            print(f"--- {cid}: pred {ps} / gold {gs}")
            pk, gk = Counter(key(s) for s in psteps), Counter(key(s) for s in gsteps)
            for s in sorted((gk - pk).elements()):
                print("   missing ", s)
            for s in sorted((pk - gk).elements()):
                print("   extra   ", s)
    out = {"cases": m["cases"], "status_accuracy": m["status_ok"] / m["cases"]}
    for tag in ("step", "res"):
        p, r, f = prf(m[tag + "_tp"], m[tag + "_np"], m[tag + "_ng"])
        out[f"{tag}_precision"], out[f"{tag}_recall"], out[f"{tag}_f1"] = p, r, f
    out["field_accuracy"] = {f: (field_hits[f] / field_total if field_total else 1.0) for f in FIELDS}
    out["field_accuracy_mean"] = sum(out["field_accuracy"].values()) / len(FIELDS)
    out["sequence_accuracy"] = m["seq_ok"] / m["cases"]
    return out


def run_all(verbose):
    import os
    import shutil
    import sys
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    code = os.path.dirname(here)
    root = os.path.dirname(code)
    sys.path.insert(0, code)
    from main import run
    suites = [("public samples", os.path.join(root, "dataset", "sample_deletion_requests.csv"),
               os.path.join(root, "dataset", "sample_output.csv")),
              ("custom cases", os.path.join(here, "custom_cases.csv"),
               os.path.join(here, "custom_expected.csv"))]
    report = {}
    with tempfile.TemporaryDirectory() as tmp:
        # synthetic dataset of our own (custom_dataset/README.md) with the real policy file
        synth = os.path.join(tmp, "custom_dataset")
        shutil.copytree(os.path.join(here, "custom_dataset"), synth)
        shutil.copy(os.path.join(root, "dataset", "deletion_rules.csv"), synth)
        suites.append(("custom synthetic dataset", os.path.join(synth, "deletion_requests.csv"),
                       os.path.join(synth, "expected.csv"), synth))
        for name, inp, gold, *data in suites:
            pred = os.path.join(tmp, "pred.csv")
            run(inp, pred, dataset_dir=data[0] if data else None)
            print(f"== {name}")
            report[name] = score(load(pred), load(gold), verbose)
            print(json.dumps(report[name], indent=2))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--pred")
    ap.add_argument("--gold")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.all:
        run_all(a.verbose)
        return
    if not (a.pred and a.gold):
        ap.error("--pred and --gold are required unless --all is given")
    res = score(load(a.pred), load(a.gold), a.verbose)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
