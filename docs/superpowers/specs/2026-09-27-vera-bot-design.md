# Vera Bot — Design Spec (magicpin AI Challenge)

**Date:** 2026-09-27 · **Author:** Vaibhav Tripathi (with Claude) · **Status:** Draft for review

---

## 1. Goal and success criteria

Build a hosted HTTP bot that plays magicpin's merchant assistant **Vera** and ranks in the **top 10** of the magicpin AI Challenge. That ranking unlocks the Phase-4 replay (+30) and an interview.

Success means:

| # | Criterion | Target |
|---|---|---|
| S1 | Replica-judge average per dimension, on the 30 canonical pairs | ≥ 8.5 / 10, no pair below 38 / 50 |
| S2 | Unseen-data suite (Section 11.3): fabricated facts | 0 |
| S3 | Operational: timeouts, malformed responses, healthz failures during a 60-min simulated run | 0 |
| S4 | Latency | `/tick` p99 ≤ 8 s with 20 actions; `/reply` p99 ≤ 6 s; `/context` p99 ≤ 200 ms; `/healthz` ≤ 50 ms |
| S5 | Replay scenarios: auto-reply ×4, intent transition, hostile→off-topic, plus 10 of our own | All pass the scripted checks and the replica flow judge ≥ 8 / 10 |
| S6 | Determinism | Identical request sequence ⇒ identical responses (asserted in tests) |
| S7 | Uptime from submission until results | Always-on VM, external monitor, state survives a restart |

Non-goals: real WhatsApp/Meta sending, real magicpin data, multi-tenant scaling, and a UI.

## 2. Source requirements (from the challenge pack)

Distilled from `challenge-brief.md`, `challenge-testing-brief.md`, `examples/*`, `judge_simulator.py` and the microsite.

