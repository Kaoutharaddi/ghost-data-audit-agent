"""Evidence ingestion: turn every participant-visible evidence file into
uniform `Observation` records.

An observation says: "evidence E, at time T, looked at resource R in system S
and saw it in this state, carrying these identifiers".  Observations are the
only thing the reasoning layer consumes, so every file format (logs, JSON,
SQL snapshots, PDFs, screenshots) is reduced to the same shape here.
"""
import csv
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field

from .logparse import parse_file

NULLS = {None, "", "null", "None", "NULL", "-", "—"}

# resource-id prefix -> owning system (used when a source does not name it)
PREFIX_SYSTEM = [
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("analytics_", "analytics_platform"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("data_flow_", "integration_service"),
    ("backup_", "backup_service"),
    ("audit_record_", "security_service"),
    ("tombstone_", "identity_service"),
    ("legal_hold_", "legal_service"),
    ("marketing_", "marketing_platform"),
    ("cache_", "cache_service"),
    ("email_delivery_", "email_delivery_service"),
    ("object_", "object_storage"),
]


def system_for(rid):
    for prefix, system in PREFIX_SYSTEM:
        if rid.startswith(prefix):
            return system
    return ""


def clean(v):
    if isinstance(v, str):
        v = v.strip()
    return None if v in NULLS else v


@dataclass
class Observation:
    eid: str                 # file_id of the evidence
    ts: str                  # when the resource was observed
    system: str
    rid: str
    present: object = None   # True / False / None (unknown)
    category: str = ""       # record_type when the source states it
    pii: dict = field(default_factory=dict)   # identifiers exposed IN the record
    keys: set = field(default_factory=set)    # identifiers used to find/link it
    enabled: object = None   # data flows
    restore_allowed: object = None  # backups
    status: str = ""         # raw state label (IDENTIFIABLE, ANONYMIZED, ...)
    hint: str = ""           # free-form source hint (attachment, contact, ...)
    retain_until: str = None # end of a retention period / legal hold, when shown
    source: str = ""         # parser that produced it

    def identifiers(self):
        vals = set(self.keys)
        for v in self.pii.values():
            if v:
                vals.add(v)
        return vals


def _pii(**kw):
    return {k: clean(v) for k, v in kw.items() if clean(v) is not None}


# ---------------------------------------------------------------- generic

RESULT_STATE = {
    "FOUND": dict(present=True),
    "NOT_FOUND": dict(present=False),
    "IDENTIFIABLE": dict(present=True, status="IDENTIFIABLE"),
    "ANONYMIZED": dict(present=True, status="ANONYMIZED"),
    "REDACTED": dict(present=True, status="REDACTED"),
    "ACTIVE": dict(present=True, status="ACTIVE"),
    "DISABLED": dict(present=True, status="DISABLED"),
    "BLOCKED": dict(present=True, status="BLOCKED"),
}


def record_lookup_obs(eid, ts, system, rid, record_type, result, subject, source):
    st = RESULT_STATE.get(result, {})
    o = Observation(eid=eid, ts=ts, system=system or system_for(rid), rid=rid,
                    category=record_type or "", source=source, **st)
    subject = clean(subject)
    if subject and subject.lower() != "no direct identifier":
        o.keys.add(subject)
    if result == "ACTIVE":
        if (record_type == "DATA_FLOW") or rid.startswith("data_flow_"):
            o.enabled = True
        else:
            o.restore_allowed = True
    elif result == "DISABLED":
        o.enabled = False
    elif result == "BLOCKED":
        o.restore_allowed = False
    elif result == "IDENTIFIABLE" and subject:
        # the subject value is what keeps the record identifiable
        o.pii["identifier"] = subject
    elif result == "FOUND" and subject and not subject.startswith("user_"):
        o.pii["identifier"] = subject
    return o


# ---------------------------------------------------------------- logs

def _b(v):
    v = clean(v)
    if v is None:
        return None
    return str(v).lower() in ("true", "1", "yes")


def parse_log(eid, path):
    recs = parse_file(path)
    obs = []
    started = {}  # trace_id -> lookup key used by the *_started event
    for r in recs:
        ev = r.get("event", "")
        tr = r.get("trace_id", "")
        key = None
        for k in ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key",
                  "subject_ref"):
            if clean(r.get(k)):
                key = clean(r.get(k))
        if ev.endswith("_started") and tr:
            started[tr] = key
            continue
        rid = clean(r.get("resource_id"))
        if not rid or not re.match(r"^[a-z_]+_\d+$", rid):
            continue
        ts = r["ts"]
        svc = r.get("service", "")
        system = system_for(rid) or svc
        o = Observation(eid=eid, ts=ts, system=system, rid=rid, source="log:" + ev)
        lk = started.get(tr)
        if lk:
            o.keys.add(lk)
        for k in ("correlation_key", "subject_reference", "subject_ref", "customer_reference"):
            if clean(r.get(k)):
                o.keys.add(clean(r[k]))
        if ev == "record_lookup":
            o = record_lookup_obs(eid, ts, clean(r.get("system")), rid, r.get("record_type"),
                                  r.get("result"), r.get("subject_reference"), "log:record_lookup")
        elif ev in ("profile_lookup_completed",):
            rows = r.get("rows")
            status = r.get("http_status")
            o.present = (rows not in (None, "0")) and status != "404"
            o.pii.update(_pii(email=r.get("email"), phone=r.get("phone"),
                              customer_reference=r.get("customer_reference")))
            if o.present and not o.pii and lk:
                o.pii["identifier"] = lk
        elif ev == "index_query_completed":
            o.present = r.get("result") == "hit"
            o.pii.update(_pii(email=r.get("email"), customer_reference=r.get("customer_reference")))
            if o.present and not o.pii:
                o.pii["identifier"] = lk or clean(r.get("correlation_key"))
        elif ev in ("analytics_profile_read_completed", "analytics_profile_read"):
            o.present = True
            o.pii.update(_pii(email=r.get("email"), phone=r.get("phone"),
                              email_hash=r.get("email_hash"),
                              customer_reference=r.get("customer_reference")))
            if r.get("direct_identifiers") not in (None, "0") and not o.pii and lk:
                o.pii["identifier"] = lk
            o.status = "IDENTIFIABLE" if o.pii else "ANONYMIZED"
        elif ev == "analytics_identity_scan":
            o.present = True
            o.status = "IDENTIFIABLE"
            o.pii["identifier"] = clean(r.get("matched_value")) or lk
            o.keys.add(o.pii["identifier"])
        elif ev == "contact_lookup_completed":
            o.present = r.get("rows") not in (None, "0")
            o.hint = "contact"
            if o.present:
                o.pii["identifier"] = clean(r.get("correlation_key")) or lk
        elif ev == "support_ticket_read_completed":
            o.present = True
            o.pii.update(_pii(name=r.get("requester_name"), email=r.get("requester_email"),
                              phone=r.get("requester_phone")))
            o.status = "IDENTIFIABLE" if o.pii else "REDACTED"
            if clean(r.get("customer_reference")):
                o.keys.add(clean(r["customer_reference"]))
        elif ev in ("message_pii_scan", "attachment_pii_scan"):
            o.present = True
            o.status = "IDENTIFIABLE"
            o.pii["identifier"] = clean(r.get("matched_value")) or lk
            o.keys.add(o.pii["identifier"])
            if ev == "attachment_pii_scan":
                o.hint = "attachment"
        elif ev == "message_read":
            o.present = True
            txt = r.get("message_text", "")
            o.status = "REDACTED" if "REDACTED" in txt else "IDENTIFIABLE"
        elif ev == "contact_query_completed":
            o.present = r.get("rows") not in (None, "0")
            o.hint = "contact"
            if o.present and lk:
                o.pii["identifier"] = lk
        elif ev == "deletion_marker_check":
            o.present = True if r.get("marker_found") == "false" else None
            o.hint = "contact"
        elif ev in ("connector_config_read_completed", "connector_heartbeat", "connector_config_loaded"):
            o.present = True
            o.enabled = _b(r.get("enabled"))
            o.pii.update(_pii(email=r.get("customer_email")))
            if clean(r.get("customer_reference")):
                o.keys.add(clean(r["customer_reference"]))
        elif ev in ("restore_policy_read_completed",):
            o.present = _b(r.get("archive_present"))
            o.restore_allowed = _b(r.get("restore_allowed"))
            o.pii.update(_pii(email=r.get("customer_email")))
        elif ev == "restore_candidate_evaluated":
            o.present = True
            eligible = _b(r.get("eligible"))
            block = _b(r.get("deletion_block"))
            o.restore_allowed = bool(eligible) and not block
        elif ev == "restore_authorization_completed":
            o.present = True
            o.restore_allowed = r.get("decision") not in ("deny", "denied")
        elif ev in ("audit_record_read_completed",):
            o.present = r.get("rows", "1") != "0"
            o.retain_until = clean(r.get("retention_until"))
        elif ev == "retained_order_loaded":
            o.present = True
            o.status = "RETAINED" if r.get("access") == "allowed" else ""
        elif ev in ("export_metadata_read_completed", "export_download_check_completed"):
            o.present = _b(r.get("available"))
            o.pii.update(_pii(email=r.get("owner_email")))
        elif ev == "thumbnail_failed":
            continue
        else:
            continue
        o.keys.discard(None)
        obs.append(o)
    return obs


