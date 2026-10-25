"""Learn the deletion workflow's batch timing from the data on every run.

Nothing here is specific to one dataset.  The training examples come only from
links the agent can already prove without batches: resources whose in-window
observations carry the subject's identifiers.  From those self-labelled
examples we measure, per run:

  * status_result   - which worker submission result accompanies each
                      reported_status (kept only if one result covers
                      >= MIN_SHARE of the examples);
  * submit_window   - offset of the primary-profile submission from reported_at;
  * policy_window   - offset of successful policy actions from reported_at;
  * chain_gaps      - recurring gaps between consecutive policy actions of the
                      same request (modes with >= MIN_SUPPORT occurrences);
  * report_window   - |offset| of "... Target resource: X" workflow messages;
  * audit_horizon   - how long before audit_at the evidence read back for a
                      request is typically captured.

Window rule: take the [Q_LO, Q_HI] quantiles of the examples (this drops
label noise, e.g. an account whose identifier-linked deletion belongs to a
different batch) and widen each side by
    pad = max(MARGIN * span, span / (n - 1))
span / (n - 1) is the expected gap between neighbouring order statistics: an
unseen example lands beyond the observed extreme with probability ~1/(n+1),
and when it does, it typically lands about one spacing away.  This term
dominates for small samples; the MARGIN floor keeps large samples from
collapsing onto their exact extremes.  One-sided quantities (|offset|, lag)
use [0, hi + pad].

Every window is checked by leave-one-out: each example must fall inside the
window learned from the others.  A mechanism is enabled when it has at least
MIN_EXAMPLES examples, or at least MIN_LOO_EXAMPLES examples that all pass
leave-one-out; otherwise it is disabled (None).
"""
from collections import Counter
from dataclasses import dataclass, field

from .activity import ts

MIN_EXAMPLES = 5      # enough examples to trust a window without further checks
MIN_LOO_EXAMPLES = 4  # smaller samples need a perfect leave-one-out score
MIN_SUPPORT = 5       # occurrences needed for a gap value to count as a cadence
MIN_SHARE = 0.9       # dominance needed to trust a status -> result mapping
Q_LO, Q_HI = 0.02, 0.98
MARGIN = 0.10         # widen learned windows by 10 % of their span
GAP_TOLERANCE_S = 1   # activity timestamps have one-second resolution


@dataclass
class BatchParams:
    status_result: dict = field(default_factory=dict)
    submit_window: tuple = None      # (lo, hi) seconds relative to reported_at
    policy_window: tuple = None
    chain_gaps: tuple = ()
    chain_max_gap: float = None
    report_window: float = None      # max |seconds| from reported_at
    audit_horizon: float = None      # seconds before audit_at
    support: dict = field(default_factory=dict)

    def describe(self):
        def mins(w):
            return None if w is None else [round(w[0] / 60, 1), round(w[1] / 60, 1)]
        return {
            "status_result": self.status_result,
            "submit_window_min": mins(self.submit_window),
            "policy_window_min": mins(self.policy_window),
            "chain_gaps_s": list(self.chain_gaps),
            "chain_max_gap_s": self.chain_max_gap,
            "report_window_s": self.report_window,
            "audit_horizon_h": None if self.audit_horizon is None else round(self.audit_horizon / 3600, 2),
            "support": self.support,
        }


def _quantile(xs, q):
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def _fit(xs, one_sided=False):
    lo, hi = _quantile(xs, Q_LO), _quantile(xs, Q_HI)
    span = hi - lo
    pad = max(MARGIN * span, span / (len(xs) - 1) if len(xs) > 1 else 0.0)
    return (0.0 if one_sided else lo - pad, hi + pad)


def _loo(xs, one_sided=False):
    """(examples inside the window learned without them, total)."""
    hits = 0
    for i, x in enumerate(xs):
        lo, hi = _fit(xs[:i] + xs[i + 1:], one_sided)
        hits += lo <= x <= hi
    return hits, len(xs)


