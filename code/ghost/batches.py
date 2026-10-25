"""Split deletion-workflow batches between the requests that share them.

The deletion worker processes every request reported at the same time R in
one batch: primary profiles are submitted shortly before R and successful
policy actions follow in a fixed-cadence chain.  Accounts and anonymized
analytics often carry no identifier any more, so this module recovers them
from the batch structure:

  * account submissions whose result matches the request's reported status
    and that are not owned by another subject;
  * policy chains that are not owned by another subject.

The timing windows, the cadence and the status -> result mapping are not
hard-coded: ghost/learn.py measures them on every run from the links that
identifiers already prove, and switches a mechanism off when it has too few
examples.  Ties are broken only between two options.
"""
from collections import defaultdict
from datetime import timedelta

from .activity import ts
from .identity import norm_value
from .intent import classify
from .learn import GAP_TOLERANCE_S, learn

MAX_TIE = 2   # only break ties between at most two equally plausible options


def _num(rid):
    return int(rid.rsplit("_", 1)[1])


def _case_num(cid):
    return int(cid.split("_")[1])


def _zip_ambiguous(cands, taken):
    """Last-resort tie-break for requests whose candidate sets are identical:
    pair them in case-number order with candidates in resource-number order."""
    out = {}
    groups = defaultdict(list)
    for cid, opts in cands.items():
        opts = set(opts) - taken
        if opts:
            groups[frozenset(opts)].append(cid)
    for opts, cids in groups.items():
        if len(opts) > MAX_TIE:
            continue  # a 1-in-3 guess costs more precision than it gains recall
        for cid, rid in zip(sorted(cids, key=_case_num), sorted(opts, key=_num)):
            out[cid] = rid
    return out


def _solve(cands):
    """cands: case_id -> set(options). Unique elimination; returns case->option."""
    out = {}
    cands = {k: set(v) for k, v in cands.items()}
    changed = True
    while changed:
        changed = False
        for cid, opts in list(cands.items()):
            if len(opts) == 1:
                (o,) = opts
                out[cid] = o
                del cands[cid]
                for other in cands.values():
                    other.discard(o)
                changed = True
        # an option wanted by exactly one case goes to that case
        if not changed:
            want = defaultdict(set)
            for cid, opts in cands.items():
                for o in opts:
                    want[o].add(cid)
            for o, cids in want.items():
                if len(cids) == 1:
                    (cid,) = cids
                    if cid in cands and len(cands[cid]) > 1 and all(len(want[x]) > 1 for x in cands[cid] if x != o):
                        cands[cid] = {o}
                        changed = True
                        break
    return out


def _chains(events, gaps, max_gap):
    events = sorted(events, key=lambda r: r["_t"])
    parent = {r["activity_id"]: r["activity_id"] for r in events}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(events):
        for b in events[i + 1:]:
            gap = (b["_t"] - a["_t"]).total_seconds()
            if gap > max_gap:
                break
            if any(abs(gap - g) <= GAP_TOLERANCE_S for g in gaps):
                parent[find(b["activity_id"])] = find(a["activity_id"])
    groups = defaultdict(list)
    for r in events:
        groups[find(r["activity_id"])].append(r)
    return list(groups.values())


