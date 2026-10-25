"""Anthropic client for intent classification (claude-haiku-5-5).

make_client() returns:
  * AnthropicClient when ANTHROPIC_API_KEY is set. prefill_intents() sends
    every uncached text in batches of 20 (effort low, json_schema output,
    max_tokens 8000). Each call is metered with the tokens from the API
    response's `usage` and priced with PRICES. The labels are written to
    code/data/llm_cache.json. An API error, a refusal, a max_tokens stop or
    unparsable JSON leaves those texts uncached, and intent.py falls back to
    its rules for them; an API error is logged with zero tokens.
  * CacheOnlyClient otherwise: it only reads the cache and never calls the
    API, so a run without a key gives the same answers as a keyed run for
    every cached text.

write_usage() appends one entry per run to code/evaluation/llm_usage_log.json
(per-call model, tokens and cost) and recomputes the totals. Nothing is
overwritten. A CacheOnlyClient run writes nothing.
"""
import datetime
import hashlib
import json
import os

INTENT_MODEL = os.environ.get("GHOST_INTENT_MODEL", "claude-haiku-5-5")
BATCH_SIZE = 20
LABELS_OK = {"DELETION", "ACCESS", "CORRECTION", "EXPORT", "MARKETING_OPT_OUT", "OTHER"}
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(CODE, "data", "llm_cache.json")
USAGE_LOG = os.path.join(CODE, "evaluation", "llm_usage_log.json")
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00),
          "claude-haiku-5-5": (0.10, 0.50)}   # USD per million tokens (input, output)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cost(model, tin, tout):
    p = PRICES.get(model)
    return None if p is None else round(tin * p[0] / 1e6 + tout * p[1] / 1e6, 6)


def _intent_key(text):
    from .intent import PROMPT_VERSION
    return PROMPT_VERSION + ":" + INTENT_MODEL + ":" + hashlib.sha256(text.encode()).hexdigest()


def _load_cache():
    return json.load(open(CACHE)) if os.path.exists(CACHE) else {}


class AnthropicClient:
    """Intent classification with claude-haiku-5-5: batched, structured JSON
    output, low effort, every billed call metered, answers cached per text."""

    def __init__(self, api_key):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.api_errors = (anthropic.APIStatusError, anthropic.APIConnectionError)
        self.cache = _load_cache()
        self.started = _now()
        self.calls = []        # billed API calls of this run
        self.cache_hits = 0

    def intent_label(self, text):
        key = _intent_key(text)
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]
        self.prefill_intents([text])
        return self.cache.get(key)

    def prefill_intents(self, texts, batch_size=BATCH_SIZE):
        """Classify every text not yet cached, in batches."""
        from .intent import SCHEMA, SYSTEM_PROMPT
        todo = list(dict.fromkeys(t for t in texts if _intent_key(t) not in self.cache))
        for i in range(0, len(todo), batch_size):
            batch = todo[i:i + batch_size]
            items = [{"id": f"r{j + 1}", "text": t} for j, t in enumerate(batch)]
            try:
                msg = self.client.messages.create(
                    model=INTENT_MODEL, max_tokens=8000, system=SYSTEM_PROMPT,
                    output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                    messages=[{"role": "user", "content": "Classify these requests:\n" + json.dumps(items, ensure_ascii=False)}])
            except self.api_errors as e:
                # nothing billed is known; these texts fall back to the rules
                self.calls.append({"at": _now(), "model": INTENT_MODEL, "purpose": "intent batch",
                                   "items": len(batch), "error": type(e).__name__,
                                   "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
                continue
            # every billed call is metered before its answer is used
            self.calls.append({"at": _now(), "model": msg.model, "purpose": "intent batch",
                               "items": len(batch), "stop_reason": msg.stop_reason,
                               "input_tokens": msg.usage.input_tokens,
                               "output_tokens": msg.usage.output_tokens,
                               "cost_usd": _cost(INTENT_MODEL, msg.usage.input_tokens, msg.usage.output_tokens)})
            if msg.stop_reason in ("refusal", "max_tokens"):
                continue  # no complete answer: these texts fall back to the rules
            text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "{}")
            try:
                by_id = {r["id"]: r["label"] for r in json.loads(text).get("results", [])}
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            for it in items:
                if by_id.get(it["id"]) in LABELS_OK:
                    self.cache[_intent_key(it["text"])] = by_id[it["id"]]
            tmp = CACHE + ".tmp"
            json.dump(self.cache, open(tmp, "w"), indent=1, ensure_ascii=False)
            os.replace(tmp, CACHE)

    def write_usage(self, meta=None, path=USAGE_LOG):
        """Append this run to the usage log and refresh the cumulative totals."""
        log = json.load(open(path)) if os.path.exists(path) else {"runs": []}
        log["runs"].append({"started": self.started, "finished": _now(), "purpose": "intent classification",
                            **(meta or {}), "billed_calls": len(self.calls),
                            "cache_hits": self.cache_hits, "calls": self.calls})
        allc = [c for r in log["runs"] for c in r.get("calls", [])]
        costs = [c.get("cost_usd") for c in allc]
        log["totals"] = {"runs": len(log["runs"]),
                         "billed_calls": sum(r.get("billed_calls", 0) for r in log["runs"]),
                         "input_tokens": sum(c.get("input_tokens") or 0 for c in allc),
                         "output_tokens": sum(c.get("output_tokens") or 0 for c in allc),
                         "cost_usd": round(sum(x for x in costs if x), 6),
                         "calls_without_metered_tokens": sum(1 for c in allc if c.get("input_tokens") is None)}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        json.dump(log, open(tmp, "w"), indent=2)
        os.replace(tmp, path)
        # start a fresh entry so a second write never records a call twice
        self.started, self.calls, self.cache_hits = _now(), [], 0


class CacheOnlyClient:
    """Used when no API key is set: answers come only from the on-disk cache,
    so a run without a key reproduces every answer a keyed run obtained.
    A cache miss returns None and the caller falls back to the rules."""

    def __init__(self):
        self.cache = _load_cache()
        self.cache_hits = 0
        self.misses = 0

    def intent_label(self, text):
        label = self.cache.get(_intent_key(text))
        if label:
            self.cache_hits += 1
        else:
            self.misses += 1
        return label

    def prefill_intents(self, texts, batch_size=BATCH_SIZE):
        pass  # nothing can be sent without a key

    def write_usage(self, meta=None, path=USAGE_LOG):
        pass  # no billed calls are possible without a key


def make_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return CacheOnlyClient()
    try:
        return AnthropicClient(key)
    except Exception:
        return CacheOnlyClient()