# ---------------------------------------------------------------- JSON records

def _analytics_obs(eid, ts, rec, source):
    o = Observation(eid=eid, ts=ts, system="analytics_platform", rid=rec["resource_id"],
                    present=True, source=source)
    o.pii = _pii(user_ref=rec.get("user_id") or rec.get("customer_reference"),
                 email=rec.get("email"), phone=rec.get("phone"), email_hash=rec.get("email_hash"))
    o.status = "IDENTIFIABLE" if o.pii else "ANONYMIZED"
    return o


def _snapshot_record_obs(eid, ts, store, coll, rec, source):
    rid = rec.get("resource_id")
    if not rid:
        return None
    system = store if store in dict(PREFIX_SYSTEM).values() else system_for(rid)
    o = Observation(eid=eid, ts=ts, system=system_for(rid) or system, rid=rid,
                    present=True, source=source)
    if coll in ("customer_profiles",):
        o.pii = _pii(user_ref=rec.get("customer_reference"), name=rec.get("full_name"),
                     email=rec.get("email"), phone=rec.get("phone"),
                     email_hash=rec.get("email_hash"))
        st = (rec.get("account_status") or "").upper()
        if st in ("DELETED",):
            o.present = False
    elif coll == "customer_analytics_reports":
        return _analytics_obs(eid, ts, rec, source)
    elif coll == "customer_search_documents":
        doc = rec.get("document") or rec
        o.pii = _pii(name=doc.get("display_name"), email=doc.get("email"),
                     user_ref=doc.get("customer_reference"))
    elif coll == "backup_catalog":
        o.keys.add(clean(rec.get("subject_reference")))
        o.restore_allowed = rec.get("restore_allowed")
    elif coll == "connector_configurations":
        o.keys.add(clean(rec.get("subject_selector")))
        o.enabled = rec.get("enabled")
    elif coll == "security_audit_events":
        o.keys.add(clean(rec.get("subject_reference")))
    elif coll == "support_conversations":
        o.keys.add(clean(rec.get("subject_reference")))
        txt = rec.get("message_text") or ""
        emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+\w", txt)
        if emails:
            o.pii["email"] = emails[0]
            o.status = "IDENTIFIABLE"
        else:
            o.status = "REDACTED" if "REDACTED" in txt or "[" in txt else ""
        o.hint = "message"
        o.text = txt
    elif coll == "ticket_attachments":
        o.pii = _pii(email=rec.get("uploaded_by"))
        o.hint = "attachment"
    elif coll == "customer_export_jobs":
        o.pii = _pii(email=rec.get("owner_email"))
        o.present = bool(rec.get("download_available", True))
    elif coll == "retained_order_records":
        o.keys.add(clean(rec.get("subject_reference")))
        o.status = "RETAINED"
        o.retain_until = clean(rec.get("retained_until"))
    elif coll == "deleted_customer_references":
        o.keys.add(clean(rec.get("subject_reference")))
        o.pii = _pii(name=rec.get("name"), email=rec.get("email"), phone=rec.get("phone"),
                     email_hash=rec.get("email_hash"))
        o.status = "IDENTIFIABLE" if o.pii else "REDACTED"
        o.category = "ACCOUNT_TOMBSTONE"
        o.account_status = (rec.get("account_status") or "").upper()
    else:
        return None
    o.keys.discard(None)
    return o


