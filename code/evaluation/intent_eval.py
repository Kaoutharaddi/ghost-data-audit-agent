"""Validate the intent classifier.

    python3 code/evaluation/intent_eval.py              # samples + invented cases
    python3 code/evaluation/intent_eval.py --eval-diff  # also list evaluation requests where
                                                        # the LLM disagrees with the v1 rules

Ground truth:
  * public samples - a request authorizes deletion unless its sample_output
    status is NOT_APPLICABLE;
  * evaluation/intent_cases.csv - 15 requests written for this check (9
    languages, including traps such as "don't delete my account, only ...").
The LLM column uses cached answers (and the API for missing ones when
ANTHROPIC_API_KEY is set); the rules column is the generic key-less fallback.
"""
import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
ROOT = os.path.dirname(CODE)
sys.path.insert(0, CODE)

from ghost.intent import classify, rule_verdict  # noqa: E402
from ghost.llm import make_client  # noqa: E402


def load_sets():
    out = []
    gold = {r["case_id"]: r["deletion_status"] != "NOT_APPLICABLE"
            for r in csv.DictReader(open(os.path.join(ROOT, "dataset", "sample_output.csv"), encoding="utf-8"))}
    for r in csv.DictReader(open(os.path.join(ROOT, "dataset", "sample_deletion_requests.csv"), encoding="utf-8")):
        out.append(("samples", r["case_id"], r["request_text"], gold[r["case_id"]]))
    for r in csv.DictReader(open(os.path.join(HERE, "intent_cases.csv"), encoding="utf-8")):
        out.append(("invented", r["id"], r["text"], r["expected"] == "DELETION"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-diff", action="store_true")
    a = ap.parse_args()
    llm = make_client()
    rows = load_sets()
    llm.prefill_intents([t for _, _, t, _ in rows])
    stats = {}
    print(f"{'set':9s} {'id':9s} {'expected':9s} {'llm':22s} {'rules':9s} text")
    for setname, rid, text, expected in rows:
        llm_ok, llm_why = classify(text, llm)
        llm_has = llm_why.startswith("llm:")
        rule_ok = rule_verdict(text)[0]
        s = stats.setdefault(setname, {"n": 0, "llm_answered": 0, "llm_right": 0, "rules_right": 0})
        s["n"] += 1
        s["llm_answered"] += llm_has
        s["llm_right"] += llm_has and llm_ok == expected
        s["rules_right"] += rule_ok == expected
        flag = "" if (not llm_has or llm_ok == expected) and rule_ok == expected else "  <-- check"
        print(f"{setname:9s} {rid:9s} {str(expected):9s} {(llm_why if llm_has else 'no answer'):22s} "
              f"{str(rule_ok):9s} {text[:70]}{flag}")
    print()
    for setname, s in stats.items():
        llm_acc = f"{s['llm_right']}/{s['llm_answered']}" if s["llm_answered"] else "n/a (no key, no cache)"
        print(f"{setname}: n={s['n']}  LLM correct {llm_acc}  rules correct {s['rules_right']}/{s['n']}")
    if a.eval_diff:
        v1 = {r["case_id"]: r["deletion_status"] != "NOT_APPLICABLE"
              for r in csv.DictReader(open(os.path.join(ROOT, "backups", "v1", "output_v1.csv"), encoding="utf-8"))}
        reqs = list(csv.DictReader(open(os.path.join(ROOT, "dataset", "deletion_requests.csv"), encoding="utf-8")))
        llm.prefill_intents([r["request_text"] for r in reqs])
        diffs, missing = [], 0
        for r in reqs:
            ok, why = classify(r["request_text"], llm)
            if not why.startswith("llm:"):
                missing += 1
                continue
            if ok != v1[r["case_id"]]:
                diffs.append((r["case_id"], v1[r["case_id"]], why, r["request_text"]))
        print(f"\nEvaluation requests: LLM vs v1 rules disagree on {len(diffs)} "
              f"({missing} without an LLM answer)")
        for cid, old, why, text in diffs:
            print(f"  {cid}: v1 rules={'DELETION' if old else 'NOT'}  {why}  | {text}")
    llm.write_usage({"input": "evaluation/intent_eval.py"})


if __name__ == "__main__":
    main()
