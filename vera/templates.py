"""Deterministic composer: one playbook per trigger family, English + Hinglish.

Shape of every proactive message (what 50/50 case studies share):
    WHY-NOW hook with a hard fact  ->  the "so what" (judgement)  ->  what Vera will do  ->  ONE CTA (last).
Every number used here comes from the FactSheet (or is registered into it as a derived proposal).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .facts import FactSheet, Insight, fmt_date, resolve_digest
from .util import fmt_int, fmt_money, fmt_pct, humanize, parse_dt, stable_index

# ---------------------------------------------------------------- family routing

FAMILY_OF_KIND = {
    "research_digest": "research", "category_research_digest_release": "research",
    "cde_opportunity": "cde",
    "regulation_change": "compliance", "supply_alert": "compliance",
    "perf_dip": "perf_dip", "seasonal_perf_dip": "seasonal_dip",
    "perf_spike": "perf_spike", "milestone_reached": "milestone",
    "review_theme_emerged": "review_theme", "competitor_opened": "competitor",
    "festival_upcoming": "festival", "ipl_match_today": "ipl", "category_seasonal": "seasonal_demand",
    "category_trend_movement": "trend", "weather_heatwave": "event", "local_news_event": "event",
    "renewal_due": "renewal", "winback_eligible": "winback", "dormant_with_vera": "dormant",
    "gbp_unverified": "gbp", "curious_ask_due": "curious", "scheduled_recurring": "curious",
    "active_planning_intent": "planning",
    # customer-facing
    "recall_due": "c_recall", "appointment_tomorrow": "c_appointment", "chronic_refill_due": "c_refill",
    "customer_lapsed_soft": "c_lapsed", "customer_lapsed_hard": "c_lapsed", "trial_followup": "c_trial",
    "wedding_package_followup": "c_bridal", "bridal_followup": "c_bridal",
}
KEYWORD_FAMILY = [  # unknown kinds -> nearest family by keyword (order matters)
    (("noshow", "no_show", "missed"), "c_event"), (("recall",), "c_recall"), (("appointment_tomorrow", "reminder"), "c_appointment"),
    (("refill", "prescription"), "c_refill"), (("lapse", "winback", "churn"), "c_lapsed"),
    (("trial",), "c_trial"), (("wedding", "bridal"), "c_bridal"),
    (("research", "digest", "journal", "study"), "research"), (("regulat", "compliance", "circular"), "compliance"),
    (("spike", "surge", "jump", "positive"), "perf_spike"), (("dip", "drop", "decline"), "perf_dip"),
    (("milestone",), "milestone"), (("review",), "review_theme"), (("competitor", "rival"), "competitor"),
    (("festival", "holiday"), "festival"), (("match", "ipl", "cricket"), "ipl"), (("season",), "seasonal_demand"),
    (("trend", "search"), "trend"),
    (("inventory", "stock", "expiry_alert", "shortage"), "event"), (("renew", "expir", "subscription"), "renewal"), (("dormant", "inactive", "silent"), "dormant"),
    (("gbp", "verif", "profile"), "gbp"), (("ask", "question", "poll"), "curious"), (("plan", "intent"), "planning"),
    (("cde", "webinar", "training", "workshop"), "cde"),
]


def family_for(kind: str, scope: str) -> str:
    fam = FAMILY_OF_KIND.get(kind)
    if not fam:
        k = (kind or "").lower()
        for keys, f in KEYWORD_FAMILY:
            if any(x in k for x in keys):
                fam = f
                break
    if not fam:
        fam = "c_event" if scope == "customer" else "event"
    if scope == "customer" and not fam.startswith("c_"):
        fam = "c_event"
    if scope != "customer" and fam.startswith("c_"):
        fam = "generic"
    return fam


# ---------------------------------------------------------------- draft + context

@dataclass
class Draft:
    body: str
    cta: str
    template_name: str
    template_params: list
    rationale: str
    deliverable: str = ""
    insights_used: list = field(default_factory=list)
    source: str = "template"


@dataclass
class Ctx:
    fs: FactSheet
    category: dict
    merchant: dict
    trigger: dict
    customer: dict | None
    insights: list[Insight]
    used_insights: set
    now: datetime | None
    family: str = ""
    variant: int = 0
    variant_offset: int = 0

    @property
    def hi(self) -> bool:
        return self.fs.lang in ("hinglish", "hindi")

    def L(self, en: str, hi: str) -> str:
        return hi if self.hi else en

    def pick(self, *options):
        return options[self.variant % len(options)]

    @property
    def payload(self) -> dict:
        p = self.trigger.get("payload") or {}
        return {} if p.get("placeholder") else p

    def insight(self, prefer: tuple = (), exclude: tuple = (), used_ok: bool = False) -> Insight | None:
        best, best_score = None, -1.0
        for ins in self.insights:
            if ins.id in exclude or any(ins.id.startswith(e) for e in exclude):
                continue
            score = ins.strength + (0.5 if set(prefer) & set(ins.tags) else 0)
            if ins.id in self.used_insights and not used_ok:
                score -= 0.6
            if score > best_score:
                best, best_score = ins, score
        return best

    def clause(self, ins: Insight | None) -> str:
        if not ins:
            return ""
        return ins.hi if self.hi else ins.en


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _first_sentence(text: str | None, max_len: int = 170) -> str:
    if not text:
        return ""
    t = str(text).strip()
    s = t
    for m in re.finditer(r"[.!?](?=\s+[A-Z(])", t):
        prev = t[:m.start()].split()[-1] if t[:m.start()].split() else ""
        if re.fullmatch(r"(Dr|Mr|Mrs|Ms|St|No|vs|p|e\.g|i\.e|approx|[A-Z])", prev.rstrip(".")):
            continue
        s = t[:m.end()]
        break
    return s if len(s) <= max_len else s[:max_len].rsplit(" ", 1)[0] + "…"


def _pretty_dates(text: str, fs: FactSheet) -> str:
    def sub(m):
        d = parse_dt(m.group(0))
        out = f"{fmt_date(d)} {d.year}" if d else m.group(0)
        fs.allow_text(out)
        return out
    return re.sub(r"\b\d{4}-\d{2}-\d{2}\b", sub, text or "")


def _active_offers(m: dict) -> list[str]:
    return [o["title"] for o in (m.get("offers") or []) if o.get("status") == "active" and o.get("title")]


def _catalog(category: dict, types=("service_at_price", "free_service", "free_trial")) -> list[str]:
    return [o["title"] for o in (category.get("offer_catalog") or []) if o.get("type") in types and o.get("title")]


def _best_offer(ctx: Ctx) -> str:
    act = _active_offers(ctx.merchant)
    if act:
        return act[0]
    cat = _catalog(ctx.category)
    return cat[0] if cat else ""


def _customer_offer(ctx: Ctx) -> str:
    """A customer promise must be real: only active merchant offers; skip trial/new-user hooks for repeat customers."""
    visits = ((ctx.customer or {}).get("relationship") or {}).get("visits_total") or 0
    for o in _active_offers(ctx.merchant):
        if visits > 1 and re.search(r"trial|first month|first visit|new", o, re.I):
            continue
        return o
    return ""


def _last_service(ctx: Ctx) -> str:
    svcs = [s for s in ((ctx.customer or {}).get("relationship") or {}).get("services_received") or [] if s and s != "..."]
    return humanize(svcs[-1]).replace(" x", " ×") if svcs else ""


def _action_for(ctx: Ctx, ins: Insight | None) -> tuple[str, str, str]:
    """(english action, hinglish action, deliverable) that fixes the diagnosed issue."""
    offer = _best_offer(ctx)
    iid = ins.id if ins else ""
    if iid == "offer_gap" and offer:
        return (f"put '{offer}' live on your profile and announce it in a Google post",
                f"'{offer}' ko profile pe live karke ek Google post daal dete hain", f"offer_live:{offer}")
    if iid == "stale_posts":
        return ("publish 2 fresh Google posts this week — I've drafted them already",
                "is hafte 2 fresh Google posts daal dete hain — draft ready hain", "gbp_posts")
    if iid == "unverified":
        return ("finish your Google verification — it's a 5-minute call or postcard",
                "Google verification complete kar lete hain — 5 minute ka kaam hai", "gbp_verification")
    if iid.startswith("review_") and "pos" not in iid:
        return ("reply to those reviews publicly and fix the root cause — I've drafted the replies",
                "un reviews ka public reply karte hain — replies maine draft kar diye hain", "review_replies")
    if iid == "lapsed_pool":
        return ("send a short win-back WhatsApp to them" + (f" with '{offer}'" if offer else ""),
                "unhe ek chhota win-back WhatsApp bhejte hain" + (f" '{offer}' ke saath" if offer else ""),
                "winback_campaign")
    if iid in ("ctr_gap", "views_gap", "calls_gap"):
        hook = f" and lead with '{offer}'" if offer else ""
        hhook = f" aur '{offer}' ko highlight karte hain" if offer else ""
        return (f"refresh your photos{hook} so more searchers click through",
                f"photos refresh karte hain{hhook} taaki zyada log click karein", "profile_refresh")
    if offer:
        return (f"push '{offer}' in a Google post + WhatsApp status this week",
                f"'{offer}' ko is hafte Google post + WhatsApp status mein push karte hain", f"offer_push:{offer}")
    return ("run a quick profile refresh — photos, hours and one fresh post",
            "profile ka quick refresh karte hain — photos, timings aur ek fresh post", "profile_refresh")


def _cta(ctx: Ctx, en_opts: tuple, hi_opts: tuple) -> str:
    return ctx.pick(*hi_opts) if ctx.hi else ctx.pick(*en_opts)


def _join(*parts: str) -> str:
    out = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if out and not re.search(r"[.!?…:]$", out[-1]):
            out[-1] += "."
        out.append(_cap(p))
    return " ".join(out)


def _draft(ctx: Ctx, body: str, cta: str, hook: str, why: str, deliverable: str, used: list, lever: str) -> Draft:
    tname = f"vera_{ctx.family}_v1" if not ctx.family.startswith("c_") else f"merchant_{ctx.family[2:]}_v1"
    ids = [i.id for i in used if i]
    rationale = f"{ctx.trigger.get('kind')}: {why}. Lever: {lever}. Lang: {ctx.fs.lang}."
    if ids:
        rationale = f"{ctx.trigger.get('kind')}: {why}; merchant anchor={','.join(ids)}. Lever: {lever}. Lang: {ctx.fs.lang}."
    return Draft(body=re.sub(r"\s+", " ", body).strip(), cta=cta, template_name=tname,
                 template_params=[ctx.fs.salutation if ctx.fs.reader == "merchant" else ctx.fs.customer_name, hook, deliverable],
                 rationale=rationale[:300], deliverable=deliverable, insights_used=ids)


# ================================================================ merchant-facing families

def f_research(ctx: Ctx) -> Draft:
    fs, d = ctx.fs, resolve_digest(ctx.category, ctx.trigger)
    if not d:
        return f_trend(ctx)
    seg = str(d.get("patient_segment") or "")
    cohort = None
    for ins in ctx.insights:
        if ins.id.startswith("cohort_") and (not seg or seg.split("_")[0] in ins.id):
            cohort = ins
            break
    src = d.get("source", "")
    n = f"{fmt_int(d['trial_n'])}-patient " if d.get("trial_n") else ""
    title = d.get("title", "").rstrip(".")
    gist = _first_sentence(d.get("summary"))
    rel_en = f"Relevant to {ctx.clause(cohort)}" if cohort else f"Relevant for your {fs.noun[0]}'s case-mix"
    rel_hi = f"Yeh {ctx.clause(cohort)} ke liye relevant hai" if cohort else f"Aapke {fs.noun[0]} ke case-mix ke liye relevant hai"
    act = d.get("actionable", "")
    if ctx.hi:
        body = _join(f"{fs.salutation}, {src} mein ek kaam ka {n}study aaya hai: {title}",
                     gist, rel_hi + (f" — {act.lower()}" if act else ""),
                     _cta(ctx, (), ("2-min summary + ek patient-education WhatsApp draft bhej doon?",
                                    "Abstract aur patients ke liye ek short WhatsApp draft kar doon?")))
    else:
        body = _join(ctx.pick(f"{fs.salutation}, new in {src}: {title} ({n}study)",
                              f"{fs.salutation}, {src} just published a {n}study — {title}"),
                     gist, rel_en + (f" — {act[0].lower() + act[1:]}" if act else ""),
                     _cta(ctx, ("Want me to send the 2-min summary plus a patient-education WhatsApp you can forward?",
                                "Shall I pull the abstract and draft a short patient WhatsApp from it?"), ()))
    return _draft(ctx, body, "open_ended", title, f"digest item '{d.get('id')}' cited with source", "digest_summary+patient_whatsapp",
                  [cohort], "curiosity + reciprocity + source credibility")


def f_cde(ctx: Ctx) -> Draft:
    fs, d = ctx.fs, resolve_digest(ctx.category, ctx.trigger)
    p = ctx.payload
    if not d:
        return f_generic(ctx)
    when = fmt_date(d.get("date")) or ""
    dt = parse_dt(d.get("date"))
    tm = f", {dt.hour % 12 or 12}{'pm' if dt.hour >= 12 else 'am'}" if dt and dt.hour else ""
    credits = p.get("credits") or d.get("credits")
    cred = f"{credits} CDE credits" if credits else ""
    fee = _first_sentence(d.get("actionable")) if "free" in str(p.get("fee", "")) or "₹" in str(d.get("actionable", "")) else ""
    speaker = _first_sentence(d.get("summary"))
    if ctx.hi:
        body = _join(f"{fs.salutation}, {d.get('title')} — {when}{tm}" + (f", {cred}" if cred else ""),
                     speaker, fee, "Registration details bhej doon aur calendar mein block kar doon?")
    else:
        body = _join(f"{fs.salutation}, {d.get('title')} is on {when}{tm}" + (f" ({cred})" if cred else ""),
                     speaker, fee, ctx.pick("Want me to send the registration details and block the evening for you?",
                                            "Shall I send the sign-up details and add it to your calendar?"))
    return _draft(ctx, body, "binary_yes_no", d.get("title", ""), "CDE event from category digest with date/credits",
                  "registration_details", [], "professional growth + effort externalisation")


def f_compliance(ctx: Ctx) -> Draft:
    fs, p, d = ctx.fs, ctx.payload, resolve_digest(ctx.category, ctx.trigger)
    kind = ctx.trigger.get("kind")
    if kind == "supply_alert" or (d and d.get("kind") in ("alert", "supply")):
        batches = p.get("affected_batches") or []
        mol = p.get("molecule") or (d or {}).get("title", "")
        mfr = p.get("manufacturer")
        cohort = next((i for i in ctx.insights if i.id == "cohort_chronic_rx_count"), None)
        b = ", ".join(batches)
        src = (d or {}).get("source", "")
        why = _first_sentence((d or {}).get("summary")) or ""
        if ctx.hi:
            body = _join(f"{fs.salutation}, urgent: {mol} ke batches {b}" + (f" ({mfr})" if mfr else "") + " par voluntary recall aaya hai" + (f" — {src}" if src else ""),
                         why, (f"Aapke {cohort.hi.replace('aapke ', '')} mein se jinhe yeh batch mila hai, unhe inform karna hoga" if cohort else "Jin customers ko yeh batch mila hai unhe inform karna hoga"),
                         "Affected customers ke liye WhatsApp note + replacement-pickup steps draft kar doon?")
        else:
            body = _join(f"{fs.salutation}, urgent: voluntary recall on {mol} batches {b}" + (f" by {mfr}" if mfr else "") + (f" ({src})" if src else ""),
                         why, (f"With {ctx.clause(cohort)}, it's worth checking who got these batches in the last 90 days" if cohort else "Customers who got these batches should be informed"),
                         "Want me to draft the customer WhatsApp note and the replacement-pickup steps?")
        return _draft(ctx, body, "binary_yes_no", f"recall {b}", "supply recall with batch numbers + source", "recall_customer_note",
                      [cohort], "urgency + risk-bounded framing + ready workflow")
    if not d:
        return f_generic(ctx)
    deadline = fmt_date(p.get("deadline_iso")) or ""
    title = _pretty_dates(d.get("title", "").rstrip("."), fs)
    gist = _first_sentence(d.get("summary"))
    act = d.get("actionable", "")
    if ctx.hi:
        body = _join(f"{fs.salutation}, compliance heads-up: {title} ({d.get('source', '')})", gist,
                     (f"{deadline} se pehle: {act}" if deadline and not re.search(r"before|by ", act, re.I) else act),
                     "Aapki team ke liye 3-point checklist ready hai — bhej doon?")
    else:
        body = _join(f"{fs.salutation}, compliance heads-up — {title} ({d.get('source', '')})", gist,
                     (f"Before {deadline}: {act[0].lower() + act[1:]}" if deadline and act and not re.search(r"before|by ", act, re.I) else act),
                     ctx.pick("I've put together a 3-point checklist for your team — want it?",
                              "Want me to send a 3-point checklist your staff can follow?"))
    return _draft(ctx, body, "binary_yes_no", title, f"regulation with deadline {deadline or 'n/a'}; cited source",
                  "compliance_checklist", [], "loss aversion (deadline) + effort externalisation")


def f_perf_dip(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    if not p.get("metric") and any("seasonal" in str(x) for x in ctx.merchant.get("signals") or []):
        return f_seasonal_dip(ctx)
    metric = p.get("metric")
    delta = p.get("delta_pct")
    hook_en = hook_hi = ""
    if metric and isinstance(delta, (int, float)):
        base = p.get("vs_baseline")
        now_val = round(base * (1 + delta)) if isinstance(base, (int, float)) else None
        if now_val is not None:
            fs.allow_number(now_val)
        tail = f" — about {now_val} vs your usual {base}" if now_val is not None else ""
        htail = f" — lagbhag {now_val}, jabki normally {base} hote hain" if now_val is not None else ""
        hook_en = f"your {humanize(metric)} dropped {fmt_pct(abs(delta))} this week{tail}"
        hook_hi = f"is hafte aapke {humanize(metric)} {fmt_pct(abs(delta))} gire hain{htail}"
        cause = ctx.insight(prefer=("visibility", "offer", "reviews"),
                            exclude=("wow_", "ctr_lead", "calls_lead", "views_lead", "cohort_", "seasonal_now", "trend"))
    else:
        dip = next((i for i in ctx.insights if i.id.startswith("wow_") and "dip" in i.tags), None)
        if dip:
            hook_en, hook_hi = dip.en, dip.hi
            hook_id = dip.id
        else:
            gap = next((i for i in ctx.insights if i.id.endswith("_gap") and "perf" in i.tags), None)
            hook_en, hook_hi = (gap.en, gap.hi) if gap else ("your profile activity dipped this week", "is hafte profile activity thodi giri hai")
            hook_id = gap.id if gap else "-"
        cause = ctx.insight(prefer=("visibility", "offer", "reviews"),
                            exclude=("wow_", "ctr_lead", "calls_lead", "views_lead", "cohort_", "seasonal_now", "trend", hook_id))
    a_en, a_hi, deliverable = _action_for(ctx, cause)
    if ctx.hi:
        body = _join(f"{fs.salutation}, {hook_hi}", (f"Ek wajah dikh rahi hai: {cause.hi}" if cause else ""),
                     f"Sabse fast fix: {a_hi}", _cta(ctx, (), ("Shuru karoon?", "Aaj hi kar doon? Reply YES.")))
    else:
        body = _join(f"{fs.salutation}, {hook_en}", (f"One likely reason: {cause.en}" if cause else ""),
                     f"Fastest fix: {a_en}", _cta(ctx, ("Want me to go ahead?", "Shall I do it today? Reply YES."), ()))
    return _draft(ctx, body, "binary_yes_no", hook_en, "performance dip quantified vs baseline", deliverable, [cause],
                  "loss aversion + concrete fix")


def f_seasonal_dip(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    delta = p.get("delta_pct")
    metric = humanize(p.get("metric") or "views")
    beat = next((b for b in ctx.category.get("seasonal_beats") or [] if "retention" in b.get("note", "") or "lowest" in b.get("note", "")), None)
    if beat:
        fs.allow_text(beat.get("month_range"))
    members = next((i for i in ctx.insights if i.id == "cohort_total_active_members"), None)
    dig = next((d for d in ctx.category.get("digest") or [] if d.get("kind") == "seasonal"), None)
    hook = f"your {metric} are down {fmt_pct(abs(delta))} this week" if isinstance(delta, (int, float)) else f"your {metric} have softened this week"
    hhook = f"is hafte {metric} {fmt_pct(abs(delta))} kam hain" if isinstance(delta, (int, float)) else f"is hafte {metric} thode kam hain"
    norm = f"{beat['month_range']} is the {beat['note']}" if beat else "this is the usual seasonal lull"
    hnorm = f"{beat['month_range']} ka yahi pattern hai — {beat['note']}" if beat else "yeh normal seasonal dip hai"
    advice = _first_sentence(dig.get("actionable")) if dig else ""
    if ctx.hi:
        body = _join(f"{fs.salutation}, {hhook} — ghabraane ki baat nahi, {hnorm}", advice,
                     (f"Abhi focus {members.hi} ko retain karne pe rakhein" if members else "Abhi focus retention pe rakhein"),
                     "Unke liye 4-week attendance challenge draft kar doon?")
    else:
        body = _join(f"{fs.salutation}, {hook} — but this isn't a red flag: {norm}", advice,
                     (f"Best use of this window is keeping {ctx.clause(members)} engaged" if members else "Best use of this window is retention"),
                     ctx.pick("Want me to draft a 4-week attendance challenge for them?",
                              "Shall I set up a 4-week attendance challenge to hold them through the dip?"))
    return _draft(ctx, body, "binary_yes_no", hook, "expected seasonal dip reframed with category seasonal data", "attendance_challenge",
                  [members], "anxiety pre-emption + reframe + concrete retention action")


def f_perf_spike(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    metric, delta = p.get("metric"), p.get("delta_pct")
    driver = humanize(p.get("likely_driver") or "")
    phrases = payload_phrases(p, fs) if p and not metric else []
    if metric and isinstance(delta, (int, float)):
        base = f" (baseline {p['vs_baseline']})" if p.get("vs_baseline") else ""
        hook = f"your {humanize(metric)} are up {fmt_pct(abs(delta))} this week{base}"
        hhook = f"is hafte aapke {humanize(metric)} {fmt_pct(abs(delta))} badhe hain{base}"
        used = None
    elif phrases:                                   # unseen positive kinds, e.g. review_spike_positive
        kind = humanize(ctx.trigger.get("kind") or "")
        hook = hhook = f"{kind} — {'; '.join(phrases[:2])}"
        used = None
    else:
        up = next((i for i in ctx.insights if i.id.startswith("wow_") and "spike" in i.tags), None) or \
            next((i for i in ctx.insights if "strength" in i.tags), None)
        if up:
            hook, hhook, used = up.en, up.hi, up
        else:
            hook, hhook, used = "your profile saw a spike in activity this week", "is hafte aapke profile pe activity mein spike aaya hai", None
    offer = _best_offer(ctx)
    if ctx.hi:
        body = _join(f"{fs.salutation}, achhi khabar — {hhook}" + (f", aur lagta hai {driver} se aa raha hai" if driver else ""),
                     "Momentum pe ek aur push karein" + (f": '{offer}' ke saath ek follow-up post" if offer else ": ek follow-up post"),
                     "Draft ready hai — daal doon?")
    else:
        body = _join(f"{fs.salutation}, good news — {hook}" + (f", most likely from your {driver}" if driver else ""),
                     "Worth riding the momentum" + (f" with a follow-up post featuring '{offer}'" if offer else " with a follow-up post"),
                     ctx.pick("I've drafted it — want me to publish?", "Shall I put the follow-up post live today?"))
    return _draft(ctx, body, "binary_yes_no", hook, "performance spike; double down on the driver", "followup_post", [used],
                  "positive reinforcement + effort externalisation")


def f_milestone(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    now_v, target = p.get("value_now"), p.get("milestone_value")
    metric = humanize(p.get("metric") or "reviews").replace("review count", "reviews")
    if isinstance(now_v, (int, float)) and isinstance(target, (int, float)):
        gap = int(target - now_v)
        fs.allow_number(gap)
        hook = f"you're at {fmt_int(now_v)} {metric} — just {gap} away from {fmt_int(target)}" if gap > 0 else f"you just crossed {fmt_int(target)} {metric}"
        hhook = f"aap {fmt_int(now_v)} {metric} pe ho — {fmt_int(target)} se sirf {gap} door" if gap > 0 else f"aapne {fmt_int(target)} {metric} cross kar liye"
        used = None
    else:
        up = next((i for i in ctx.insights if "strength" in i.tags or "spike" in i.tags), None)
        if not up:
            return f_generic(ctx)
        hook = f"you've hit a good stretch — {up.en}"
        hhook = f"aap achhe phase mein ho — {up.hi}"
        used = up
    if ctx.hi:
        body = _join(f"{fs.salutation}, {hhook}", "Abhi happy regulars se ek chhota review request bhejein toh next milestone is hafte pakka ho sakta hai",
                     "Maine request message draft kar diya hai — bhej doon?")
    else:
        body = _join(f"{fs.salutation}, {hook}", "A short review request to your happy regulars right now usually lands the next milestone within a week",
                     ctx.pick("I've drafted the request — want me to send it?", "Shall I share the ready-to-send review request?"))
    return _draft(ctx, body, "binary_yes_no", hook, "milestone gap computed from trigger", "review_request", [used],
                  "goal-gradient + effort externalisation")


def f_review_theme(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    theme = humanize(p.get("theme") or "")
    if theme:
        occ, quote, trend = p.get("occurrences_30d"), p.get("common_quote"), p.get("trend")
        hook = f"{occ} reviews in the last 30 days mention {theme}" + (" and it's rising" if trend == "rising" else "")
        hhook = f"pichhle 30 din mein {occ} reviews mein {theme} ki complaint aayi hai" + (" aur yeh badh rahi hai" if trend == "rising" else "")
        q = f" — one says \"{quote}\"" if quote else ""
        used = None
    else:
        neg = next((i for i in ctx.insights if i.id.startswith("review_") and "pos" not in i.id), None)
        if neg:
            hook, hhook, q, used = neg.en, neg.hi, "", neg
        else:
            top = ctx.insight(prefer=("visibility", "perf"), exclude=("cohort_", "seasonal_now", "trend"))
            hook = f"a common theme is building up in your recent reviews" + (f", while {top.en}" if top else "")
            hhook = f"aapke recent reviews mein ek common theme ban raha hai" + (f", aur {top.hi}" if top else "")
            q, used = "", top
    if ctx.hi:
        body = _join(f"{fs.salutation}, {hhook}{q}", "Public reply + ek chhota fix se agle reviews badal jaate hain",
                     "Maine polite replies draft kiye hain — bhej doon?")
    else:
        body = _join(f"{fs.salutation}, {hook}{q}", "Unanswered, this pattern starts showing up in searchers' first impression",
                     ctx.pick("I've drafted calm public replies you can approve — want them?", "Want me to send over ready replies for these reviews?"))
    return _draft(ctx, body, "binary_yes_no", hook, "negative review theme with count/quote", "review_replies", [used],
                  "loss aversion + ready replies")


def f_competitor(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    name, km, offer, opened = p.get("competitor_name"), p.get("distance_km"), p.get("their_offer"), fmt_date(p.get("opened_date"))
    strength = next((i for i in ctx.insights if i.id.startswith("review_pos_")), None) or \
        next((i for i in ctx.insights if "strength" in i.tags), None)
    mine = _active_offers(ctx.merchant)
    if name:
        hook = f"{name} opened {km} km from you" + (f" on {opened}" if opened else "") + (f", leading with '{offer}'" if offer else "")
        hhook = f"{name} aapse {km} km door khula hai" + (f" ({opened})" if opened else "") + (f", '{offer}' ke saath" if offer else "")
    else:
        hook, hhook = "a new competitor has opened near you", "aapke paas ek naya competitor khula hai"
    if ctx.hi:
        body = _join(f"{fs.salutation}, heads-up: {hhook}",
                     "Price war mein jaane ki zaroorat nahi" + (f" — aapka edge: {strength.hi}" if strength else ""),
                     (f"Aapka '{mine[0]}' already strong hai; " if mine else "") + "ek post jo aapki quality highlight kare, woh draft kar doon?")
    else:
        body = _join(f"{fs.salutation}, heads-up — {hook}",
                     "I wouldn't match the price" + (f" — your edge: {strength.en}" if strength else "; compete on trust and experience instead"),
                     (f"Keep '{mine[0]}' as the entry offer, and " if mine else "") + ctx.pick("want me to draft a post that leans on that?",
                                                                                            "shall I draft a Google post that highlights it?"))
    return _draft(ctx, body, "binary_yes_no", hook, "competitor from trigger; advise against price war", "differentiation_post", [strength],
                  "loss aversion + contrarian judgement")


def f_festival(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    fest, date, days = p.get("festival"), fmt_date(p.get("date")), p.get("days_until")
    beat = None
    dt = parse_dt(p.get("date"))
    if dt:
        from .util import month_in_range
        beat = next((b for b in ctx.category.get("seasonal_beats") or [] if month_in_range(b.get("month_range", ""), dt.month)), None)
        if beat:
            fs.allow_text(beat.get("note"))
            fs.allow_text(beat.get("month_range"))
    offer = _best_offer(ctx)
    if not fest:
        beat = _next_festive_beat(ctx)
        if not beat:
            return f_generic(ctx)
        fs.allow_text(beat["month_range"])
        fs.allow_text(beat["note"])
        season = type("S", (), {"en": f"for {fs.noun[2]}, {beat['month_range']} is the {beat['note']}",
                                "hi": f"{beat['month_range']} {fs.noun[2]} ke liye {beat['note']} ka time hota hai",
                                "id": "festive_beat"})()
        if ctx.hi:
            body = _join(f"{fs.salutation}, festive season ki planning ka time hai — {season.hi}",
                         "Early bookings pakadne ke liye" + (f" '{offer}' ke around" if offer else "") + " ek festive package abhi set kar lete hain",
                         "Draft bhej doon?")
        else:
            body = _join(f"{fs.salutation}, the festive calendar is coming up — {season.en}",
                         "An early festive package" + (f" built around '{offer}'" if offer else "") + " catches planners before the rush",
                         ctx.pick("Want me to draft it?", "Shall I draft the package + a Google post?"))
        return _draft(ctx, body, "binary_yes_no", season.en, "festival trigger (no date in payload) -> category seasonal beat for this month",
                      "festival_package", [season], "timeliness + early-mover advantage")
    when = f"{fest} is on {date}" + (f" — {days} days out" if isinstance(days, int) and days > 0 else "")
    hwhen = f"{fest} {date} ko hai" + (f" — {days} din baaki" if isinstance(days, int) and days > 0 else "")
    if ctx.hi:
        body = _join(f"{fs.salutation}, {hwhen}", (f"{beat['month_range']} mein {beat['note']}" if beat else ""),
                     f"Early bookings pakadne ke liye {fest} package" + (f" '{offer}' ke around" if offer else "") + " abhi set kar lete hain",
                     "Draft bhej doon?")
    else:
        body = _join(f"{fs.salutation}, {when}", (f"For {fs.noun[2]}, {beat['month_range']} is the {beat['note']}" if beat else ""),
                     f"Locking an early {fest} package" + (f" built around '{offer}'" if offer else "") + " now catches the planners before the rush",
                     ctx.pick("Want me to draft it?", "Shall I draft the package + a Google post?"))
    return _draft(ctx, body, "binary_yes_no", when, "festival date from trigger + category seasonal beat", "festival_package", [],
                  "timeliness + early-mover advantage")


def _next_festive_beat(ctx: Ctx):
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    cur = ctx.now.month if ctx.now else 1
    best, best_d = None, 99
    for b in ctx.category.get("seasonal_beats") or []:
        if not re.search(r"festiv|festival|diwali|wedding|christmas|new year", b.get("note", ""), re.I):
            continue
        tok = re.findall(r"[A-Za-z]{3}", b.get("month_range", ""))
        if not tok or tok[0].lower() not in months:
            continue
        start = months.index(tok[0].lower()) + 1
        d = (start - cur) % 12
        if d < best_d:
            best, best_d = b, d
    return best


def f_ipl(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    match, venue = p.get("match", "tonight's match"), p.get("venue")
    t = parse_dt(p.get("match_time_iso"))
    tm = f"{t.hour % 12 or 12}:{t.minute:02d}{'pm' if t.hour >= 12 else 'am'}" if t else ""
    if t:
        fs.allow_text(tm)
    weeknight = p.get("is_weeknight")
    dig = next((d for d in ctx.category.get("digest") or [] if "ipl" in d.get("id", "").lower()), None)
    offer = _best_offer(ctx)
    head = f"{match}" + (f" at {venue}" if venue else "") + (f", {tm}" if tm else "")
    if weeknight is False and dig:
        insight = _first_sentence(dig.get("summary")).rstrip(".")
        if ctx.hi:
            body = _join(f"{fs.salutation}, aaj {head}", f"Ek zaroori baat: {insight} ({dig.get('source')})",
                         "Isliye aaj dine-in promo skip karein" + (f" aur '{offer}' ko delivery special ki tarah push karein" if offer else " aur delivery push karein"),
                         "Swiggy banner + Insta story draft kar doon?")
        else:
            body = _join(f"{fs.salutation}, {head} today", f"Worth knowing: {insight} ({dig.get('source')})",
                         "So I'd skip a dine-in match promo tonight" + (f" and push '{offer}' as a delivery-first special" if offer else " and lean on delivery"),
                         ctx.pick("Want me to draft the delivery banner + an Insta story?", "Shall I prep the delivery banner and story now?"))
        why = "Saturday match + digest says weekend matches cut covers; contrarian delivery advice"
    else:
        combo = next((o for o in _catalog(ctx.category) if "match" in o.lower()), offer)
        if ctx.hi:
            body = _join(f"{fs.salutation}, aaj {head}", "Weeknight matches pe covers badhte hain",
                         f"'{combo}' ko aaj ke liye live kar dein?" if combo else "Match-night special live kar dein?")
        else:
            body = _join(f"{fs.salutation}, {head} today", "Weeknight matches usually lift covers",
                         f"Want me to put '{combo}' live for tonight?" if combo else "Want me to set up a match-night special for tonight?")
        why = "weeknight match; push match-night offer"
    return _draft(ctx, body, "binary_yes_no", head, why, "match_day_creatives", [], "timeliness + data-backed judgement")


def f_seasonal_demand(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    trends = []
    for t in p.get("trends") or []:
        m = re.match(r"([A-Za-z_]+?)_demand_([+-]\d+)", str(t))
        if m:
            item, pct = humanize(m.group(1)).replace("antifungal", "anti-fungal"), m.group(2)
            fs.allow_number(pct.lstrip("+-"))
            trends.append(f"{item} {pct}%")
    dig = resolve_digest(ctx.category, ctx.trigger) or next((d for d in ctx.category.get("digest") or [] if d.get("kind") == "seasonal"), None)
    if not trends and not dig:
        return f_generic(ctx)
    season = humanize(p.get("season") or "").replace(" 2026", "")
    lead = ", ".join(trends[:4]) if trends else _first_sentence(dig.get("title"))
    act = _first_sentence(dig.get("actionable")) if dig else ""
    if ctx.hi:
        body = _join(f"{fs.salutation}, {season or 'season'} demand shift aa gaya hai: {lead}", act,
                     "Customers ke liye ek short 'summer essentials' WhatsApp bhi draft kar doon?")
    else:
        body = _join(f"{fs.salutation}, the {season or 'seasonal'} demand shift is here: {lead}" + (f" ({dig.get('source')})" if dig else ""),
                     act, ctx.pick("Want me to draft a short 'summer essentials' WhatsApp for your regulars too?",
                                   "Shall I also prep a customer WhatsApp featuring these items?"))
    return _draft(ctx, body, "binary_yes_no", lead, "seasonal demand trends from trigger + digest action", "seasonal_whatsapp", [],
                  "timeliness + concrete shelf action")


def f_trend(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    tr = next((i for i in ctx.insights if i.id == "trend"), None)
    if p.get("query") and isinstance(p.get("delta_yoy"), (int, float)):
        q, d = p["query"], p["delta_yoy"]
        tr = Insight("trend_payload", 1.0, f"'{q}' searches are up {fmt_pct(d)} YoY", f"'{q}' searches {fmt_pct(d)} YoY badhi hain", ("trend",))
        fs.allow_text(tr.en)
        match = next((o for o in _active_offers(ctx.merchant) + _catalog(ctx.category) if any(w in o.lower() for w in q.lower().split()[:2])), "")
        if ctx.hi:
            body = _join(f"{fs.salutation}, {tr.hi}", f"{fs.locality or 'Aapke area'} ke searchers ke liye ek post" + (f" '{match}' ke saath" if match else " is service pe"),
                         "Draft kar doon?")
        else:
            body = _join(f"{fs.salutation}, {tr.en}", f"A post aimed at searchers in {fs.locality or 'your area'}" + (f" featuring '{match}'" if match else " about this service") + " would catch that demand",
                         ctx.pick("Want me to draft it?", "Shall I prepare the post?"))
        return _draft(ctx, body, "binary_yes_no", tr.en, "trend movement from trigger payload", "trend_post", [], "curiosity + demand proof")
    if not tr:
        return f_generic(ctx)
    offer = _best_offer(ctx)
    if ctx.hi:
        body = _join(f"{fs.salutation}, {tr.hi}", f"Isko pakadne ke liye {fs.locality or 'aapke area'} ke searchers ke liye ek post" + (f" '{offer}' ke saath" if offer else ""),
                     "Draft kar doon?")
    else:
        body = _join(f"{fs.salutation}, {tr.en}", f"A post aimed at searchers in {fs.locality or 'your area'}" + (f" featuring '{offer}'" if offer else "") + " would catch that demand",
                     ctx.pick("Want me to draft it?", "Shall I prepare the post?"))
    return _draft(ctx, body, "binary_yes_no", tr.en, "category trend signal", "trend_post", [tr], "curiosity + demand proof")


_KEY_FMT = [
    (r"temp", lambda v: f"{v}°C"), (r"(^|_)days?$|duration_days", lambda v: f"for {v} days"),
    (r"delta_yoy|_pct$|pct_", lambda v: fmt_pct(v, signed=True) if isinstance(v, float) else f"{v}%"),
    (r"value_inr|amount|price|_inr$", lambda v: fmt_money(v)), (r"rating", lambda v: f"{v}★"),
]


def payload_phrases(p: dict, fs: FactSheet) -> list[str]:
    """Turn an unknown trigger payload into short, verifiable phrases (every value is from the payload)."""
    texts, facts = [], []
    for k, v in p.items():
        if k in ("placeholder", "merchant_id", "customer_id", "category") or v in (None, "", [], {}):
            continue
        if isinstance(v, str) and not re.fullmatch(r"[a-z0-9_]+", v) and not re.match(r"\d{4}-\d{2}-\d{2}", v) and len(v) > 3:
            texts.append(v.rstrip("."))
            continue
        if isinstance(v, bool) or isinstance(v, (list, dict)):
            continue
        label = humanize(re.sub(r"_(7d|30d)$", r" (\1)", k)).replace("(7d)", "in 7 days").replace("(30d)", "in 30 days")
        val = None
        for pat, fn in _KEY_FMT:
            if re.search(pat, k):
                val = fn(v)
                break
        if val is None:
            val = humanize(v) if isinstance(v, str) else (fmt_int(v) if isinstance(v, (int, float)) and v >= 1000 else str(v))
        if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
            val = fmt_date(v)
        fs.allow_text(val)
        formatted = any(re.search(pat, k) for pat, _ in _KEY_FMT)
        facts.append(val if formatted and not re.search(r"value|amount|price", k) else f"{label}: {val}")
    return texts + facts


def _related_knowledge(ctx: Ctx, words: str):
    """Category digest item / seasonal beat that shares vocabulary with the event (for the 'so what')."""
    toks = {w for w in re.findall(r"[a-z]{4,}", words.lower())} | ({"summer", "heat", "ors", "sunscreen"} if re.search(r"heat|temp", words, re.I) else set()) \
        | ({"monsoon", "rain"} if re.search(r"rain|monsoon|flood", words, re.I) else set())
    toks -= _STOP
    best, best_n = None, 0
    for d in ctx.category.get("digest") or []:
        n = len(toks & (set(re.findall(r"[a-z]{4,}", (d.get("title", "") + " " + d.get("summary", "")).lower())) - _STOP))
        if n > best_n:
            best, best_n = d, n
    return best if best_n >= 2 else None


_STOP = {"with", "from", "this", "that", "your", "near", "work", "days", "week", "more", "less", "have", "been", "will",
         "into", "over", "than", "when", "what", "they", "them", "their", "there", "about", "after", "before", "local",
         "news", "event", "update", "alert", "metro", "metros", "city", "cities", "india", "indian"}


def f_event(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    kind = humanize(ctx.trigger.get("kind") or "update")
    phrases = payload_phrases(p, fs)
    if not phrases:
        return f_generic(ctx)
    head = "; ".join(phrases[:4])
    rel = _related_knowledge(ctx, kind + " " + head)
    implication = _first_sentence(rel.get("actionable") or rel.get("summary")) if rel else ""
    offer = _best_offer(ctx)
    if ctx.hi:
        body = _join(f"{fs.salutation}, heads-up — {kind}: {head}", implication,
                     (f"Main '{offer}' ke saath iske hisaab se ek quick update" if offer else "Main iske hisaab se ek quick update") + " customers ke liye draft kar sakti hoon",
                     "Bhej doon?")
    else:
        body = _join(f"{fs.salutation}, heads-up — {kind}: {head}", implication,
                     "I can draft a quick customer update around this" + (f", leading with '{offer}'" if offer else ""),
                     ctx.pick("Want me to prepare it?", "Shall I draft it now?"))
    return _draft(ctx, body, "binary_yes_no", f"{kind}: {head}", f"unseen trigger '{ctx.trigger.get('kind')}' rendered from its own payload" +
                  (f" + related category item '{rel.get('id')}'" if rel else ""), "event_update", [], "timeliness + effort externalisation")


def f_c_event(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    kind = ctx.trigger.get("kind") or ""
    slot = p.get("missed_slot_label") or p.get("slot_label")
    svc = humanize(p.get("service") or "")
    if slot or "noshow" in kind or "missed" in kind:
        if ctx.hi:
            body = _join(_c_open(ctx), (f"{slot} pe aapka appointment" if slot else "Aapka pichhla appointment") + (f" ({svc})" if svc else "") + " miss ho gaya — koi baat nahi",
                         "Naya time chahiye toh YES reply karein, hum 2 options bhej denge.")
        else:
            body = _join(_c_open(ctx), "we missed you" + (f" at your {slot} appointment" if slot else " at your last appointment") + (f" for {svc}" if svc else "") + " — no worries",
                         "Reply YES and we'll send two new time options.")
        return _draft(ctx, body, "binary_yes_no", "missed appointment", "no-show follow-up from trigger payload; no-shame reschedule", "booking",
                      [], "warmth + easy reschedule")
    phrases = payload_phrases(p, fs)
    if not phrases:
        return f_c_generic(ctx)
    if ctx.hi:
        body = _join(_c_open(ctx), "; ".join(phrases[:2]), "Koi sawaal ho ya book karna ho toh YES reply karein.")
    else:
        body = _join(_c_open(ctx), "; ".join(phrases[:2]), "Reply YES if you'd like us to set it up for you.")
    return _draft(ctx, body, "binary_yes_no", humanize(kind), f"unseen customer trigger '{kind}' rendered from its payload", "booking", [],
                  "relevance + low friction")


def f_renewal(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    days, plan, amt = p.get("days_remaining"), p.get("plan") or (ctx.merchant.get("subscription") or {}).get("plan"), p.get("renewal_amount")
    views, calls = fs.get("perf.views"), fs.get("perf.calls")
    if not isinstance(days, (int, float)):
        days = (ctx.merchant.get("subscription") or {}).get("days_remaining")
    sub = ctx.merchant.get("subscription") or {}
    if sub.get("status") == "expired" or not isinstance(days, (int, float)) or days <= 0 or days > 60:
        return f_winback(ctx) if sub.get("status") == "expired" else f_generic(ctx)
    amt_s = f" ({fmt_money(amt)})" if isinstance(amt, (int, float)) else ""
    dip = next((i for i in ctx.insights if i.id.startswith("wow_") and "dip" in i.tags), None)
    if ctx.hi:
        body = _join(f"{fs.salutation}, aapka {plan or ''} plan {days} din mein renew hona hai{amt_s}".replace("  ", " "),
                     (f"Pichhle 30 din mein profile pe {views} aur {calls} aaye" if views and calls else ""),
                     (f"Waise {dip.hi} — renew ke saath main profile refresh bhi kar doongi" if dip else "Renew ke saath main profile refresh bhi kar doongi"),
                     "Renewal link ki jagah main sab process kar doon? Reply YES.")
    else:
        body = _join(f"{fs.salutation}, your {plan or ''} plan renews in {days} days{amt_s}".replace("  ", " "),
                     (f"In the last 30 days your listing brought in {views} and {calls}" if views and calls else ""),
                     (f"Also, {dip.en} — I'll pair the renewal with a profile refresh to fix that" if dip else "I'll pair the renewal with a quick profile refresh"),
                     "Want me to process it so nothing goes offline? Reply YES.")
    return _draft(ctx, body, "binary_yes_no", f"renewal in {days} days", "renewal due; value recap from 30d performance", "renewal+refresh",
                  [dip], "loss aversion + value recap")


def f_winback(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    since = p.get("days_since_expiry") or (ctx.merchant.get("subscription") or {}).get("days_since_expiry")
    lapsed_new = p.get("lapsed_customers_added_since_expiry")
    dip = p.get("perf_dip_pct")
    parts_en, parts_hi = [], []
    if isinstance(dip, (int, float)):
        parts_en.append(f"profile activity is down {fmt_pct(abs(dip))}")
        parts_hi.append(f"profile activity {fmt_pct(abs(dip))} gir gayi hai")
    if isinstance(lapsed_new, int):
        parts_en.append(f"{lapsed_new} more {fs.noun[1]} have lapsed")
        parts_hi.append(f"{lapsed_new} aur {fs.noun[1]} lapse ho gaye hain")
    offer = _best_offer(ctx)
    if ctx.hi:
        body = _join(f"{fs.salutation}, plan band hue {since} din ho gaye" if since else f"{fs.salutation}, aapka plan abhi paused hai",
                     ("Tab se " + " aur ".join(parts_hi)) if parts_hi else "",
                     "Main 10 minute mein profile wapas live karke" + (f" '{offer}' ke saath" if offer else "") + " un customers ko win-back message bhej sakti hoon",
                     "Restart karein? Reply YES.")
    else:
        body = _join(f"{fs.salutation}, it's been {since} days since your plan paused" if since else f"{fs.salutation}, your plan is currently paused",
                     ("Since then, " + " and ".join(parts_en)) if parts_en else "",
                     "I can bring the profile back live" + (f" with '{offer}'" if offer else "") + " and send those customers a win-back note — about 10 minutes of setup",
                     "Want to restart? Reply YES.")
    return _draft(ctx, body, "binary_yes_no", f"{since} days since expiry", "winback: quantified loss since expiry", "reactivation+winback",
                  [], "loss aversion + effort externalisation")


def f_dormant(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    days = p.get("days_since_last_merchant_message")
    top = ctx.insight(prefer=("perf", "visibility", "offer", "trend"))
    a_en, a_hi, deliv = _action_for(ctx, top)
    if ctx.hi:
        body = _join(f"{fs.salutation}, {days} din ho gaye baat kiye" if days else f"{fs.salutation}, kaafi din ho gaye",
                     (f"Is beech ek cheez notice ki: {top.hi}" if top else ""), f"Mera suggestion: {a_hi}", "Karoon?")
    else:
        body = _join(f"{fs.salutation}, it's been {days} days since we last spoke" if days else f"{fs.salutation}, it's been a while",
                     (f"One thing I noticed meanwhile: {top.en}" if top else ""), f"My suggestion: {a_en}",
                     ctx.pick("Want me to take care of it?", "Shall I go ahead?"))
    return _draft(ctx, body, "binary_yes_no", f"dormant {days}d", "re-engage dormant merchant with a fresh grounded observation", deliv,
                  [top], "reciprocity + curiosity")


def f_gbp(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    path = humanize(p.get("verification_path") or "")
    up = p.get("estimated_uplift_pct")
    upl = f"verified profiles typically see ~{fmt_pct(up)} more visibility" if isinstance(up, (int, float)) else "verified profiles rank and convert better"
    hupl = f"verified profiles ko ~{fmt_pct(up)} zyada visibility milti hai" if isinstance(up, (int, float)) else "verified profiles zyada dikhte aur convert karte hain"
    base = fs.get("perf.views")
    if ctx.hi:
        body = _join(f"{fs.salutation}, aapka Google profile abhi unverified hai", hupl + (f" — aapke {base} pe yeh seedha fark hai" if base else ""),
                     (f"Verification {path} se hota hai; main step-by-step guide kar doongi" if path else "Main step-by-step guide kar doongi"),
                     "Abhi shuru karein?")
    else:
        body = _join(f"{fs.salutation}, your Google profile is still unverified", _cap(upl) + (f" — that's on top of your current {base}" if base else ""),
                     (f"It's done via {path}; I'll walk you through it in 5 minutes" if path else "I'll walk you through it in 5 minutes"),
                     ctx.pick("Want to start now?", "Shall we do it today?"))
    return _draft(ctx, body, "binary_yes_no", "unverified profile", "GBP unverified; uplift from trigger", "gbp_verification", [],
                  "loss aversion + effort externalisation")


def f_curious(ctx: Ctx) -> Draft:
    fs = ctx.fs
    offers = _active_offers(ctx.merchant)
    if len(offers) >= 2:
        g = f"'{offers[0]}' or '{offers[1]}'"
        guess = Insight("offers_guess", 1, f"{g}", f"{g} mein se koi ek", ())
    elif offers:
        guess = Insight("offers_guess", 1, f"still '{offers[0]}'", f"abhi bhi '{offers[0]}'", ())
    else:
        guess = next((i for i in ctx.insights if i.id.startswith("review_pos_")), None) or next((i for i in ctx.insights if i.id == "trend"), None)
    quote = next((t.get("common_quote") for t in ctx.merchant.get("review_themes") or []
                  if t.get("sentiment") == "pos" and t.get("common_quote")), None)
    if guess and guess.id == "offers_guess":
        quote = None
    g_en = (f"My guess: {guess.en}" + (f" (\"{quote}\")" if quote else "")) if guess else ""
    g_hi = (f"Mera guess: {guess.hi}" + (f" (\"{quote}\")" if quote else "")) if guess else ""
    if ctx.hi:
        body = _join(f"{fs.salutation}, is hafte {fs.biz} ki sabse zyada demand wali service ko main ek Google post + customers ke liye ready price-reply bana sakti hoon — 5 minute ka kaam",
                     g_hi, "Is hafte sabse zyada kya poocha gaya?")
    else:
        body = _join(f"{fs.salutation}, I can turn this week's most-asked-for service at {fs.biz} into a Google post plus a ready price-reply for customer queries — 5 minutes, tops",
                     g_en, "Which one's been most in demand this week?")
    return _draft(ctx, body, "open_ended", "what's in demand this week", "weekly curious-ask; guess grounded in reviews/trends",
                  "gbp_post+price_reply", [guess], "asking the merchant + reciprocity")


def f_planning(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    topic = humanize(p.get("intent_topic") or "the plan")
    hist = " ".join(t.get("body", "") for t in (ctx.merchant.get("conversation_history") or []) if t.get("from") == "vera")
    offers = _active_offers(ctx.merchant)
    lines = []
    if "thali" in topic or "bulk" in topic or "corporate" in topic:
        base = next((o for o in offers if re.search(r"₹\s?\d", o)), None)
        m = re.search(r"₹\s?([\d,]+)", base or "")
        if m:
            price = int(m.group(1).replace(",", ""))
            t1, t2 = int(round(price * 0.9 / 5) * 5), int(round(price * 0.85 / 5) * 5)
            for n in (t1, t2, 10, 25):
                fs.allow_number(n)
            lines = [f"10+ thalis/day: ₹{t1} each (vs ₹{price} retail)", f"25+ thalis/day: ₹{t2} each + free delivery",
                     "Order by 5pm the day before; delivery in the lunch window"]
    elif "yoga" in topic or "program" in topic or "camp" in topic:
        m = re.search(r"(\d+)-week program, (\d+) classes/week, age (\d+-\d+), (₹[\d,]+)", hist)
        if m:
            lines = [f"{m.group(1)} weeks, {m.group(2)} classes/week, ages {m.group(3)}", f"Fee: {m.group(4)}",
                     "Saturday morning slot for parents who work weekdays"]
    head = f"here's a starter version of the {topic} — edit anything"
    hhead = f"{topic} ka starter version yeh raha — kuch bhi edit kar sakte hain"
    body_lines = "\n".join(f"• {l}" for l in lines)
    if ctx.hi:
        body = f"{fs.salutation}, {hhead}:\n{body_lines}\n" if lines else f"{fs.salutation}, {hhead}. "
        body += "Iska Google post + WhatsApp announcement draft kar doon?"
    else:
        body = f"{fs.salutation}, {head}:\n{body_lines}\n" if lines else f"{fs.salutation}, {head}. "
        body += ctx.pick("Want me to turn this into a Google post and a WhatsApp announcement?",
                         "Shall I draft the announcement post for it next?")
    d = _draft(ctx, body, "binary_yes_no", topic, "merchant already asked; deliver draft immediately (no qualifying)", "plan_announcement",
               [], "effort externalisation + momentum")
    d.body = re.sub(r"[ \t]+", " ", body).strip()
    return d


def f_generic(ctx: Ctx) -> Draft:
    fs = ctx.fs
    kind = humanize(ctx.trigger.get("kind") or "")
    top = ctx.insight(prefer=("perf", "visibility", "offer", "retention"))
    a_en, a_hi, deliv = _action_for(ctx, top)
    if ctx.hi:
        body = _join(f"{fs.salutation}, {top.hi}" if top else f"{fs.salutation}, {fs.biz} ke liye ek quick update",
                     f"Suggestion: {a_hi}", "Kar doon?")
    else:
        body = _join(f"{fs.salutation}, {top.en}" if top else f"{fs.salutation}, a quick update for {fs.biz}",
                     f"Suggestion: {a_en}", ctx.pick("Want me to go ahead?", "Shall I set it up?"))
    return _draft(ctx, body, "binary_yes_no", kind, f"'{kind}' with thin payload -> strongest grounded merchant insight", deliv,
                  [top], "specificity + effort externalisation")


# ================================================================ customer-facing families

def _c_open(ctx: Ctx) -> str:
    fs = ctx.fs
    who = fs.customer_parent or fs.customer_name
    channel = str(((ctx.customer or {}).get("preferences") or {}).get("channel") or "")
    if "via_son" in channel or "via_daughter" in channel or "via_family" in channel:
        who = ""
    if fs.lang == "hindi":
        greet = "Namaste" + (f" {who} ji" if who else "")
    elif fs.native_greeting:
        greet = f"{fs.native_greeting}" + (f" {who}" if who else "")
    else:
        greet = f"Hi {who}" if who else "Hello"
    emoji = {"dentists": " 🦷", "salons": " ✨", "gyms": " 👋", "restaurants": " 🍽️", "pharmacies": ""}.get(fs.category, "")
    here = f"{fs.biz}" + (f", {fs.locality}" if fs.locality and fs.category == "pharmacies" else "")
    if ctx.hi:
        return f"{greet}, {here} se{emoji}"
    return f"{greet}, {here} here{emoji}"


def _slot_phrase(slots: str | None) -> tuple[str, str]:
    if not slots:
        return "", ""
    h = humanize(slots).replace("weekday ", "weekday ").replace("_", " ")
    return h, h


def f_c_recall(ctx: Ctx) -> Draft:
    fs, p, c = ctx.fs, ctx.payload, ctx.customer or {}
    default_svc = {"dentists": "dental check-up", "salons": "next appointment", "gyms": "next session",
                   "pharmacies": "health check", "restaurants": "next visit"}.get(fs.category, "next visit")
    svc = humanize(p.get("service_due") or default_svc).replace("6 month", "6-month")
    last = fmt_date(p.get("last_service_date")) or fs.get("cust.last_visit")
    slots = [s.get("label") for s in (p.get("available_slots") or []) if s.get("label")]
    price = next((o for o in _active_offers(ctx.merchant) if "clean" in o.lower() or "check" in o.lower()), None) \
        if fs.category == "dentists" else _customer_offer(ctx) or None
    pref, _ = _slot_phrase((c.get("preferences") or {}).get("preferred_slots"))
    kid = f"{fs.customer_name}'s " if fs.customer_parent else ""
    hkid = f"{fs.customer_name} ka " if fs.customer_parent else ""
    if slots:
        opts = " or ".join(f"{s}" for s in slots[:2])
        hopts = " ya ".join(slots[:2])
        if len(slots) >= 2:
            cta_en, cta_hi = "Reply 1 or 2 to book — or tell us a time that suits you.", "Book karne ke liye 1 ya 2 reply karein, ya apna time batayein."
            slot_en, slot_hi = f"We've kept 2 slots for you: {opts}", f"Aapke liye 2 slots rakhe hain: {hopts}"
            cta = "multi_choice_slot"
        else:
            cta_en, cta_hi = "Reply YES to book it — or tell us a time that suits you.", "Book karne ke liye YES reply karein, ya apna time batayein."
            slot_en, slot_hi = f"We've kept a slot for you: {opts}", f"Aapke liye ek slot rakha hai: {hopts}"
            cta = "binary_yes_no"
    else:
        cta_en = "Reply YES and we'll share a couple of " + (f"{pref} " if pref else "") + "slots."
        cta_hi = "YES reply karein, hum " + (f"{pref} " if pref else "") + "slots bhej denge."
        slot_en = slot_hi = ""
        cta = "binary_yes_no"
    if ctx.hi:
        body = _join(_c_open(ctx), f"{hkid}{svc} due hai" + (f" (last visit {last})" if last else ""), slot_hi,
                     (f"{price} hi rahega" if price else ""), cta_hi)
    else:
        body = _join(_c_open(ctx), f"{kid}{svc} is due" + (f" — last visit was {last}" if last else ""), slot_en,
                     (f"Same {price}" if price else ""), cta_en)
    return _draft(ctx, body, cta, svc, "recall due; real slots/price from trigger + merchant offer; language pref honoured", "booking",
                  [], "timeliness + low-friction booking")


def f_c_appointment(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    t = parse_dt(p.get("appointment_iso") or p.get("slot_iso"))
    when = f" at {t.hour % 12 or 12}{'pm' if t.hour >= 12 else 'am'}" if t else ""
    if t:
        fs.allow_text(when)
    svc = humanize(p.get("service") or "") or _last_service(ctx)
    who = f" with Dr. {fs.owner}" if fs.category == "dentists" and fs.owner else ""
    hwho = f" Dr. {fs.owner} ke saath" if fs.category == "dentists" and fs.owner else ""
    if ctx.hi:
        body = _join(_c_open(ctx), f"Reminder: kal{when}{hwho} aapka appointment hai" + (f" (pichhli baar: {svc})" if svc else ""),
                     "Confirm karne ke liye YES reply karein, ya time badalna ho toh batayein.")
    else:
        body = _join(_c_open(ctx), f"a quick reminder that your appointment{who} is tomorrow{when}" + (f" (last time: {svc})" if svc else ""),
                     "Reply YES to confirm, or tell us if another time works better.")
    return _draft(ctx, body, "binary_yes_no", "appointment tomorrow", "transactional reminder; no invented time", "confirmation", [],
                  "commitment + easy reschedule")


def f_c_refill(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    mols = p.get("molecule_list") or []
    runs = fmt_date(p.get("stock_runs_out_iso"))
    if not mols or ctx.fs.category != "pharmacies":
        return f_c_generic(ctx)
    offers = _active_offers(ctx.merchant)
    senior = next((o for o in offers if "senior" in o.lower()), None) if (ctx.customer or {}).get("identity", {}).get("senior_citizen") else None
    deliv = next((o for o in offers if "deliver" in o.lower()), None)
    saved = p.get("delivery_address_saved")
    who = fs.customer_name.replace("Mr. ", "").strip()
    if ctx.hi:
        subj = f"{who} ji ki" if who else "Aapki"
        body = _join(_c_open(ctx), f"{subj} monthly dawaiyan ({', '.join(mols)}) {runs} tak khatam ho jayengi" if runs else f"{subj} monthly dawaiyan ({', '.join(mols)}) refill ke liye due hain",
                     "Same brand, same dose ka pack ready hai",
                     (f"{senior} lagega" if senior else "") + (f", aur saved address pe {deliv.lower()}" if deliv and saved and senior else ""),
                     (f"Saved address pe {deliv.lower()}" if deliv and saved and not senior else ""),
                     "Dispatch karne ke liye CONFIRM reply karein, dose mein koi badlav ho toh batayein.")
    else:
        body = _join(_c_open(ctx), f"the monthly refill ({', '.join(mols)}) runs out on {runs}" if runs else f"the monthly refill ({', '.join(mols)}) is due",
                     "Same brand and dose are packed and ready", (f"{senior} applies" if senior else ""),
                     (f"{deliv} to your saved address" if deliv and saved else ""),
                     "Reply CONFIRM to dispatch, or tell us if the dosage has changed.")
    return _draft(ctx, body, "binary_confirm_cancel", f"refill by {runs}", "chronic refill; molecules/date from trigger; real offers",
                  "refill_dispatch", [], "convenience + continuity of care")


def f_c_lapsed(ctx: Ctx) -> Draft:
    fs, p, c = ctx.fs, ctx.payload, ctx.customer or {}
    days = p.get("days_since_last_visit")
    weeks = round(days / 7) if isinstance(days, (int, float)) else None
    if weeks:
        fs.allow_number(weeks)
    focus = humanize(p.get("previous_focus") or (c.get("preferences") or {}).get("training_focus") or "")
    offer = _customer_offer(ctx) or next((o for o in _active_offers(ctx.merchant)), "")
    if fs.category in ("pharmacies", "restaurants"):
        return f_c_generic(ctx)
    pref, _ = _slot_phrase((c.get("preferences") or {}).get("preferred_slots"))
    since = f"it's been about {weeks} weeks" if weeks else "it's been a while since your last visit"
    hsince = f"lagbhag {weeks} hafte ho gaye" if weeks else "kaafi time ho gaya aapse mile"
    if ctx.hi:
        body = _join(_c_open(ctx), f"{hsince} — koi baat nahi, sabke saath hota hai",
                     ((f"Aapke {focus} goal ke liye" if focus else "Wapas shuru karne ke liye") + f" '{offer}' ready hai") if offer else
                     (f"Aapke {focus} goal mein phir se madad karna chahenge" if focus else "Aapko phir se dekhna achha lagega"),
                     "Agle " + (f"{pref} " if pref else "") + "slot mein aapke liye jagah rakh dein? YES reply karein — koi commitment nahi.")
    else:
        body = _join(_c_open(ctx), f"{since} — happens to everyone, no judgment",
                     ((f"For your {focus} goal, " if focus else "To ease back in, ") + f"'{offer}' is open for you") if offer else
                     (f"We'd love to help you get back to your {focus} goal" if focus else "We'd love to see you again"),
                     "Want us to hold a " + (f"{pref} " if pref else "") + "spot for you? Reply YES — no commitment.")
    return _draft(ctx, body, "binary_yes_no", since, "lapsed customer; no-shame winback with real offer + preference", "hold_slot",
                  [], "warmth + no-commitment + preference match")


def f_c_trial(ctx: Ctx) -> Draft:
    fs, p = ctx.fs, ctx.payload
    trial = fmt_date(p.get("trial_date"))
    opts = [o.get("label") for o in (p.get("next_session_options") or []) if o.get("label")]
    kid = fs.customer_name if fs.customer_parent else ""
    if fs.category in ("pharmacies", "restaurants", "dentists"):
        return f_c_generic(ctx)
    offer = next((o for o in _active_offers(ctx.merchant) if re.search(r"month|member|plan|combo|package|₹", o, re.I)), "")
    unit = "session" if fs.category == "gyms" else "appointment"
    if ctx.hi:
        body = _join(_c_open(ctx), (f"{kid} ka trial" if kid else "Aapka trial") + (f" ({trial})" if trial else "") + " kaisa raha?",
                     (f"Agla {unit}: {opts[0]}" if opts else "") + (f" — continue karne ke liye '{offer}' available hai" if offer else ""),
                     f"Agla {unit} book kar dein? YES reply karein.")
    else:
        body = _join(_c_open(ctx), (f"hope {kid} enjoyed the trial" if kid else "hope you enjoyed your trial") + (f" on {trial}" if trial else ""),
                     (f"The next {unit} is {opts[0]}" if opts else "") + (f"{'; ' if opts else ''}'{offer}' is open if you'd like to continue" if offer else ""),
                     f"Shall we book your next {unit}? Reply YES.")
    return _draft(ctx, body, "binary_yes_no", "trial follow-up", "trial follow-up with next real session", "hold_slot", [],
                  "momentum + single binary")


def f_c_bridal(ctx: Ctx) -> Draft:
    fs, p, c = ctx.fs, ctx.payload, ctx.customer or {}
    days = p.get("days_to_wedding")
    wed = fmt_date(p.get("wedding_date") or (c.get("preferences") or {}).get("wedding_date"))
    step = humanize(p.get("next_step_window_open") or "").replace("30day", "30-day")
    trial = fmt_date(p.get("trial_completed"))
    pref = humanize((c.get("preferences") or {}).get("preferred_slots") or "")
    if ctx.hi:
        body = _join(_c_open(ctx), (f"Wedding ({wed}) mein {days} din bache hain" if days else "Aapki wedding prep ka time aa gaya hai"),
                     (f"Trial ({trial}) ke baad ab {step} shuru karne ka sahi window hai" if step else ""),
                     "Pehle session ke liye aapka " + (f"{pref} " if pref else "") + "slot block kar dein?")
    else:
        body = _join(_c_open(ctx), (f"{days} days to go until your wedding on {wed}" if days else "your wedding prep window is opening"),
                     (f"Since your trial on {trial}, this is the right window to start the {step}" if step else ""),
                     "Shall we block your " + (f"{pref} " if pref else "") + "slot for the first session?")
    return _draft(ctx, body, "binary_yes_no", f"{days} days to wedding", "bridal follow-up window from trigger", "hold_slot", [],
                  "timeliness + personal milestone")


def f_c_generic(ctx: Ctx) -> Draft:
    fs = ctx.fs
    offer = _customer_offer(ctx)
    visits = fs.get("cust.visits")
    ask_en, ask_hi = {
        "pharmacies": ("Reply YES and we'll keep your usual items ready for pickup or delivery.",
                       "YES reply karein, hum aapka usual saamaan pickup/delivery ke liye ready rakhenge."),
        "restaurants": ("Reply YES and we'll hold a table for you this week.", "YES reply karein, is hafte aapke liye table rakh denge."),
    }.get(fs.category, ("Reply YES and we'll book a slot for you.", "Slot book karne ke liye YES reply karein."))
    if ctx.hi:
        body = _join(_c_open(ctx), "aapko phir se dekhne ka man hai" + (f" — {visits} visits ke liye shukriya" if visits and visits not in ("0", "1") else ""),
                     (f"'{offer}' abhi available hai" if offer else ""), ask_hi)
    else:
        body = _join(_c_open(ctx), "it's a good time for your next visit" + (f" — thanks for the {visits} visits so far" if visits and visits not in ("0", "1") else ""),
                     (f"'{offer}' is available right now" if offer else ""), ask_en)
    return _draft(ctx, body, "binary_yes_no", humanize(ctx.trigger.get("kind") or ""), "customer follow-up with thin payload; grounded in relationship",
                  "booking", [], "relationship + low friction")


FAMILIES = {
    "research": f_research, "cde": f_cde, "compliance": f_compliance, "perf_dip": f_perf_dip,
    "seasonal_dip": f_seasonal_dip, "perf_spike": f_perf_spike, "milestone": f_milestone,
    "review_theme": f_review_theme, "competitor": f_competitor, "festival": f_festival, "ipl": f_ipl,
    "seasonal_demand": f_seasonal_demand, "trend": f_trend, "event": f_event, "renewal": f_renewal,
    "winback": f_winback, "dormant": f_dormant, "gbp": f_gbp, "curious": f_curious, "planning": f_planning,
    "generic": f_generic, "c_recall": f_c_recall, "c_appointment": f_c_appointment, "c_refill": f_c_refill,
    "c_lapsed": f_c_lapsed, "c_trial": f_c_trial, "c_bridal": f_c_bridal, "c_generic": f_c_generic, "c_event": f_c_event,
}


def render(ctx: Ctx) -> Draft:
    ctx.family = family_for(ctx.trigger.get("kind", ""), ctx.trigger.get("scope", "customer" if ctx.customer else "merchant"))
    ctx.variant = stable_index(str(ctx.trigger.get("id", "")) + ctx.family, 2) + ctx.variant_offset
    fn = FAMILIES.get(ctx.family, f_generic)
    try:
        return fn(ctx)
    except Exception:  # a template bug must never take the bot down; fall back to the generic grounded family
        return (f_c_generic if ctx.customer else f_generic)(ctx)
