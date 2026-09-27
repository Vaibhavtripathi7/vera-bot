"""FactSheet: the only facts a message may use, with provenance + allowed-number registry.

Every number/name that reaches a message must be registered here (validator enforces it).
All arithmetic (gaps vs peers, days-to-X, % formatting) happens here, never in the LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .util import (canon_number, clean_first_name, customer_language, fmt_int, fmt_money, fmt_pct,
                   humanize, merchant_language, month_in_range, numbers_in, parse_dt, parse_person)

CATEGORY_NOUN = {  # (singular business noun, people noun, peer noun)
    "dentists": ("clinic", "patients", "clinics"),
    "salons": ("salon", "clients", "salons"),
    "restaurants": ("restaurant", "customers", "restaurants"),
    "gyms": ("studio", "members", "gyms"),
    "pharmacies": ("pharmacy", "customers", "pharmacies"),
}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Fact:
    id: str
    display: str
    value: object = None
    source: str = ""


@dataclass
class FactSheet:
    reader: str = "merchant"                      # merchant | customer
    lang: str = "en"                              # en | en_light | hinglish | hindi
    native_greeting: str | None = None
    facts: dict = field(default_factory=dict)
    allowed_numbers: set = field(default_factory=set)
    allowed_names: set = field(default_factory=set)
    # convenience fields filled by build()
    salutation: str = ""
    owner: str = ""
    biz: str = ""
    locality: str = ""
    city: str = ""
    category: str = ""
    noun: tuple = ("business", "customers", "businesses")
    peer_scope: str = ""
    customer_name: str = ""
    customer_parent: str | None = None
    now: datetime | None = None

    def add(self, fid: str, display: str, value=None, source: str = "") -> Fact:
        f = Fact(fid, display, value, source)
        self.facts[fid] = f
        self.allow_text(display)
        return f

    def allow_text(self, text):
        if text is None:
            return
        text = str(text)
        self.allowed_numbers |= numbers_in(text)
        for w in re.findall(r"\b[A-Z][A-Za-z0-9'&.+-]*(?:\s+[A-Z][A-Za-z0-9'&.+-]*)*", text):
            for part in w.split():
                self.allowed_names.add(part.strip(".,'").lower())

    def allow_number(self, n):
        self.allowed_numbers.add(canon_number(str(n)))

    def get(self, fid: str) -> str | None:
        f = self.facts.get(fid)
        return f.display if f else None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_date(value) -> str | None:
    dt = parse_dt(value) if isinstance(value, str) else value
    if not dt:
        return None
    return f"{dt.day} {MONTHS[dt.month - 1]}"


def salutation_for(category: str, merchant: dict) -> tuple[str, str]:
    ident = merchant.get("identity") or {}
    first = clean_first_name(ident.get("owner_first_name"))
    if not first:
        return f"{ident.get('name') or 'there'} team", ""
    if category == "dentists":
        return f"Dr. {first}", first
    return first, first


def resolve_digest(category: dict, trigger: dict) -> dict | None:
    digest = category.get("digest") or []
    by_id = {d.get("id"): d for d in digest if isinstance(d, dict)}
    payload = trigger.get("payload") or {}
    for k, v in payload.items():
        if isinstance(v, str) and v in by_id:
            return by_id[v]
    kind = trigger.get("kind", "")
    want = {"research_digest": ("research",), "regulation_change": ("compliance",),
            "cde_opportunity": ("cde",), "supply_alert": ("alert", "supply"),
            "category_seasonal": ("seasonal",), "competitor_opened": ("compete",)}.get(kind)
    if want:
        for d in digest:
            if d.get("kind") in want:
                return d
    if kind == "research_digest":        # category has no 'research' item: newest knowledge item still beats a generic nudge
        for d in digest:
            if d.get("kind") in ("tech", "trend", "cde"):
                return d
    return None


def build(category: dict, merchant: dict, trigger: dict, customer: dict | None, now: datetime | None) -> FactSheet:
    cat = category.get("slug") or merchant.get("category_slug") or ""
    fs = FactSheet(reader="customer" if customer else "merchant", category=cat, now=now)
    fs.noun = CATEGORY_NOUN.get(cat, ("business", "customers", "businesses"))
    ident = merchant.get("identity") or {}
    fs.biz = ident.get("name") or "your business"
    fs.locality = ident.get("locality") or ""
    fs.city = ident.get("city") or ""
    fs.salutation, fs.owner = salutation_for(cat, merchant)
    for t in (fs.biz, fs.locality, fs.city, fs.salutation, fs.owner, cat):
        fs.allow_text(t)
    fs.allowed_names |= {fs.biz.lower(), fs.locality.lower(), fs.city.lower()}

    if customer:
        fs.lang, fs.native_greeting = customer_language(customer, merchant)
        name, parent = parse_person((customer.get("identity") or {}).get("name"))
        fs.customer_name, fs.customer_parent = name, parent
        fs.allow_text(name)
        fs.allow_text(parent)
    else:
        fs.lang = merchant_language(merchant)

    peer = category.get("peer_stats") or {}
    fs.peer_scope = _peer_scope_phrase(peer.get("scope"), cat, fs.city)
    fs.allow_text(fs.peer_scope)

    _merchant_facts(fs, merchant, peer)
    _offer_facts(fs, merchant, category)
    _trigger_facts(fs, trigger, category)
    if customer:
        _customer_facts(fs, customer, trigger)
    for d in (category.get("digest") or []):
        # digest content is quotable (titles, sources, summaries) - it is real context
        for k in ("title", "source", "summary", "actionable"):
            fs.allow_text(d.get(k))
    return fs


def _peer_scope_phrase(scope: str | None, cat: str, city: str) -> str:
    noun = CATEGORY_NOUN.get(cat, ("", "", "businesses"))[2]
    if not scope:
        return f"similar {noun}"
    s = re.sub(r"_?20\d\d$", "", str(scope)).replace("_", " ")
    s = s.replace("practices", "clinics") if cat == "dentists" else s
    return f"{s}"


def _merchant_facts(fs: FactSheet, m: dict, peer: dict):
    perf = m.get("performance") or {}
    win = perf.get("window_days") or 30
    for key in ("views", "calls", "directions", "leads"):
        v = _num(perf.get(key))
        if v is not None:
            fs.add(f"perf.{key}", f"{fmt_int(v)} {key}", v, f"merchant.performance.{key}")
            fs.allow_number(int(v))
    fs.allow_number(win)
    ctr = _num(perf.get("ctr"))
    if ctr is not None:
        fs.add("perf.ctr", fmt_pct(ctr, digits=1), ctr, "merchant.performance.ctr")
    for key, v in (perf.get("delta_7d") or {}).items():
        v = _num(v)
        if v is not None:
            metric = key.replace("_pct", "")
            fs.add(f"delta.{metric}", fmt_pct(v, signed=True), v, f"merchant.performance.delta_7d.{key}")
            fs.allow_text(fmt_pct(abs(v)))
    for key, v in peer.items():
        nv = _num(v)
        if nv is None:
            continue
        disp = fmt_pct(nv, digits=1) if ("ctr" in key or "pct" in key) else (f"{nv:g}" if nv < 10 else fmt_int(nv))
        fs.add(f"peer.{key}", disp, nv, f"category.peer_stats.{key}")
    agg = m.get("customer_aggregate") or {}
    for key, v in agg.items():
        nv = _num(v)
        if nv is None:
            continue
        disp = fmt_pct(nv) if key.endswith("_pct") else fmt_int(nv)
        fs.add(f"agg.{key}", disp, nv, f"merchant.customer_aggregate.{key}")
    sub = m.get("subscription") or {}
    for key in ("days_remaining", "days_since_expiry"):
        if _num(sub.get(key)) is not None:
            fs.add(f"sub.{key}", fmt_int(sub[key]), sub[key], f"merchant.subscription.{key}")
    if sub.get("plan"):
        fs.allow_text(sub["plan"])
    for sig in m.get("signals") or []:
        fs.allow_text(humanize(sig))
    for i, th in enumerate(m.get("review_themes") or []):
        occ = th.get("occurrences_30d")
        if occ is not None:
            fs.allow_number(occ)
        fs.allow_text(th.get("common_quote"))
    ident = m.get("identity") or {}
    if ident.get("established_year"):
        fs.allow_number(ident["established_year"])
    for turn in (m.get("conversation_history") or [])[-4:]:
        fs.allow_text(turn.get("body"))


def _offer_facts(fs: FactSheet, m: dict, category: dict):
    for o in m.get("offers") or []:
        fs.allow_text(o.get("title"))
        if o.get("ended"):
            fs.allow_text(fmt_date(o["ended"]))
    for o in category.get("offer_catalog") or []:
        fs.allow_text(o.get("title"))


def _trigger_facts(fs: FactSheet, trigger: dict, category: dict):
    payload = trigger.get("payload") or {}

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            fs.allow_number(v if isinstance(v, int) else f"{v:g}")
            if isinstance(v, float) and abs(v) <= 1.5:
                fs.allow_text(fmt_pct(abs(v)))
        elif isinstance(v, str):
            fs.allow_text(v if not re.fullmatch(r"[a-z0-9_]+", v) else humanize(v))
            d = parse_dt(v) if re.match(r"\d{4}-\d{2}-\d{2}", v) else None
            if d:
                fs.allow_text(fmt_date(d))
                fs.allow_text(f"{d.hour % 12 or 12}{'pm' if d.hour >= 12 else 'am'}")
    walk(payload)
    digest = resolve_digest(category, trigger)
    if digest:
        for k in ("title", "source", "summary", "actionable", "trial_n", "credits", "date"):
            if digest.get(k) is not None:
                fs.allow_text(str(digest[k]))
        if digest.get("trial_n"):
            fs.allow_text(fmt_int(digest["trial_n"]))
        if digest.get("date"):
            fs.allow_text(fmt_date(digest["date"]))


def _customer_facts(fs: FactSheet, c: dict, trigger: dict):
    rel = c.get("relationship") or {}
    if rel.get("visits_total") is not None:
        fs.add("cust.visits", fmt_int(rel["visits_total"]), rel["visits_total"], "customer.relationship.visits_total")
    last = parse_dt(rel.get("last_visit"))
    if last:
        fs.add("cust.last_visit", fmt_date(last), last, "customer.relationship.last_visit")
        if fs.now:
            days = (fs.now - last).days
            if 20 <= days <= 400:
                months = round(days / 30.4)
                fs.add("cust.months_since", f"{months}", months, "derived: now - last_visit")
                fs.add("cust.weeks_since", f"{round(days / 7)}", round(days / 7), "derived: now - last_visit")
    for k in ("favourite_dish",):
        if rel.get(k):
            fs.allow_text(rel[k])
    prefs = c.get("preferences") or {}
    for k in ("preferred_stylist", "wedding_date"):
        if prefs.get(k):
            fs.allow_text(prefs[k])
            if k == "wedding_date":
                fs.allow_text(fmt_date(prefs[k]))


# ---------------- insight engine ----------------

@dataclass
class Insight:
    id: str
    strength: float
    en: str                   # clause, English
    hi: str                   # clause, Hinglish
    tags: tuple = ()          # affinity tags, e.g. ("perf", "visibility")


def diagnose(fs: FactSheet, category: dict, merchant: dict, now: datetime | None) -> list[Insight]:
    """Rank grounded, merchant-specific observations. Only fire when the inputs exist."""
    out: list[Insight] = []
    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}
    scope = fs.peer_scope
    people = fs.noun[1]

    ctr, pctr = _num(perf.get("ctr")), _num(peer.get("avg_ctr"))
    if ctr is not None and pctr:
        gap = (ctr - pctr) / pctr
        if gap <= -0.12:
            out.append(Insight("ctr_gap", 0.8 + min(0.2, -gap / 3),
                               f"your profile CTR is {fmt_pct(ctr, digits=1)} vs {fmt_pct(pctr, digits=1)} avg for {scope}",
                               f"aapka profile CTR {fmt_pct(ctr, digits=1)} hai, jabki {scope} ka avg {fmt_pct(pctr, digits=1)} hai",
                               ("visibility", "perf")))
        elif gap >= 0.15:
            out.append(Insight("ctr_lead", 0.55,
                               f"your CTR of {fmt_pct(ctr, digits=1)} already beats the {fmt_pct(pctr, digits=1)} avg for {scope}",
                               f"aapka CTR {fmt_pct(ctr, digits=1)} already {scope} ke avg {fmt_pct(pctr, digits=1)} se upar hai",
                               ("strength",)))
    for key, label_en, label_hi in (("calls", "calls", "calls"), ("views", "profile views", "profile views")):
        v, pv = _num(perf.get(key)), _num(peer.get(f"avg_{key}_30d"))
        if v is not None and pv:
            gap = (v - pv) / pv
            if gap <= -0.25:
                out.append(Insight(f"{key}_gap", 0.7 + min(0.25, -gap / 3),
                                   f"{fmt_int(v)} {label_en} in 30 days vs ~{fmt_int(pv)} for {scope}",
                                   f"30 din mein {fmt_int(v)} {label_hi}, jabki {scope} ka avg ~{fmt_int(pv)} hai",
                                   ("perf", "visibility")))
            elif gap >= 0.3:
                out.append(Insight(f"{key}_lead", 0.5,
                                   f"{fmt_int(v)} {label_en} in 30 days, well above the ~{fmt_int(pv)} avg for {scope}",
                                   f"30 din mein {fmt_int(v)} {label_hi} — {scope} ke ~{fmt_int(pv)} avg se kaafi upar",
                                   ("strength",)))
    for key, v in (perf.get("delta_7d") or {}).items():
        v = _num(v)
        metric = key.replace("_pct", "").replace("ctr", "CTR")
        if v is None or abs(v) < 0.1:
            continue
        word = "up" if v > 0 else "down"
        hiword = "badhe" if v > 0 else "gire"
        verb = "is" if metric == "CTR" else "are"
        out.append(Insight(f"wow_{metric}", 0.6 + min(0.3, abs(v)),
                           f"{metric} {verb} {word} {fmt_pct(abs(v))} this week",
                           f"is hafte {metric} {fmt_pct(abs(v))} {hiword} hain",
                           ("perf", "dip" if v < 0 else "spike")))
    agg = merchant.get("customer_aggregate") or {}
    for key in ("lapsed_180d_plus", "lapsed_90d_plus"):
        if _num(agg.get(key)):
            days = "6+ months" if "180" in key else "3+ months"
            hdays = "6 mahine se zyada" if "180" in key else "3 mahine se zyada"
            out.append(Insight("lapsed_pool", 0.75,
                               f"{fmt_int(agg[key])} of your {people} haven't been back in {days}",
                               f"aapke {fmt_int(agg[key])} {people} {hdays} se wapas nahi aaye",
                               ("retention", "customers")))
            break
    for key, (en_lbl, hi_lbl) in {"high_risk_adult_count": ("high-risk adult patients", "high-risk adult patients"),
                                  "chronic_rx_count": ("chronic-Rx customers", "chronic-Rx customers"),
                                  "total_active_members": ("active members", "active members")}.items():
        if _num(agg.get(key)):
            out.append(Insight(f"cohort_{key}", 0.5, f"your {fmt_int(agg[key])} {en_lbl}",
                               f"aapke {fmt_int(agg[key])} {hi_lbl}", ("cohort",)))
    for key, pkey in (("retention_6mo_pct", "retention_6mo_pct"), ("retention_3mo_pct", "retention_3mo_pct")):
        r, pr = _num(agg.get(key)), _num(peer.get(pkey))
        if r is not None and pr and r < pr - 0.03:
            out.append(Insight("retention_gap", 0.7,
                               f"{fmt_pct(r)} of {people} return within {'6' if '6' in key else '3'} months vs {fmt_pct(pr)} for {scope}",
                               f"{'6' if '6' in key else '3'} mahine mein sirf {fmt_pct(r)} {people} wapas aate hain, {scope} ka avg {fmt_pct(pr)} hai",
                               ("retention",)))
    offers = merchant.get("offers") or []
    active = [o for o in offers if o.get("status") == "active"]
    catalog = [o for o in (category.get("offer_catalog") or []) if o.get("type") in ("service_at_price", "free_service", "free_trial")]
    if not active and catalog:
        best = catalog[0]
        out.append(Insight("offer_gap", 0.72,
                           f"there's no live offer on your profile right now — {fs.noun[2]} like yours usually lead with a service+price hook such as '{best['title']}' (your price, your call)",
                           f"aapke profile pe abhi koi live offer nahi hai — aise {fs.noun[2]} aksar '{best['title']}' jaisa service+price hook rakhte hain (price aap decide karein)",
                           ("offer", "visibility")))
    for o in offers:
        if o.get("status") == "expired" and o.get("ended"):
            out.append(Insight("offer_expired", 0.55,
                               f"your '{o['title']}' offer lapsed on {fmt_date(o['ended'])}",
                               f"aapka '{o['title']}' offer {fmt_date(o['ended'])} ko khatam ho gaya",
                               ("offer",)))
            break
    for sig in merchant.get("signals") or []:
        m = re.match(r"stale_posts:(\d+)d", str(sig))
        if m:
            pf = _num(peer.get("avg_post_freq_days"))
            tail = f"; {fs.noun[2]} around you post every ~{int(pf)} days" if pf else ""
            htail = f"; aas-paas ke {fs.noun[2]} har ~{int(pf)} din mein post karte hain" if pf else ""
            out.append(Insight("stale_posts", 0.7, f"your last Google post was {m.group(1)} days ago{tail}",
                               f"aapki last Google post {m.group(1)} din pehle thi{htail}", ("visibility", "content")))
    for th in merchant.get("review_themes") or []:
        occ = th.get("occurrences_30d")
        if not occ:
            continue
        theme = humanize(th.get("theme"))
        if th.get("sentiment") == "neg":
            out.append(Insight(f"review_{th.get('theme')}", 0.65 + min(0.2, occ / 20),
                               f"{occ} reviews this month mention {theme}",
                               f"is mahine {occ} reviews mein {theme} ki baat aayi hai", ("reviews", "ops")))
        elif occ >= 5:
            out.append(Insight(f"review_pos_{th.get('theme')}", 0.45,
                               f"{occ} reviews this month praise your {theme}",
                               f"is mahine {occ} reviews ne aapke {theme} ki tareef ki hai", ("reviews", "strength")))
    ident = merchant.get("identity") or {}
    if ident.get("verified") is False:
        out.append(Insight("unverified", 0.7, "your Google profile is still unverified",
                           "aapka Google profile abhi tak verified nahi hai", ("visibility", "gbp")))
    sub = merchant.get("subscription") or {}
    dr = _num(sub.get("days_remaining"))
    if sub.get("status") == "expired" and _num(sub.get("days_since_expiry")):
        out.append(Insight("sub_expired", 0.6, f"your plan lapsed {fmt_int(sub['days_since_expiry'])} days ago",
                           f"aapka plan {fmt_int(sub['days_since_expiry'])} din pehle expire ho gaya", ("subscription",)))
    elif dr is not None and 0 < dr <= 15:
        out.append(Insight("sub_ending", 0.6, f"your {sub.get('plan') or ''} plan ends in {fmt_int(dr)} days".replace("  ", " "),
                           f"aapka {sub.get('plan') or ''} plan {fmt_int(dr)} din mein khatam ho raha hai".replace("  ", " "),
                           ("subscription",)))
    if now:
        for beat in category.get("seasonal_beats") or []:
            if month_in_range(beat.get("month_range", ""), now.month):
                note = beat.get("note", "")
                fs.allow_text(note)
                out.append(Insight("seasonal_now", 0.5, f"it's {beat['month_range']} — {note}",
                                   f"{beat['month_range']} chal raha hai — {note}", ("seasonal",)))
                break
    trends = sorted(category.get("trend_signals") or [], key=lambda t: -(_num(t.get("delta_yoy")) or 0))
    city = fs.city.lower()
    trends = sorted(trends, key=lambda t: 0 if city and city in str(t.get("query", "")).lower() else 1)
    if trends:
        t = trends[0]
        q, d = t.get("query"), _num(t.get("delta_yoy"))
        if q and d:
            fs.allow_text(q)
            out.append(Insight("trend", 0.5, f"'{q}' searches are up {fmt_pct(d)} YoY",
                               f"'{q}' searches {fmt_pct(d)} YoY badhi hain", ("trend", "demand")))
    VISIBLE = {"ctr_gap": 0.15, "ctr_lead": 0.15, "calls_gap": 0.15, "views_gap": 0.15, "calls_lead": 0.15, "views_lead": 0.15,
               "offer_gap": 0.2, "stale_posts": 0.2, "unverified": 0.2, "sub_ending": 0.1, "sub_expired": 0.1}
    for ins in out:          # insight clauses are computed from registered facts -> quotable
        fs.allow_text(ins.en)
        ins.strength += VISIBLE.get(ins.id, 0.0)
    out.sort(key=lambda i: -i.strength)
    return out
