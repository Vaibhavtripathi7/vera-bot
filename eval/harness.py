"""Local replica of the judge harness lifecycle, stricter than judge_simulator.py.

Warmup -> 12 ticks of 5 simulated minutes -> adaptive injections the bot has never seen
(new digest items, perf shifts, unseen trigger kinds, new customers + recall 1 tick later,
malformed/missing fields) -> scripted merchant/customer personas for multi-turn replies ->
independent grounding + operational checks -> optional replica LLM judge.

Usage:
  uv run python -m eval.harness                 # spawns the bot locally on :8765
  uv run python -m eval.harness --url https://your-bot --judge gemini
Outputs eval/out/{transcript.jsonl, review.md, summary.json}
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "expanded"
OUT = ROOT / "eval" / "out"

# ---------------------------------------------------------------- unseen injections

NEW_DIGEST = {
    "dentists": {"id": "d_2026W40_ida_sealant", "kind": "research", "title": "Resin sealants cut molar caries 44% in 6-12 year olds over 3 years",
                 "source": "Indian Journal of Dental Research, Sep 2026", "trial_n": 1380, "patient_segment": "children",
                 "summary": "School-based cohort across 4 states; single application of resin sealant at age 6-7. Effect strongest in first permanent molars.",
                 "actionable": "Offer sealants at pediatric check-ups for the 6-12 age band"},
    "salons": {"id": "d_2026W40_scalp_detox", "kind": "trend", "title": "Scalp-detox treatments +57% YoY in metro salons",
               "source": "Salon India magazine, Sep 2026", "summary": "Hard-water scalp build-up is the top stated reason; average ticket ₹899.",
               "actionable": "Add a scalp-detox add-on to hair spa bookings"},
    "restaurants": {"id": "d_2026W40_fssai_label", "kind": "compliance", "title": "FSSAI allergen labelling mandatory on delivery menus from 2026-11-01",
                    "source": "FSSAI order 2026/09/17", "summary": "Top 8 allergens must be flagged per dish on aggregator menus. Non-compliant listings may be delisted.",
                    "actionable": "Tag allergens on every dish in your Swiggy/Zomato menu before 1 Nov"},
    "gyms": {"id": "d_2026W40_zone2", "kind": "research", "title": "Zone-2 cardio 3x/week improved HbA1c by 0.6 points in 12 weeks",
             "source": "ICMR-NIN trial, Sep 2026", "trial_n": 420, "patient_segment": "prediabetic adults 30-55",
             "summary": "Supervised 40-minute zone-2 sessions outperformed unsupervised walking.", "actionable": "Package a 12-week supervised zone-2 program"},
    "pharmacies": {"id": "d_2026W40_insulin_cold", "kind": "compliance", "title": "CDSCO cold-chain audit for insulin storage — Q4 2026",
                   "source": "CDSCO circular 2026-09-20", "summary": "Pharmacies must log fridge temperature twice daily; 2-8°C band. Penalties for missing logs.",
                   "actionable": "Start a twice-daily temperature log for your insulin fridge this week"},
}
UNSEEN_TRIGGERS = [
    ("weather_heatwave", "m_009_apollo_pharmacy_jaipur", None, {"city": "Jaipur", "temp_c": 44, "days": 3, "advisory": "IMD orange alert"}, 3),
    ("local_news_event", "m_005_pizzajunction_restaurant_delhi", None, {"headline": "Ring Road closed near Sant Nagar for metro work", "duration_days": 4}, 2),
    ("appointment_noshow", "m_001_drmeera_dentist_delhi", "c_002_rohit_for_m001", {"missed_slot_label": "Sat 3 Oct, 10am", "service": "root canal session 2"}, 3),
    ("category_trend_movement", "m_003_studio11_salon_hyderabad", None, {"query": "scalp detox hyderabad", "delta_yoy": 0.57}, 2),
    ("inventory_expiry_alert", "m_010_sunrisepharm_pharmacy_lucknow", None, {"items_expiring_30d": 14, "value_inr": 8200}, 3),
    ("review_spike_positive", "m_008_zenyoga_gym_chennai", None, {"new_reviews_7d": 9, "avg_rating_7d": 4.9}, 1),
    ("perf_dip", "m_007_powerhouse_gym_bangalore", None, {}, 4),                                       # missing payload fields
    ("research_digest", "m_001_drmeera_dentist_delhi", None, {"category": "dentists", "top_item_id": "d_2026W40_ida_sealant"}, 2),
    ("regulation_change", "m_006_southindiancafe_restaurant_bangalore", None, {"top_item_id": "d_2026W40_fssai_label", "deadline_iso": "2026-11-01"}, 4),
    ("regulation_change", "m_009_apollo_pharmacy_jaipur", None, {"top_item_id": "d_2026W40_insulin_cold"}, 4),
]
NEW_CUSTOMERS = [
    ("m_003_studio11_salon_hyderabad", {"name": "Meghna", "language_pref": "te-en mix"}, "haircut", "saturday_afternoon"),
    ("m_007_powerhouse_gym_bangalore", {"name": "Farhan", "language_pref": "english"}, "strength_program", "weekday_7am"),
    ("m_009_apollo_pharmacy_jaipur", {"name": "Kamla Devi", "language_pref": "hi", "senior_citizen": True}, "chronic_rx_bp", "morning_delivery"),
    ("m_006_southindiancafe_restaurant_bangalore", {"name": "Ritu", "language_pref": "hi-en mix"}, "family_brunch", "sunday_brunch"),
    ("m_002_bharat_dentist_mumbai", {"name": "Omkar", "language_pref": "mr-en mix"}, "scaling", "weekday_evening"),
]

PERSONAS = ["engaged_yes", "question_then_yes", "auto_reply", "not_interested", "later", "hostile_then_offtopic",
            "hinglish_commit", "curveball"]


def persona_turns(p: str, merchant_name: str) -> list[str]:
    return {
        "engaged_yes": ["Yes please, go ahead.", "Looks good. What else?"],
        "question_then_yes": ["How much will this cost me?", "Ok, let's do it. What's next?"],
        "auto_reply": [f"Thank you for contacting {merchant_name}! Our team will respond shortly."] * 4,
        "not_interested": ["Not interested, thanks.", "I said no."],
        "later": ["Busy right now, message me tomorrow."],
        "hostile_then_offtopic": ["Why do you keep bothering me? This is useless.", "Can you help me file my GST this month?"],
        "hinglish_commit": ["Haan theek hai, kar do", "Accha, aur kya karna hai?"],
        "curveball": ["Where did you get this data from?", "Ok send it"],
    }[p]


def customer_turns(i: int) -> list[str]:
    return [["1", "Thanks!"], ["Can I come on Sunday instead?"], ["YES"], ["Stop sending me messages"]][i % 4]


# ---------------------------------------------------------------- grounding check (independent of the bot's validator)

def _num_tokens(text: str) -> set[str]:
    out = set()
    for m in re.finditer(r"(?<![A-Za-z])(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)", text):
        t = m.group(1).replace(",", "")
        t = t.rstrip("0").rstrip(".") if "." in t else t
        out.add(t.lstrip("0") or "0")
    return out


def grounded_numbers(ctx_blobs: list) -> set[str]:
    s = set()
    for b in ctx_blobs:
        txt = json.dumps(b, ensure_ascii=False)
        s |= _num_tokens(txt)
        for m in re.finditer(r"(?<![\d.])(0\.\d+)", txt):          # fractions -> percents
            v = float(m.group(1)) * 100
            s.add(f"{v:.1f}".rstrip("0").rstrip("."))
            s.add(str(round(v)))
        for m in re.finditer(r"\d{4}-(\d{2})-(\d{2})", txt):       # dates -> day numbers
            s.add(str(int(m.group(2))))
    return s | {str(i) for i in range(0, 11)} | {"15", "24", "30", "48", "60", "90"}


# ---------------------------------------------------------------- runner

class Runner:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.http = httpx.Client(timeout=35)
        self.log: list[dict] = []
        self.latency: dict[str, list[float]] = {"context": [], "tick": [], "reply": [], "healthz": []}
        self.penalties: list[str] = []
        self.ctx = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
        self.versions: dict[tuple, int] = {}

    def call(self, method: str, path: str, body=None, kind="context"):
        t0 = time.monotonic()
        try:
            r = self.http.request(method, self.url + path, json=body)
            dt = time.monotonic() - t0
            self.latency.setdefault(kind, []).append(dt)
            if dt > 10:
                self.penalties.append(f"slow {kind} {dt:.1f}s")
            try:
                return r.status_code, r.json()
            except ValueError:
                self.penalties.append(f"malformed json from {path}")
                return r.status_code, None
        except httpx.HTTPError as e:
            self.penalties.append(f"{kind} error {type(e).__name__}")
            return 0, None

    def push(self, scope, cid, payload, version=None):
        v = version or self.versions.get((scope, cid), 0) + 1
        self.versions[(scope, cid)] = v
        self.ctx[scope][cid] = payload
        code, body = self.call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": v, "payload": payload,
                                                       "delivered_at": datetime.now(timezone.utc).isoformat()})
        if code != 200:
            self.penalties.append(f"context {scope}/{cid} v{v} -> {code}")
        return code


def load_dataset():
    cats = {json.load(open(f))["slug"]: json.load(open(f)) for f in glob.glob(str(EXP / "categories" / "*.json"))}
    rd = lambda sub, key: {json.load(open(f))[key]: json.load(open(f)) for f in sorted(glob.glob(str(EXP / sub / "*.json")))}
    return cats, rd("merchants", "merchant_id"), rd("customers", "customer_id"), rd("triggers", "id")


def run(url: str, judge: str | None, ticks: int = 12):
    OUT.mkdir(parents=True, exist_ok=True)
    cats, merchants, customers, triggers = load_dataset()
    R = Runner(url)
    code, h = R.call("GET", "/v1/healthz", kind="healthz")
    assert code == 200, f"healthz failed: {code}"
    R.call("GET", "/v1/metadata", kind="healthz")
    R.call("POST", "/v1/teardown", {}, kind="context")
    for slug, c in cats.items():
        R.push("category", slug, c)
    for mid, m in merchants.items():
        R.push("merchant", mid, m)
    for cid, c in customers.items():
        R.push("customer", cid, c)
    _, h = R.call("GET", "/v1/healthz", kind="healthz")
    warm_ok = h and h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}
    if not warm_ok:
        R.penalties.append(f"warmup counts wrong: {h}")

    pairs = json.load(open(EXP / "test_pairs.json"))["pairs"]
    canon = [p["trigger_id"] for p in pairs]
    rest = [t for t in triggers if t not in canon]
    schedule: dict[int, list[str]] = {i: [] for i in range(ticks)}
    for i, tid in enumerate(canon + rest):
        schedule[i % 8].append(tid)
    now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    active: list[str] = []
    transcripts = []
    persona_i = 0
    new_cust_ids = []
    for k in range(ticks):
        tnow = now + timedelta(minutes=5 * k)
        # ---- injections
        if k == 2:
            for slug, item in NEW_DIGEST.items():
                c2 = copy.deepcopy(cats[slug])
                c2["digest"] = [item] + c2["digest"]
                R.push("category", slug, c2)
        if k == 3:
            for mid in list(merchants)[:10]:
                m2 = copy.deepcopy(merchants[mid])
                perf = m2["performance"]
                f = 0.62 if hash(mid) % 2 else 1.38
                perf["views"], perf["calls"] = int(perf["views"] * f), max(1, int(perf["calls"] * f))
                perf["delta_7d"] = {"views_pct": round(f - 1, 2), "calls_pct": round(f - 1, 2)}
                R.push("merchant", mid, m2)
        if k == 4:
            for i, (mid, ident, svc, slot) in enumerate(NEW_CUSTOMERS):
                cid = f"c_new_{i}_for_{mid[:6]}"
                R.push("customer", cid, {"customer_id": cid, "merchant_id": mid, "identity": {**ident, "phone_redacted": "<phone>"},
                                         "relationship": {"first_visit": "2025-10-01", "last_visit": "2026-03-20", "visits_total": 5,
                                                          "services_received": [svc]}, "state": "lapsed_soft",
                                         "preferences": {"preferred_slots": slot, "reminder_opt_in": True},
                                         "consent": {"opted_in_at": "2025-10-01", "scope": ["recall_reminders"]}})
                new_cust_ids.append((cid, mid))
        if k == 5:
            for i, (cid, mid) in enumerate(new_cust_ids):
                tid = f"trg_new_recall_{i}"
                R.push("trigger", tid, {"id": tid, "scope": "customer", "kind": "recall_due", "source": "internal", "merchant_id": mid,
                                        "customer_id": cid, "payload": {"service_due": "follow_up_visit", "last_service_date": "2026-03-20",
                                                                         "available_slots": [{"iso": "2026-10-03T11:00:00+05:30", "label": "Sat 3 Oct, 11am"}]},
                                        "urgency": 3, "suppression_key": f"recall:{cid}", "expires_at": "2026-10-30T00:00:00Z"})
                active.append(tid)
        if k in (5, 6, 7, 8, 9):
            for j, (kind, mid, cid, payload, urg) in enumerate(UNSEEN_TRIGGERS[(k - 5) * 2:(k - 5) * 2 + 2]):
                tid = f"trg_unseen_{kind}_{k}_{j}"
                R.push("trigger", tid, {"id": tid, "scope": "customer" if cid else "merchant", "kind": kind, "source": "external",
                                        "merchant_id": mid, "customer_id": cid, "payload": payload, "urgency": urg,
                                        "suppression_key": f"{kind}:{mid}:{k}", "expires_at": "2026-10-30T00:00:00Z"})
                active.append(tid)
        for tid in schedule.get(k, []):
            R.push("trigger", tid, triggers[tid])
            active.append(tid)
        # ---- tick
        code, out = R.call("POST", "/v1/tick", {"now": tnow.isoformat().replace("+00:00", "Z"), "available_triggers": active}, kind="tick")
        acts = (out or {}).get("actions", []) if code == 200 else []
        for a in acts:
            missing = {"conversation_id", "merchant_id", "send_as", "trigger_id", "body", "cta", "suppression_key", "rationale"} - set(a)
            if missing or not a.get("body"):
                R.penalties.append(f"malformed action {a.get('trigger_id')}: missing {missing}")
            trg = R.ctx["trigger"].get(a.get("trigger_id"), {})
            m = R.ctx["merchant"].get(a.get("merchant_id"), {})
            cat = R.ctx["category"].get(m.get("category_slug"), {})
            cust = R.ctx["customer"].get(a.get("customer_id")) if a.get("customer_id") else None
            ungrounded = _num_tokens(a.get("body", "")) - grounded_numbers([trg, m, cat, cust or {}])
            conv = {"tick": k, "action": a, "ungrounded_numbers": sorted(ungrounded), "turns": []}
            # ---- replies
            if cust:
                turns = customer_turns(persona_i)
                role = "customer"
            else:
                p = PERSONAS[persona_i % len(PERSONAS)]
                turns = persona_turns(p, (m.get("identity") or {}).get("name", "our business"))
                conv["persona"] = p
                role = "merchant"
            persona_i += 1
            sent = [a.get("body", "")]
            for n, msg in enumerate(turns):
                code, r = R.call("POST", "/v1/reply", {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"],
                                                       "customer_id": a.get("customer_id"), "from_role": role, "message": msg,
                                                       "received_at": tnow.isoformat(), "turn_number": n + 2}, kind="reply")
                r = r or {}
                conv["turns"].append({"in": msg, "out": r})
                if r.get("action") == "send":
                    if r.get("body") in sent:
                        R.penalties.append(f"repeat body in {a['conversation_id']}")
                    sent.append(r.get("body", ""))
                if r.get("action") in ("end", "wait") or code != 200:
                    break
            transcripts.append(conv)
        R.call("GET", "/v1/healthz", kind="healthz")

    scores = judge_all(transcripts, R, judge) if judge else None
    report(R, transcripts, scores, warm_ok)


# ---------------------------------------------------------------- replica judge (optional)

JUDGE_SYSTEM = open(ROOT / "judge_simulator.py").read().split('SYSTEM = """')[1].split('"""')[0]


def judge_all(transcripts, R: Runner, provider: str):
    from vera.llm import Gemini, Groq, parse_json
    import asyncio
    key = os.environ.get("GEMINI_API_KEY" if provider == "gemini" else "GROQ_API_KEY", "")
    model = os.environ.get("JUDGE_MODEL", "gemini-2.5-flash" if provider == "gemini" else "llama-3.3-70b-versatile")
    P = (Gemini if provider == "gemini" else Groq)("judge", model, key, 1000, 100000)
    out = []

    async def one(client, conv):
        a = conv["action"]
        m = R.ctx["merchant"].get(a["merchant_id"], {})
        cat = R.ctx["category"].get(m.get("category_slug"), {})
        trg = R.ctx["trigger"].get(a["trigger_id"], {})
        cust = R.ctx["customer"].get(a.get("customer_id")) if a.get("customer_id") else None
        prompt = f"""SCORE THIS MESSAGE:

