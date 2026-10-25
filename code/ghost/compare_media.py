"""Compare an API media extraction with the hand-written cache.

    python3 code/ghost/compare_media.py                       # field-level diff
    python3 code/ghost/compare_media.py --impact              # + rerun the agent with each file
    python3 code/ghost/compare_media.py --report code/evaluation/media_comparison.md

Records are matched per media file by resource_id.  Compared fields: system,
present, category, status, enabled, restore_allowed, pii (values normalized
like the pipeline does: lower-cased emails, digits-only phones) and keys (as
a normalized set).  `--impact` runs the full agent once with each extraction
and lists the evaluation cases whose output changes, plus the public-sample
metrics for both.
"""
import argparse
import csv
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
ROOT = os.path.dirname(CODE)
sys.path.insert(0, CODE)

from ghost.identity import norm_value  # noqa: E402

CACHE = os.path.join(CODE, "data", "media_extractions.json")
API = os.path.join(CODE, "data", "media_extractions.api_v2.json")
SCALARS = ["system", "present", "category", "status", "enabled", "restore_allowed"]


def _norm_scalar(field, v):
    if field == "status":
        return (v or "").upper()
    if field == "present":
        return True if v is None else bool(v)
    return v


def _norm_pii(p):
    return {k: norm_value(v) for k, v in (p or {}).items() if v}


def _norm_keys(keys):
    return sorted({norm_value(k) for k in (keys or []) if k})


def compare(cache, api):
    files = sorted(k for k in cache if not k.startswith("_"))
    rows, agree, total = [], 0, 0
    counts = {"matched": 0, "only_cache": 0, "only_api": 0, "files_missing": 0}
    for name in files:
        if name not in api:
            counts["files_missing"] += 1
            rows.append((name, "-", "file not extracted by the API run", "", ""))
            continue
        c = {r["resource_id"]: r for r in cache[name].get("records", [])}
        a = {r.get("resource_id"): r for r in api[name].get("records", [])}
        for rid in sorted(set(c) - set(a)):
            counts["only_cache"] += 1
            rows.append((name, rid, "missing in API output", "", ""))
        for rid in sorted(set(a) - set(c)):
            counts["only_api"] += 1
            rows.append((name, rid, "extra in API output", "", ""))
        for rid in sorted(set(c) & set(a)):
            counts["matched"] += 1
            for f in SCALARS + ["pii", "keys"]:
                if f == "pii":
                    cv, av = _norm_pii(c[rid].get("pii")), _norm_pii(a[rid].get("pii"))
                elif f == "keys":
                    cv, av = _norm_keys(c[rid].get("keys")), _norm_keys(a[rid].get("keys"))
                else:
                    cv, av = _norm_scalar(f, c[rid].get(f)), _norm_scalar(f, a[rid].get(f))
                total += 1
                if cv == av:
                    agree += 1
                else:
                    rows.append((name, rid, f, json.dumps(cv, ensure_ascii=False),
                                 json.dumps(av, ensure_ascii=False)))
    counts["field_agreement"] = agree / total if total else None
    return counts, rows


def impact(api_path):
    from main import run
    from evaluation.main import load, score
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        for tag, media in (("cache", CACHE), ("api", api_path)):
            pred = os.path.join(tmp, f"eval_{tag}.csv")
            run(os.path.join(ROOT, "dataset", "deletion_requests.csv"), pred, extractions_path=media)
            sample = os.path.join(tmp, f"sample_{tag}.csv")
            run(os.path.join(ROOT, "dataset", "sample_deletion_requests.csv"), sample,
                extractions_path=media)
            out[tag] = (load(pred), score(load(sample), load(os.path.join(ROOT, "dataset", "sample_output.csv"))))
    changed = sorted((cid for cid in out["cache"][0] if out["cache"][0][cid] != out["api"][0].get(cid)),
                     key=lambda c: int(c.split("_")[1]))
    return changed, out["cache"][1], out["api"][1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=API)
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--impact", action="store_true")
    ap.add_argument("--report", help="also write a Markdown report to this path")
    a = ap.parse_args()
    if not os.path.exists(a.api):
        sys.exit(f"{a.api} not found - run `python3 code/ghost/media.py` first")
    cache, api = json.load(open(a.cache, encoding="utf-8")), json.load(open(a.api, encoding="utf-8"))
    counts, rows = compare(cache, api)
    lines = ["# Media extraction: API run vs hand-written cache", "",
             f"- API file: `{os.path.relpath(a.api, ROOT)}`",
             f"- Cache file: `{os.path.relpath(a.cache, ROOT)}`", "",
             "| Matched records | Missing in API | Extra in API | Files not extracted | Field agreement |",
             "|---|---|---|---|---|",
             "| {matched} | {only_cache} | {only_api} | {files_missing} | {fa} |".format(
                 fa="n/a" if counts["field_agreement"] is None else f"{counts['field_agreement']:.1%}", **counts),
             "", "## Differences", "", "| File | Resource | Field / issue | Cache | API |", "|---|---|---|---|---|"]
    lines += [f"| {f} | {r} | {k} | {c} | {v} |" for f, r, k, c, v in rows] or ["| (none) | | | | |"]
    if a.impact:
        changed, m_cache, m_api = impact(a.api)
        lines += ["", "## Impact on the agent", "",
                  f"Evaluation cases whose output changes: **{len(changed)}**"
                  + (f" ({', '.join(changed)})" if changed else ""), "",
                  "| Public samples | status acc. | step F1 | sequence acc. |", "|---|---|---|---|",
                  f"| with cache | {m_cache['status_accuracy']:.3f} | {m_cache['step_f1']:.3f} | {m_cache['sequence_accuracy']:.3f} |",
                  f"| with API extraction | {m_api['status_accuracy']:.3f} | {m_api['step_f1']:.3f} | {m_api['sequence_accuracy']:.3f} |"]
    text = "\n".join(lines) + "\n"
    print(text)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    main()