def parse_snapshot_bundle(eid, path, scopes=None):
    data = json.load(open(path, encoding="utf-8"))
    obs = []
    for snap in data.get("snapshots", []):
        ts = snap.get("captured_at")
        coll = snap.get("collection")
        store = snap.get("data_store")
        if coll == "customer_profiles" and not snap.get("records") and scopes is not None:
            # an empty profile collection proves no profile row exists
            scopes.append((store, ts, "bundle:" + coll))
        for rec in snap.get("records", []):
            if coll == "operational_record_history":
                obs.append(record_lookup_obs(eid, rec.get("recorded_at", ts), rec.get("system"),
                                             rec["resource_id"], rec.get("record_type"),
                                             rec.get("result"), rec.get("lookup_value"),
                                             "bundle:history"))
                continue
            o = _snapshot_record_obs(eid, ts, store, coll, rec, "bundle:" + coll)
            if o:
                obs.append(o)
    return obs


def parse_record_list(eid, path):
    """Files shaped {records:[{recorded_at, system, resource_id, record_type, result, subject_reference}]}"""
    data = json.load(open(path, encoding="utf-8"))
    obs = []
    for rec in data.get("records", []):
        if "record_type" in rec:
            obs.append(record_lookup_obs(eid, rec.get("recorded_at"), rec.get("system"),
                                         rec["resource_id"], rec.get("record_type"),
                                         rec.get("result"), rec.get("subject_reference"),
                                         "records"))
    return obs