=== CONTEXT PROVIDED TO BOT ===
Category: {cat.get('slug')}
Voice: {cat.get('voice', {}).get('tone')}
Taboos: {cat.get('voice', {}).get('vocab_taboo', [])[:5]}

Merchant: {m.get('identity', {}).get('name')}
Owner: {m.get('identity', {}).get('owner_first_name')}
Locality: {m.get('identity', {}).get('locality')}
Languages: {m.get('identity', {}).get('languages', [])}
Performance: views={m.get('performance', {}).get('views')}, calls={m.get('performance', {}).get('calls')}, ctr={m.get('performance', {}).get('ctr')}
Signals: {m.get('signals', [])}
Active Offers: {[o.get('title') for o in m.get('offers', []) if o.get('status') == 'active']}

Trigger Kind: {trg.get('kind')}
Trigger Payload: {json.dumps(trg.get('payload', {}))}
Trigger Urgency: {trg.get('urgency')}

Customer: {json.dumps(cust.get('identity', {})) if cust else 'None (merchant-facing)'}

=== BOT'S MESSAGE ===
Body ({len(a['body'])} chars): "{a['body']}"
CTA: {a.get('cta')}
Send As: {a.get('send_as')}

Score each dimension 0-10 with clear reasoning. Be STRICT."""
        for attempt in range(3):
            try:
                text = await P.call(client, JUDGE_SYSTEM, prompt, 40, 900)
                d = parse_json(text)
                if d:
                    return d
            except Exception:  # noqa: BLE001
                await asyncio.sleep(12 * (attempt + 1))
        return None

    async def main():
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(2)

            async def guarded(c):
                async with sem:
                    return await one(client, c)
            return await asyncio.gather(*[guarded(c) for c in transcripts])
    res = asyncio.run(main())
    for conv, d in zip(transcripts, res):
        conv["judge"] = d
        out.append(d)
    return out


def report(R: Runner, transcripts, scores, warm_ok):
    dims = ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement_compulsion"]
    summ = {"actions": len(transcripts), "warmup_ok": warm_ok, "penalties": R.penalties,
            "latency_p50_p99": {k: (round(sorted(v)[len(v) // 2], 3), round(sorted(v)[int(len(v) * 0.99) - 1 if len(v) > 1 else 0], 3))
                                for k, v in R.latency.items() if v},
            "ungrounded_number_msgs": [(c["action"]["trigger_id"], c["ungrounded_numbers"]) for c in transcripts if c["ungrounded_numbers"]]}
    valid = [s for s in (scores or []) if s]
    if valid:
        summ["judge_avg"] = {d: round(sum(float(s.get(d, 0)) for s in valid) / len(valid), 2) for d in dims}
        summ["judge_total_avg"] = round(sum(summ["judge_avg"].values()), 2)
        worst = sorted(((sum(float(c["judge"].get(d, 0)) for d in dims), c["action"]["trigger_id"], c["judge"].get("hint"))
                        for c in transcripts if c.get("judge")))[:8]
        summ["worst"] = worst
    json.dump(summ, open(OUT / "summary.json", "w"), indent=2, ensure_ascii=False)
    with open(OUT / "transcript.jsonl", "w") as f:
        for c in transcripts:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with open(OUT / "review.md", "w") as f:
        for c in transcripts:
            a = c["action"]
            f.write(f"### tick {c['tick']} · {a['trigger_id']} · {a['send_as']} · persona={c.get('persona', 'customer')}\n\n")
            f.write(f"> {a['body']}\n\n_rationale_: {a['rationale']}\n\n")
            if c.get("judge"):
                j = c["judge"]
                f.write("judge: " + ", ".join(f"{d}={j.get(d)}" for d in dims) + f" — hint: {j.get('hint')}\n\n")
            for t in c["turns"]:
                o = t["out"]
                f.write(f"- **in:** {t['in']}\n  - **{o.get('action')}**: {o.get('body') or o.get('rationale')}\n")
            f.write("\n")
    print(json.dumps(summ, indent=2, ensure_ascii=False)[:4000])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--judge", choices=["gemini", "groq"], default=None)
    ap.add_argument("--ticks", type=int, default=12)
    a = ap.parse_args()
    proc = None
    url = a.url
    if not url:
        env = {**os.environ, "VERA_DB_PATH": "", "VERA_SESSION_IDLE_RESET": "0"}
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "vera.api:app", "--port", "8765", "--log-level", "warning"],
                                cwd=ROOT, env=env)
        url = "http://127.0.0.1:8765"
        for _ in range(50):
            try:
                if httpx.get(url + "/v1/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
    try:
        run(url, a.judge, a.ticks)
    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    main()
