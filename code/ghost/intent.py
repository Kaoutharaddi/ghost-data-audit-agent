"""Decide whether a request text authorizes deletion.

What happens on a run:
  1. main.py (and evaluation/intent_eval.py) first sends every request text
     that is not yet in code/data/llm_cache.json to claude-haiku-5-5. Texts go
     in batches of 20 with structured JSON output (one label per text), and
     the answers are stored in the cache under PROMPT_VERSION + model + text
     hash. This needs ANTHROPIC_API_KEY.
  2. classify() then reads each text's label from that cache. DELETION means
     the request authorizes deletion; any other label is NOT_APPLICABLE.
  3. Only a text with no cached label (no key, or a failed / refused /
     truncated batch) falls back to rule_verdict(). These are generic cues:
     deletion verbs in several languages, the statement's non-deletion
     categories, and explicit refusals.

State of the committed cache: claude-haiku-5-5 labelled 416 distinct texts
in one keyed run of intent_eval.py on 2026-10-25 (22 billed calls; see
evaluation/llm_usage_log.json). They cover the 518 evaluation requests, the
32 samples, the 18 invented texts in evaluation/intent_cases.csv and the
synthetic evaluation/custom_dataset. The final output.csv therefore takes
every intent from Haiku labels, and a run without a key reproduces it from
the cache. The three texts in evaluation/custom_cases.csv were never sent,
so they are decided by the fallback rules.

Provenance: SYSTEM_PROMPT and the fallback rules were written from the
problem statement's categories, without re-reading the evaluation requests.
They had been read earlier in the same development session, so the prompt
and rules are not strictly independent of them. Phrases copied from
evaluation requests in an earlier version were removed.
"""
import re

PROMPT_VERSION = "intent-v2"

LABELS = ["DELETION", "ACCESS", "CORRECTION", "EXPORT", "MARKETING_OPT_OUT", "OTHER"]

SYSTEM_PROMPT = """You classify privacy requests sent to an online shop. For each request decide
whether the customer authorizes deletion of their account or personal data.

Labels:
- DELETION: the customer asks to delete, erase, remove or permanently close their account
  or their personal data, or asks the shop to finish or verify such a deletion. Allowing the
  shop to keep records the law requires does not change this.
- ACCESS: asks what data is held, or for a copy of it, without asking for deletion.
- CORRECTION: asks to correct, update or change some data while keeping the account.
- EXPORT: asks to download or receive their data in a portable format.
- MARKETING_OPT_OUT: asks to stop marketing or promotional messages only.
- OTHER: anything else that does not authorize deletion (login help, order questions, ...).

Rules:
- Requests can be in any language.
- An explicit refusal ("do not delete my account", "keep my account", "I still use it")
  means the request is NOT a deletion, even if a deletion verb appears.
- Removing one detail (an address, a phone number) while keeping the account is CORRECTION.
- Judge only the customer's words. Return one result per request id."""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string", "enum": LABELS},
                },
                "required": ["id", "label"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------- fallback rules
# Generic cues only, grouped by the statement's categories.

DELETE_CUES = [
    r"\bdelet", r"\beras", r"\bremov", r"\bwipe", r"forget me", r"\bclos(e|ed|ing) (my|this|the) account",
    r"\bis gone\b",
    r"supprim", r"suppression", r"fermer (mon|le) compte",            # fr
    r"elimin", r"borr", r"cerrar (mi|la) cuenta",                      # es / it
    r"exclu", r"apag", r"encerr",                                      # pt
    r"cancell", r"lösch", r"verwijder",                                # it / de / nl
    r"削除", r"消去", r"退会", r"삭제", r"탈퇴",                        # ja / ko
]

REFUSAL_CUES = [
    # negated deletion verb, any inflection ("without removing", "not deleted")
    r"\b(do not|don'?t|never|not|without)\b[^.!?]{0,25}\b(delet|eras|remov|clos)",
    r"\bnot (asking|requesting) (for )?(a )?deletion", r"\bnot a (deletion|request to delete)",
    r"\bkeep(ing)? (my|the) account", r"\bstill (use|need|want)\b",
    r"\bno (quiero )?(borr|elimin|cerr)", r"mantener (mi|la) cuenta",          # es
    r"não (quero )?(exclu|apag|encerr)", r"manter (minha|a) conta",           # pt
    r"\bne [^.]{0,20}pas [^.]{0,10}(supprim|ferm)", r"garder mon compte",      # fr
    r"nicht (lösch|schließ)", r"(lösch|schließ)\w*[^.]{0,30}\bnicht\b",          # de (verb ... nicht)
    r"konto [^.]{0,20}behalten",
    r"non (cancell|elimin)",                                                   # it
    r"しないで", r"残(して|したい)", r"하지 마", r"유지",                       # ja / ko
]

NON_DELETION_CUES = [
    # access
    r"\b(copy|access) (of|to) (my|the|all my) (data|information|personal)",
    r"\bwhat (personal )?(data|information) (do )?you (hold|have|store)",
    r"copia de (mis|los) datos", r"cópia dos (meus )?dados", r"copie de mes données", r"kopie meiner daten",
    # export
    r"\b(download|export)\b",
    # correction
    r"\b(correct|update|change|fix)\b (my|the)",
    # marketing opt-out
    r"\b(marketing|newsletter|promotional|unsubscribe)\b",
]

ACCOUNT_DELETION = re.compile(
    r"\b(delet|eras|clos|remov)\w*[^.!?]{0,20}\baccount|account[^.!?]{0,20}\b(deleted|erased|closed|removed)", re.I)


def rule_verdict(text):
    """Return (authorizes_deletion: bool, confident: bool, reason)."""
    t = text.lower()
    refusal = next((p for p in REFUSAL_CUES if re.search(p, t)), None)
    if refusal:
        return False, True, "rules: refusal cue " + refusal
    other = next((p for p in NON_DELETION_CUES if re.search(p, t)), None)
    delete = next((p for p in DELETE_CUES if re.search(p, t)), None)
    if other and not ACCOUNT_DELETION.search(t):
        return False, delete is None, "rules: non-deletion cue " + other
    if delete:
        return True, other is None, "rules: deletion cue " + delete
    return False, False, "rules: no deletion cue"


def classify(text, llm=None):
    """LLM label from the cache (or API), else the generic rules."""
    if llm is not None:
        label = llm.intent_label(text)
        if label:
            return label == "DELETION", "llm:" + label
    ok, _confident, reason = rule_verdict(text)
    return ok, reason + (" (no cached LLM answer)" if llm is not None else "")