**Endpoints:**
- `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `GET /v1/healthz`, `GET /v1/metadata`.
- Optional `POST /v1/teardown`, which wipes state.

**Context contract:**
- Idempotent per `(scope, context_id, version)`.
- Same or lower version ⇒ 409 with `{accepted:false, reason:"stale_version", current_version}`.
- Bad scope ⇒ 400 with `{accepted:false, reason:"invalid_scope", details}`.
- Higher version replaces the old one atomically.
- Payload limit: 500 KB.

**Tick contract:**
- At most 20 actions.
- Each action needs `conversation_id` (must be new), `merchant_id`, `customer_id`, `send_as`, `trigger_id`, `template_name`, `template_params`, `body`, `cta`, `suppression_key` and `rationale`.
- At most one action per (merchant, conversation) per tick.
- `actions: []` is valid, and holding back is rewarded.

**Reply contract:** the response is one of
- `send` (`body`, `cta`, `rationale`)
- `wait` (`wait_seconds`, `rationale`)
- `end` (`rationale`)

**Timing:**
- Hard timeout 30 s.
- The documented latency budget is 10 s for tick/reply, 5 s for context and 2 s for healthz.
- The local simulator uses 15 s for tick/reply and 10 s for context.
- The judge sends up to 10 requests per second.

**Penalties:**
- Malformed response or empty body: −2.
- Same body repeated in a conversation: −2.
- URL in the body: −3.
- Timeout: −1.
- 3 consecutive healthz failures ⇒ offline (−10).
- Any fabrication ⇒ that message is capped at 5 per dimension.
- Copying case-study wording is penalised (similarity check).

**Rubric (0–10 each):** Decision quality / trigger relevance, Specificity, Category fit, Merchant fit, Engagement compulsion.
- The judge cross-checks the rationale against the message.
- It penalises internal jargon.
- A research or compliance claim without a citation is capped at 7.
- Not using the owner's name costs about 1 point.

**Test lifecycle:**
1. Warmup: 5 categories, 50 merchants and 200 customers pushed, 0 triggers; `contexts_loaded` must match.
2. 60 simulated minutes of 5-minute ticks. Before each tick, the judge pushes new context.
3. Injections during the run:
   - 5 new digest items per category, as a new version
   - 10 merchants get updated performance numbers
   - 15 new triggers
   - 5 new customers, each followed by a `recall_due` trigger 2 minutes later
4. Each action gets up to 5 reply turns, played by an LLM acting as the merchant or customer.
5. The top 10 bots get 3 replay scenarios of 5 turns each.

**Dataset facts that shape the design:**
- 16 of the 30 canonical pairs, and 75 of the 100 triggers, have placeholder payloads (`{"placeholder": true, "metric_or_topic": kind}`).
- The generated merchants have no offers, signals or review themes, and only `total_unique_ytd` in `customer_aggregate`.
- Trigger payloads point to digest items by id (`top_item_id`, `digest_item_id`, `alert_id`).
- The simulator sends each auto-reply turn with a **different `conversation_id`** for the same merchant.
- The simulator's intent check passes only if the body contains one of {done, sending, draft, here, confirm, proceed, next} and none of {would you, do you, can you tell, what if, how about}.
- The simulator's hostile check passes on `end`, or on `send` with "sorry/apolog/won't".
- The simulator's scoring prompt shows the judge only this subset of context: name, owner, locality, languages, views/calls/ctr, signals, active offers, trigger payload, customer identity. Per brief §16, the real judge has the full dataset. So we attribute every fact that falls outside that subset.

## 3. Competitive landscape (why this design)

At least 9 entries are public on GitHub. Most converge on: deterministic playbook per trigger kind + fact sheet + validator + optional LLM polish + regex-based reply handling. The strongest (Shaurya55555/magicpin) adds trigger priority scoring, restraint thresholds and rationales that list the options considered. Many claim 50/50 on the local simulator, so it doesn't separate entries.

The gaps we target:
- (a) Thin-payload triggers get generic "conservative" fallbacks.
- (b) Replies are classified, then answered with canned text; no real deliverable is produced when the merchant says yes.
- (c) The LLM gets one attempt, with no candidate selection.
- (d) Evaluation stops at the simulator.
- (e) Nothing explicitly uses newly injected context.

## 4. Architecture

Single Python 3.12 process: FastAPI + uvicorn, **1 worker**, asyncio. All state lives in memory and is written through to SQLite.

```
                    ┌──────────────────────── api.py (FastAPI) ─────────────────────────┐
 judge ──HTTP──►    │ /context   /tick   /reply   /healthz   /metadata   /teardown      │
                    └────┬──────────┬────────┬──────────────────────────────────────────┘
                         │          │        │
               ┌─────────▼───┐  ┌───▼──────────────┐  ┌──▼─────────────────────┐
               │ store.py    │  │ scheduler.py     │  │ conversation/          │
               │ versioned   │  │ tick decision,   │  │  classifier.py         │
               │ contexts,   │  │ ranking,         │  │  policy.py (state m/c) │
               │ convs,      │  │ suppression,     │  │  artifacts.py          │
               │ suppression │  │ deadline mgmt    │  └──┬─────────────────────┘
               │ + SQLite WAL│  └───┬──────────────┘     │
               └──────┬──────┘      │                    │
                      │     ┌───────▼────────────────────▼───────┐
                      │     │ compose/pipeline.py                 │
                      │     │  facts.py → insights.py → planner.py│
                      │     │  → templates/ (per family, en/hinglish)
                      │     │  → llm_writer.py (N variants)       │
                      │     │  → validator.py → critic.py → pick  │
                      │     └───────┬─────────────────────────────┘
                      │             │
                      │     ┌───────▼──────────┐   ┌──────────────────┐
                      └────►│ cache.py (hash)  │   │ llm/pool.py      │
                            └──────────────────┘   │ gemini, groq,    │
                                                   │ token buckets,   │
                                                   │ circuit breakers │
                                                   └──────────────────┘
 playbook.py — static DO/DON'T rules + dynamic per-message rules (used by templates, llm_writer, critic, validator)
