"""Normalize the heterogeneous observability log formats into flat dicts.

Supported line shapes (all start with an ISO timestamp):
  kv       TS level=X service=Y trace_id=T event=E k=v k="v"
  json     TS {"severity":..,"service":..,"trace_id":..,"event":..}
  syslog   TS <15>1 node-1 SERVICE TRACE - - level=X k="v"
  bracket  TS [LEVEL] [SERVICE] [TRACE] EVENT: k="v"
  pipe     TS | LEVEL | SERVICE | TRACE | EVENT | k="v"
  cloud    TS requestId=TRACE level=X function=SERVICE message="k=\\"v\\" ..."
"""
import json
import re

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
SYSLOG_RE = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)$")
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+(\w+):\s*(.*)$")
PIPE_RE = re.compile(r"^\|?\s*(\w+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(\w+)\s*\|\s*(.*)$")


def _kv(text):
    out = {}
    for k, v in KV_RE.findall(text):
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"')
        out[k] = v
    return out


def parse_line(line):
    m = TS_RE.match(line.strip())
    if not m:
        return None
    ts, rest = m.groups()
    rec = {"ts": ts}
    if rest.startswith("{"):
        try:
            data = json.loads(rest)
        except ValueError:
            return None
        rec.update({k: str(v) if v is not None else "null" for k, v in data.items()})
        rec.setdefault("level", rec.get("severity", ""))
    elif rest.startswith("<"):
        sm = SYSLOG_RE.match(rest)
        if not sm:
            return None
        _node, service, trace, body = sm.groups()
        rec.update(_kv(body))
        rec["service"], rec["trace_id"] = service, trace
    elif rest.startswith("["):
        bm = BRACKET_RE.match(rest)
        if not bm:
            return None
        level, service, trace, event, body = bm.groups()
        rec.update(_kv(body))
        rec.update(level=level, service=service, trace_id=trace, event=event)
    elif rest.startswith("|") or re.match(r"^\w+\s*\|", rest):
        pm = PIPE_RE.match(rest)
        if not pm:
            return None
        level, service, trace, event, body = pm.groups()
        rec.update(_kv(body))
        rec.update(level=level, service=service, trace_id=trace, event=event)
    elif rest.startswith("requestId="):
        outer = _kv(rest)
        rec.update(_kv(outer.get("message", "")))
        rec["trace_id"] = outer.get("requestId", "")
        rec["service"] = outer.get("function", "")
        rec["level"] = outer.get("level", "")
    else:
        rec.update(_kv(rest))
    return rec


def parse_file(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = parse_line(line)
            if rec:
                out.append(rec)
    return out
