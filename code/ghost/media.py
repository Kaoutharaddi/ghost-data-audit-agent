"""Extract structured facts from screenshot and PDF evidence with the Claude API.

    python3 code/ghost/media.py --dry-run          # list what would be sent; no key needed
    python3 code/ghost/media.py                    # extract all 15 media files
    python3 code/ghost/media.py --only marketing_profile_console.png

Output never overwrites the hand-written cache used by the pipeline
(code/data/media_extractions.json).  By default it goes to
code/data/media_extractions.api.json; compare the two with
`python3 code/ghost/compare_media.py`.

Every API call is metered from the response's `usage` object and written to
code/evaluation/media_api_usage.json, which accumulates one entry per run
(per-call records + run totals + totals over all runs).  Both files are
saved after each call, so an interrupted run keeps what it finished.

Images are sent as base64 image blocks.  PDFs are sent as text extracted with
`pdftotext -layout` when poppler is installed, otherwise as base64 document
blocks.  Requires ANTHROPIC_API_KEY (or another credential the SDK resolves).
"""
import argparse
import base64
import csv
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
ROOT = os.path.dirname(CODE)
CACHE = os.path.join(CODE, "data", "media_extractions.json")
DEFAULT_OUT = os.path.join(CODE, "data", "media_extractions.api.json")
DEFAULT_USAGE = os.path.join(CODE, "evaluation", "media_api_usage.json")
MODEL = os.environ.get("GHOST_VISION_MODEL", "claude-opus-5-5")
# Anthropic first-party list prices, USD per million tokens (input, output)
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
          "claude-haiku-5-5": (0.10, 0.50)}
MEDIA_EXT = (".png", ".jpg", ".jpeg", ".pdf")

PROMPT = """You are extracting facts from one business artifact (screenshot or document) of an online shop.
Return ONLY JSON: {"summary": str, "references": [str], "records": [ ... ]}.

"references": every business reference visible anywhere in the artifact, copied exactly:
document / statement / order / ticket numbers, file names, customer keys (user_N),
resource ids, and email addresses.

"records": one entry per data resource that the artifact itself displays - its own
fields or state are visible - and that carries an explicit resource id (account_N,
search_N, analytics_N, support_N, export_N, data_flow_N, backup_N, audit_record_N,
tombstone_N, legal_hold_N, marketing_N, object_N). A resource that is only mentioned as
a link or reference inside another record's panel (for example "Support: CS-1 / support_9")
is NOT a record: put it in "references" only. Do not invent resource ids for business
references (statement numbers, file names); list those in "references".

Each record:
  {"system": owning service (account_service, search_service, analytics_platform,
             support_platform, export_service, integration_service, backup_service,
             security_service, identity_service, legal_service, marketing_platform, object_storage),
   "resource_id": str, "present": true|false,
   "category": one of USER_PROFILE, SEARCH_INDEX_ENTRY, ANALYTICS_PROFILE, SUPPORT_MESSAGE,
               SUPPORT_ATTACHMENT, USER_EXPORT, MARKETING_PROFILE, SECURITY_AUDIT_LOG,
               LEGAL_HOLD_RECORD, BACKUP_ARCHIVE, ACCOUNT_TOMBSTONE, DATA_FLOW,
   "pii": {"name"?, "email"?, "phone"?, "email_hash"?, "user_ref"?} - only identifiers shown as
          part of this record,
   "keys": [identifiers that tie the record to a person: emails, user_N references],
   "status": "IDENTIFIABLE" | "ANONYMIZED" | "REDACTED" | "",
   "enabled": true|false|null, "restore_allowed": true|false|null}

Category rules:
- A document or file that a customer uploaded or attached to a support ticket (the artifact
  is the file itself, or shows its file properties) is SUPPORT_ATTACHMENT; a conversation
  or message thread is SUPPORT_MESSAGE.
Status rules (fill "status" only for analytics profiles, support messages and account
tombstones; use "" for every other category):
- IDENTIFIABLE if the record shows a direct identifier of a person: name, email, phone or
  email hash. A stable internal reference such as user_N alone is not a direct identifier.
- REDACTED / ANONYMIZED if such identifiers are shown as removed, masked or empty.
Flag rules:
- "enabled" (connectors / data flows): true if shown active, healthy or syncing; false if shown
  disabled, paused or stopped; null if not shown.
- "restore_allowed" (backups / archives): false only if the artifact shows restore blocked,
  denied, locked or on hold for this archive; true if the archive is listed as available or
  restorable with no such block; null only if the archive's restore state cannot be judged.
Report what is shown; do not decide whether a deletion is complete. Ignore decoy rows that
merely look similar unless they carry their own resource id."""