def parse_collection_file(eid, path):
    """Files shaped {captured_at, collection, records:[...]}"""
    data = json.load(open(path, encoding="utf-8"))
    ts = data.get("captured_at")
    coll = data.get("collection")
    obs = []
    for rec in data.get("records", []):
        o = _snapshot_record_obs(eid, ts, "", coll, rec, "collection:" + coll)
        if o:
            obs.append(o)
    return obs


def parse_service_export(eid, path):
    data = json.load(open(path, encoding="utf-8"))
    obs = []
    for system, colls in data.get("service_exports", {}).items():
        for coll, recs in colls.items():
            for rec in recs:
                rid = rec.get("resource_id")
                ts = rec.get("observed_at") or data.get("exported_at")
                o = Observation(eid=eid, ts=ts, system=system_for(rid) or system, rid=rid,
                                present=True, source="export:" + coll)
                if coll == "profile_lookups":
                    o.present = rec.get("status_code") == 200
                    prof = rec.get("profile") or {}
                    o.pii = _pii(user_ref=prof.get("customer_reference"), email=prof.get("email"),
                                 email_hash=prof.get("email_hash"), name=prof.get("full_name"),
                                 phone=prof.get("phone"))
                elif coll == "profiles":
                    o = _analytics_obs(eid, ts, rec, "export:profiles")
                elif coll == "restore_entries":
                    o.restore_allowed = rec.get("restore_allowed")
                    o.pii = _pii(email=rec.get("customer_email"))
                    o.keys.add(clean(rec.get("customer_reference")))
                elif coll == "exports":
                    o.pii = _pii(email=rec.get("owner_email"), phone=rec.get("owner_phone"))
                    o.keys.add(clean(rec.get("customer_reference")))
                elif coll == "connectors":
                    o.enabled = rec.get("enabled")
                    o.pii = _pii(email=rec.get("customer_email"))
                    o.keys.add(clean(rec.get("customer_reference")))
                elif coll == "index_queries":
                    docs = (rec.get("hits") or {}).get("documents") or []
                    o.keys.add(clean(rec.get("query")))
                    o.present = bool(docs)
                    for d in docs:
                        if d.get("resource_id") == rid:
                            o.pii = _pii(email=d.get("email"), name=d.get("display_name"),
                                         phone=d.get("phone"), email_hash=d.get("email_hash"),
                                         user_ref=d.get("customer_reference"))
                elif coll == "audit_records":
                    o.keys.add(clean(rec.get("subject_reference")))
                    o.retain_until = clean(rec.get("retention_until"))
                elif coll == "tickets":
                    o.pii = _pii(name=rec.get("requester_name"), email=rec.get("requester_email"),
                                 phone=rec.get("requester_phone"))
                    o.keys.add(clean(rec.get("customer_reference")))
                    o.status = "IDENTIFIABLE" if o.pii else "REDACTED"
                else:
                    continue
                o.keys.discard(None)
                obs.append(o)
    return obs


