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
HOSTILE = [r"useless", r"\bspam", r"bother", r"fraud", r"scam", r"bakwa+s", r"pagal", r"shut up", r"idiot", r"stupid",
           r"faltu", r"dimaag mat", r"dimag mat", r"band kar\b", r"bekaar", r"bakwaas", r"tang mat", r"pareshan mat",
           r"nonsense", r"irritat", r"harass", r"waste of (my )?time", r"\bchup (kar|ho|raho)\b", r"bewakoof", r"\bf+u+c*k", r"\bdamn\b",
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
IDENTITY = [r"kaun ho", r"kon ho", r"who (are|is) (you|this)", r"kiska message", r"kis ka message", r"ye kya hai", r"yeh kya hai",
            r"what is this", r"aap kaun", r"who sent", r"kahan se (message|msg)"]
HUMAN = [r"call pe", r"phone pe", r"phone par", r"call (kar|kr) (sakte|sakti|sakoge|lo)", r"can you call", r"talk to (a )?(human|person|someone)",
         r"\bhuman\b", r"\bagent\b", r"real person", r"insaan se", r"kisi se baat", r"baat karni hai", r"number (do|dijiye|share)"]
EDIT_HINT = re.compile(r"₹\s?\d+|\bsirf\b|\bonly\b|weekdays?|weekend|rakhna|rakh do|rakho|karo na|kar do na|instead|badal|change (it|the)|"
                       r"\bcatchy\b|chhota|shorter|longer", re.I)
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
    engaged = _any(COMMIT, t) or "?" in t
    repeats = sum(1 for prev in prior_texts[-6:] if prev and SequenceMatcher(None, prev.lower(), t).ratio() >= 0.9)
    # verbatim repeats are auto-replies only if they don't read like a real (engaged) human reply, or keep coming
    if len(t) > 25 and repeats and (not engaged or repeats >= 2):
        return "auto_reply"
    if _any(AUTO_PATTERNS, t) and not (_any(COMMIT, t) and not re.search(r"thank(s| you) for contacting", t)):
        return "auto_reply"
    if _any(OPT_OUT, t):
        return "opt_out"
    if _any(HOSTILE, t):
        return "hostile"
    if _any(DECLINE, t):
        return "decline"
    if from_role == "customer" and re.match(r"^\s*(1|2|3|one|two|first|second|pehla|doosra)\b", t):
        return "slot_choice"
    if _any(HUMAN, t) and not _any(COMMIT, t):
        return "human"
    if _any(IDENTITY, t) and not _any(COMMIT, t):
        return "identity"
    if _any(LATER, t) and not _any([r"\byes\b", r"\bhaan\b", r"go ahead", r"do it"], t):
        return "later"
    if _any(COMMIT, t):          # a "yes" wins over an off-topic aside in the same message (handled inline)
        return "commit"
    if _any(OFF_TOPIC, t):
        return "off_topic"
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
        try:
            turn = int(req.get("turn_number") or (len(conv.turns) + 2))
        except (TypeError, ValueError):
            turn = len(conv.turns) + 2
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
            body = self._t(conv, "Sorry about that — I won't push.", "Maaf kijiye — main pressure nahi daalungi.")
            if _any(OFF_TOPIC, msg.lower()):
                body += " " + self._offtopic_note(conv, msg)
                conv.meta["ca_noted"] = True
            body += " " + self._t(conv, "If you'd rather not hear from me, just reply STOP and I'll stop right away.",
                                  "Agar aap messages nahi chahte toh bas STOP reply karein, main turant band kar doongi.")
            return self._send(conv, body, "none", "Merchant frustrated: short apology, off-topic ask answered honestly, explicit opt-out path, no pitch.")
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
            body = f"{self._offtopic_note(conv, msg)} {self._redirect(conv)}"
            return self._send(conv, body, "open_ended", "Off-topic request politely declined (topic-aware); redirected to the open thread.")
        if klass == "identity":
            body = f"{self._identity(conv)} {self._redirect(conv)}"
            return self._send(conv, body, "binary_yes_no", "Merchant asked who is messaging; introduced Vera plainly, then one clear next step.")
        if klass == "human":
            body = self._t(conv, "Sure — I'll ask the magicpin team to call you. What time works best? Meanwhile the draft is ready whenever you want it.",
                           "Bilkul — main magicpin team se aapko call karwa deti hoon. Kaunsa time theek rahega? Tab tak draft ready hai, jab chahein dekh lijiye.")
            return self._send(conv, body, "open_ended", "Merchant wants a human/call: acknowledged, asked for a time, kept the thread open.")
        if role == "customer":
            return self._customer_reply(conv, klass, msg)
        if klass in ("commit", "question", "info") and conv.meta.get("stage", 0) >= 1 and \
                re.search(r"mistake|wrong|galat|incorrect|should be|isn'?t (it|the)|not correct|by mistake|fix (it|this|that)|typo", msg.lower()):
            conv.meta["stage"] = min(conv.meta.get("stage", 1), 2)
            return self._send(conv, self._t(conv,
                "Good catch — thanks. I'll correct that before anything goes out and resend the fixed version here for a final OK.",
                "Sahi pakda — shukriya. Bhejne se pehle isko theek karke corrected version yahin final OK ke liye bhejti hoon."),
                "none", "Merchant flagged an error; acknowledged and holding the send until corrected.")
        if klass == "commit":
            conv.committed = True
            stage = conv.meta.get("stage", 0)
            aside = self._aside(conv, msg)
            if stage == 0:
                conv.meta["stage"] = 1
                art = self._curious_followup(conv, msg) if conv.family == "curious" and len(msg) > 12 else self._artifact(conv)
                edited, art2 = self._apply_price_edit(msg, art)
                if edited:
                    art = art2
                    aside = re.sub(r"Noted — \"[^\"]*\": [^.]*\.\s*", "", aside)
                    aside += self._t(conv, "Done — updated with your price. ", "Ho gaya — aapka price daal diya. ")
                elif EDIT_HINT.search(msg) and re.search(r"₹\s?\d", msg):
                    conv.meta["stage"] = 0      # can't apply safely: acknowledge, don't show a stale draft
                    return self._send(conv, aside + self._t(conv, "I'll send the updated draft with that change for your final OK.",
                                                            "Yeh change karke updated draft final OK ke liye bhejti hoon."),
                                      "open_ended", "Merchant changed the terms; acknowledged instead of re-sending an outdated draft.")
                if aside and any(core and core in art for core in
                                 (re.sub(r"^(On cost|Cost):\s*", "", part).strip(" .") for part in re.split(r"(?<=\.)\s", aside))):
                    aside = ""                   # the artifact already states it (e.g. the fee) - don't repeat
                m = re.search(r'"([^"]{20,})"', art)
                if m:
                    conv.meta["artifact_note"] = m.group(1)
                return self._send(conv, aside + art, "binary_confirm_cancel",
                                  "Merchant committed: switched to action mode and delivered the artifact immediately (no qualifying).",
                                  committed=True)
            if stage == 1:
                conv.meta["stage"] = 2
                return self._send(conv, aside + self._done(conv), "binary_yes_no",
                                  "Merchant approved the draft: executed it and offered one concrete follow-on.", committed=True)
            if stage == 2:
                conv.meta["stage"] = 3
                return self._send(conv, aside + self._second_artifact(conv), "binary_confirm_cancel",
                                  "Merchant accepted the follow-on: delivered it immediately.", committed=True)
            if stage == 3:
                conv.meta["stage"] = 4
                return self._send(conv, aside + self._wrap(conv), "none", "Both deliverables done; closing the loop with a clear summary.",
                                  committed=True)
            return ReplyAction("end", rationale="Work delivered and acknowledged; ending without adding noise.")
        if turn >= 6:
            body = self._t(conv, "Here's where we are: the draft is ready whenever you want it. Reply YES anytime and I'll publish it.",
                           "Summary: draft ready hai, jab chahein YES reply kar dijiye aur main publish kar doongi.")
            return self._send(conv, body, "binary_yes_no", "Long thread; wrapping up with a clear, low-effort next step.")
        if klass == "question" and conv.committed and re.search(r"what else|what next|whats next|what's next|aur kya|next kya|anything else|aage kya", msg.lower()):
            conv.meta["stage"] = max(conv.meta.get("stage", 0), 2)
            return self._send(conv, self._followon(conv), "binary_yes_no", "Merchant engaged after delivery; offering the next concrete step.")
        if conv.meta.get("stage", 0) >= 4:
            return ReplyAction("end", rationale="Thread complete; acknowledging silently rather than adding noise.")
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
            alt = self._wrap(conv)
            if any(validator._jaccard(p, alt) >= 0.8 for p in prior):
                return ReplyAction("wait", wait_seconds=3600, rationale="Nothing new to add without repeating myself; waiting.")
            body, cta = alt, "none"
        if committed:
            low = body.lower()
            for q in ("would you", "do you", "can you tell", "what if", "how about"):
                body = re.sub(re.escape(q), "", body, flags=re.I) if q in low else body
        return ReplyAction("send", body=re.sub(r"[ \t]+", " ", body).strip(), cta=cta, rationale=rationale)

    def _auto_nudge(self, conv: Conversation) -> str:
        en, hi = self._offer_noun(conv)
        return self._t(conv, f"Looks like an auto-reply 🙂 When the owner sees this, a quick YES is all I need to send {en}.",
                       f"Lagta hai yeh auto-reply hai 🙂 Owner dekhein toh bas YES reply kar dein — main {hi} bhej doongi.")

    NOUNS = {
        "digest_summary+patient_whatsapp": ("the summary + patient WhatsApp draft", "summary + patient WhatsApp draft"),
        "compliance_checklist": ("the compliance checklist", "compliance checklist"),
        "recall_customer_note": ("the customer note + pickup steps", "customer note + pickup steps"),
        "review_request": ("the review request", "review request"),
        "review_replies": ("the drafted review replies", "review replies ka draft"),
        "registration_details": ("the registration details", "registration details"),
        "renewal+refresh": ("the renewal details", "renewal details"),
        "gbp_verification": ("the verification steps", "verification steps"),
        "seasonal_whatsapp": ("the customer WhatsApp draft", "customer WhatsApp draft"),
        "plan_announcement": ("the announcement post", "announcement post"),
        "match_day_creatives": ("the delivery banner + story copy", "delivery banner + story copy"),
        "customer_reminder": ("the customer reminder", "customer reminder"),
        "attendance_challenge": ("the challenge draft", "challenge draft"),
    }

    def _offer_noun(self, conv: Conversation) -> tuple[str, str]:
        return self.NOUNS.get(conv.deliverable or "", ("the draft I've prepared", "taiyaar draft"))

    def _redirect(self, conv: Conversation) -> str:
        en, hi = self._offer_noun(conv)
        return self._t(conv, f"Meanwhile, shall I send {en}?", f"Tab tak {hi} bhej doon?")

    def _next_step(self, conv: Conversation) -> str:
        en, hi = self._offer_noun(conv)
        return self._t(conv, f"Next step: I'll send {en} — reply YES and it's done.", f"Next step: main {hi} bhej doongi — YES reply karein aur ho jayega.")

    @staticmethod
    def _apply_price_edit(msg: str, art: str) -> tuple[bool, str]:
        """'₹120 karo na 25+ ke liye' -> replace the price inside the draft's '25+' tier (only when unambiguous)."""
        prices = re.findall(r"₹\s?(\d[\d,]*)", msg)
        tiers = re.findall(r"(\d+)\s*\+", msg)
        if len(prices) != 1 or len(tiers) != 1:
            return False, art
        seg = re.search(rf"({re.escape(tiers[0])}\+[^;\n•]*?₹)(\d[\d,]*)", art)
        if not seg:
            return False, art
        return True, art[:seg.start(2)] + prices[0] + art[seg.end(2):]

    def _identity(self, conv: Conversation) -> str:
        category, merchant, trigger, customer = self._contexts(conv)
        biz = ((merchant or {}).get("identity") or {}).get("name") or "your business"
        return self._t(conv, f"I'm Vera, magicpin's assistant for {biz} — I help with your Google profile, offers and customer messages.",
                       f"Main Vera hoon, magicpin ki assistant — {biz} ke Google profile, offers aur customer messages mein madad karti hoon.")

    def _offtopic_note(self, conv: Conversation, msg: str) -> str:
        low = msg.lower()
        if re.search(r"loan|bank|interest rate|emi", low):
            return self._t(conv, "Loan rates are best compared with your bank or CA — that's outside what I handle.",
                           "Loan rates ke liye aapka bank ya CA sahi bata payenge — yeh mere scope se bahar hai.")
        if re.search(r"insurance", low):
            return self._t(conv, "An insurance advisor is the right person for that — it's outside what I handle.",
                           "Insurance ke liye advisor sahi rahenge — yeh mere scope se bahar hai.")
        if re.search(r"gst|tax|itr|return", low):
            return self._t(conv, "GST/tax filing is best done by your CA — it's outside what I handle.",
                           "GST/tax filing ke liye aapke CA sahi rahenge — yeh mere scope se bahar hai.")
        return self._t(conv, "That's outside what I can help with here.", "Yeh mere scope se bahar hai.")

    def _aside(self, conv: Conversation, msg: str) -> str:
        """One-line handling of an off-topic ask or a data-source question inside a 'yes' message."""
        low = msg.lower()
        out = []
        if _any(IDENTITY, low):
            out.append(self._identity(conv))
        if _any(OFF_TOPIC, low):
            if not conv.meta.get("ca_noted"):
                conv.meta["ca_noted"] = True
                out.append(self._offtopic_note(conv, msg))
            return " ".join(out) + (" " if out else "")
        m = EDIT_HINT.search(msg)
        if m:
            clause = next((c.strip() for c in re.split(r"[.!?,;]\s*", msg) if EDIT_HINT.search(c)), "")[:80]
            if clause:
                out.append(self._t(conv, f"Noted — \"{clause}\": I'll keep that in the final version.",
                                   f"Noted — \"{clause}\": final version mein yahi rakhungi."))
        if re.search(r"where.*(data|number)|source|kahan se|kaha se|kidhar se|how do you know", low):
            out.append(self._t(conv, "The numbers come from your Google profile insights and magicpin's category benchmark.",
                               "Yeh numbers aapke Google profile insights aur magicpin ke category benchmark se hain."))
        elif re.search(r"cost|price|kitna|charge|fee|paisa|kitne ka", low):
            ans = self._fact_answer(conv)
            if ans:
                out.append(ans)
        elif "?" in msg and not out and not conv.meta.get("q_noted"):
            conv.meta["q_noted"] = True
            out.append(self._t(conv, "On your question — I'll confirm that detail before anything goes live.",
                               "Aapke sawaal pe — woh detail live hone se pehle confirm kar doongi."))
        return " ".join(out) + (" " if out else "")

    def _fact_answer(self, conv: Conversation) -> str:
        """Cost questions answered only from context (digest fee lines, renewal amount); else nothing."""
        category, merchant, trigger, customer = self._contexts(conv)
        amt = (trigger.get("payload") or {}).get("renewal_amount")
        if amt:
            return self._t(conv, f"The renewal is ₹{amt:,}.", f"Renewal ₹{amt:,} ka hai.")
        d = resolve_digest(category, trigger) if category else None
        if d and re.search(r"₹|free", str(d.get("actionable", "")), re.I):
            return self._t(conv, f"On cost: {d['actionable'].rstrip('.')}.", f"Cost: {d['actionable'].rstrip('.')}.")
        return self._t(conv, "No ad spend is needed for this — it's something I draft for you to approve.",
                       "Isme koi ad spend nahi lagta — yeh main draft karti hoon, aap bas approve karein.")

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
            en, hi = self._offer_noun(conv)
            return self._t(conv, f"From {listing}. Nothing is estimated. Shall I send {en}?",
                           f"Yeh {listing} se hai — kuch bhi andaaza nahi. {hi.capitalize()} bhej doon?")
        if re.search(r"price|cost|kitna|charge|fee|₹|rupee|paisa|paise", low):
            offers = _active_offers(merchant) or _catalog(category)[:2]
            amt = (trigger.get("payload") or {}).get("renewal_amount")
            if amt:
                return self._t(conv, f"The renewal is ₹{amt:,} for the plan. Shall I process it?",
                               f"Renewal ₹{amt:,} ka hai. Process kar doon?")
            if re.search(r"(cost|charge|pay).*(me|us|this)|mujhe|hume|kitna lagega|kitne ka", low):
                en, hi = self._offer_noun(conv)
                return self._t(conv, f"No ad spend is needed for this — it's a post/update I draft for you to approve. Shall I send {en}?",
                               f"Isme koi ad spend nahi lagta — yeh post/update main draft karti hoon, aap bas approve karein. {hi.capitalize()} bhej doon?")
            if offers:
                listing = ", ".join(f"'{o}'" for o in offers[:2])
                return self._t(conv, f"Current pricing on your profile: {listing}. Shall I use these in the draft?",
                               f"Profile pe abhi yeh pricing hai: {listing}. Draft mein yahi use karoon?")
        d = resolve_digest(category, trigger) if category else None
        if d and re.search(r"source|study|research|trial|circular|kya hai|what is|details", low):
            return self._t(conv, f"It's from {d.get('source')}: {_first_sentence(d.get('summary'))} Shall I send the full summary?",
                           f"Yeh {d.get('source')} se hai: {_first_sentence(d.get('summary'))} Poora summary bhej doon?")
        en, hi = self._offer_noun(conv)
        return self._t(conv, f"Good question — I don't want to guess, so I'll confirm and get back to you here. Meanwhile, shall I send {en}?",
                       f"Achha sawaal — main guess nahi karungi, confirm karke yahin bataungi. Tab tak {hi} bhej doon?")

    def _curious_followup(self, conv: Conversation, msg: str) -> str:
        category, merchant, trigger, customer = self._contexts(conv)
        name = (merchant.get("identity") or {}).get("name", "your business")
        locality = (merchant.get("identity") or {}).get("locality", "")
        offers = _active_offers(merchant) + _catalog(category)
        words = [w for w in re.findall(r"[a-zA-Z]{4,}", msg.lower()) if w not in {"haan", "theek", "chal", "raha", "hai", "yeah", "this", "week",
                                                                              "most", "sabse", "zyada", "please", "post", "banao", "karo"}]
        svc = next((o for o in offers if any(w in o.lower() for w in words)), "") or re.sub(r"[^\w\s&+₹@-]", "", msg).strip()[:60] or "your top service"
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
            return self._t(conv, f"Here are the details: {dig.get('title')} — {_first_sentence(dig.get('summary'))} {dig.get('actionable', '').rstrip('.')}. Reply CONFIRM and I'll add it to your calendar.",
                           f"Details yeh rahe: {dig.get('title')} — {_first_sentence(dig.get('summary'))} {dig.get('actionable', '').rstrip('.')}. CONFIRM reply karein, calendar mein add kar doongi.")
        if d in ("renewal+refresh", "reactivation+winback"):
            return self._t(conv, "Done — I've started it. Next: you'll get the payment confirmation here, and I'll refresh your photos and post an offer the same day. Reply CONFIRM to proceed.",
                           "Done — process shuru kar diya hai. Next: payment confirmation yahin aayega, aur usi din photos refresh + offer post kar doongi. Aage badhne ke liye CONFIRM reply karein.")
        if d == "gbp_verification":
            return self._t(conv, "Here's the plan: 1) I'll request the verification code, 2) you share it here when it arrives, 3) I'll finish the rest. Reply CONFIRM to start.",
                           "Plan yeh hai: 1) main verification code request karti hoon, 2) code aate hi yahin share kar dijiye, 3) baaki main kar doongi. Shuru karne ke liye CONFIRM reply karein.")
        if d == "attendance_challenge":
            return self._t(conv, f"Here's the draft: \"{name} 4-Week Consistency Challenge — 12 sessions in 4 weeks, members who finish get a shout-out on our wall.\" Reply CONFIRM and I'll send it to your members.",
                           f"Draft yeh raha: \"{name} 4-Week Consistency Challenge — 4 hafte mein 12 sessions, complete karne walon ka wall pe shout-out.\" CONFIRM reply karein, members ko bhej doongi.")
        p = trigger.get("payload") or {}
        where = f"{name}{', ' + loc if loc else ''}"
        if d == "seasonal_whatsapp":
            items = [re.sub(r"_demand_[+-]\d+", "", str(x)).replace("_", " ") for x in (p.get("trends") or []) if "+" in str(x)]
            items_s = ", ".join(items[:3]) or "seasonal essentials"
            note = f"Summer essentials in stock at {where}: {items_s}." + (f" {offer}." if offer else "") + " Reply to order."
            return self._t(conv, f"Here's the customer WhatsApp: \"{note}\" Reply CONFIRM and I'll send it to your regulars.",
                           f"Customer WhatsApp yeh raha: \"{note}\" CONFIRM reply karein, regulars ko bhej doongi.")
        if d == "plan_announcement":
            lines = [l.lstrip("• ").strip() for l in (conv.bodies[0] if conv.bodies else "").splitlines() if l.strip().startswith("•")]
            topic = humanize((p.get("intent_topic") or "new program"))
            note = f"New at {where}: {topic} — " + "; ".join(lines[:3]) + ". Reply to reserve a spot."
            return self._t(conv, f"Here's the announcement: \"{note}\" Reply CONFIRM and it goes out as a Google post + WhatsApp broadcast.",
                           f"Announcement yeh raha: \"{note}\" CONFIRM reply karein, Google post + WhatsApp broadcast dono chale jayenge.")
        if d == "match_day_creatives":
            note = f"Match night at home? {offer or 'Our match-night specials'} — delivered hot from {where}. Order on Swiggy now."
            return self._t(conv, f"Here's the banner + story copy: \"{note}\" Reply CONFIRM and I'll push it before the first ball.",
                           f"Banner + story copy yeh raha: \"{note}\" CONFIRM reply karein, match se pehle live kar doongi.")
        if d == "festival_package":
            fest = p.get("festival") or "Festive"
            note = f"{fest} at {where}: book early" + (f" — {offer}" if offer else "") + ". Limited slots, reply to reserve."
            return self._t(conv, f"Here's the {fest} package post: \"{note}\" Reply CONFIRM and it goes live today.",
                           f"{fest} package post yeh raha: \"{note}\" CONFIRM reply karein, aaj live kar doongi.")
        if d == "event_update":
            kind = humanize(str(trigger.get("kind", "update")))
            note = f"{kind.capitalize()} update from {where}: we're open and ready to help" + (f" — {offer}" if offer else "") + "."
            return self._t(conv, f"Here's the customer update: \"{note}\" Reply CONFIRM and I'll send it out.",
                           f"Customer update yeh raha: \"{note}\" CONFIRM reply karein, bhej doongi.")
        if d == "differentiation_post":
            quote = next((t.get("common_quote") for t in merchant.get("review_themes") or []
                          if t.get("sentiment") == "pos" and t.get("common_quote")), None)
            praise = next((humanize(t.get("theme")) for t in merchant.get("review_themes") or []
                           if t.get("sentiment") == "pos" and (t.get("occurrences_30d") or 0) >= 5), None)
            lead = f"Our customers say: '{quote}'." if quote else (f"Loved for our {praise}." if praise else "Trusted by our neighbourhood.")
            note = f"{where} — {lead}" + (f" {offer}." if offer else "") + " Message us to book."
            return self._t(conv, f"Here's the post, leading with your strength rather than price: \"{note}\" Reply CONFIRM and it goes live today.",
                           f"Post yeh raha — price nahi, aapki strength pe: \"{note}\" CONFIRM reply karein, aaj live kar doongi.")
        if d == "winback_campaign":
            note = f"We miss you at {where}!" + (f" {offer} this week" if offer else " Come by this week") + " — reply to book."
            return self._t(conv, f"Here's the win-back note: \"{note}\" Reply CONFIRM and it goes to your lapsed customers.",
                           f"Win-back note yeh raha: \"{note}\" CONFIRM reply karein, lapsed customers ko bhej doongi.")
        post_offer = f" {offer}." if offer else ""
        return self._t(conv, f"Here's the Google post draft: \"{where} —{post_offer} Message us on WhatsApp to book.\" Reply CONFIRM and it goes live today; next I'll prep the WhatsApp status version.",
                       f"Google post draft yeh raha: \"{where} —{post_offer} Book karne ke liye WhatsApp karein.\" CONFIRM reply karein, aaj hi live kar doongi; next WhatsApp status version bhi ready karungi.")

    # ---------------------------------------------------------- later stages
    def _done(self, conv: Conversation) -> str:
        d = conv.deliverable or ""
        done_en, done_hi = {
            "compliance_checklist": ("Done ✅ — checklist saved and a reminder is set for a week before the deadline.",
                                     "Done ✅ — checklist save ho gayi, deadline se ek hafte pehle reminder set hai."),
            "recall_customer_note": ("Done ✅ — the note is queued for the affected customers.", "Done ✅ — affected customers ke liye note queue ho gaya."),
            "renewal+refresh": ("Done ✅ — renewal is in process; your listing stays live.", "Done ✅ — renewal process mein hai; listing live rahegi."),
            "registration_details": ("Done ✅ — it's in your calendar.", "Done ✅ — calendar mein add ho gaya."),
            "customer_reminder": ("Done ✅ — the reminder has gone out from your number.", "Done ✅ — reminder aapke number se chala gaya."),
        }.get(d, ("Done ✅ — it's live.", "Done ✅ — live ho gaya."))
        if d in self.SELF_CONTAINED:
            conv.meta["stage"] = 4
            return self._t(conv, f"{done_en} Nothing else needed from you — I'll flag anything that changes.",
                           f"{done_hi} Aapko aur kuch nahi karna — kuch badla toh main bata doongi.")
        nxt_en, nxt_hi = ("Next: a WhatsApp status version + a 2-line reply your staff can paste when customers ask. Want both?",
                          "Next: ek WhatsApp status version + customers ke sawaal ke liye 2-line ready reply. Dono bhej doon?")
        return self._t(conv, f"{done_en} {nxt_en}", f"{done_hi} {nxt_hi}")

    SELF_CONTAINED = {"compliance_checklist", "registration_details", "renewal+refresh", "reactivation+winback",
                      "gbp_verification", "customer_reminder", "recall_customer_note", "review_replies"}

    def _second_artifact(self, conv: Conversation) -> str:
        category, merchant, trigger, customer = self._contexts(conv)
        ident = (merchant or {}).get("identity") or {}
        name = ident.get("name", "our store")
        offer = _best_offer(build_ctx(category, merchant, trigger, customer, None)) if merchant else ""
        note = conv.meta.get("artifact_note")
        if note:
            sents = re.split(r"(?<=[.!?])\s+", note)
            status = sents[0] if len(sents[0]) >= 40 or len(sents) == 1 else " ".join(sents[:2])
            status = status if len(status) <= 150 else status[:147].rsplit(" ", 1)[0] + "…"
            core = re.sub(r"^(New at|Festive at)\s+", "", status.split(" — ")[0]).rstrip(".:")
            reply = f"Thanks for asking! {core} — share a day or time that suits you and we'll take care of it."
        else:
            status = f"{offer} at {name} — message us to book!" if offer else f"New this week at {name} — message us to know more!"
            reply = f"Thanks for asking! {offer} is available right now — share a time and we'll book you in." if offer else \
                "Thanks for asking! Share a time that suits you and we'll take care of it."
        return self._t(conv, f"Here you go —\nStatus: \"{status}\"\nQuick reply: \"{reply}\"\nReply CONFIRM and I'll set the status live.",
                       f"Yeh raha —\nStatus: \"{status}\"\nQuick reply: \"{reply}\"\nCONFIRM reply karein, status live kar doongi.")

    def _wrap(self, conv: Conversation) -> str:
        return self._t(conv, "All set ✅ Everything's live. I'll share how it performs next week — no action needed from you.",
                       "Sab set ✅ Sab live hai. Agle hafte performance share karungi — aapko kuch karne ki zaroorat nahi.")
