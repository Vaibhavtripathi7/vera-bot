"""Hard gate for every outgoing body (template or LLM). Returns a list of violations (empty = ok)."""
from __future__ import annotations

import re

from . import playbook
from .facts import FactSheet
from .util import numbers_in

URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|org|net|io|ai|co)\b)", re.I)
SNAKE_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
PREAMBLE_RE = re.compile(r"^\s*(i hope|hope you|greetings|dear sir|dear madam|i am reaching|i'm reaching|this is vera)", re.I)
DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
HINDI_MARKERS = {"hai", "hain", "aap", "aapke", "aapka", "aapki", "kya", "karna", "kar", "doon", "bhej", "mein",
                 "nahi", "ji", "chalega", "sakte", "karein", "karoon", "wala", "abhi", "liye", "ke", "ka", "ki",
                 "hoon", "raha", "rahi", "batayein", "bataiye", "karte", "hua", "gaya", "theek"}
# small counts that appear as ordinary effort/deliverable words, not data claims
SAFE_SMALL = {"1", "2", "3", "4", "5", "10", "15", "24", "30", "48", "60", "90"}

# Stopwords for the capitalised-name check: sentence-initial words and common nouns/brands that
# are safe to capitalise without being a factual claim.
SAFE_CAPS = {
    "hi", "hello", "namaste", "vanakkam", "namaskaram", "namaskara", "namaskar", "quick", "reply", "yes", "no",
    "stop", "want", "shall", "should", "i", "i'll", "i've", "we", "we've", "we'll", "your", "you", "this", "that",
    "the", "a", "an", "it", "its", "it's", "there", "here", "done", "sending", "drafted", "draft", "next", "confirm",
    "google", "whatsapp", "gbp", "insta", "instagram", "swiggy", "zomato", "vera", "magicpin", "also", "and", "or",
    "but", "so", "if", "just", "one", "two", "three", "four", "five", "good", "great", "thanks", "thank", "sorry",
    "apologies", "noted", "got", "sure", "ok", "okay", "happy", "heads", "heads-up", "worth", "after", "before",
    "today", "tomorrow", "tonight", "this", "these", "those", "since", "with", "when", "what", "which", "who", "how",
    "why", "for", "on", "in", "at", "by", "to", "of", "from", "every", "each", "all", "most", "more", "less",
    "no-shame", "friendly", "reminder", "please", "kindly", "free", "new", "fresh", "same", "saturday", "sunday",
    "monday", "tuesday", "wednesday", "thursday", "friday", "sat", "sun", "mon", "tue", "wed", "thu", "fri",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "january", "february",
    "march", "april", "june", "july", "august", "september", "october", "november", "december", "diwali", "holi",
    "ipl", "ctr", "cde", "yoy", "rx", "otc", "pt", "hiit", "dr", "dr.", "mr", "mr.", "ji", "coach", "team", "clinic",
    "salon", "studio", "pharmacy", "restaurant", "gym", "aap", "aapke", "aapka", "aapki", "apke", "kya", "haan",
    "bas", "abhi", "chalega", "theek", "ek", "koi", "main", "hum", "yeh", "ye", "woh", "agar", "toh", "phir",
    "is", "are", "was", "be", "not", "any", "my", "our", "me", "us", "am", "pm", "rs", "inr", "dental", "cleaning",
    "looks", "looking", "let", "let's", "can", "could", "would", "will", "may", "might", "must", "take", "tell",
    "share", "send", "sent", "book", "booked", "hold", "block", "keep", "check", "see", "look", "note", "plan",
    "post", "posts", "offer", "offers", "profile", "listing", "review", "reviews", "search", "searches", "calls",
    "views", "call", "slot", "slots", "sunday's", "monday's", "match", "cheers", "welcome", "regards",
}