```

### 4.1 Module responsibilities and interfaces

| Module | Responsibility | Key interface |
|---|---|---|
| `store.py` | Versioned context store; conversations; suppression ledger; per-merchant counters (auto-reply streak, unanswered nudges, opt-out); SQLite write-through and restore on boot | `put(scope,id,version,payload) -> Ack`, `get(scope,id)`, `counts()`, `conv(id)`, `mark_suppressed(key)` |
| `facts.py` | Builds a **FactSheet** from the 4 contexts: typed, provenance-tagged facts with pre-formatted display strings. All arithmetic lives here | `build(category, merchant, trigger, customer, now) -> FactSheet` |
| `insights.py` | Merchant diagnosis plus opportunity mining; ranked `Insight` list, cached per (merchant v, category v) | `diagnose(merchant, category, now) -> list[Insight]` |
| `planner.py` | Chooses trigger family, primary insight, persuasion lever, CTA type, `send_as`, template id, language mode and deliverable offer | `plan(fs, insights, trigger) -> MessagePlan` |
| `templates/` | Deterministic renderers per family × language (en, hinglish), each with 3 phrasing variants picked by a stable hash | `render(plan, fs) -> Draft` |
| `llm_writer.py` | Prompts the LLM with the playbook, fact sheet, plan and angle; returns strict JSON drafts | `write(plan, fs, angle) -> Draft | None` |
| `validator.py` | Hard checks (Section 8) | `validate(draft, fs, conv) -> list[Violation]` |
| `critic.py` | Scores candidates: deterministic checklist + LLM rubric critic (replica of the judge rubric) + case-study similarity penalty | `rank(cands, fs) -> Ranked` |
| `pipeline.py` | Orchestrates generate → validate → critique → pick within a deadline; caches by input hash | `compose(ctx4, now, deadline) -> Composed` |
| `scheduler.py` | `/tick` decision logic (Section 9) | `decide(now, available) -> list[Action]` |
| `conversation/*` | Reply classification, dialogue policy, artifact generation (Section 10) | `handle(reply) -> ReplyAction` |
| `llm/pool.py` | Pool of providers with rate limits, timeouts, circuit breakers and priority queue; temperature 0 | `complete(prompt, schema, priority, timeout)` |
| `playbook.py` | Single source of the do/don't rules (Section 7) | `static_rules()`, `dynamic_rules(fs, conv)` |

### 4.2 Data model (core types)

```python
Fact(id: str, kind: Literal["number","money","pct","date","name","text","citation"],
     value: Any, display: str, source: str,      # e.g. "merchant.performance.ctr"
     visible_to_judge: bool,                      # in the simulator's visible subset?
     attribution: str | None)                     # phrase to use when not visible, e.g. "peer avg for metro solo clinics"
FactSheet(facts: dict[str, Fact], reader: "merchant"|"customer", owner_salutation: str,
          language_mode: "en"|"hinglish"|"hi"|"xx-en", category_voice: Voice,
          allowed_numbers: set[str], allowed_names: set[str], trigger_family: str, ...)
Insight(id, family: str, strength: float, facts: list[str], claim: str, implication: str, action: str)
MessagePlan(family, primary_insight, supporting: list[Insight], lever, cta_type, send_as,
            deliverable: str | None, language_mode, template_id, rationale_seed)
Draft(body, cta, template_params, source: "template"|"llm:<provider>", angle)
```

## 5. Composition pipeline

```
trigger arrives (/context) ──► precompose job (background, priority by urgency)
/tick needs it ─► cache hit? return : compose now with deadline

compose():
  fs  = facts.build(...)
  ins = insights.diagnose(...)                  # cached per merchant/category version
  plan= planner.plan(fs, ins, trigger)
  cands = [templates.render(plan, fs)]          # always present, always valid-by-construction
  cands += await gather(llm_writer.write(plan, fs, angle) for angle in plan.angles[:N]), timeout
  valid = [c for c in cands if not validator.validate(c, fs, conv)]
  best  = critic.rank(valid, fs).top          # ties → template; deterministic ordering
  cache[input_hash] = best
```

- **Input hash:** sha256 of (category v, merchant v, trigger v, customer v, conversation state digest, composer version). This ensures determinism and invalidates automatically when any context version changes.
- **N (number of LLM variants):** chosen by the pool from available budget. 3 when idle, 1 under pressure, 0 when exhausted.
- **Deadlines:**
  - Precompose (background): 20 s.
  - Inline compose during `/tick`: `min(7 s, tick_budget_remaining)`.
  - `/reply`: 5 s.
- **Tick-time rule:** a cached best candidate is returned instantly. Otherwise the inline compose runs under the deadline. At the deadline, the best candidate that has already passed validation is used; the template is always there.
- **LLM determinism:** temperature 0, fixed seed where supported, and the response is cached by prompt hash. When the pool is healthy we accept that different providers may produce different text; the cache guarantees identical output for repeated inputs within the run.

## 6. Insight engine (differentiator 1)

It turns the merchant and category contexts into ranked, grounded insights. Each insight carries its fact ids, a claim, an implication (the "so what") and an action (what Vera can do).

### 6.1 Diagnosis signals (computed; only fire when the inputs exist)

| Insight family | Computation | Example claim |
|---|---|---|
| `ctr_gap` | merchant.ctr vs peer.avg_ctr; fires when ≥ 15% relative gap | "CTR 2.1% vs 3.0% avg for metro solo clinics" |
| `views_gap` / `calls_gap` / `directions_gap` | vs peer avg_*_30d | "18 calls in 30 days vs ~12 peer avg" |
| `conversion` | calls/views vs peer calls/views | "1 call per 134 views vs 1 per 152 for peers" |
| `wow_move` | delta_7d.* where \|Δ\| ≥ 10% | "views up 18% this week" |
| `retention_gap` | retention_6mo/3mo vs peer | "38% 6-month retention vs 42% peer" |
| `lapsed_pool` | lapsed_180d_plus / lapsed_90d_plus counts | "78 patients not seen in 6+ months" |
| `cohort` | named aggregates (high_risk_adult_count, chronic_rx_count…) | "your 124 high-risk adult patients" |
| `offer_gap` | no active offers while the catalog has service+price items | "no live offer; category standard is Dental Cleaning @ ₹299" |
| `offer_expired` | expired offer with a price | "Deep Cleaning @ ₹499 lapsed on 28 Feb" |
| `stale_posts` | from signals (`stale_posts:22d`) vs peer avg_post_freq_days | "last Google post 22 days ago; peers post every 14" |
| `review_theme` | review_themes with occurrences | "3 reviews this month mention wait time" |
| `subscription` | days_remaining / expired days_since_expiry | "Pro plan ends in 12 days" |
| `unverified` | identity.verified == false | "your Google profile is unverified" |
| `seasonal_now` | category.seasonal_beats matching `now` month | "Apr–Jun: pediatric appointments +50%" |
| `trend` | category.trend_signals (city match preferred) | "'clear aligners delhi' searches +62% YoY" |
| `digest_match` | digest items relevant to merchant signals/cohorts/kind | JIDA fluoride item ↔ high-risk cohort |
| `conversation_thread` | last merchant message in conversation_history with intent | "you asked for whitening + aligner posts on 24 Apr" |

### 6.2 Ranking

`strength = magnitude_norm × trigger_affinity[family][insight] × recency × actionability`

`trigger_affinity` is a hand-built matrix (for example, `perf_dip` → calls_gap, conversion, review_theme, offer_gap). For placeholder triggers, the family's affinity row picks the strongest grounded insight, so the message stays specific.

### 6.3 Expert judgement rules

Encoded as code, each grounded in category data:
- **Seasonal dip is normal:** if the trigger or signal says seasonal and category.seasonal_beats confirms it ⇒ reframe as normal, advise against spending on ads now, focus on retention.
- **IPL on a Saturday:** weekend home matches underperform per the restaurants digest ⇒ recommend a delivery push using the existing offer.
- **Competitor undercuts on price** (payload has their price < ours) ⇒ do not recommend a price war; compete on rating, reviews and specialty, citing our numbers.
- **Compliance deadline** ⇒ lead with the deadline and the exact action from the digest's `actionable` field.
- **Research digest** ⇒ link to the merchant's cohort when one exists, cite the source, and offer patient-education content from `patient_content_library`.
- **Perf spike** ⇒ attribute it to `likely_driver` when present, and recommend doing more of it.
- **Milestone close** ⇒ ask for reviews using the gap value ("5 away from 150").
- **Supply recall** ⇒ include batch numbers, then offer a customer-notification workflow. Any affected count must come from context only.
- **Customer-facing messages:** never show the merchant's performance stats; stay within consent scope; no shaming; honour preferred slots and language.

## 7. Playbook (do/don't layer)

`playbook.py` is the single source of rules. It is used by:
- the templates, which comply by construction
- the LLM system prompt, where the rules are listed verbatim
- the critic prompt
- the validator, which enforces every rule that can be checked by machine

**Static DO:**
- Why-now in the first sentence.
- 1–2 verifiable facts, attributed when needed.
- Service + price offers.
- One CTA, as the last sentence, low friction (YES/STOP, "want me to draft it?").
- Say what Vera will do for them.
- Owner name / "Dr. X".
- Match the language.
- Cite the source for research or compliance.
- Customer-facing: warm, no shame, within consent.

**Static DON'T:**
- No preamble, no re-introduction after turn 1.
- No invented number, name, source, competitor or offer.
- No "% off" framing when service + price exists.
- No multiple CTAs, no URLs, no hype or ALL CAPS.
- No internal jargon (snake_case, "trigger", "suppression", "signal", "payload", "ctr_below_peer_median").
- No qualifying questions after a commitment.
- No repeating a prior body.
- No persisting after "no", abuse, or the 3rd auto-reply.
- No taboo vocabulary.

**Dynamic rules, per message:**
- The category's `vocab_taboo`, `tone`, `register`, `vocab_allowed` and `salutation_examples`.
- The merchant's last 3 Vera messages ("already said X").
- Conversation state (committed?, auto-reply streak, opted-out topics).
- Customer consent scope.
- Language mode.

## 8. Validator (hard gate)

A candidate is rejected when any of the following holds:

1. **Unknown numbers:** after normalisation (commas, ₹, %, decimals; 0.021 ↔ 2.1%; "2,100" ↔ 2100), a number in the body is not in `fs.allowed_numbers`. Counts ≤ 3 used as ordinary words ("2 slots", "3 posts") are allowed only from a small whitelist of effort/deliverable phrases.
2. **Unknown names:** a capitalised proper noun that is not in `fs.allowed_names` ∪ a common-word/Hindi-word lexicon ∪ category vocabulary.
3. Taboo phrase present (case-insensitive, category taboo + global).
4. URL or domain present.
5. Jargon regex hit (snake_case tokens, the internal-terms list).
6. CTA count ≠ 1 for action plans (question marks / "reply X" patterns), or the CTA is not in the final sentence.
7. Language mismatch: Hinglish mode requires Hindi-marker tokens; English mode forbids Devanagari.
8. Body empty, over 700 characters, or starts with a preamble pattern.
9. Near-duplicate of a prior body in the same conversation or merchant thread (token Jaccard ≥ 0.8).
10. Near-duplicate of a case-study body (5-gram overlap ≥ 0.35).
11. Customer-facing: contains merchant performance numbers, or a purpose outside consent scope.

The template renderer is unit-tested to pass the validator for every trigger in the dataset and the unseen-data suite.

## 9. Tick decision (scheduler)

For each id in `available_triggers` that the bot knows about:
- Drop only for: suppression_key already sent, merchant opted out or hostile end, consent blocked per R2, or merchant/category context missing. Expiry is NOT a drop reason (R1).
- Score: `urgency×10 + stakes[kind] + signal_match + insight_strength×5 + freshness − thin_payload_penalty − open_conversation_penalty`.

Then:
- Keep the top 1 per merchant (customer-scope triggers count per customer) and cap at 20.
- Apply a restraint floor: a low score with no strong insight ⇒ skip.
- Open conversations only lower priority; they never block (R5, R6).
- `conversation_id` = `conv_{merchant_short}_{kind}_{yyyymmdd}`, plus a suffix when a collision would occur.
- The suppression_key comes from the trigger (never invented). It is marked as used when the action is emitted.
- `template_name` / `template_params` come from the plan (e.g. `vera_research_digest_v1`, `[salutation, hook, cta]`).
- The rationale lists: the chosen signal, facts used, lever, language, the other options and why they lost, and any changed context version used.
- The scheduler also records conversation state for `/reply`: plan, facts, deliverable offered, bodies sent.

Unknown trigger ids are ignored. An unknown `kind` is mapped to the nearest family by keyword similarity; if there is none, the generic grounded family is used.

## 10. Conversation engine (differentiator 4)

### 10.1 Classifier

Rule layer first: normalised text plus Hindi/Hinglish lexicons. An LLM classifier (strict JSON) runs only when rules give low confidence and budget allows.

Classes:
- `auto_reply`: canned patterns, "automated assistant", "will respond shortly", "thank you for contacting". Also the same text ≥ 2 times for this merchant (fuzzy ≥ 0.9, tracked **per merchant** across conversation ids).
- `commit`: yes / ok / let's do it / go ahead / haan / kar do / theek hai / send it / confirm / chalega
- `question` (topic sub-typed)
- `objection` (price, time, trust)
- `later` (baad mein / busy / call later / tomorrow)
- `decline` (not interested / nahi chahiye)
- `hostile` (abuse / spam / stop bothering)
- `opt_out` (stop / unsubscribe / mat bhejo)
- `off_topic` (GST / loans / unrelated)
- `slot_choice` (customer "1" / "2" / a time)
- `info_provided` (answer to a curious ask)
- `language_switch`: detected per turn, and switches reply language

### 10.2 Policy (state machine per conversation, counters per merchant)

| Situation | Action |
|---|---|
| auto_reply #1 | `send` a one-line owner-flag nudge ("Looks like an auto-reply — when the owner sees this, a quick YES works") |
| auto_reply #2 | `wait` 14400–86400 s |
| auto_reply #3+ | `end` |
| commit | `send` the **deliverable** immediately (10.3) + one confirm CTA. Never ask a qualifying question. Uses action verbs (done / drafted / here / sending / confirm / next) |
| question | answer from facts only; if the fact isn't there, "I'll confirm and get back" + re-offer. One CTA |
| objection | acknowledge + one grounded counterpoint + lower-effort option |
| later | `wait` for the parsed duration (default 4 h; "tomorrow" = next day 10:00 IST) |
| decline | `send` one short graceful close (no pitch) → then `end` on any further message; suppress the merchant for this family |
| hostile | `end` (if an apology line is used: "Sorry — I won't message again.") and suppress the merchant for the rest of the test |
| opt_out | `end` + global suppression for the merchant |
| off_topic (after hostile or alone) | polite decline ("that's for your CA") + one-line redirect to the open deliverable |
| slot_choice (customer) | confirm the booking with the slot label from the trigger payload + reminder offer |
| info_provided | thank + turn the answer into the promised deliverable (e.g. GBP post draft) |
| 3 unanswered nudges | stop sending to this merchant |
| turn ≥ 5 | wrap up with a summary and no new asks |

Reply bodies go through the same validator. They never repeat a prior body; if a repeat would happen, a variant is picked.

### 10.3 Artifacts (deliverables built from context)

- **GBP post draft:** from an offer, digest or trend + locality.
- **Patient/customer WhatsApp draft:** from `patient_content_library` or a digest summary, at patient reading level.
- **Offer card:** from a catalog item or the merchant's own offer (price tiers only when grounded in a real price, e.g. the menu item ₹149).
- **Review-request message:** for milestones.
- **Recall/notification workflow:** steps + message.
- **Booking confirmation:** from payload slot labels.
- **Checklist:** for compliance, from the digest's `actionable` field.

These are template-first, LLM-polished and validated.

## 11. LLM layer

### 11.1 Provider pool

- **Providers:** Gemini Flash (primary writer), Gemini Flash-Lite (critic/classifier), Groq (Llama-3.3-70B or gpt-oss, as fallback writer). Keys come from env; all are optional.
- **Limits:** a token bucket per provider sized from configured RPM/RPD, which we set from the AI Studio dashboard at deploy time. A circuit breaker opens after 3 errors or timeouts and half-opens after 60 s.
- **Priority queue:** P0 inline tick compose > P1 reply > P2 precompose > P3 extra candidates / critic.
- **Budget guard:** reserve 30% of the daily quota for the judged run window. `N` and critic use shrink as the remaining budget falls.
- **Calls:** JSON mode / response schema, temperature 0, max tokens 400, timeout 6 s.

### 11.2 Prompts

- **Writer prompt** = playbook rules + fact sheet (display strings + attribution phrases only; no raw JSON) + plan (family, insight, lever/angle, CTA type, deliverable, language mode, salutation) + prior bodies to avoid + output schema `{body, cta, template_params, used_fact_ids}`. `used_fact_ids` is cross-checked by the validator.
- **Critic prompt** = the judge's 5-dimension rubric (the simulator's wording plus the brief's) + the visible context subset + the candidate. Returns scores and a single weakest point. The critic score is combined with the deterministic checklist score (weights 0.6 / 0.4). Ties ⇒ the lower candidate index (template first).
- **Angles for variants:** loss_aversion, social_proof (only when grounded, e.g. peer stats), curiosity, effort_externalization, ask_the_merchant. Picked by the planner per family.

### 11.3 Evaluation lab (offline, `eval/`)

1. **Replica judge:** the simulator's exact prompt, run with 2 judge models (Gemini + Groq), averaged. Output: a per-dimension table for the 30 pairs + all 100 triggers.
2. **Checklist scorer:** deterministic, runs in CI on every change.
3. **Unseen-data suite:** mutations of the dataset:
   - performance numbers shifted ±40%
   - new digest items (hand-written, 2 per category)
   - 10 unseen trigger kinds (e.g. `weather_heatwave`, `local_news_event`, `appointment_noshow`)
   - payloads with missing fields
   - merchants with no owner name, customers with no language pref
   - version bumps mid-run

   Asserts: zero validator failures on the output, no fabricated tokens, and the new facts are actually used.
4. **Replay suite:** the 3 official scenarios + 10 of our own (Hinglish commit, language switch, price question, "call me tomorrow", customer slot choice, decline→re-ping, curveball question, info_provided, double auto-reply with a variant text, turn-5 wrap-up), scored by a flow judge prompt.
5. **Harness runner:** a local judge simulation of the 60-minute lifecycle (warmup, 12 ticks, injections, LLM-played replies) against the local or deployed URL, with latency percentiles and a penalty tally.
6. **Human review sheet:** `eval/out/review.md`, with all 30 outputs side by side, plus rationale and scores.

## 12. State, persistence, operations

- **State:** in-memory dicts guarded by an asyncio lock. Every mutation is written to SQLite (WAL) through a background writer. On boot, state is restored from SQLite, so a crash or restart loses nothing.
- **`/healthz`:** served from memory, never blocked by LLM work (LLM calls are async and CPU work is minimal). `contexts_loaded` counts the current contexts per scope.
- **`/teardown`:** wipes memory and SQLite.
- **Deployment:**
  - Oracle Cloud Always-Free VM (fallback: GCP e2-micro), Ubuntu.
  - `uv` venv + systemd service (`Restart=always`); Caddy for automatic HTTPS on a free subdomain (DuckDNS or sslip.io).
  - An external uptime monitor (UptimeRobot free) on `/v1/healthz` every 5 minutes.
  - Logs via journald, with no payload bodies logged (privacy rule).
- **Config (env):** `GEMINI_API_KEY`, `GROQ_API_KEY`, provider RPM/RPD, `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`, `COMPOSER_VERSION`, `LLM_ENABLED`.
- **Privacy:** only LLM APIs receive payload-derived text; no other outbound calls.

## 13. Error handling

- Pydantic models with lenient parsing (extra fields allowed, missing optional fields tolerated). A malformed request gets a 400 with the contract body, never a 500.
- A global exception handler wraps every endpoint:
  - `/tick` returns the actions composed so far (or `[]`)
  - `/reply` returns `wait 1800` with a rationale
  - `/context` returns a 400
- Every response is validated against the output schema before sending. A send with an empty body can't be emitted; it becomes a `wait`.
- An LLM failure never propagates; the pipeline degrades to the template.

## 14. Testing strategy

- **Unit tests:** store versioning (200 / 409 / 400), number normalisation, validator rules, fact sheet building for every dataset trigger, insight computations, classifier lexicon cases, policy transitions, scheduler ranking and suppression.
- **Contract tests:** every endpoint against the documented examples (`examples/api-call-examples.md`), including idempotency and version bump.
- **Golden tests:** template output for the 30 canonical pairs, snapshotted (determinism).
- **Property test:** for every trigger in dataset ∪ unseen-suite, compose(template-only) passes the validator.
- **Load test:** 20 triggers in one tick with the LLM enabled but throttled ⇒ p99 < 8 s; 10 req/s for 60 s ⇒ no errors.
- **Official simulator:** `judge_simulator.py` (all scenarios + full_evaluation) against the local and deployed bot.

## 15. Build phases (each ends with a submittable bot)

1. **Skeleton:** store (+SQLite), endpoints, fact sheet, validator, templates for all 26 known families + generic fallback, scheduler, basic reply policy, metadata. Deploy to the VM; pass the official simulator. *(Submittable.)*
2. **Insight engine** + planner + expert rules; placeholder triggers become specific.
3. **Conversation engine:** full classifier, policy and artifacts; replay suite green.
4. **LLM layer:** pool, writer, best-of-N, critic, precompose/cache, deadlines.
5. **Evaluation lab:** replica judge ×2, unseen suite, lifecycle harness. Iterate until S1–S5 are met.
6. **Hardening:** load test, uptime monitor, README (1 page: approach, tradeoffs, what context would help), final submission.

## 16. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Free-tier limits lower than expected, or changed | Multi-provider pool, adaptive N, template floor. The bot is fully functional without any LLM |
| Templated feel penalised as "generic" | 3 phrasing variants per family × language; LLM polish; the critic prefers natural wording; human review pass |
| Validator too strict ⇒ LLM candidates always rejected | Measure the rejection rate in eval; tune whitelist/lexicon; log violation types |
| Judge sees only part of the context ⇒ true facts look invented | Inline attribution phrases for facts outside the visible subset |
| Mid-test unseen trigger kinds | Keyword family mapper + generic grounded family + LLM writer uses the payload display facts |
| VM reclaimed or down | systemd restart, SQLite restore, uptime alerts; documented redeploy in < 10 min |
| Case-study similarity penalty | 5-gram overlap check in the validator |
| Interview verification ("did you do the work") | Vaibhav reviews each phase, owns the key decisions, README written in his voice |

## 17. Open items (resolved at implementation time)

- Exact free-tier RPM/RPD per model, read from the AI Studio dashboard, set in env.
- Final hosting choice between Oracle and GCP, depending on which signup works.
- Team name and metadata values.

---

## 18. Senior review: gaps found and binding fixes

This section **overrides** earlier sections where they conflict. Each item was checked against the simulator source or the generated data.

### 18.1 Critical: would cause zero or blocked output

| # | Gap | Evidence | Fix |
|---|---|---|---|
| R1 | Dropping triggers with `expires_at < now` empties every tick | The simulator's `now` = real UTC time (2026-09); seed triggers expire 2026-04/05 | **Trust `available_triggers`**: the judge lists what is active. Use expiry only for wording ("expires today") when `0 ≤ expires_at−now ≤ 7d` |
| R2 | Consent gate blocks 6 of 30 canonical pairs | Generated customers only have `["promotional_offers"]`; triggers are appointment/recall/refill | Map kinds to purposes: **transactional** (appointment_tomorrow, booking confirm, chronic_refill_due, trial_followup) allowed with any opt-in; **promotional/recall/winback** allowed with a matching scope OR `promotional_offers` OR `reminder_opt_in=true`. Block only on `reminder_opt_in=false` with no matching scope, or a customer opt-out. State the consent basis in the rationale |
| R3 | Persisted state leaks between judge runs | Next run re-pushes v1 → our 409; old suppressions ⇒ silence | **Session epochs**: auto-wipe when a request arrives after ≥ 45 min of inactivity; `/teardown` wipes. SQLite is used only to survive a crash *within* a session. Our own testing on the prod URL is followed by `/teardown` |
| R4 | Relative dates go negative or absurd when `now` doesn't match the dataset's time | "−150 days to wedding" | Prefer values from the payload (`days_until`, `days_to_wedding`, labels). Computed deltas are used only if 0 ≤ value ≤ 400; otherwise omitted |
| R5 | Skipping triggers loses canonical pairs outright | Pairs are scored per message; the brief says "all participants must produce a message" | Never skip an eligible listed trigger. Restraint applies only to opt-out / hostile / duplicate suppression_key / missing merchant context |
| R6 | "Top-1 per merchant" loses pairs | Dr. Meera alone has 5 triggers (T06, T09, T28, T30, trg_001) | Contract allows one action per (merchant, **conversation**). Send ≤ 2 merchant-facing actions per merchant per tick (distinct conversations, ordered by score) + all customer-facing ones; defer the rest to later ticks while still listed. Rotate insights per merchant so bodies differ |
| R7 | FastAPI rejects bodies without `Content-Type: application/json` | The brief's curl examples omit the header for /tick and /reply | Parse the raw body with `json.loads` in every handler; never rely on content-type |

### 18.2 High: scoring losses

| # | Gap | Fix |
|---|---|---|
| R8 | The intent check fails on the substring "do you", which also matches "**do you**r" | Commit replies: validator forbids {would you, do you, can you tell, what if, how about} as substrings, and requires one of {done, sending, draft, here, confirm, proceed, next} |
| R9 | Replies can arrive for unknown conversations or merchants (simulator scenarios use fresh ids; only 5 merchants pushed) | Policy works without a plan: builds the conversation from merchant context if present, otherwise a context-free safe reply (still passes the checks) |
| R10 | Trigger/customer state contradictions (T03 appointment for lapsed_hard; T14 lapsed_soft for churned; T08 chronic refill at a dentist) | Trigger kind drives the message; customer `state` is never stated literally; a kind that doesn't fit the category ⇒ neutral "regular follow-up" framing, with no invented service |
| R11 | Placeholder customer triggers have no time or slot data | Never invent times. Appointment: "your appointment tomorrow" + "Reply YES to confirm, or tell us a time that suits". Recall: offer to find a slot based on `preferred_slots` wording |
| R12 | Salutation bugs: owner_first_name already contains "Dr." ("Dr. Sameer"); missing owner | Normalise: strip honorifics then re-add per category; fallback to "{business name} team" |
| R13 | Language: 20 of 50 merchants list `hi` plus a southern/Marathi language; Hinglish in Chennai may read off | Merchant: `hi` ∈ languages & Hindi-belt city ⇒ Hinglish; `hi` elsewhere ⇒ English with light Hindi touches ("ji", "chalega?"); customers use `language_pref` (`hi` ⇒ Roman Hindi-heavy; `xx-en mix` ⇒ English + one native greeting: Vanakkam/Namaskaram/Namaskara/Namaskar) |
| R14 | Similar bodies across one merchant's several triggers | Per-merchant used-insight ledger; the planner prefers unused insights; Jaccard check across the merchant thread |
| R15 | Rationale too long or unfocused | ≤ 280 chars: signal → facts → lever → (alternatives count) → (context version used) |
| R16 | Case-study-like wording for seed triggers | Our own phrasing; 5-gram overlap check (Section 8 #10) |

### 18.3 Limits and resources: the LLM budget reality

- **Burst math:** a tick can list 20 triggers, pushed seconds before `/tick`. Free Gemini is ~10–15 requests/min. One call per candidate would exceed the limit in a single tick. **So:**
  - **The templates are the primary product**; the LLM improves them when budget allows. Template quality gets the most engineering effort: 3 variants per family × language, insight-driven.
  - **Batch writer:** one LLM call writes messages for up to 5 triggers (JSON array). 20 triggers ⇒ 4 parallel requests ≈ 4–6 s. Variants per trigger: 2 when fewer than 6 triggers are pending, else 1.
  - **Batch critic** (Flash-Lite): one call scores all candidates of a batch. It is skipped if under 2.5 s of deadline remains; the deterministic checklist decides alone.
  - **Gemini 2.5 Flash has "thinking" on by default**, which is slow. Set `thinkingBudget: 0`; model ids come from config.
- **Daily quota is shared by development and evaluation:** the eval lab's judge runs on Groq/OpenRouter where possible, and quota is tracked in `llm/pool.py` counters exposed on `/v1/healthz` (debug field).
- **Submission timing:** the portal says evaluation may start right after submission. Submit only after all gates pass, **right after the daily quota reset** (midnight Pacific ≈ 12:30–13:30 IST), with no eval runs afterwards that day.
- **Determinism caveat:** the LLM path depends on budget. Guarantees: temperature 0 + seed + a response cache persisted within the session; the template path is fully deterministic. Documented in the README.

### 18.4 Operations

- Always-free VM with 1 GB RAM: the process stays under ~200 MB; SQLite + in-memory state is fine for 255 contexts plus about 100 triggers.
- HTTPS: Caddy + DuckDNS hostname (sslip.io as fallback); `HEAD`/`GET /` and `/v1/healthz` for uptime monitors.
- Body size limit of 600 KB enforced; oversized ⇒ 400 `payload_too_large`.
- Latency budget from India/EU to a US/Mumbai VM is under 300 ms; choose the Mumbai (ap-mumbai-1) region on Oracle if capacity allows.
