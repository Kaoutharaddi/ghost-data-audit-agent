"""Deletion-audit reasoning: link resources to the subject, reconstruct their
state at audit_at, apply deletion_rules.csv and emit ordered steps."""
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .activity import ActivityIndex, ts
from .evidence import EvidenceStore, system_for
from .identity import IdentityIndex, norm_value
from .intent import classify
from .policy import Policy

# ------------------------------------------------------------------ policy

# resource family -> data categories it can hold (first = default).  Which
# action each category requires comes from deletion_rules.csv (ghost/policy.py).
FAMILY_CATEGORIES = {
    "account": ["USER_PROFILE"], "search": ["SEARCH_INDEX_ENTRY"], "cache": ["CACHE_ENTRY"],
    "analytics": ["ANALYTICS_PROFILE", "PRODUCT_TELEMETRY", "MARKETING_PROFILE"],
    "support": ["SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT"], "export": ["USER_EXPORT"],
    "marketing": ["MARKETING_PROFILE"], "email_delivery": ["EMAIL_DELIVERY_LOG"],
    "audit_record": ["SECURITY_AUDIT_LOG"], "legal_hold": ["LEGAL_HOLD_RECORD"],
    "backup": ["BACKUP_ARCHIVE"], "tombstone": ["ACCOUNT_TOMBSTONE"],
    "object": ["SUPPORT_ATTACHMENT"], "data_flow": ["DATA_FLOW"],
}
SHARED_CONTEXT = re.compile(r"\b(order|agent|delivery|shipment|parcel)", re.I)


def family_of(rid):
    return rid.rsplit("_", 1)[0]


WORKER_ACTION = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE",
                 "DISABLE_DATA_FLOW": "DISABLE", "BLOCK_RESTORE": "BLOCK", "RETAIN_RECORD": "RETAIN",
                 "CHECK_DATA_FLOW": "DISABLE", "CHECK_RESTORE_ACCESS": "BLOCK"}

OP_ORDER = {
    "FINDING": ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW",
                "REPORT_RESTORE_RISK"],
    "REMEDIATION": ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD",
                    "ANONYMIZE_RECORD"],
    "VERIFICATION": ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                     "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED",
                     "VERIFY_RESTORE_BLOCKED"],
}
STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
# action -> (finding op, remediation op, verification op)
ACTION_OPS = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "DISABLE": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCK": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
}


# ------------------------------------------------------------------ data

@dataclass
class Resource:
    rid: str
    system: str
    links: list = field(default_factory=list)    # why it is linked to the subject
    obs: list = field(default_factory=list)      # observations inside the audit window
    category: str = ""
    action: str = ""
    rule: object = None      # policy.Decision that produced the action
    state: str = ""          # COMPLIANT / VIOLATION / UNKNOWN
    evidence: str = ""
    note: str = ""


@dataclass
class Result:
    case_id: str
    status: str
    steps: list
    trace: dict


