"""Ghost Data Still Exists - deletion-audit agent.

Usage:
    python3 code/main.py                       # audit dataset/deletion_requests.csv -> output.csv
    python3 code/main.py --input dataset/sample_deletion_requests.csv --output /tmp/sample_pred.csv
    python3 code/main.py --trace traces.jsonl  # also write per-case reasoning traces
"""
import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from ghost import contract  # noqa: E402
from ghost.engine import Auditor  # noqa: E402
from ghost.llm import make_client  # noqa: E402


def run(input_csv, output_csv, trace_path=None, dataset_dir=None, extractions_path=None):
    dataset_dir = dataset_dir or os.path.join(ROOT, "dataset")
    llm = make_client()
    # media facts come from the metered Claude vision run (ghost/media.py, prompt v2);
    # the hand-written data/media_extractions.json is kept only as a comparison reference
    extractions_path = extractions_path or os.path.join(HERE, "data", "media_extractions.api_v2.json")
    auditor = Auditor(dataset_dir, extractions_path, llm=llm)
    with open(input_csv, encoding="utf-8") as fh:
        cases = list(csv.DictReader(fh))
    # every participant-visible request shares the deletion-worker batches
    known = []
    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        path = os.path.join(dataset_dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                known += list(csv.DictReader(fh))
    # classify every request's intent up front: one batched pass, cached per text
    llm.prefill_intents([c["request_text"] for c in known + cases])
    # ad-hoc requests (e.g. evaluation/custom_cases.csv) reuse the batch
    # assignment of the dataset request for the same subject and batch
    auditor.prepare(known)
    params = getattr(auditor, "batch_params", None)
    if params is not None:
        print("learned batch parameters: " + json.dumps(params.describe()), file=sys.stderr)
    results = [auditor.audit(c) for c in cases]
    results.sort(key=lambda r: int(r.case_id.split("_")[1]))
    # never write an output that breaks the contract (e.g. COMPLETE with no steps)
    errs = contract.validate([(r.case_id, r.status, r.steps) for r in results],
                             {c["case_id"]: c for c in cases}, contract.evidence_times(dataset_dir))
    if errs:
        sys.exit("output contract violated, nothing written:\n  " + "\n  ".join(errs[:20]))
    for w in contract.warnings([(r.case_id, r.status, r.steps) for r in results]):
        print("contract warning: " + w, file=sys.stderr)
    with open(output_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in results:
            w.writerow([r.case_id, r.status, json.dumps(r.steps, separators=(",", ":"))])
    if trace_path:
        with open(trace_path, "w", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps({"case_id": r.case_id, "status": r.status, **r.trace}) + "\n")
    if llm is not None:
        llm.write_usage({"input": os.path.relpath(input_csv, ROOT), "output": os.path.relpath(output_csv, ROOT)})
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=os.path.join(ROOT, "dataset", "deletion_requests.csv"))
    ap.add_argument("--output", default=os.path.join(ROOT, "output.csv"))
    ap.add_argument("--trace", default=None)
    ap.add_argument("--media", default=None,
                    help="media facts file (default: code/data/media_extractions.api_v2.json)")
    args = ap.parse_args()
    results = run(args.input, args.output, args.trace, extractions_path=args.media)
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"wrote {len(results)} rows to {args.output}: {counts}")


if __name__ == "__main__":
    main()