# ---------------------------------------------------------------- SQL snapshots

def _load_sql(path):
    con = sqlite3.connect(":memory:")
    sql = open(path, encoding="utf-8").read()
    con.executescript(sql)
    return con, sql


def parse_sql(eid, path, captured_at):
    con, sql = _load_sql(path)
    m = re.search(r"Captured at:\s*(\S+)", sql)
    ts = m.group(1) if m else captured_at
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    obs = []
    scopes = []  # (system, ts, table) -> full-table coverage
    cur = con.cursor()

    def rows(table):
        cur.execute(f'select * from "{table}"')
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    if "snapshot_scope" in tables:
        scope = {r["origin_system"]: r["captured_at"] for r in rows("snapshot_scope")}
        table_system = {
            "customer_profiles": "account_service", "customer_search_documents": "search_service",
            "customer_analytics_profiles": "analytics_platform", "support_records": "support_platform",
            "customer_exports": "export_service", "connector_subscriptions": "integration_service",
            "backup_restore_entries": "backup_service", "security_audit_records": "security_service",
        }
        for table, system in table_system.items():
            if system not in scope or table not in tables:
                continue
            sts = scope[system]
            scopes.append((system, sts, "inventory:" + table))
            for r in rows(table):
                o = Observation(eid=eid, ts=r.get("updated_at") or sts, system=system,
                                rid=r["resource_id"], present=True, source="sql:" + table)
                o.ts = sts
                if table == "customer_profiles":
                    o.pii = _pii(user_ref=r.get("customer_reference"), name=r.get("full_name"),
                                 email=r.get("email"), phone=r.get("phone"),
                                 email_hash=r.get("email_hash"))
                    if (r.get("account_status") or "").upper() == "DELETED":
                        o.present = False
                elif table == "customer_search_documents":
                    o.pii = _pii(user_ref=r.get("customer_reference"), name=r.get("display_name"),
                                 email=r.get("email"), phone=r.get("phone"),
                                 email_hash=r.get("email_hash"))
                elif table == "customer_analytics_profiles":
                    o.pii = _pii(user_ref=r.get("customer_reference"), email=r.get("email"),
                                 phone=r.get("phone"), email_hash=r.get("email_hash"))
                    o.status = "IDENTIFIABLE" if o.pii else "ANONYMIZED"
                elif table == "support_records":
                    o.pii = _pii(name=r.get("requester_name"), email=r.get("requester_email"),
                                 phone=r.get("requester_phone"))
                    o.keys.add(clean(r.get("customer_reference")))
                    o.status = "IDENTIFIABLE" if o.pii else "REDACTED"
                    o.text = r.get("message_excerpt") or ""
                elif table == "customer_exports":
                    o.pii = _pii(email=r.get("owner_email"), phone=r.get("owner_phone"))
                    o.keys.add(clean(r.get("customer_reference")))
                elif table == "connector_subscriptions":
                    o.pii = _pii(email=r.get("customer_email"))
                    o.keys.add(clean(r.get("customer_reference")))
                    o.enabled = bool(int(r.get("enabled")))
                elif table == "backup_restore_entries":
                    o.pii = _pii(email=r.get("customer_email"))
                    o.keys.add(clean(r.get("customer_reference")))
                    o.restore_allowed = bool(int(r.get("restore_allowed")))
                elif table == "security_audit_records":
                    o.keys.add(clean(r.get("subject_reference")))
                    o.retain_until = clean(r.get("retention_until"))
                o.keys.discard(None)
                obs.append(o)
    else:
        for table in tables:
            for r in rows(table):
                rid = r.get("resource_id")
                if not rid:
                    continue
                o = Observation(eid=eid, ts=ts, system=system_for(rid), rid=rid,
                                present=True, source="sql:" + table)
                o.hint = table
                if "record_json" in r:
                    rec = json.loads(r["record_json"])
                    sub = _snapshot_record_obs(eid, ts, r.get("origin_system"),
                                               r.get("collection_name"), rec, "sql:" + table)
                    if sub is None:
                        continue
                    sub.hint = table
                    o = sub
                else:
                    o.pii = _pii(user_ref=r.get("user_id"), name=r.get("full_name") or r.get("display_name"),
                                 email=r.get("email"), phone=r.get("phone"))
                    if r.get("source_resource_id"):
                        o.keys.add(r["source_resource_id"])
                    if (r.get("account_status") or "").upper() == "DELETED":
                        o.present = False
                obs.append(o)
        # every table in a full snapshot proves absence of anything not listed
        for table in tables:
            cur.execute(f'select * from "{table}" limit 1')
            cols = [d[0] for d in cur.description]
            if "origin_system" in cols:
                for (system,) in con.execute(f'select distinct origin_system from "{table}"'):
                    scopes.append((system, ts, table))
            elif table.startswith("customer_profiles"):
                scopes.append(("account_service", ts, table))
            elif table.startswith("search_documents"):
                scopes.append(("search_service", ts, table))
    con.close()
    return obs, scopes


