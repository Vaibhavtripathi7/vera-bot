"""/v1/tick decision: which listed triggers become messages this tick, and in what order.

Rules (spec section 9 + review R1/R2/R5/R6): trust the judge's available_triggers (no expiry drops),
skip only for real reasons (opt-out, duplicate suppression key, consent, missing context), rank by
stakes x urgency x merchant-state fit, cap merchant-facing sends per merchant per tick.
"""
from __future__ import annotations

import re
import time

from . import config
from .pipeline import Item, compose_items, make_item
from .store import Conversation, Store
from .templates import family_for
from .util import parse_dt

STAKES = {
    "supply_alert": 9, "regulation_change": 8, "chronic_refill_due": 6, "appointment_tomorrow": 6, "recall_due": 5,
    "active_planning_intent": 7, "renewal_due": 5, "perf_dip": 5, "competitor_opened": 4, "winback_eligible": 4,
    "customer_lapsed_hard": 4, "customer_lapsed_soft": 3, "review_theme_emerged": 4, "ipl_match_today": 5,
    "gbp_unverified": 3, "trial_followup": 4, "wedding_package_followup": 4, "festival_upcoming": 2,
    "category_seasonal": 3, "research_digest": 3, "cde_opportunity": 2, "perf_spike": 2, "milestone_reached": 2,
    "dormant_with_vera": 2, "curious_ask_due": 1, "seasonal_perf_dip": 3,
}
TRANSACTIONAL = {"appointment_tomorrow", "chronic_refill_due", "trial_followup", "booking_confirmation"}
CONSENT_WORDS = {  # trigger kind -> consent scope keywords that cover it
    "recall_due": ("recall",), "customer_lapsed_soft": ("winback", "promo", "recall"), "customer_lapsed_hard": ("winback", "promo"),
    "wedding_package_followup": ("bridal", "promo"), "chronic_refill_due": ("refill", "delivery"),
    "appointment_tomorrow": ("appointment",), "trial_followup": ("program", "trial", "kids", "promo"),
}


def consent_ok(trigger: dict, customer: dict | None) -> tuple[bool, str]:
    if not customer:
        return True, ""
    scope = [str(s).lower() for s in (customer.get("consent") or {}).get("scope") or []]
    opt_in = (customer.get("preferences") or {}).get("reminder_opt_in")
    kind = trigger.get("kind", "")
    words = CONSENT_WORDS.get(kind, ())
    if any(w in s for s in scope for w in words):
        return True, "consent scope matches"
    if kind in TRANSACTIONAL and (scope or opt_in):
        return True, "transactional message with active opt-in"
    if any("promo" in s for s in scope):
        return True, "covered by promotional opt-in"
    if opt_in and scope:
        return True, "reminder opt-in on record"
    return False, "no consent on record"


def _short(merchant_id: str) -> str:
    parts = (merchant_id or "m").split("_")
    return "_".join(parts[1:3]) if len(parts) > 2 else merchant_id


