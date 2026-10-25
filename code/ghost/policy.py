"""Apply dataset/deletion_rules.csv.

The CSV is the source of truth for the policy: which rule governs a data
category, under which condition, and with which required action.  The code
only knows how to *test* a kind of condition against the evidence; the tests
are selected by matching the condition text, so editing the CSV (actions,
or which condition a category uses) changes the agent's behaviour.

A condition test returns True, False or None (the evidence does not say).
  * True or None -> the rule's action applies (no evidence against it).
  * False        -> the test's `on_false` decides:
      "met"    the condition is the trigger of the treatment and is already
               satisfied (e.g. an analytics record that is no longer
               attributable): the required end state is unchanged;
      "DELETE" the condition was the only justification for keeping the
               record (retention period, active legal hold, shared support
               context, deleted-account tombstone): under a deletion request
               the record must go;
      "REDACT" deletion is only allowed for data of the subject alone; a
               shared attachment keeps the other subject's data and loses
               the subject's identifiers.

Data flows are not a data category in the CSV; they come from the
completeness rule of the problem statement ("no active sync, import, or
restore path can recreate the data") and are always required to be disabled.
"""
import csv
import os
import re
from dataclasses import dataclass

# condition text pattern -> (fact name tested, outcome when the fact is False)
CONDITION_TESTS = [
    (r"^\s*always\s*$", None, None),
    (r"still attributable", "attributable", "met"),
    (r"contains a user email", "has_email", "met"),
    (r"retention period", "retention_active", "DELETE"),
    (r"legal hold is active", "hold_active", "DELETE"),
    (r"belongs only to the deletion subject", "exclusive", "REDACT"),
    (r"shared .*context", "shared_context", "DELETE"),
    (r"restore access can be blocked", "restore_blockable", "DELETE"),
    (r"account is deleted", "account_deleted", "DELETE"),
]

FLOW_RULE = ("statement:completeness", "DATA_FLOW", "an active sync, import or restore path exists", "DISABLE")


@dataclass
class Decision:
    rule_id: str
    category: str
    condition: str
    condition_met: object   # True / False / None (unknown)
    action: str             # DELETE / REDACT / ANONYMIZE / RETAIN / BLOCK / DISABLE
    note: str = ""


class Policy:
    def __init__(self, dataset_dir):
        self.rules = {}
        with open(os.path.join(dataset_dir, "deletion_rules.csv"), encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                self.rules[r["data_category"]] = r

    def actions_for(self, categories):
        """CSV action of each category (used to map a workflow operation to a category)."""
        return {c: self._base_action(self.rules[c]) for c in categories if c in self.rules}

    @staticmethod
    def _base_action(rule):
        action = rule["required_action"].strip().upper()
        if action == "RETAIN" and re.search(r"restore", rule["condition"], re.I):
            return "BLOCK"   # keep the archive, block its restore path
        return action

    def decide(self, category, facts):
        if category == "DATA_FLOW":
            rid, cat, cond, action = FLOW_RULE
            return Decision(rid, cat, cond, True, action)
        rule = self.rules.get(category)
        if rule is None:
            return Decision("none", category, "category not in deletion_rules.csv", None, "DELETE",
                            "unknown category: treated as personal data to delete")
        action = self._base_action(rule)
        cond = rule["condition"]
        test = next(((fact, on_false) for pat, fact, on_false in CONDITION_TESTS
                     if re.search(pat, cond, re.I)), ("?", None))
        fact, on_false = test
        if fact is None:
            return Decision(rule["rule_id"], category, cond, True, action)
        if fact == "?":
            return Decision(rule["rule_id"], category, cond, None, action,
                            "condition not testable by the agent: rule applied")
        met = facts.get(fact)
        if met is False and on_false != "met":
            return Decision(rule["rule_id"], category, cond, False, on_false,
                            f"condition not met ({fact}=False): {on_false}")
        return Decision(rule["rule_id"], category, cond, met, action)
