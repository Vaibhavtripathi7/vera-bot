"""Best-of-N composition under a deadline.

For each item: the template draft (always valid) + LLM rewrites (batched: one call writes up to
5 messages) -> validator gate -> critic (batched rubric scoring) or deterministic score -> pick.
Results are cached by an input hash so identical inputs return identical output.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

from . import config, playbook, validator
from .compose import build_ctx
from .llm import POOL
from .templates import Ctx, Draft, render
from .util import numbers_in, sha

BATCH = 5
WRITER_SYSTEM = """You are Vera, magicpin's WhatsApp assistant for Indian local merchants. You rewrite a BASELINE draft into the
best possible WhatsApp message for each item. The baseline is factually correct; your job is to make it sharper, more natural
and more compelling for this exact reader, WITHOUT adding any new fact.

Hard rules:
- Use ONLY facts present in that item's FACTS list or BASELINE. Every number, date, price, name and source must appear there.
- Keep the baseline's key fact(s), its why-now, and its single ask. The ask/CTA must be the LAST sentence.
- Respect LANGUAGE exactly (Hinglish = natural Roman-script Hindi-English mix, not formal Hindi).
- No URLs, no hype, no preamble, no internal jargon or snake_case, no multiple asks.
- Keep proactive messages to 2-4 sentences (bulleted drafts may stay as lists).
Return JSON: {"items":[{"id":"<id>","bodies":["<variant 1>", "<variant 2 if requested>"]}]}"""

CRITIC_SYSTEM = """You are a STRICT judge for the magicpin AI Challenge scoring WhatsApp messages to Indian merchants/customers.
Score each candidate 0-10 on: specificity (verifiable facts), category_fit (voice for the business type), merchant_fit
(personalised, correct language), decision_quality (clear why-now tied to the trigger), engagement (would they reply: one
low-friction CTA, loss aversion/curiosity/social proof/effort externalisation). Penalise any fact not in the item's FACTS
(-5 total), generic copy, multiple CTAs, awkward language. Be strict: 5 = average, 9+ = excellent.
Return JSON: {"scores":[{"id":"<item id>","candidate":<index>,"total":<0-50>}]}"""


@dataclass
class Item:
    key: str
    ctx: Ctx
    template: Draft
    prior_bodies: list = field(default_factory=list)
    candidates: list = field(default_factory=list)   # list[Draft]
    chosen: Draft | None = None


CACHE: dict[str, Draft] = {}
STATIC_RULES = "\n".join(["DO:"] + [f"- {r}" for r in playbook.STATIC_DO] + ["DON'T:"] + [f"- {r}" for r in playbook.STATIC_DONT])


def input_key(store, trigger: dict, merchant: dict, customer: dict | None, used: set, prior: list) -> str:
    parts = [config.COMPOSER_VERSION,
             f"t:{trigger.get('id')}:{store.version('trigger', trigger.get('id'))}",
             f"m:{merchant.get('merchant_id')}:{store.version('merchant', merchant.get('merchant_id'))}",
             f"c:{merchant.get('category_slug')}:{store.version('category', merchant.get('category_slug'))}",
             f"u:{(customer or {}).get('customer_id')}:{store.version('customer', (customer or {}).get('customer_id'))}",
             "used:" + ",".join(sorted(used)), "prior:" + sha("|".join(prior[-3:]))[:12]]
    return sha("\n".join(parts))


def det_score(d: Draft, ctx: Ctx) -> float:
    """Cheap rubric proxy used when the critic is unavailable or as a tie-breaker."""
    b = d.body
    s = 0.0
    nums = numbers_in(b)
    s += min(3, len(nums)) * 1.0
    if ctx.fs.salutation.split()[0].lower() in b.lower() or (ctx.fs.customer_name and ctx.fs.customer_name.lower() in b.lower()):
        s += 1.5
    if re.search(r"\(|—\s*[A-Z]|JIDA|DCI|CDSCO|ICMR|circular|source", b):
        s += 0.5
    n = len(b)
    s += 1.0 if 140 <= n <= 520 else 0.0
    if "'" in b or "₹" in b:
        s += 0.5
    return s


def _facts_payload(it: Item) -> dict:
    ctx = it.ctx
    fs = ctx.fs
    facts = [f.display for f in fs.facts.values() if f.id.startswith(("perf.", "delta.", "agg.", "sub.", "cust."))]
    facts += [i.en for i in ctx.insights[:6]]
    tp = ctx.trigger.get("payload") or {}
    if not tp.get("placeholder"):
        facts.append("TRIGGER: " + json.dumps(tp, ensure_ascii=False)[:600])
    from .facts import resolve_digest
    d = resolve_digest(ctx.category, ctx.trigger)
    if d:
        facts.append("SOURCE ITEM: " + json.dumps({k: d.get(k) for k in ("title", "source", "summary", "actionable", "trial_n", "date")
                                                   if d.get(k)}, ensure_ascii=False))
    voice = ctx.category.get("voice") or {}
    return {
        "id": it.key[:10],
        "reader": fs.reader,
        "send_as": "merchant's business to its customer" if fs.reader == "customer" else "Vera to the merchant",
        "language": {"hinglish": "Hinglish", "hindi": "Hindi-heavy Roman script (respectful)", "en_light": "English",
                     "en": "English"}.get(fs.lang, "English") + (f" (open with '{fs.native_greeting}')" if fs.native_greeting else ""),
        "salutation": fs.salutation if fs.reader == "merchant" else (fs.customer_parent or fs.customer_name),
        "business": fs.biz, "category": fs.category, "voice": voice.get("tone"),
        "taboos": (voice.get("vocab_taboo") or [])[:8],
        "trigger_kind": ctx.trigger.get("kind"),
        "facts": facts[:14],
        "already_sent": [b[:160] for b in it.prior_bodies[-3:]],
        "baseline": it.template.body,
    }


async def _write_batch(items: list[Item], variants: int, timeout: float):
    payload = [_facts_payload(it) for it in items]
    for p in payload:
        p["variants"] = variants
    prompt = "Rewrite each item. Items:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    data = await POOL.complete_json(WRITER_SYSTEM + "\n\n" + STATIC_RULES, prompt,
                                    role="writer", timeout=timeout, max_tokens=350 * len(items) * variants + 200)
    if not data:
        return
    by_id = {it.key[:10]: it for it in items}
    for row in data.get("items") or []:
        it = by_id.get(str(row.get("id")))
        if not it:
            continue
        for body in (row.get("bodies") or [])[:variants]:
            if not isinstance(body, str) or not body.strip():
                continue
            body = re.sub(r"[ \t]+", " ", body).strip()
            v = validator.validate(body, it.ctx.fs, category=it.ctx.category, prior_bodies=it.prior_bodies,
                                   require_hinglish=it.ctx.fs.lang == "hinglish",
                                   kind="artifact" if it.ctx.family == "planning" else "proactive")
            if validator.case_study_overlap(body) >= 0.35:
                v.append("case_study_overlap")
            if not v:
                d = Draft(**{**it.template.__dict__, "body": body, "source": "llm"})
                d.rationale = it.template.rationale
                it.candidates.append(d)


async def _critic(items: list[Item], timeout: float):
    rows = []
    for it in items:
        if len(it.candidates) < 2:
            continue
        rows.append({"id": it.key[:10], "facts": _facts_payload(it)["facts"], "reader": it.ctx.fs.reader,
                     "language": it.ctx.fs.lang, "trigger_kind": it.ctx.trigger.get("kind"),
                     "candidates": [c.body for c in it.candidates]})
    if not rows:
        return {}
    data = await POOL.complete_json(CRITIC_SYSTEM, json.dumps(rows, ensure_ascii=False, indent=1), role="critic",
                                    timeout=timeout, max_tokens=60 * sum(len(r["candidates"]) for r in rows) + 100)
    scores: dict = {}
    for s in (data or {}).get("scores") or []:
        try:
            scores[(str(s["id"]), int(s["candidate"]))] = float(s["total"])
        except (KeyError, TypeError, ValueError):
            continue
    return scores


async def compose_items(items: list[Item], deadline: float) -> None:
    """Fill item.chosen for every item. Never raises; never exceeds the deadline (monotonic seconds)."""
    todo = []
    for it in items:
        if it.key in CACHE:
            it.chosen = CACHE[it.key]
        else:
            it.candidates = [it.template]
            todo.append(it)
    if todo and POOL.enabled:
        remaining = deadline - time.monotonic()
        batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
        cap = POOL.capacity("writer")
        variants = 2 if len(todo) <= 5 and cap >= 3 else 1
        batches = batches[:max(0, cap)]
        if remaining > 2.0 and batches:
            wtimeout = min(config.LLM_TIMEOUT, remaining - 1.2)
            try:
                await asyncio.wait_for(asyncio.gather(*[_write_batch(b, variants, wtimeout) for b in batches],
                                                      return_exceptions=True), timeout=wtimeout + 0.3)
            except asyncio.TimeoutError:
                pass
        remaining = deadline - time.monotonic()
        scores = {}
        if remaining > 2.5 and any(len(it.candidates) >= 2 for it in todo) and POOL.capacity("critic") > 0:
            try:
                scores = await asyncio.wait_for(_critic(todo, min(config.LLM_TIMEOUT, remaining - 1.0)), timeout=remaining - 0.7)
            except asyncio.TimeoutError:
                scores = {}
        for it in todo:
            best, best_s = None, -1e9
            for idx, c in enumerate(it.candidates):
                s = scores.get((it.key[:10], idx))
                if s is None:
                    s = det_score(c, it.ctx) * 5 + (0.5 if c.source == "llm" else 0)   # no critic: LLM wins ties (fluency)
                else:
                    s += 0.001 * (len(it.candidates) - idx)                           # stable tie-break
                if s > best_s:
                    best, best_s = c, s
            it.chosen = best or it.template
            if best is not None and best.source == "llm":
                it.chosen.rationale = (it.chosen.rationale + " Polished by LLM from grounded draft; picked by critic.")[:300]
            CACHE[it.key] = it.chosen
    for it in items:
        if it.chosen is None:
            it.chosen = it.template
            CACHE[it.key] = it.template


def make_item(store, category, merchant, trigger, customer, now, used: set, prior: list) -> Item:
    ctx = build_ctx(category, merchant, trigger, customer, now, used)
    draft = render(ctx)
    v = validator.validate(draft.body, ctx.fs, category=category, prior_bodies=prior,
                           require_hinglish=ctx.fs.lang == "hinglish",
                           kind="artifact" if ctx.family == "planning" else "proactive")
    if "repeat" in v:
        ctx.variant_offset += 1
        draft = render(ctx)
    return Item(key=input_key(store, trigger, merchant, customer, used, prior), ctx=ctx, template=draft, prior_bodies=prior)