def media_files():
    with open(os.path.join(ROOT, "dataset", "evidence_files.csv"), encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [(r["file_id"], os.path.join(ROOT, "dataset", r["file_path"]))
            for r in rows if r["file_path"].lower().endswith(MEDIA_EXT)]


def pdf_text(path):
    try:
        return subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True,
                              text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def build_content(path):
    """Return (content blocks, how the file was sent)."""
    if path.lower().endswith(".pdf"):
        text = pdf_text(path)
        if text is not None:
            return [{"type": "text", "text": PROMPT + "\n\nDocument text:\n" + text}], "pdf-as-text"
        data = base64.standard_b64encode(open(path, "rb").read()).decode("utf-8")
        return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": data}},
                {"type": "text", "text": PROMPT}], "pdf-as-document"
    data = base64.standard_b64encode(open(path, "rb").read()).decode("utf-8")
    media = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return [{"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
            {"type": "text", "text": PROMPT}], "image"


def parse_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def call_record(file_id, name, sent_as, msg):
    u = msg.usage
    rec = {"file_id": file_id, "file": name, "sent_as": sent_as, "model": msg.model,
           "message_id": getattr(msg, "id", None), "stop_reason": msg.stop_reason,
           "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
           "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
           "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
           "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    price = PRICES.get(MODEL)
    rec["cost_usd"] = (round(rec["input_tokens"] * price[0] / 1e6 + rec["output_tokens"] * price[1] / 1e6, 6)
                       if price else None)
    return rec


def totals(calls):
    t = {"calls": len(calls), "model": MODEL,
         "input_tokens": sum(c["input_tokens"] for c in calls),
         "output_tokens": sum(c["output_tokens"] for c in calls),
         "cache_read_input_tokens": sum(c["cache_read_input_tokens"] for c in calls),
         "cache_creation_input_tokens": sum(c["cache_creation_input_tokens"] for c in calls)}
    costs = [c["cost_usd"] for c in calls if c["cost_usd"] is not None]
    t["cost_usd"] = round(sum(costs), 6) if len(costs) == len(calls) else None
    t["pricing_note"] = "USD list price per million tokens (input, output): %s" % (PRICES.get(MODEL),)
    return t


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def run(out_path, usage_path, only=None, dry_run=False, client=None):
    if os.path.abspath(out_path) == os.path.abspath(CACHE):
        sys.exit("refusing to overwrite the hand-written cache %s; choose another --out" % CACHE)
    files = [(fid, p) for fid, p in media_files() if not only or os.path.basename(p) in only]
    if dry_run:
        for fid, p in files:
            _, how = build_content(p)
            print(f"{fid}\t{os.path.basename(p)}\t{how}")
        print(f"{len(files)} files would be sent to {MODEL}; output -> {out_path}; usage -> {usage_path}")
        return
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {"_about": (f"Produced by code/ghost/media.py with {MODEL} via the Anthropic API "
                      f"(run started {started}). Raw model output parsed as JSON; not reviewed by hand.")}
    calls, errors = [], []

    # the usage file accumulates one entry per run; older single-run files are kept as run 1
    history = []
    if os.path.exists(usage_path):
        old = json.load(open(usage_path, encoding="utf-8"))
        history = old["runs"] if "runs" in old else [old]

    def save():
        write_json(out_path, out)
        runs = history + [{"started": started, "out": os.path.relpath(out_path, ROOT),
                           "calls": calls, "errors": errors, "totals": totals(calls)}]
        allc = [c for r in runs for c in r["calls"]]
        write_json(usage_path, {"runs": runs, "totals_all_runs": totals(allc)})

    for fid, path in files:
        name = os.path.basename(path)
        content, how = build_content(path)
        try:
            msg = client.messages.create(model=MODEL, max_tokens=16000,
                                         messages=[{"role": "user", "content": content}])
        except Exception as exc:  # keep going; the failure is recorded
            errors.append({"file_id": fid, "file": name, "error": repr(exc)})
            print(f"{fid} {name}: API error {exc!r}", file=sys.stderr)
            save()
            continue
        calls.append(call_record(fid, name, how, msg))  # billed even if parsing fails
        try:
            if msg.stop_reason == "refusal":
                raise ValueError("model declined (stop_reason=refusal)")
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            out[name] = parse_json(text)
            print(f"{fid} {name}: {len(out[name].get('records', []))} records, "
                  f"{calls[-1]['input_tokens']} in / {calls[-1]['output_tokens']} out tokens", file=sys.stderr)
        except ValueError as exc:
            errors.append({"file_id": fid, "file": name, "error": str(exc)})
            print(f"{fid} {name}: {exc}", file=sys.stderr)
        save()
    save()
    print(json.dumps(totals(calls), indent=2))
    if errors:
        print(f"{len(errors)} file(s) failed; see {usage_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--usage", default=DEFAULT_USAGE)
    ap.add_argument("--only", nargs="*", help="file names to extract (default: all media files)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.out, a.usage, set(a.only) if a.only else None, a.dry_run)


if __name__ == "__main__":
    main()