def _window(xs, name, support, one_sided=False):
    """Learn a window, or None when the evidence is too thin."""
    xs = list(xs)
    if len(xs) < MIN_LOO_EXAMPLES:
        support[name] = f"disabled: {len(xs)} examples"
        return None
    hits, n = _loo(xs, one_sided)
    support[name + "_loo"] = f"{hits}/{n}"
    if n < MIN_EXAMPLES and hits < n:
        support[name] = f"disabled: {n} examples, leave-one-out {hits}/{n}"
        return None
    return _fit(xs, one_sided)


def learn(auditor, info, linked):
    """info: case_id -> (case, identity); linked: case_id -> identifier-linked rids."""
    sub_off, pol_off, rep_off, horizon = [], [], [], []
    status_pairs = Counter()
    gaps = []
    for cid, (c, ident) in info.items():
        R = ts(c["reported_at"])
        audit = ts(c["audit_at"])
        policy_events = []
        for rid in linked[cid]:
            ops = auditor.act.worker_ops(rid)

            def nearest(kind):
                ev = [r for r in ops if r["_kind"] == kind]
                return min(ev, key=lambda r: abs((r["_t"] - R).total_seconds())) if ev else None

            s = nearest("submit")
            if s is not None:
                sub_off.append((s["_t"] - R).total_seconds())
                status_pairs[(c["reported_status"], s["reported_result"])] += 1
            p = nearest("policy")
            if p is not None:
                pol_off.append((p["_t"] - R).total_seconds())
                policy_events.append(p)
            rep = [r for r in ops if "Target resource:" in r["details"]]
            if rep:
                rep_off.append(min(abs((r["_t"] - R).total_seconds()) for r in rep))
        policy_events.sort(key=lambda r: r["_t"])
        gaps += [(b["_t"] - a["_t"]).total_seconds() for a, b in zip(policy_events, policy_events[1:])]
        # earliest evidence file, before the cutoff, that read this subject back
        lags = [(audit - ts(o.file_ts)).total_seconds()
                for o in auditor.store.obs
                if o.rid in linked[cid] and auditor.in_window(o, c) and o.file_ts <= c["audit_at"]]
        if lags:
            horizon.append(min(lags))

    p = BatchParams()
    by_status = Counter()
    for (status, _), n in status_pairs.items():
        by_status[status] += n
    for status, total in by_status.items():
        result, n = max(((r, n) for (s, r), n in status_pairs.items() if s == status), key=lambda x: x[1])
        if total >= MIN_EXAMPLES and n / total >= MIN_SHARE:
            p.status_result[status] = result
    p.support = {}
    p.submit_window = _window(sub_off, "submit", p.support)
    p.policy_window = _window(pol_off, "policy", p.support)
    counts = Counter(round(g) for g in gaps)
    modes = []
    for g, n in counts.most_common():
        support = sum(m for h, m in counts.items() if abs(h - g) <= GAP_TOLERANCE_S)
        if support >= MIN_SUPPORT and all(abs(g - x) > GAP_TOLERANCE_S for x in modes):
            modes.append(g)
    if len(gaps) >= MIN_EXAMPLES and modes:
        p.chain_gaps = tuple(sorted(modes))
        p.chain_max_gap = max(modes) + GAP_TOLERANCE_S
    w = _window(rep_off, "report", p.support, one_sided=True)
    p.report_window = None if w is None else w[1]
    w = _window(horizon, "audit_horizon", p.support, one_sided=True)
    p.audit_horizon = None if w is None else w[1]
    p.support.update({"n_submit": len(sub_off), "n_policy": len(pol_off), "status_pairs": dict(
        (f"{s}->{r}", n) for (s, r), n in sorted(status_pairs.items())),
        "n_policy_gaps": len(gaps), "n_report": len(rep_off), "n_audit_horizon": len(horizon)})
    return p