class Scheduler:
    def __init__(self, store: Store):
        self.store = store

    def _candidates(self, ids: list[str]):
        s = self.store
        out, skipped = [], []
        for tid in dict.fromkeys(ids):      # de-dup, keep order
            trg = s.get("trigger", tid)
            if not trg:
                skipped.append((tid, "unknown trigger"))
                continue
            mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
            merchant = s.get("merchant", mid)
            if not merchant:
                skipped.append((tid, "merchant context missing"))
                continue
            category = s.get("category", merchant.get("category_slug"))
            if not category:
                skipped.append((tid, "category context missing"))
                continue
            cust_id = trg.get("customer_id")
            customer = s.get("customer", cust_id) if cust_id else None
            # customer-scope trigger without a customer context: brief the merchant instead of going silent
            via_merchant = trg.get("scope") == "customer" and not customer
            key = trg.get("suppression_key") or f"{trg.get('kind')}:{tid}"
            if key in s.suppressed:
                skipped.append((tid, "already sent (suppression key)"))
                continue
            mst = s.mstate(mid)
            if mst.get("opted_out"):
                skipped.append((tid, "merchant opted out"))
                continue
            if customer and s.mstate(f"cust:{customer.get('customer_id')}").get("opted_out"):
                skipped.append((tid, "customer opted out"))
                continue
            ok, why = consent_ok(trg, customer)
            if not ok:
                skipped.append((tid, why))
                continue
            kind = trg.get("kind", "")
            score = float(trg.get("urgency") or 2) * 10 + STAKES.get(kind, 2)
            sigs = " ".join(map(str, merchant.get("signals") or []))
            if kind.split("_")[0] in sigs or (kind == "renewal_due" and "renewal" in sigs):
                score += 3
            if (trg.get("payload") or {}).get("placeholder"):
                score -= 3
            if mst.get("unanswered", 0) >= 3 and not customer:
                skipped.append((tid, "3 unanswered nudges - holding off"))
                continue
            if via_merchant:
                trg = {**trg, "scope": "merchant", "kind": f"{trg.get('kind')}__via_merchant", "_orig_kind": trg.get("kind")}
            out.append({"score": score, "trigger": trg, "merchant": merchant, "category": category,
                        "customer": customer, "key": key, "consent": why})
        out.sort(key=lambda c: (-c["score"], c["trigger"].get("id", "")))
        return out, skipped

    async def tick(self, now_str: str | None, ids: list[str]) -> dict:
        t0 = time.monotonic()
        deadline = t0 + config.TICK_DEADLINE
        s = self.store
        now = parse_dt(now_str)
        cands, skipped = self._candidates(ids)
        per_merchant: dict[str, int] = {}
        per_customer: set = set()
        chosen = []
        for c in cands:
            if len(chosen) >= config.MAX_ACTIONS_PER_TICK:
                break
            mid = c["merchant"].get("merchant_id")
            if c["customer"]:
                cid = c["customer"].get("customer_id")
                if cid in per_customer:
                    continue
                per_customer.add(cid)
            else:
                if per_merchant.get(mid, 0) >= config.MAX_MERCHANT_FACING_PER_MERCHANT_PER_TICK:
                    continue
                per_merchant[mid] = per_merchant.get(mid, 0) + 1
            chosen.append(c)
        # build items sequentially so the per-merchant insight rotation / anti-repeat applies within a tick
        items: list[tuple[dict, Item]] = []
        for c in chosen:
            mid = c["merchant"].get("merchant_id")
            mst = s.mstate(mid)
            used = set(mst.get("used_insights", []))
            prior = list(mst.get("bodies", []))[-6:]
            it = make_item(s, c["category"], c["merchant"], c["trigger"], c["customer"], now, used, prior)
            mst.setdefault("used_insights", []).extend(i for i in it.template.insights_used if i)
            mst.setdefault("bodies", []).append(it.template.body)
            items.append((c, it))
        await compose_items([it for _, it in items], deadline)
        actions = []
        for c, it in items:
            d = it.chosen
            trg, merchant, customer = c["trigger"], c["merchant"], c["customer"]
            mid = merchant.get("merchant_id")
            mst = s.mstate(mid)
            if mst["bodies"] and mst["bodies"][-1] != d.body and it.template.body in mst["bodies"]:
                mst["bodies"][mst["bodies"].index(it.template.body)] = d.body
            conv_id = self._conv_id(mid, trg, customer)
            versions = f"m v{s.version('merchant', mid)}, cat v{s.version('category', merchant.get('category_slug'))}"
            extra = f" Chosen over {len(cands) - 1} other listed trigger(s) by urgency/stakes." if len(cands) > 1 else ""
            if c["consent"]:
                extra += f" Consent: {c['consent']}."
            rationale = (d.rationale + extra + f" Context: {versions}.")[:420]
            send_as = "merchant_on_behalf" if customer or trg.get("scope") == "customer" else "vera"
            action = {
                "conversation_id": conv_id, "merchant_id": mid,
                "customer_id": customer.get("customer_id") if customer else None,
                "send_as": send_as, "trigger_id": trg.get("id"),
                "template_name": d.template_name, "template_params": [str(p) for p in d.template_params],
                "body": d.body, "cta": d.cta, "suppression_key": c["key"], "rationale": rationale,
            }
            actions.append(action)
            s.suppressed.add(c["key"])
            s.sent_triggers.add(trg.get("id"))
            mst["unanswered"] = mst.get("unanswered", 0) + (0 if customer else 1)
            s.save_conv(Conversation(conversation_id=conv_id, merchant_id=mid,
                                     customer_id=customer.get("customer_id") if customer else None,
                                     trigger_id=trg.get("id"), family=family_for(trg.get("kind", ""), trg.get("scope", "merchant")),
                                     send_as=send_as, deliverable=d.deliverable, bodies=[d.body],
                                     turns=[{"from": "bot", "msg": d.body, "turn": 1}],
                                     meta={"kind": trg.get("kind"), "source": d.source, "hook": str(d.template_params[1])[:200]}))
            s.save_mstate(mid)
        s.save_meta()
        return {"actions": actions}

    def _conv_id(self, mid: str, trg: dict, customer: dict | None) -> str:
        base = f"conv_{_short(mid)}_{re.sub(r'[^a-z0-9]+', '_', str(trg.get('kind', 'msg')).lower())}"
        if customer:
            base += "_" + str(customer.get("customer_id", "")).split("_")[1] if "_" in str(customer.get("customer_id", "")) else ""
        cid, n = base, 2
        while self.store.conv(cid) is not None:
            cid, n = f"{base}_{n}", n + 1
        return cid
