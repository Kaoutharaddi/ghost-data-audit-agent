# Usage report

The pipeline uses two models, both from Anthropic's first-party API:

| Purpose | Model | Where | Log |
|---|---|---|---|
| Facts from screenshots and PDFs (media extraction) | `claude-opus-5-5` | `code/ghost/media.py` | `code/evaluation/media_api_usage.json` |
| Request intent (does the text authorize deletion?) | `claude-haiku-5-5` | `code/ghost/intent.py`, `code/ghost/llm.py` | `code/evaluation/llm_usage_log.json` |

Both logs **accumulate**. Each run appends an entry with one record per call, and the tokens come from the `usage` object of the API response. Nothing is overwritten.

Costs use Anthropic first-party list prices per million tokens:

- `claude-opus-5-5`: $4 input / $20 output.
- `claude-haiku-5-5`: $0.10 input / $0.50 output (prompts up to 100K tokens).

All figures below are measured unless marked *estimate*.

## 1. How the final output.csv uses the models

`python3 code/main.py` makes **no model call** for this dataset:

- Media facts are read from `code/data/media_extractions.api_v2.json`, written by one metered `claude-opus-5-5` run (§2).
- Every intent is read from `code/data/llm_cache.json`, written by one metered `claude-haiku-5-5` run (§3). All 518 evaluation requests are in the cache.

The participant's keyed run of `main.py` on 2026-10-25 21:36:59Z is the last entry in `llm_usage_log.json`. It made **0 billed calls**: all 1,068 intent lookups were cache hits. The final `output.csv` was regenerated without a key and is byte-identical. A run with or without a key therefore gives the same output and costs nothing.

So the cost of the final output is the cost of the two model runs whose results it reads:

| Model | Purpose | Calls | Input tokens | Output tokens | Cost |
|---|---|---|---|---|---|
| `claude-opus-5-5` | media extraction, prompt v2 (15 files) | 15 | 48,039 | 8,550 | $0.3632 |
| `claude-haiku-5-5` | intent labels (416 distinct texts) | 22 | 42,893 | 7,967 | $0.0083 |
| **Total** | | **37** | **90,932** | **16,517** | **$0.3714** |

| Per evaluation request (÷ 518) | Tokens (in + out) | Cost |
|---|---|---|
| `claude-opus-5-5` media | 109.2 | $0.000701 |
| `claude-haiku-5-5` intent | 98.2 | $0.000016 |
| **Overall** | **207.4** | **$0.000717** |

Both runs are shared by the whole dataset, so these per-request figures are averages, not per-request calls:

- The 15 media files serve many requests.
- The Haiku run also labelled the 32 samples, the 18 invented intent texts and the synthetic custom cases. Per labelled text it used 122.3 tokens and $0.000020.

## 2. Media extraction, `claude-opus-5-5` (run by the participant, 2026-10-24)

The participant ran `python3 code/ghost/media.py` in their own terminal with their own API key. Each run sends 15 files: 11 images, and 4 PDFs as text.

| Run (UTC start) | Prompt | Output file | Calls | Input tokens | Output tokens | Cost |
|---|---|---|---|---|---|---|
| 19:45:52 | v1 | `media_extractions.api.json` (superseded) | 15 | 40,014 | 7,513 | $0.3103 |
| 20:00:04 | v2 | — (the SDK found no API key; nothing was sent) | 0 | 0 | 0 | $0.00 |
| 20:00:12 | v2 | — (same authentication error) | 0 | 0 | 0 | $0.00 |
| 20:00:55 | v2 | `media_extractions.api_v2.json` (**used by the final output**) | 15 | 48,039 | 8,550 | $0.3632 |

## 3. Intent classification, `claude-haiku-5-5` (run by the participant, 2026-10-25)

The participant ran `python3 code/evaluation/intent_eval.py` once with their own key, from 21:36:10Z to 21:36:59Z.

- **Batches.** It sent every uncached text in batches of 20 (effort `low`, `json_schema` structured output): 22 calls covering 416 distinct texts. Every call ended with `stop_reason: end_turn`, and there were no errors, refusals or truncations.
- **Tokens and cost.** 42,893 input tokens and 7,967 output tokens, for $0.008274. Per call, the tokens are in `llm_usage_log.json`.
- **Results.** Haiku is right on 32/32 samples and 18/18 invented requests, and it disagrees with the previous rules on 0 of the 518 evaluation requests.

## 4. Earlier development usage

| What | Model | Calls | Input tokens | Output tokens | Cost |
|---|---|---|---|---|---|
| Intent call for case_538, under the earlier fallback design (2026-10-24) | `claude-opus-5-5` | 1 | not recorded | not recorded | < $0.01 (*estimate*) |
| Hand-written media cache, made in the coding session with no API call | `claude-opus-5-5` (Claude Code session) | 0 | ~28,000 (*estimate*) | ~4,000 (*estimate*) | not billed as API usage (~$0.19 equivalent) |

**Why case_538 has no tokens.** At the time, the client kept only a per-run summary, and later runs overwrote it. The call is known only from its cache entry: key `37744ddf…`, answer `OTHER`. It is recorded in `llm_usage_log.json` as a reconstructed entry with null token counts. The current code does not use that entry, because its cache keys carry the prompt version and the model.

**The hand-written cache.** `code/data/media_extractions.json` was written by hand by the coding agent:

- It viewed the 11 PNGs with the session's image tool.
- It read the 4 PDFs through `pdftotext -layout`.

It is kept only as a comparison reference. The final output does not use it.

## Totals

| | Calls | Input tokens | Output tokens | Cost |
|---|---|---|---|---|
| Final `main.py` run | 0 | 0 | 0 | $0.00 |
| Model runs the final output reads (§1) | 37 | 90,932 | 16,517 | $0.3714 |
| — per evaluation request | — | 175.5 | 31.9 | $0.000717 |
| All billed development usage (Opus media v1 + v2, Haiku intent) | 52 | 130,946 | 24,030 | $0.6817 |
| plus the unmetered Opus intent call | 1 | not recorded | not recorded | < $0.01 (*estimate*) |
| — per evaluation request (metered) | — | 252.8 | 46.4 | $0.001316 |

| Per model (all billed usage) | Calls | Input tokens | Output tokens | Cost |
|---|---|---|---|---|
| `claude-opus-5-5` | 30 (+1 unmetered) | 88,053 | 16,063 | $0.6735 |
| `claude-haiku-5-5` | 22 | 42,893 | 7,967 | $0.0083 |
