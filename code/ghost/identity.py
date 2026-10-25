"""Identity resolution: map a request email to the stable user_id and every
current or historical identifier that may still appear in older systems."""
import csv
import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field


def norm_email(v):
    return v.strip().lower()


def norm_phone(v):
    return re.sub(r"\D", "", v or "")


def norm_value(v):
    """Normalize an arbitrary identifier for comparison."""
    if v is None:
        return ""
    v = str(v).strip()
    if "@" in v:
        return norm_email(v)
    digits = norm_phone(v)
    if v.startswith("+") or (len(digits) >= 8 and re.fullmatch(r"[\d\s()+\-.]+", v)):
        return "tel:" + digits
    return v.lower()


@dataclass
class Identity:
    user_id: str
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    hashes: set = field(default_factory=set)
    names: set = field(default_factory=set)
    audit_ids: list = field(default_factory=list)   # (audit_id, event_time, field)

    def norm_values(self):
        vals = {self.user_id.lower()}
        vals |= {norm_email(e) for e in self.emails}
        vals |= {"tel:" + norm_phone(p) for p in self.phones}
        vals |= {h.lower() for h in self.hashes}
        return vals

    def matches(self, value):
        return norm_value(value) in self.norm_values()

    def name_matches(self, value):
        return bool(value) and value.strip().lower() in {n.lower() for n in self.names}


class IdentityIndex:
    def __init__(self, dataset_dir):
        self.users = {}
        self.by_email = {}
        with open(os.path.join(dataset_dir, "user_identities.csv"), encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                ident = Identity(user_id=r["user_id"])
                ident.emails.add(r["email"])
                ident.phones.add(r["phone"])
                ident.hashes.add(r["email_hash"])
                ident.names.add(r["name"])
                ident.current = r
                self.users[r["user_id"]] = ident
        with open(os.path.join(dataset_dir, "user_audit_log.csv"), encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                ident = self.users.get(r["user_id"])
                if not ident:
                    continue
                f = r["field_name"]
                for v in (r["old_value"], r["new_value"]):
                    if not v:
                        continue
                    if f == "email":
                        ident.emails.add(v)
                    elif f == "phone":
                        ident.phones.add(v)
                    elif f == "email_hash":
                        ident.hashes.add(v)
                    elif f == "name":
                        ident.names.add(v)
                ident.audit_ids.append((r["audit_id"], r["event_time"], f))
        for ident in self.users.values():
            for e in list(ident.emails):
                ident.hashes.add(hashlib.sha256(norm_email(e).encode()).hexdigest())
            for e in ident.emails:
                self.by_email.setdefault(norm_email(e), ident.user_id)

    def resolve(self, email):
        uid = self.by_email.get(norm_email(email))
        return self.users.get(uid) if uid else None
