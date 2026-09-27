"""Single source of the DO / DON'T rules.

Used verbatim in the LLM writer + critic prompts; the machine-checkable subset is enforced in
validator.py; templates comply by construction.
"""
from __future__ import annotations

STATIC_DO = [
    "Open with WHY NOW: the specific event/trigger, in the first sentence.",
    "Anchor on 1-2 verifiable facts from the FACTS list (number, date, source, price). Quote them exactly.",
    "When a fact is a benchmark or research item, attribute it inline (e.g. 'vs 3.0% avg for metro solo clinics', 'JIDA Oct 2026, p.14').",
    "Prefer service+price offers ('Dental Cleaning @ ₹299') over percentage discounts.",
    "Add judgement: say what the fact MEANS for this merchant and what to do about it.",
    "Externalise effort: say what Vera has drafted / will do ('I've drafted…', 'takes 5 min').",
    "Exactly ONE call-to-action, as the LAST sentence; low-friction (YES/STOP or a single short question).",
    "Address the owner by name (Dr. {name} for dentists). Match the language mode exactly.",
    "Customer-facing: warm, respectful, no shame/guilt; stay within consent; no merchant performance stats.",
    "Keep it tight: 2-4 sentences for proactive messages, WhatsApp-friendly, no headings.",
]

STATIC_DONT = [
    "Never invent a number, date, name, source, competitor, offer, price, slot or statistic not in FACTS.",
    "No preamble ('Hope you are doing well', 'I am reaching out'). No re-introducing yourself.",
    "No generic '% off' / 'increase your sales' framing when a concrete service+price exists.",
    "No multiple CTAs, no 'Reply 1 for X, 2 for Y' menus (except customer slot booking).",
    "No URLs or links.",
    "No hype, ALL CAPS, or exclamation spam; clinical/peer tone for dentists and pharmacies.",
    "No internal jargon: never write snake_case tokens or words like trigger, payload, signal, suppression, CTR-peer-median.",
    "After the merchant says yes, never ask another qualifying question - deliver.",
    "Never repeat a message you already sent in this conversation.",
    "Never use the category's taboo words.",
]

GLOBAL_TABOOS = ["guaranteed", "100% safe", "miracle", "best in city", "amazing deal", "limited time only",
                 "act now", "hurry", "risk-free", "cure"]

JARGON_WORDS = ["trigger", "payload", "suppression", "signal", "ctr_below", "peer_median", "context_id",
                "merchant_id", "placeholder", "dormant_with_vera", "perf_dip", "perf_spike", "curious_ask",
                "research_digest", "json", "api", "template"]

# Replay-scenario checks from judge_simulator.py (_intent): commit replies must contain an action verb
# and none of these qualifying phrases (substring match - 'do you' also matches 'do your').
QUALIFYING_SUBSTRINGS = ["would you", "do you", "can you tell", "what if", "how about"]
ACTION_WORDS = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]


def dynamic_rules(category: dict, lang: str, reader: str, prior_bodies: list[str] | None = None,
                  committed: bool = False) -> list[str]:
    voice = category.get("voice") or {}
    rules = []
    if voice.get("tone"):
        rules.append(f"Category voice: tone={voice.get('tone')}, register={voice.get('register', '')}.")
    taboos = voice.get("vocab_taboo") or voice.get("taboos") or []
    if taboos:
        rules.append("Taboo words (never use): " + ", ".join(map(str, taboos)) + ".")
    allowed = voice.get("vocab_allowed") or []
    if allowed:
        rules.append("Category vocabulary you may use naturally: " + ", ".join(map(str, allowed[:12])) + ".")
    rules.append({
        "hinglish": "Language: natural Hindi-English code-mix in Roman script (like 'Aapke liye draft ready hai — bhej doon?'). Keep numbers/offer names as-is.",
        "hindi": "Language: Hindi-heavy Roman script, respectful ('aap', 'ji'); keep medicine/offer names and numbers exactly.",
        "en_light": "Language: simple Indian English; no Hindi needed.",
        "en": "Language: simple Indian English.",
    }.get(lang, "Language: simple Indian English."))
    if reader == "customer":
        rules.append("You write AS the merchant's business to its customer. No merchant stats, no Vera mention.")
    if committed:
        rules.append("The merchant already said yes: deliver the artifact now; use words like 'here', 'drafted', 'next'; never ask 'would you'/'do you'.")
    if prior_bodies:
        rules.append("Already sent (do not repeat or paraphrase closely): " + " | ".join(b[:120] for b in prior_bodies[-3:]))
    return rules


def rules_text(category: dict, lang: str, reader: str, prior_bodies=None, committed=False) -> str:
    lines = ["DO:"] + [f"- {r}" for r in STATIC_DO] + ["DON'T:"] + [f"- {r}" for r in STATIC_DONT]
    lines += ["THIS MESSAGE:"] + [f"- {r}" for r in dynamic_rules(category, lang, reader, prior_bodies, committed)]
    return "\n".join(lines)