def _caps_tokens(body: str) -> set[str]:
    """Capitalised tokens that are NOT sentence/clause-initial (those are ordinary words)."""
    toks = set()
    for m in re.finditer(r"(?<![\w'])([A-Z][A-Za-z0-9'&.+-]*)", body):
        before = body[:m.start()].rstrip(" \t\"'(‘“")
        if not before or before[-1] in ".!?:;—–-\n•,":
            if not before or before[-1] != ",":
                continue
        w = m.group(1).strip(".,'&+-").lower()
        if w:
            toks.add(w)
    return toks


def validate(body: str, fs: FactSheet, *, category: dict | None = None, prior_bodies: list[str] | None = None,
             kind: str = "proactive", require_hinglish: bool | None = None, committed: bool = False) -> list[str]:
    v: list[str] = []
    if not body or not body.strip():
        return ["empty_body"]
    low = body.lower()
    if len(body) > (1100 if kind == "artifact" else 700):
        v.append("too_long")
    if URL_RE.search(body):
        v.append("url")
    if PREAMBLE_RE.search(body):
        v.append("preamble")
    taboos = list(playbook.GLOBAL_TABOOS)
    if category:
        voice = category.get("voice") or {}
        taboos += [str(t).split("(")[0].strip() for t in (voice.get("vocab_taboo") or voice.get("taboos") or [])]
    for t in taboos:
        if t and re.search(r"\b" + re.escape(t.lower()) + r"\b", low):
            v.append(f"taboo:{t}")
    if SNAKE_RE.search(body):
        v.append("jargon:snake_case")
    for j in playbook.JARGON_WORDS:
        if "_" not in j and re.search(r"\b" + re.escape(j) + r"\b", low):
            v.append(f"jargon:{j}")
    # numbers must be grounded
    unknown = {n for n in numbers_in(body) if n not in fs.allowed_numbers and n not in SAFE_SMALL}
    if unknown:
        v.append("unknown_numbers:" + ",".join(sorted(unknown)))
    # capitalised names must be grounded
    names = {t for t in _caps_tokens(body) if t not in SAFE_CAPS and t not in fs.allowed_names and not t.isdigit()}
    if category:
        vocab = " ".join(map(str, (category.get("voice") or {}).get("vocab_allowed") or [])).lower()
        names = {n for n in names if n not in vocab}
    if names:
        v.append("unknown_names:" + ",".join(sorted(names)))
    # language
    if DEVANAGARI_RE.search(body):
        v.append("devanagari")
    if any(ch.isalpha() and ord(ch) > 0x24F for ch in body):     # Arabic/Urdu, Devanagari, Tamil, CJK... (Roman script only)
        v.append("non_latin_script")
    words = set(re.findall(r"[a-z']+", low))
    if require_hinglish and len(words & HINDI_MARKERS) < 2:
        v.append("language:hinglish_expected")
    # CTA shape: proactive messages end on the ask
    if kind == "proactive":
        last = re.split(r"(?<=[.!?])\s+", body.strip())[-1]
        if "?" not in last and not re.search(r"\breply\b|\bbatayein\b|\bbataiye\b|\bconfirm\b", last.lower()):
            v.append("cta_not_last")
        if body.count("?") > 2:
            v.append("multiple_ctas")
    if committed:
        for q in playbook.QUALIFYING_SUBSTRINGS:
            if q in low:
                v.append(f"qualifying_after_commit:{q}")
        if not any(a in low for a in playbook.ACTION_WORDS):
            v.append("no_action_word_after_commit")
    for prev in prior_bodies or []:
        if _jaccard(prev, body) >= 0.8:
            v.append("repeat")
            break
    return v


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


CASE_STUDY_BODIES: list[str] = []


def case_study_overlap(body: str) -> float:
    """Max 5-gram overlap ratio vs the published case-study bodies (plagiarism guard)."""
    grams = _ngrams(body, 5)
    if not grams:
        return 0.0
    best = 0.0
    for cs in CASE_STUDY_BODIES:
        g2 = _ngrams(cs, 5)
        if g2:
            best = max(best, len(grams & g2) / len(grams))
    return best


def _ngrams(text: str, n: int) -> set:
    w = re.findall(r"\w+", text.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}
