"""Index of the global system_activity stream.

Activity rows are *claims*: a SUCCESS result never proves a resource state.
They are used for three grounded purposes only:
  * linking resources to a subject (data-catalog links, restore/sync lineage,
    deletion-worker batches),
  * learning which policy action the deletion workflow attempted for a
    resource (which reveals the record's data category), and
  * risk signals (a restore or connector that recreated data).
"""
import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


WORKER_OPS = {"DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW",
              "BLOCK_RESTORE", "RETAIN_RECORD"}


class ActivityIndex:
    def __init__(self, dataset_dir):
        with open(os.path.join(dataset_dir, "system_activity.csv"), encoding="utf-8") as fh:
            self.rows = list(csv.DictReader(fh))
        self.by_rid = defaultdict(list)
        self.catalog = []          # (time, activity_id, rid, email, user_id, exclusive)
        self.catalog_alias = {}    # activity_id -> (reference shown in artifacts, system)
        self.lineage = []          # (time, activity_id, source_rid, target_rid, kind)
        self.worker = []           # deletion_worker rows
        self.non_deletion = {}     # account rid -> activity row (privacy intake)
        for r in self.rows:
            r["_t"] = ts(r["event_time"])
            self.by_rid[r["resource_id"]].append(r)
            d = r["details"]
            et = r["event_type"]
            if et == "LINK_RESOURCE_IDENTITY":
                m = re.search(r"to ([\w.+-]+@[\w.-]+\w) and (user_\d+)", d)
                if m:
                    self.catalog.append((r["event_time"], r["activity_id"], r["resource_id"],
                                         m.group(1), m.group(2), "belongs only" in d))
                    # the catalog names the business reference it mapped, e.g.
                    # "export_1842 (NST-2026-0718-1842)" or "object_19 (file.pdf)"
                    a = re.search(re.escape(r["resource_id"]) + r" \(([^)]+)\)", d)
                    self.catalog_alias[r["activity_id"]] = (a.group(1) if a else r["resource_id"],
                                                            r["target_system"])
            elif "repopulated downstream resource" in d:
                m = re.search(r"downstream resource (\w+)", d)
                kind = "restore" if et == "RESTORE_BACKUP" else "sync"
                self.lineage.append((r["event_time"], r["activity_id"], r["resource_id"],
                                     m.group(1), kind))
            elif et == "CLASSIFY_REQUEST":
                self.non_deletion[r["resource_id"]] = r
            if r["source_system"] == "deletion_worker":
                r["_kind"] = self._kind(d)
                self.worker.append(r)

    @staticmethod
    def _kind(d):
        if d.startswith("Policy action completed"):
            return "policy"
        if "submitted primary profile" in d:
            return "submit"
        if "received the final result" in d or "stopped after" in d or "could not confirm" in d:
            return "result"
        if "remains enabled" in d or "can still be restored" in d:
            return "check"
        if "reported the requested operation as completed" in d:
            return "reported"
        return "other"

    def worker_ops(self, rid, until=None):
        out = []
        for r in self.by_rid.get(rid, []):
            if r["source_system"] != "deletion_worker":
                continue
            if until and r["event_time"] > until:
                continue
            out.append(r)
        return out

    def worker_window(self, start, end):
        return [r for r in self.worker if start <= r["_t"] <= end]
