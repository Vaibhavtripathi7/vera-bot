"""Reply engine: classify the merchant/customer turn, then act (send / wait / end).

Rules decide *what* to do (deterministic, replay-safe); artifacts are built from context so a
"yes" gets the actual deliverable, not a promise. Texts pass the same validator as proactive sends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from . import validator
from .compose import build_ctx
from .facts import resolve_digest
from .store import Conversation, Store
from .templates import _active_offers, _best_offer, _catalog, _first_sentence
from .util import humanize, stable_index

# ------------------------------------------------------------------ classifier

AUTO_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)", r"will (get back|respond|reply) (to you )?(shortly|soon|asap)",
    r"our team will", r"automated (assistant|response|reply|message)", r"i am an automated", r"i'm an automated",
    r"currently (unavailable|away|closed)", r"out of (office|station)", r"business hours", r"this is an auto",
    r"auto[- ]?reply", r"aapki (madad|jaankari) ke liye (bahut[- ]bahut )?shukriya", r"hum jald hi", r"team tak pahuncha",
    r"we have received your (message|query)", r"for (urgent|immediate) (queries|assistance)", r"visit us at",
]
OPT_OUT = [r"\bstop\b", r"unsubscribe", r"don'?t (message|text|contact|send)", r"do not (message|text|contact|send)",
           r"mat bhej", r"band karo", r"remove (me|my number)", r"no more messages", r"message mat", r"leave me alone"]
HOSTILE = [r"useless", r"\bspam", r"bother", r"fraud", r"scam", r"bakwas", r"pagal", r"shut up", r"idiot", r"stupid",
           r"nonsense", r"irritat", r"harass", r"waste of (my )?time", r"chup", r"bewakoof", r"\bf+u+c*k", r"\bdamn\b",
           r"annoying", r"get lost", r"bloody"]
DECLINE = [r"not interested", r"no thanks", r"no,? thank", r"nahi chahiye", r"zaroorat nahi", r"interest nahi",
           r"we'?re good", r"don'?t need", r"no need", r"\bnope\b", r"^\s*no\s*[.!]*\s*$", r"^\s*nahi\s*[.!]*\s*$",
           r"\bsaid no\b", r"\bi said\b.*\bno\b", r"mana kiya", r"bola na"]
LATER = [r"\blater\b", r"\bbusy\b", r"baad mein", r"baad me\b", r"abhi nahi", r"not now", r"call (me )?(later|tomorrow)",
         r"\btomorrow\b", r"\bkal\b", r"next week", r"in a (bit|while)", r"after (\d+|some)", r"give me (some )?time",
         r"thodi der", r"\bevening\b"]
COMMIT = [r"\byes\b", r"\byeah\b", r"\byep\b", r"\bhaan\b", r"\bhan ji\b", r"\bha ji\b", r"\bji haan\b", r"\bok(ay)?\b",
          r"\bsure\b", r"go ahead", r"let'?s do (it|this)", r"lets do it", r"\bdo it\b", r"kar do", r"kardo", r"\bkaro\b",
          r"send it", r"\bconfirm", r"\bchalo\b", r"theek hai", r"thik hai", r"\bproceed\b", r"please do", r"sounds good",
          r"\bdone\b", r"👍", r"\bagreed\b", r"\binterested\b", r"i want to join", r"judna hai", r"\bchalega\b",
          r"what'?s next", r"whats next", r"\bbhej do\b", r"\bpublish\b", r"\bbook it\b"]
OFF_TOPIC = [r"\bgst\b", r"income tax", r"\bitr\b", r"\btax\b", r"\bloan\b", r"insurance", r"electricity", r"\bbank\b",
             r"\bvisa\b", r"passport", r"aadhaar", r"\bpan card\b", r"cricket score", r"politic", r"stock market", r"crypto",
             r"file my", r"accountant", r"\bca\b", r"\blegal\b", r"lawyer"]
QUESTION_WORDS = [r"\?", r"\bkya\b", r"\bkitna\b", r"\bkitne\b", r"\bkaise\b", r"\bkab\b", r"\bkyun\b", r"\bhow\b", r"\bwhat\b",
                  r"\bwhen\b", r"\bwhy\b", r"\bwhich\b", r"\bcost\b", r"\bprice\b", r"\bcharge", r"\bfees?\b", r"\bdetails\b"]
HINGLISH_MARKERS = {"hai", "hain", "kya", "nahi", "haan", "karo", "kar", "do", "mujhe", "hum", "aap", "bhej", "chahiye",
                    "theek", "accha", "acha", "baad", "mein", "kal", "abhi", "ji", "bhai", "yaar", "kaise", "kitna", "wala"}


def _any(patterns, text):
    return any(re.search(p, text) for p in patterns)


def detect_lang(text: str) -> str | None:
    words = set(re.findall(r"[a-z]+", text.lower()))
    if len(words & HINGLISH_MARKERS) >= 2 or re.search(r"[ऀ-ॿ]", text):
        return "hinglish"
    if len(words) >= 3:
        return "en"
    return None


def classify(message: str, from_role: str, prior_texts: list[str]) -> str:
    t = (message or "").strip().lower()
    if not t:
        return "empty"
    for prev in prior_texts[-6:]:
        if prev and SequenceMatcher(None, prev.lower(), t).ratio() >= 0.9 and len(t) > 25:
            return "auto_reply"
    if _any(AUTO_PATTERNS, t):
        return "auto_reply"
    if _any(OPT_OUT, t):
        return "opt_out"
    if _any(HOSTILE, t):
        return "hostile"
    if _any(DECLINE, t):
        return "decline"
    if from_role == "customer" and re.match(r"^\s*(1|2|3|one|two|first|second|pehla|doosra)\b", t):
        return "slot_choice"
    if _any(OFF_TOPIC, t):
        return "off_topic"
    if _any(LATER, t) and not _any([r"\byes\b", r"\bhaan\b", r"go ahead", r"do it"], t):
        return "later"
    if _any(COMMIT, t):
        return "commit"
    if _any(QUESTION_WORDS, t):
        return "question"
    return "info"


# ------------------------------------------------------------------ reply result

@dataclass
class ReplyAction:
    action: str
    body: str = ""
    cta: str = "open_ended"
    wait_seconds: int = 0
    rationale: str = ""

    def to_json(self) -> dict:
        if self.action == "send":
            return {"action": "send", "body": self.body, "cta": self.cta, "rationale": self.rationale}
        if self.action == "wait":
            return {"action": "wait", "wait_seconds": self.wait_seconds, "rationale": self.rationale}
        return {"action": "end", "rationale": self.rationale}


# ------------------------------------------------------------------ engine

class ReplyEngine:
    def __init__(self, store: Store):
        self.store = store

    def _contexts(self, conv: Conversation):
        s = self.store
        merchant = s.get("merchant", conv.merchant_id) or {}
        category = s.get("category", merchant.get("category_slug")) or {}
        trigger = s.get("trigger", conv.trigger_id) or {"kind": conv.family or "generic", "scope": "customer" if conv.customer_id else "merchant"}
        customer = s.get("customer", conv.customer_id) if conv.customer_id else None
        return category, merchant, trigger, customer

    def handle(self, req: dict) -> tuple[ReplyAction, Conversation, str]:
        s = self.store
        cid = str(req.get("conversation_id") or "conv_unknown")
        conv = s.conv(cid)
        if conv is None:
            conv = Conversation(conversation_id=cid, merchant_id=req.get("merchant_id"), customer_id=req.get("customer_id"))
        role = str(req.get("from_role") or ("customer" if conv.customer_id else "merchant"))
        msg = str(req.get("message") or "")
        turn = int(req.get("turn_number") or (len(conv.turns) + 2))
        mst = s.mstate(conv.merchant_id)
        mst["unanswered"] = 0
        klass = classify(msg, role, mst.get("auto_texts", []) + [t["msg"] for t in conv.turns if t.get("from") != "bot"])
        conv.turns.append({"from": role, "msg": msg, "turn": turn, "class": klass})
        lang = detect_lang(msg)
        if lang:
            conv.language = lang
        act = self._decide(conv, klass, role, msg, turn, mst)
        if act.action == "send":
            conv.bodies.append(act.body)
            mst.setdefault("bodies", []).append(act.body)
            conv.turns.append({"from": "bot", "msg": act.body, "turn": turn})
        elif act.action == "end":
            conv.status = "ended"
        elif act.action == "wait":
            conv.status = "waiting"
        s.save_conv(conv)
        s.save_mstate(conv.merchant_id)
        return act, conv, klass

    # ---------------------------------------------------------- policy
    def _decide(self, conv: Conversation, klass: str, role: str, msg: str, turn: int, mst: dict) -> ReplyAction:
        if conv.status == "ended":
            if klass in ("commit", "question") and not (mst.get("opted_out") or mst.get("hostile_count", 0) >= 2):
                conv.status = "open"
            else:
                return ReplyAction("end", rationale="Conversation already closed; not re-engaging.")
        if klass == "empty":
            return ReplyAction("wait", wait_seconds=1800, rationale="Empty message; waiting for a real reply.")
        if klass == "auto_reply":
            conv.auto_replies += 1
            mst.setdefault("auto_texts", []).append(msg)
            mst["auto_count"] = mst.get("auto_count", 0) + 1
            n = max(conv.auto_replies, mst["auto_count"])
            if n == 1:
                return self._send(conv, self._auto_nudge(conv), "binary_yes_no",
                                  "Detected WhatsApp Business auto-reply; one short nudge so the owner can respond.")
            if n == 2:
                return ReplyAction("wait", wait_seconds=14400,
                                   rationale="Same canned auto-reply again -> owner not at phone; backing off 4h instead of burning turns.")
            return ReplyAction("end", rationale=f"Auto-reply {n}x with no human response; closing gracefully to avoid spamming.")
        if klass == "opt_out":
            if role == "customer" and conv.customer_id:           # a customer's STOP never silences the merchant
                cst = self.store.mstate(f"cust:{conv.customer_id}")
                cst["opted_out"] = True
                self.store.save_mstate(f"cust:{conv.customer_id}")
                return ReplyAction("end", rationale="Customer opted out; suppressing further messages to this customer only.")
            mst["opted_out"] = True
            return ReplyAction("end", rationale="Explicit opt-out/stop request; closing and suppressing further outreach to this merchant.")
        if klass == "hostile":
            mst["hostile_count"] = mst.get("hostile_count", 0) + 1
            if mst["hostile_count"] >= 2:
                mst["opted_out"] = True
                return ReplyAction("end", rationale="Repeated frustration; exiting and suppressing further messages.")
            body = self._t(conv, "Sorry about that — I won't push. If you'd rather not hear from me, just reply STOP and I'll stop right away.",
                           "Maaf kijiye — main pressure nahi daalungi. Agar aap messages nahi chahte toh bas STOP reply karein, main turant band kar doongi.")
            return self._send(conv, body, "none", "Merchant frustrated: one short apology + explicit opt-out path, no pitch.")
        if klass == "decline":
            mst.setdefault("declined_families", []).append(conv.family)
            if any(t.get("class") == "decline" for t in conv.turns[:-1]):
                return ReplyAction("end", rationale="Second decline; closing.")
            body = self._t(conv, "Understood — I won't follow up on this. If anything changes, just message 'Hi Vera' anytime.",
                           "Samajh gayi — is baare mein follow-up nahi karungi. Kabhi zaroorat ho toh bas 'Hi Vera' likh dijiye.")
            return self._send(conv, body, "none", "Merchant declined; graceful close without pitching, topic suppressed.")
        if any(t.get("class") == "decline" for t in conv.turns[:-1]) and klass != "commit":
            return ReplyAction("end", rationale="Merchant already declined; not pushing further.")
        if klass == "later":
            secs = 86400 if re.search(r"tomorrow|\bkal\b|next week", msg.lower()) else 14400
            return ReplyAction("wait", wait_seconds=secs, rationale=f"Merchant asked for time; backing off {secs // 3600}h.")
        if klass == "off_topic":
            conv.offtopic += 1
            redirect = self._redirect(conv)
            body = self._t(conv, f"That one's best handled by your CA — it's outside what I can do here. {redirect}",
                           f"Woh aapke CA behtar handle karenge — yeh mere scope se bahar hai. {redirect}")
            return self._send(conv, body, "open_ended", "Off-topic request politely declined; redirected to the open thread.")
        if role == "customer":
            return self._customer_reply(conv, klass, msg)
        if klass == "commit":
            conv.committed = True
            body = self._artifact(conv)
            return self._send(conv, body, "binary_confirm_cancel",
                              "Merchant committed: switched to action mode and delivered the artifact immediately (no qualifying).",
                              committed=True)
        if turn >= 6:
            body = self._t(conv, "Here's where we are: the draft is ready whenever you want it. Reply YES anytime and I'll publish it.",
                           "Summary: draft ready hai, jab chahein YES reply kar dijiye aur main publish kar doongi.")
            return self._send(conv, body, "binary_yes_no", "Long thread; wrapping up with a clear, low-effort next step.")
        if klass == "question" and conv.committed and re.search(r"what else|what next|whats next|what's next|aur kya|next kya|anything else|aage kya", msg.lower()):
            return self._send(conv, self._followon(conv), "binary_yes_no", "Merchant engaged after delivery; offering the next concrete step.")
        if klass == "question":
            return self._send(conv, self._answer(conv, msg), "open_ended", "Answered from context only; re-offered the next step.")
        # info / neutral engagement -> use it
        if conv.family == "curious" or "demand" in (conv.deliverable or "") or "price_reply" in (conv.deliverable or ""):
            body = self._curious_followup(conv, msg)
            return self._send(conv, body, "binary_confirm_cancel", "Merchant shared info; turned it into the promised deliverable.")
        body = self._t(conv, f"Got it, thanks. {self._next_step(conv)}", f"Samajh gayi, thanks. {self._next_step(conv)}")
        return self._send(conv, body, "binary_yes_no", "Engaged reply; advancing to one concrete next step.")

    # ---------------------------------------------------------- helpers
    def _t(self, conv: Conversation, en: str, hi: str) -> str:
        return hi if (conv.language or self._default_lang(conv)) in ("hinglish", "hindi") else en

    def _default_lang(self, conv: Conversation) -> str:
        category, merchant, trigger, customer = self._contexts(conv)
        if not merchant:
            return "en"
        from .util import customer_language, merchant_language
        if customer:
            return customer_language(customer, merchant)[0]
        return merchant_language(merchant)

    def _send(self, conv: Conversation, body: str, cta: str, rationale: str, committed: bool = False) -> ReplyAction:
        prior = conv.bodies
        if any(validator._jaccard(p, body) >= 0.8 for p in prior):
            body = body + self._t(conv, " (Just reply YES and I'll take it from here.)", " (Bas YES reply kar dijiye, baaki main dekh loongi.)")
        if committed:
            low = body.lower()
            for q in ("would you", "do you", "can you tell", "what if", "how about"):
                body = re.sub(re.escape(q), "", body, flags=re.I) if q in low else body
        return ReplyAction("send", body=re.sub(r"[ \t]+", " ", body).strip(), cta=cta, rationale=rationale)

    def _auto_nudge(self, conv: Conversation) -> str:
        what = self._offer_phrase(conv)
        return self._t(conv, f"Looks like an auto-reply 🙂 When the owner sees this, a quick YES is all I need to {what}.",
                       f"Lagta hai yeh auto-reply hai 🙂 Owner dekhein toh bas YES reply kar dein — main {what} kar doongi.")

    def _offer_phrase(self, conv: Conversation) -> str:
        d = conv.deliverable or ""
        if (conv.language or self._default_lang(conv)) in ("hinglish", "hindi"):
            return {
                "digest_summary+patient_whatsapp": "summary + patient WhatsApp draft bhej doon",
                "compliance_checklist": "compliance checklist bhej doon",
                "recall_customer_note": "customer note + pickup steps share kar doon",
                "review_request": "review request bhej doon",
                "review_replies": "review replies share kar doon",
                "registration_details": "registration details bhej doon",
                "renewal+refresh": "renewal process kar doon",
                "gbp_verification": "verification mein guide kar doon",
            }.get(d, "taiyaar draft share kar doon")
        return {
            "digest_summary+patient_whatsapp": "send the summary + patient WhatsApp draft",
            "compliance_checklist": "send the compliance checklist",
            "recall_customer_note": "share the customer note + pickup steps",
            "review_request": "send the review request",
            "review_replies": "share the drafted review replies",
            "registration_details": "send the registration details",
            "renewal+refresh": "process the renewal",
            "gbp_verification": "walk you through verification",
        }.get(d, "share the draft I've prepared")

    def _redirect(self, conv: Conversation) -> str:
        return self._t(conv, f"Meanwhile, shall I {self._offer_phrase(conv)}?", f"Tab tak, {self._offer_phrase(conv)}?")

    def _next_step(self, conv: Conversation) -> str:
        return self._t(conv, f"Next step: I'll {self._offer_phrase(conv)} — reply YES and it's done.",
                       f"Next step: main {self._offer_phrase(conv).replace(' doon', '').replace(' kar', '')} — YES reply karein aur ho jayega.")

    def _followon(self, conv: Conversation) -> str:
        category, merchant, trigger, customer = self._contexts(conv)
        return self._t(conv, "Next up: I'll turn the same draft into a WhatsApp status + a 2-line reply your staff can paste when customers ask. "
                             "Reply YES and I'll send both here.",
                       "Next: isi draft se ek WhatsApp status + customers ke sawaal ke liye 2-line ready reply bana doongi. YES reply karein, dono yahin bhej doongi.")

    def _answer(self, conv: Conversation, msg: str) -> str:
        category, merchant, trigger, customer = self._contexts(conv)
        low = msg.lower()
        if re.search(r"where.*(data|number|info)|source|kahan se|how do you know|data kaha", low):
            srcs = []
            if merchant.get("performance"):
                srcs.append(self._t(conv, "your Google Business Profile insights for the last 30 days", "aapke Google Business Profile ke last 30 din ke insights"))
            peer = (category.get("peer_stats") or {}).get("scope")
            if peer:
                srcs.append(self._t(conv, f"magicpin's benchmark for {humanize(re.sub(r'_?20\d\d$', '', peer))}",
                                    f"magicpin ka {humanize(re.sub(r'_?20\d\d$', '', peer))} benchmark"))
            d = resolve_digest(category, trigger) if category else None
            if d and d.get("source"):
                srcs.append(d["source"])
            if (trigger.get("payload") or {}) and not (trigger.get("payload") or {}).get("placeholder"):
                srcs.append(self._t(conv, "the alert on your account this week", "is hafte aapke account pe aaya alert"))
            listing = "; ".join(srcs) or self._t(conv, "your magicpin account data", "aapka magicpin account data")
            return self._t(conv, f"From {listing}. Nothing is estimated. Shall I {self._offer_phrase(conv)}?",
                           f"Yeh {listing} se hai — kuch bhi andaaza nahi. {self._offer_phrase(conv).capitalize()}?")
        if re.search(r"price|cost|kitna|charge|fee|₹|rupee|paisa|paise", low):
            offers = _active_offers(merchant) or _catalog(category)[:2]
            amt = (trigger.get("payload") or {}).get("renewal_amount")
            if amt:
                return self._t(conv, f"The renewal is ₹{amt:,} for the plan. Shall I process it?",
                               f"Renewal ₹{amt:,} ka hai. Process kar doon?")
            if re.search(r"(cost|charge|pay).*(me|us|this)|mujhe|hume|kitna lagega|kitne ka", low):
                return self._t(conv, f"No ad spend is needed for this — it's a post/update I draft for you to approve. Shall I {self._offer_phrase(conv)}?",
                               f"Isme koi ad spend nahi lagta — yeh post/update main draft karti hoon, aap bas approve karein. {self._offer_phrase(conv).capitalize()}?")
            if offers:
                listing = ", ".join(f"'{o}'" for o in offers[:2])
                return self._t(conv, f"Current pricing on your profile: {listing}. Shall I use these in the draft?",
                               f"Profile pe abhi yeh pricing hai: {listing}. Draft mein yahi use karoon?")
        d = resolve_digest(category, trigger) if category else None
        if d and re.search(r"source|study|research|trial|circular|kya hai|what is|details", low):
            return self._t(conv, f"It's from {d.get('source')}: {_first_sentence(d.get('summary'))} Shall I send the full summary?",
                           f"Yeh {d.get('source')} se hai: {_first_sentence(d.get('summary'))} Poora summary bhej doon?")
        return self._t(conv, f"Good question — I don't want to guess, so I'll confirm and get back to you here. Meanwhile, shall I {self._offer_phrase(conv)}?",
                       f"Achha sawaal — main guess nahi karungi, confirm karke yahin bataungi. Tab tak {self._offer_phrase(conv)} kar doon?")

    def _curious_followup(self, conv: Conversation, msg: str) -> str:
        category, merchant, trigger, customer = self._contexts(conv)
        name = (merchant.get("identity") or {}).get("name", "your business")
        locality = (merchant.get("identity") or {}).get("locality", "")
        svc = re.sub(r"[^\w\s&+₹@-]", "", msg).strip()[:60] or "your top service"
        return self._t(conv,
                       f"Done — here's the Google post draft: \"Most-booked at {name}{', ' + locality if locality else ''} this week: {svc}. Walk in or message us to book.\" "
                       "Reply CONFIRM and I'll publish it, then the price-reply snippet comes next.",
                       f"Ho gaya — Google post draft yeh raha: \"Most-booked at {name}{', ' + locality if locality else ''} this week: {svc}. Walk in karein ya message karke book karein.\" "
                       "CONFIRM reply karein toh publish kar doongi, next price-reply snippet bhejungi.")

    def _customer_reply(self, conv: Conversation, klass: str, msg: str) -> ReplyAction:
        category, merchant, trigger, customer = self._contexts(conv)
        slots = [s.get("label") for s in ((trigger.get("payload") or {}).get("available_slots") or []) if s.get("label")]
        if klass == "slot_choice" and slots:
            idx = 1 if re.match(r"^\s*(2|two|second|doosra)", msg.lower()) else 0
            slot = slots[min(idx, len(slots) - 1)]
            body = self._t(conv, f"Done — you're booked for {slot}. We'll send a reminder the day before. See you then!",
                           f"Ho gaya — aapka slot {slot} book hai. Ek din pehle reminder bhej denge. Milte hain!")
            return self._send(conv, body, "none", f"Customer picked slot {slot}; confirmed booking from offered slots.")
        if klass in ("commit", "slot_choice"):
            body = self._t(conv, "Done — we've noted it and will confirm the exact time shortly. Anything specific we should keep in mind?",
                           "Ho gaya — note kar liya hai, exact time jaldi confirm karenge. Kuch khaas dhyan rakhna ho toh batayein.")
            return self._send(conv, body, "open_ended", "Customer confirmed; acknowledged and moved to scheduling.")
        if klass == "question" and re.search(r"instead|another|different|other (day|time)|reschedule|monday|tuesday|wednesday|thursday|friday|saturday|sunday|kal|parso", msg.lower()):
            body = self._t(conv, "Sure — we'll check that day and confirm the closest available time here shortly.",
                           "Bilkul — us din ka slot check karke jaldi yahin confirm karte hain.")
            return self._send(conv, body, "none", "Customer asked for a different day; acknowledged without inventing availability.")
        if klass == "question":
            offer = _best_offer(build_ctx(category, merchant, trigger, customer, None)) if merchant else ""
            body = self._t(conv, "Happy to help — " + (f"'{offer}' is available right now. " if offer else "") + "Reply with a day that suits you and we'll book it.",
                           "Zaroor — " + (f"'{offer}' abhi available hai. " if offer else "") + "Jo din suit kare woh batayein, hum book kar denge.")
            return self._send(conv, body, "open_ended", "Customer question answered from merchant offers only.")
        body = self._t(conv, "Thanks! Reply with a day or time that suits you and we'll take care of the rest.",
                       "Shukriya! Jo din ya time suit kare batayein, baaki hum sambhal lenge.")
        return self._send(conv, body, "open_ended", "Customer engaged; low-friction next step.")

    # ---------------------------------------------------------- artifacts (the deliverable on "yes")
    def _artifact(self, conv: Conversation) -> str:
        category, merchant, trigger, customer = self._contexts(conv)
        if not merchant:
            return self._t(conv, "Done — drafting it now. Here's the next step: I'll share the draft right here and you reply CONFIRM to publish.",
                           "Done — abhi draft kar rahi hoon. Next step: draft yahin bhejungi, aap CONFIRM reply karke publish karwa dijiye.")
        ident = merchant.get("identity") or {}
        name, loc = ident.get("name", "your business"), ident.get("locality", "")
        offer = _best_offer(build_ctx(category, merchant, trigger, customer, None))
        d = conv.deliverable or ""
        dig = resolve_digest(category, trigger) if category else None
        if d.startswith("digest_summary") and dig:
            pc = next(iter(category.get("patient_content_library") or []), None)
            patient = _first_sentence(pc.get("body")) if pc else _first_sentence(dig.get("summary"))
            return self._t(conv,
                           f"Here's the summary: {_first_sentence(dig.get('summary'))} ({dig.get('source')}). "
                           f"Patient WhatsApp draft: \"{patient} Reply to book a check-up at {name}.\" Reply CONFIRM and I'll schedule it for your patient list.",
                           f"Summary yeh raha: {_first_sentence(dig.get('summary'))} ({dig.get('source')}). "
                           f"Patient WhatsApp draft: \"{patient} Check-up ke liye {name} ko reply karein.\" CONFIRM reply karein, main patient list ko schedule kar doongi.")
        if d == "compliance_checklist" and dig:
            steps = [s.strip() for s in re.split(r";|\.\s", dig.get("actionable", "")) if s.strip()][:2]
            lines = "\n".join(f"{i + 1}. {st.rstrip('.')}" for i, st in enumerate(steps + ["Note the audit date and owner in your SOP file"]))
            return self._t(conv, f"Here's the checklist ({dig.get('source')}):\n{lines}\nNext: reply CONFIRM and I'll set a reminder a week before the deadline.",
                           f"Checklist yeh rahi ({dig.get('source')}):\n{lines}\nNext: CONFIRM reply karein, deadline se ek hafte pehle reminder set kar doongi.")
        if d == "recall_customer_note":
            p = trigger.get("payload") or {}
            b = ", ".join(p.get("affected_batches") or [])
            return self._t(conv, f"Here's the customer note: \"Namaste from {name}. A batch of {p.get('molecule', 'your medicine')} ({b}) is being replaced as a precaution — please bring your strip in or reply and we'll deliver a replacement.\" "
                           "Next: I'll filter your repeat-Rx list for these batches. Reply CONFIRM to send.",
                           f"Customer note draft: \"{name} se namaste. {p.get('molecule', 'aapki dawai')} ka batch ({b}) precaution ke liye replace ho raha hai — strip le aayein ya reply karein, hum replacement deliver kar denge.\" "
                           "Next: repeat-Rx list mein in batches wale customers filter kar doongi. Bhejne ke liye CONFIRM reply karein.")
        if d == "customer_reminder":
            p = trigger.get("payload") or {}
            m = re.match(r"c_\d+_([a-z]+)", str(trigger.get("customer_id") or ""))
            who = m.group(1).capitalize() if m else ""
            what = humanize(p.get("service_due") or str(trigger.get("kind", "")).replace("_due", "")).replace("6 month", "6-month")
            slots = " or ".join(x.get("label") for x in (p.get("available_slots") or [])[:2] if x.get("label"))
            note = f"Hi {who}, {name} here. Your {what} is due" + (f" — we've kept {slots} for you" if slots else "") + ". Reply to book."
            return self._t(conv, f"Here's the reminder going out from your number: \"{note}\" Reply CONFIRM and I'll send it now.",
                           f"Yeh reminder aapke number se jayega: \"{note}\" CONFIRM reply karein, abhi bhej doongi.")
        if d == "review_request":
            return self._t(conv, f"Here's the review request: \"Thanks for choosing {name}! If we made your day, a quick Google review helps neighbours find us 🙏\" Reply CONFIRM and I'll send it to your recent regulars.",
                           f"Review request draft: \"{name} choose karne ke liye shukriya! Achha laga ho toh ek Google review se aas-paas ke logon ko madad milegi 🙏\" CONFIRM reply karein, recent regulars ko bhej doongi.")
        if d == "review_replies":
            return self._t(conv, "Here's the reply I've drafted: \"Thank you for the honest feedback — we've fixed this with the team and would love to welcome you back.\" Reply CONFIRM and I'll post it on each of those reviews.",
                           "Reply draft yeh raha: \"Honest feedback ke liye shukriya — team ke saath isko fix kar diya hai, aapka phir se swagat hai.\" CONFIRM reply karein, sab reviews pe post kar doongi.")
        if d == "registration_details" and dig:
            return self._t(conv, f"Here are the details: {dig.get('title')} — {_first_sentence(dig.get('summary'))} {dig.get('actionable', '')} Reply CONFIRM and I'll add it to your calendar.",
                           f"Details yeh rahe: {dig.get('title')} — {_first_sentence(dig.get('summary'))} {dig.get('actionable', '')} CONFIRM reply karein, calendar mein add kar doongi.")
        if d in ("renewal+refresh", "reactivation+winback"):
            return self._t(conv, "Done — I've started it. Next: you'll get the payment confirmation here, and I'll refresh your photos and post an offer the same day. Reply CONFIRM to proceed.",
                           "Done — process shuru kar diya hai. Next: payment confirmation yahin aayega, aur usi din photos refresh + offer post kar doongi. Aage badhne ke liye CONFIRM reply karein.")
        if d == "gbp_verification":
            return self._t(conv, "Here's the plan: 1) I'll request the verification code, 2) you share it here when it arrives, 3) I'll finish the rest. Reply CONFIRM to start.",
                           "Plan yeh hai: 1) main verification code request karti hoon, 2) code aate hi yahin share kar dijiye, 3) baaki main kar doongi. Shuru karne ke liye CONFIRM reply karein.")
        if d == "attendance_challenge":
            return self._t(conv, f"Here's the draft: \"{name} 4-Week Consistency Challenge — 12 sessions in 4 weeks, members who finish get a shout-out on our wall.\" Reply CONFIRM and I'll send it to your members.",
                           f"Draft yeh raha: \"{name} 4-Week Consistency Challenge — 4 hafte mein 12 sessions, complete karne walon ka wall pe shout-out.\" CONFIRM reply karein, members ko bhej doongi.")
        post_offer = f" {offer}." if offer else ""
        return self._t(conv, f"Here's the Google post draft: \"{name}{', ' + loc if loc else ''} —{post_offer} Message us on WhatsApp to book.\" Reply CONFIRM and it goes live today; next I'll prep the WhatsApp status version.",
                       f"Google post draft yeh raha: \"{name}{', ' + loc if loc else ''} —{post_offer} Book karne ke liye WhatsApp karein.\" CONFIRM reply karein, aaj hi live kar doongi; next WhatsApp status version bhi ready karungi.")