def assign_batches(auditor, cases):
    act = auditor.act
    links = defaultdict(list)
    info = {}
    for c in cases:
        ok, _ = classify(c["request_text"], auditor.llm)  # same verdict as the per-case audit
        ident = auditor.ids.resolve(c["email"])
        if not ok or ident is None:
            continue
        info[c["case_id"]] = (c, ident)
    by_rep = defaultdict(list)
    for cid, (c, ident) in info.items():
        by_rep[c["reported_at"]].append(cid)

    # resources each request reaches through its own identifiers (in window)
    linked = {}
    for cid, (c, ident) in info.items():
        vals = ident.norm_values()
        linked[cid] = {o.rid for o in auditor.store.obs
                       if auditor.in_window(o, c) and any(norm_value(v) in vals for v in o.identifiers())}
    params = learn(auditor, info, linked)
    auditor.batch_params = params
    has_account = {cid: any(r.startswith("account_") for r in rids) for cid, rids in linked.items()}
    has_kind = {cid: {r.rsplit("_", 1)[0] for r in rids} for cid, rids in linked.items()}

    def near_audit(rid, c):
        """Was rid read back by evidence captured for this request's audit
        (inside its window, in a file captured within the learned horizon)?"""
        if params.audit_horizon is None:
            return False
        audit = ts(c["audit_at"])
        for o in auditor.obs_by_rid.get(rid, []):
            cap = ts(o.file_ts)
            if auditor.in_window(o, c) and audit - timedelta(seconds=params.audit_horizon) <= cap <= audit:
                return True
        return False

    def owned_by_other(rid, uid):
        owners = auditor.owners(rid)
        return bool(owners) and uid not in owners

    for rep, cids in by_rep.items():
        R = ts(rep)
        # -- workflow messages naming their "Target resource:" around R
        near = [] if params.report_window is None else [
            r for r in act.worker
            if abs((r["_t"] - R).total_seconds()) <= params.report_window and "Target resource:" in r["details"]]
        if len(cids) == 1 and near:
            cid = cids[0]
            uid = info[cid][1].user_id
            for r in near:
                if not owned_by_other(r["resource_id"], uid):
                    links[cid].append((r["resource_id"], f"workflow-report@{r['activity_id']}"))
        # -- account submissions
        subs = []
        if params.submit_window is not None:
            lo, hi = params.submit_window
            window = act.worker_window(R + timedelta(seconds=lo), R + timedelta(seconds=hi))
            subs = [r for r in window if r["_kind"] == "submit" and r["resource_id"].startswith("account_")]
        cands, full = {}, {}
        for cid in cids:
            c, ident = info[cid]
            if has_account[cid]:
                continue
            want = params.status_result.get(c["reported_status"])  # None: no filter learned
            opts = {r["resource_id"] for r in subs
                    if (want is None or r["reported_result"] == want)
                    and not owned_by_other(r["resource_id"], ident.user_id)}
            # prefer accounts read back by this request's own audit evidence
            seen = {rid for rid in opts if near_audit(rid, c)}
            cands[cid] = seen or opts
            full[cid] = opts
        solved = _solve(cands)
        taken = set(solved.values())
        retry = {cid: full[cid] - taken for cid in cands if cid not in solved}
        solved.update(_solve(retry))
        rest = {cid: o for cid, o in retry.items() if cid not in solved}
        solved.update(_zip_ambiguous(rest, set(solved.values())))
        for cid, rid in solved.items():
            aid = next(r["activity_id"] for r in subs if r["resource_id"] == rid)
            how = "batch-submit" if cid not in rest else "batch-submit-tiebreak"
            links[cid].append((rid, f"{how}@{aid}"))
        # -- policy chains
        if params.policy_window is None or not params.chain_gaps:
            continue
        lo, hi = params.policy_window
        policy = [r for r in act.worker_window(R + timedelta(seconds=lo), R + timedelta(seconds=hi))
                  if r["_kind"] == "policy"]
        chains = _chains(policy, params.chain_gaps, params.chain_max_gap)
        open_chains = []
        claimed = set()
        for ch in chains:
            owners = set()
            for r in ch:
                owners |= auditor.owners(r["resource_id"])
            mine = [cid for cid in cids if info[cid][1].user_id in owners]
            if mine:
                claimed.update(mine)
                for cid in mine:
                    for r in ch:
                        links[cid].append((r["resource_id"], f"batch-chain@{r['activity_id']}"))
            elif not owners:
                open_chains.append(ch)
        # an open chain goes to a request that has no identifiable record of
        # the chain's kinds yet; prefer requests that talk about that data.
        for ch in sorted(open_chains, key=lambda ch: ch[0]["_t"]):
            kinds = {r["resource_id"].rsplit("_", 1)[0] for r in ch}
            eligible = [cid for cid in cids if cid not in claimed
                        and not (kinds & has_kind[cid])]
            if not eligible:
                continue
            words = {"analytics": ("analytic", "statistic"), "support": ("support",),
                     "data_flow": ("integration", "sync"), "backup": ("backup", "restore"),
                     "tombstone": ("reference",)}
            def mentions(cid):
                t = info[cid][0]["request_text"].lower()
                return any(w in t for k in kinds for w in words.get(k, ()))
            eligible.sort(key=lambda cid: (not mentions(cid), _case_num(cid)))
            near = [e for e in eligible if any(near_audit(r["resource_id"], info[e][0]) for r in ch)]
            preferred = [e for e in eligible if mentions(e)]
            pool = near or eligible
            if len(pool) > 1:
                talk = [e for e in pool if e in preferred]
                pool = talk if len(talk) == 1 else pool
            if len(pool) > MAX_TIE:
                continue
            cid = pool[0]
            claimed.add(cid)
            how = "batch-chain" if len(eligible) == 1 else "batch-chain-tiebreak"
            for r in ch:
                links[cid].append((r["resource_id"], f"{how}@{r['activity_id']}"))
    return dict(links)