class Auditor:
    def __init__(self, dataset_dir, extractions_path, llm=None, lower_bound="requested_at"):
        self.dataset_dir = dataset_dir
        self.store = EvidenceStore(dataset_dir, extractions_path)
        self.ids = IdentityIndex(dataset_dir)
        self.act = ActivityIndex(dataset_dir)
        self.store.join_catalog(self.act.catalog, self.act.catalog_alias, self.ids)
        self.policy = Policy(dataset_dir)
        self.llm = llm
        self.lower_bound = lower_bound
        self.obs_by_rid = defaultdict(list)
        for o in self.store.obs:
            self.obs_by_rid[o.rid].append(o)
        # every identifier ever seen on a resource -> which users it belongs to
        self._owner_cache = {}
        self.batch_links = {}
        self.batch_by_subject = {}

    # -------------------------------------------------------------- helpers
    def owners(self, rid):
        """Users whose identifiers appear on any observation of rid."""
        if rid in self._owner_cache:
            return self._owner_cache[rid]
        found = set()
        for o in self.obs_by_rid.get(rid, []):
            for v in o.identifiers():
                uid = self._uid_for_value(v)
                if uid:
                    found.add(uid)
        for (_t, _a, crid, email, uid, _x) in self.act.catalog:
            if crid == rid:
                found.add(uid)
        self._owner_cache[rid] = found
        return found

    def _uid_for_value(self, v):
        nv = norm_value(v)
        if not hasattr(self, "_value_index"):
            idx = {}
            for uid, ident in self.ids.users.items():
                for val in ident.norm_values():
                    idx.setdefault(val, uid)
            self._value_index = idx
        return self._value_index.get(nv)

    def in_window(self, o, case):
        """Observation happened between the request and the audit cutoff."""
        return case[self.lower_bound] <= o.ts <= case["audit_at"]

    @staticmethod
    def citable(o, case):
        """Evidence may only be cited if its file was captured by audit_at."""
        return o.file_ts <= case["audit_at"]

    # -------------------------------------------------------------- linking
    def link_resources(self, case, ident):
        vals = ident.norm_values()
        res = {}

        def add(rid, why):
            if rid not in res:
                res[rid] = Resource(rid=rid, system=system_for(rid))
            res[rid].links.append(why)

        for o in self.store.obs:
            if not self.in_window(o, case):
                continue
            if any(norm_value(v) in vals for v in o.identifiers()):
                add(o.rid, f"identifier@{o.eid}")
        for (t, aid, rid, email, uid, _x) in self.act.catalog:
            if uid == ident.user_id and case[self.lower_bound] <= t <= case["audit_at"]:
                add(rid, f"catalog@{aid}")
        # search documents derived from a linked account
        for o in self.store.obs:
            if not self.in_window(o, case):
                continue
            for k in o.keys:
                if k in res and k.startswith("account_") and o.rid.startswith("search_"):
                    add(o.rid, f"source:{k}@{o.eid}")
        # restore / connector lineage
        for (t, aid, src, dst, kind) in self.act.lineage:
            if t > case["audit_at"]:
                continue
            if src in res:
                add(dst, f"{kind}-lineage@{aid}")
            elif dst in res:
                add(src, f"{kind}-lineage@{aid}")
        self.link_batch(case, ident, res, add)
        return res

    def link_batch(self, case, ident, res, add):
        links = self.batch_links.get(case["case_id"])
        if links is None:
            # an ad-hoc request for a subject/batch already seen in the dataset
            links = self.batch_by_subject.get((ident.user_id, case["reported_at"]), [])
        for rid, why in links:
            if rid.startswith("account_") and any(r.startswith("account_") for r in res):
                continue
            add(rid, why)

    # -------------------------------------------------------------- state
    def category_of(self, res, case):
        """Data category of a resource: stated record type, else the category
        whose CSV action matches what the deletion workflow attempted, else
        evidence hints, else the family default."""
        family = FAMILY_CATEGORIES.get(family_of(res.rid), [""])
        cats = [o.category for o in res.obs if o.category]
        hints = {o.hint for o in res.obs if o.hint}
        ops = [r for r in self.act.worker_ops(res.rid, case["audit_at"])
               if r["event_type"] in WORKER_ACTION]
        if cats:
            cat = cats[-1]
            if cat == "SUPPORT_MESSAGE" and "attachment" in hints:
                cat = "SUPPORT_ATTACHMENT"
            return cat
        if ops:
            wanted = WORKER_ACTION[ops[-1]["event_type"]]
            actions = self.policy.actions_for(family)
            match = [c for c in family if actions.get(c) == wanted]
            return match[0] if match else family[0]
        if family[0] == "SUPPORT_MESSAGE" and ("attachment" in hints or "contact" in hints):
            return "SUPPORT_ATTACHMENT"
        if family[0] == "ANALYTICS_PROFILE" and "contact" in hints:
            return "MARKETING_PROFILE"
        return family[0]

    def condition_facts(self, res, case):
        """Facts the rule conditions are tested against (True/False/None)."""
        known = [o for o in self.obs_by_rid.get(res.rid, []) if o.ts <= case["audit_at"]]
        cur = sorted(res.obs, key=lambda o: (o.ts, o.file_ts))
        last = cur[-1] if cur else None
        f = {}
        if last is not None and last.present:
            if last.pii or last.status == "IDENTIFIABLE":
                f["attributable"] = True
            elif last.status in ("ANONYMIZED", "REDACTED"):
                f["attributable"] = False
            emails = [v for v in list(last.pii.values()) if isinstance(v, str) and "@" in v]
            f["has_email"] = bool(emails) if (last.pii or last.status) else None
        dates = [o.retain_until for o in known if o.retain_until]
        if dates:
            f["retention_active"] = f["hold_active"] = max(dates) >= case["audit_at"]
        elif any(o.status == "RETAINED" for o in known):
            f["hold_active"] = True
        if any(rid == res.rid and excl for (_t, _a, rid, _e, _u, excl) in self.act.catalog):
            f["exclusive"] = True
        if any(SHARED_CONTEXT.search(getattr(o, "text", "") or "") for o in known):
            f["shared_context"] = True
        if any(o.restore_allowed is False for o in known):
            f["restore_blockable"] = True
        statuses = [getattr(o, "account_status", "") for o in known if getattr(o, "account_status", "")]
        if statuses:
            f["account_deleted"] = statuses[-1] == "DELETED"
        return f

    def assess(self, res, case):
        """Decide compliance from the latest in-window observation."""
        obs = sorted(res.obs, key=lambda o: (o.ts, o.file_ts))
        a = res.action
        if not obs:
            res.state = "UNKNOWN"
            return
        latest_ts = obs[-1].ts
        last = [o for o in obs if o.ts == latest_ts]
        o = last[-1]

        def worst(pred):
            bad = [x for x in last if pred(x)]
            return bad[-1] if bad else None

        if a == "DELETE":
            bad = worst(lambda x: x.present is True)
            if bad:
                res.state, res.evidence = "VIOLATION", bad.eid
            elif o.present is False:
                res.state, res.evidence = "COMPLIANT", o.eid
            else:
                res.state = "UNKNOWN"
        elif a in ("REDACT", "ANONYMIZE"):
            bad = worst(lambda x: x.present and (x.pii or x.status == "IDENTIFIABLE"))
            if bad:
                res.state, res.evidence = "VIOLATION", bad.eid
            elif o.present is False:
                # the policy keeps this record (redacted / anonymized); its
                # absence is not proof of the required state
                res.state, res.note = "MISSING", f"absent@{o.eid}"
            else:
                res.state, res.evidence = "COMPLIANT", o.eid
        elif a == "DISABLE":
            bad = worst(lambda x: x.enabled is True)
            ok = [x for x in last if x.enabled is False]
            if bad:
                res.state, res.evidence = "VIOLATION", bad.eid
            elif ok:
                res.state, res.evidence = "COMPLIANT", ok[-1].eid
            else:
                res.state = "UNKNOWN"  # the evidence does not show the flow's switch
        elif a == "BLOCK":
            bad = worst(lambda x: x.restore_allowed is True)
            ok = [x for x in last if x.restore_allowed is False]
            if bad:
                res.state, res.evidence = "VIOLATION", bad.eid
            elif ok:
                res.state, res.evidence = "COMPLIANT", ok[-1].eid
            else:
                res.state = "UNKNOWN"  # the evidence does not show whether restore is blocked
        elif a == "RETAIN":
            if o.present is False:
                # a record the policy requires to remain is gone: the
                # verification stays pending and the case cannot be COMPLETE
                res.state, res.note = "MISSING", f"absent@{o.eid}"
            else:
                res.state, res.evidence = "COMPLIANT", o.eid

    def prepare(self, cases):
        """Global pass over every known request so deletion-workflow batches
        can be split exclusively between the requests that share them."""
        from .batches import assign_batches
        self.batch_links = assign_batches(self, cases)
        self.batch_by_subject = {}
        for c in cases:
            ident = self.ids.resolve(c["email"])
            if ident and c["case_id"] in self.batch_links:
                self.batch_by_subject[(ident.user_id, c["reported_at"])] = self.batch_links[c["case_id"]]

    def absence_proof(self, res, case):
        """For a record with no in-window observation, cite the most recent
        full-collection snapshot of its system (captured after the deletion
        was reported) that does not list it.  Dedicated profile snapshots are
        preferred over broad inventory captures."""
        lo = case["reported_at"]
        best = None
        for (eid, system, t, table) in self.store.scopes:
            if system != res.system or not (lo <= t <= case["audit_at"]):
                continue
            if self.store.captured_at(eid) > case["audit_at"]:
                continue
            listed = any(o.eid == eid for o in self.obs_by_rid.get(res.rid, []))
            if listed:
                continue
            key = (0 if table.startswith("inventory:") else 1, t)
            if best is None or key > best[1]:
                best = (eid, key)
        return best[0] if best else ""

    # -------------------------------------------------------------- main
    def audit(self, case):
        ok, why = classify(case["request_text"], self.llm)
        trace = {"intent": why}
        if not ok:
            return Result(case["case_id"], "NOT_APPLICABLE", [], trace)
        # an authorized deletion is COMPLETE only when evidence proves every
        # required state; with no subject or no linked record nothing is
        # proven, so the case stays open instead of a vacuous COMPLETE []
        # (main.py reports such rows as contract warnings)
        ident = self.ids.resolve(case["email"])
        if ident is None:
            trace["identity"] = "unresolved"
            return Result(case["case_id"], "INCOMPLETE", [], trace)
        trace["user_id"] = ident.user_id
        res = self.link_resources(case, ident)
        if not res:
            trace["resources"] = {}
            trace["unproven"] = ["<no linked record>"]
            return Result(case["case_id"], "INCOMPLETE", [], trace)
        for r in res.values():
            r.obs = [o for o in self.obs_by_rid.get(r.rid, [])
                     if self.in_window(o, case) and self.citable(o, case)]
            r.category = self.category_of(r, case)
            r.rule = self.policy.decide(r.category, self.condition_facts(r, case))
            r.action = r.rule.action
            self.assess(r, case)
            if r.state == "UNKNOWN" and r.action == "DELETE":
                proof = self.absence_proof(r, case)
                if proof:
                    r.state, r.evidence = "COMPLIANT", proof
        steps = self.plan(res)
        # COMPLETE needs evidence for every required state; a record whose
        # state the evidence cannot establish keeps the case open, with the
        # check that must pass listed as pending verification.
        unproven = [r.rid for r in res.values() if not r.evidence]
        status = ("INCOMPLETE" if any(s["phase"] == "FINDING" for s in steps) or unproven
                  else "COMPLETE")
        trace["unproven"] = unproven
        if status == "INCOMPLETE":
            for s in steps:
                if s["phase"] != "FINDING":
                    s["evidence_id"] = ""
        trace["resources"] = {r.rid: dict(links=r.links, category=r.category, action=r.action,
                                          rule=r.rule.rule_id, condition_met=r.rule.condition_met,
                                          rule_note=r.rule.note, state=r.state, evidence=r.evidence,
                                          note=r.note)
                              for r in res.values()}
        return Result(case["case_id"], status, steps, trace)

    def plan(self, res):
        rows = []
        for r in res.values():
            find, fix, verify = ACTION_OPS[r.action]
            if r.state == "VIOLATION":
                rows.append(("FINDING", find, r, r.evidence))
                rows.append(("REMEDIATION", fix, r, ""))
                rows.append(("VERIFICATION", verify, r, ""))
            elif r.state in ("COMPLIANT", "UNKNOWN", "MISSING"):
                rows.append(("VERIFICATION", verify, r, r.evidence))
        phase_rank = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
        rows.sort(key=lambda x: (phase_rank[x[0]], OP_ORDER[x[0]].index(x[1]), x[2].system, x[2].rid))
        steps = []
        for i, (phase, op, r, ev) in enumerate(rows, 1):
            steps.append({"step_no": i, "phase": phase, "system_id": r.system,
                          "resource_id": r.rid, "operation": op, "state": STATE[op],
                          "evidence_id": ev})
        return steps