# ---------------------------------------------------------------- media (PDF / screenshots)

def parse_media(eid, path, captured_at, extractions):
    """PDFs and screenshots are reduced to structured facts by the Claude
    vision extraction in ghost/media.py (default file:
    code/data/media_extractions.api_v2.json, keyed by file name).  Catalog
    references the artifact shows are resolved later by join_catalog()."""
    name = os.path.basename(path)
    facts = extractions.get(name, {})
    obs = []
    for f in facts.get("records", []):
        rid = f["resource_id"]
        o = Observation(eid=eid, ts=f.get("observed_at") or captured_at,
                        system=f.get("system") or system_for(rid), rid=rid,
                        present=f.get("present", True), source="media:" + name)
        o.pii = {k: v for k, v in (f.get("pii") or {}).items() if v}
        o.keys = set(f.get("keys") or [])
        o.enabled = f.get("enabled")
        o.restore_allowed = f.get("restore_allowed")
        o.status = f.get("status", "")
        o.category = f.get("category", "")
        o.hint = f.get("hint", "")
        obs.append(o)
    return obs


# ---------------------------------------------------------------- loader

class EvidenceStore:
    def __init__(self, dataset_dir, extractions_path):
        self.dataset_dir = dataset_dir
        self.manifest = {}
        self.obs = []
        self.scopes = []  # (eid, system, ts, table)
        self.media = {}   # eid -> {"text": extraction as text, "rids": resource ids extracted}
        extractions = {}
        if os.path.exists(extractions_path):
            extractions = json.load(open(extractions_path, encoding="utf-8"))
        with open(os.path.join(dataset_dir, "evidence_files.csv"), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self.manifest[row["file_id"]] = row
        for eid, row in sorted(self.manifest.items()):
            path = os.path.join(dataset_dir, row["file_path"])
            cap = row["captured_at"]
            ext = os.path.splitext(path)[1].lower()
            name = os.path.basename(path)
            if ext == ".log":
                got = parse_log(eid, path)
            elif ext == ".sql":
                got, scopes = parse_sql(eid, path, cap)
                self.scopes += [(eid, s, t, tb) for (s, t, tb) in scopes]
            elif ext == ".json":
                data = json.load(open(path, encoding="utf-8"))
                if "snapshots" in data:
                    sc = []
                    got = parse_snapshot_bundle(eid, path, sc)
                    self.scopes += [(eid, s_, t, tb) for (s_, t, tb) in sc]
                elif "service_exports" in data:
                    got = parse_service_export(eid, path)
                elif "collection" in data:
                    got = parse_collection_file(eid, path)
                else:
                    got = parse_record_list(eid, path)
            elif ext == ".txt":
                got = parse_handover(eid, path)
            elif ext in (".pdf", ".png", ".jpg", ".jpeg"):
                got = parse_media(eid, path, cap, extractions)
                facts = extractions.get(name, {})
                self.media[eid] = {"text": json.dumps(facts, ensure_ascii=False),
                                   "rids": {f.get("resource_id") for f in facts.get("records", [])}}
            else:
                got = []
            for o in got:
                if not o.ts:
                    o.ts = cap
                # an observation can never be later than the capture of its file
                o.file_ts = cap
            self.obs += got

    def captured_at(self, eid):
        return self.manifest[eid]["captured_at"]

    def join_catalog(self, catalog, aliases, identities):
        """Resolve catalog-mapped references shown in screenshots/PDFs.

        An artifact often shows a business reference (statement number, file
        name, customer key) rather than the internal resource id; the data
        catalog maps one to the other.  Each catalog link is attached to the
        most recent artifact captured at or before the link that shows its
        reference.  If the extraction did not already produce that resource,
        an observation is added: the record is present, and its exposed
        identifiers are the subject's identifiers visible in the artifact."""
        for (t, aid, rid, email, uid, _x) in catalog:
            alias, system = aliases.get(aid, (rid, ""))
            pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])", re.I)
            shown = [eid for eid, m in self.media.items()
                     if self.captured_at(eid) <= t and pat.search(m["text"])]
            if not shown:
                continue
            eid = max(shown, key=self.captured_at)
            if rid in self.media[eid]["rids"]:
                continue
            text = self.media[eid]["text"]
            ident = identities.users.get(uid)
            pii = {}
            if ident:
                for kind, vals in (("email", ident.emails), ("name", ident.names), ("phone", ident.phones)):
                    for v in vals:
                        if v and (v.lower() in text.lower()):
                            pii[kind] = v
            o = Observation(eid=eid, ts=self.captured_at(eid), system=system or system_for(rid), rid=rid,
                            present=True, source=f"media+catalog@{aid}")
            o.pii = pii
            o.keys = {email, uid}
            o.status = "IDENTIFIABLE" if pii else "REDACTED"
            o.hint = "attachment" if re.search(r"\.(pdf|png|jpe?g|zip)$", alias, re.I) else ""
            o.file_ts = self.captured_at(eid)
            self.obs.append(o)
            self.media[eid]["rids"].add(rid)


def parse_handover(eid, path):
    obs = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line.startswith("- "):
            continue
        parts = [p.strip() for p in line[2:].split("|")]
        if len(parts) != 6:
            continue
        ts, system, rid, rtype, result, subject = parts
        obs.append(record_lookup_obs(eid, ts, system, rid, rtype, result, subject, "handover"))
    return obs
