# Vera, rebuilt: a grounded merchant-engagement bot

**magicpin AI Challenge submission** · Vaibhav Tripathi · `uvicorn vera.api:app`

## Approach

The deterministic layer makes every decision and checks every fact. The LLM is only used to write the prose, and anything it writes is rejected if it adds a fact.

```
context push ─► FactSheet (typed, provenance-tagged facts; all arithmetic in code)
            ─► Insight engine (merchant vs peer benchmarks, deltas, lapsed pools, offer gaps,
               review themes, seasonal beats, trends, digest ↔ cohort matches → ranked "so-whats")
/tick ──────► Scheduler (urgency × stakes × merchant-state fit; consent; suppression; ≤2 merchant-facing/merchant/tick)
            ─► Per-trigger playbook (28 families, English + Hinglish, 2 phrasings each):
               why-now hook with a hard fact → judgement → what Vera will do → ONE CTA last
            ─► Batched LLM rewrite (≤5 messages per call) ─► Validator gate ─► Critic (rubric) ─► pick
/reply ─────► Classifier (auto-reply / commit / question / later / decline / hostile / opt-out / off-topic / slot)
            ─► State machine → send the real deliverable on "yes" (post draft, checklist, patient note, booking)
```

## What makes it robust

- **No invented facts, enforced by code.** Every number and proper noun in a message must come from the four contexts. The validator extracts them, normalises them (0.021 ↔ 2.1%, 2,100 ↔ 2100) and rejects any candidate containing an unknown one. The template draft is always valid, so the bot never goes silent.
- **Specific even when the trigger payload is empty.** 75 of the 100 dataset triggers carry `{"placeholder": true}`. For those, the insight engine builds a specific message from the merchant's own numbers against category peers ("CTR 1.8% vs 3.0% avg for metro solo clinics"), their offer gaps and their review themes.
- **Adapts to data injected mid-test.**
  - Cached drafts are keyed on context versions, so pushed updates are used immediately.
  - Trigger kinds it has never seen are written from their own payload plus the matching category knowledge, e.g. heatwave → the "summer demand" digest item.
  - New digest items are cited with their source.
- **Replay-ready replies.**
  - Auto-replies are tracked per merchant, across conversation IDs: nudge once → wait → end.
  - "Let's do it" switches straight to delivering (never another qualifying question).
  - Abuse gets one apology plus a STOP option; STOP ends the conversation; GST-type questions are declined politely and steered back.
  - A customer's STOP only silences that customer, never the merchant.
- **Operational.**
  - Raw-body JSON parsing, so a missing Content-Type header is fine.
  - Idempotent versioned contexts (same or older version → 409).
  - `/tick` is deadline-bounded at 7.5 s; LLM calls use batches, token buckets per provider, circuit breakers and a template fallback.
  - State is written through to SQLite and auto-wiped between judge runs.

## Measured results (local replica of the official judge prompt)

- **30 canonical pairs:** 43.4/50 average. Specificity 8.7, category fit 9.0, merchant fit 8.9, decision quality 8.6, engagement 8.2.
- **60-minute lifecycle replica** (113 messages, including never-seen triggers, digest items and customers injected mid-run): 42.5/50 average, 0 operational penalties, tick p99 under 3.5s on the deployed free instance.
- **Official `judge_simulator.py`:** warmup, auto-reply, intent and hostile scenarios all pass.
- **Hardening from testing against LLM-played merchants:** customer STOP never silences the merchant; conversations move through stages (draft → done → follow-on → wrap-up) instead of looping; the bot writes Roman script only.

## Model choice and tradeoffs

- **Writer:** Gemini 3.5 Flash (with 3.8 Flash pooled in), thinking minimal for latency. **Critic:** Gemini 3.5 Flash-Lite. **Fallback writer:** Groq Llama-3.3-70B. All run at temperature 0 with a fixed seed, and responses are cached by prompt hash.
- **Free-tier limits** (about 10–15 requests/min) would be blown by one LLM call per message when a tick holds 20 triggers. So the templates are the main product, and the LLM rewrites them in batches when budget allows. The tradeoff is some phrasing variety in exchange for guaranteed grounding and zero timeouts.
- **Determinism:** the template path is fully deterministic. The LLM path is deterministic within a session through the cache, but depends on the available quota.

## What additional context would help most

- The merchant's real appointment calendar and open slots.
- Per-merchant reply-time patterns, to pick send windows.
- The WhatsApp template registry.
- Explicit "last N Vera messages" with engagement outcomes, to learn which persuasion levers work for each merchant.

## Run / test

```bash
uv sync && uv run uvicorn vera.api:app --port 8080     # bot
uv run pytest -q                                        # 13 contract + pipeline tests
uv run python -m eval.render_all --pairs --show         # the 30 canonical messages
uv run python -m eval.harness [--judge gemini]          # 60-min lifecycle replica + unseen injections
uv run python bot.py                                    # submission.jsonl
```
