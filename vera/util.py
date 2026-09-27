"""Small shared helpers: number formatting/normalisation, dates, names, language."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

HINDI_BELT = {
    "delhi", "new delhi", "jaipur", "lucknow", "chandigarh", "noida", "gurgaon", "gurugram",
    "kanpur", "agra", "bhopal", "indore", "patna", "varanasi", "dehradun", "ghaziabad",
    "faridabad", "meerut", "allahabad", "prayagraj", "ranchi", "raipur", "jodhpur", "udaipur",
    "ahmedabad", "pune", "mumbai", "nagpur", "surat",
}
NATIVE_GREETING = {"ta": "Vanakkam", "te": "Namaskaram", "kn": "Namaskara", "mr": "Namaskar",
                   "bn": "Nomoshkar", "ml": "Namaskaram", "gu": "Kem cho"}


def stable_index(key: str, n: int) -> int:
    """Deterministic pick in [0, n) from a string key (process-independent, unlike hash())."""
    if n <= 1:
        return 0
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % n


def sha(obj: str) -> str:
    return hashlib.sha256(obj.encode()).hexdigest()


def parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        s = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------- number formatting ----------

def fmt_int(n) -> str:
    """Indian-style grouping for numbers >= 1 lakh, western commas below (2,410 / 1,20,000)."""
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    neg, n = n < 0, abs(n)
    s = str(n)
    if n >= 100000:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    elif n >= 1000:
        s = f"{n:,}"
    return ("-" if neg else "") + s


def fmt_money(n) -> str:
    return "₹" + fmt_int(n)


def fmt_pct(fraction, signed: bool = False, digits: int | None = None) -> str:
    """0.021 -> '2.1%'; 0.38 -> '38%'; signed adds +/-."""
    try:
        v = float(fraction) * 100
    except (TypeError, ValueError):
        return str(fraction)
    if digits is None:
        digits = 0 if abs(v) >= 10 or abs(v - round(v)) < 0.05 else 1
    s = f"{abs(v):.{digits}f}".rstrip("0").rstrip(".") if digits else f"{abs(v):.0f}"
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    return f"{sign}{s}%"


_NUM_RE = re.compile(r"(?<![A-Za-z])(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)")


def numbers_in(text: str) -> set[str]:
    """Canonical number tokens found in text ('2,100' -> '2100', '2.10' -> '2.1')."""
    out = set()
    for m in _NUM_RE.finditer(text or ""):
        out.add(canon_number(m.group(1)))
    return out


def canon_number(tok: str) -> str:
    t = tok.replace(",", "")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t.lstrip("0") or "0"


# ---------- names / text ----------

HONORIFIC_RE = re.compile(r"^(dr\.?|mr\.?|mrs\.?|ms\.?|shri|smt\.?)\s+", re.I)


def clean_first_name(raw: str | None) -> str:
    if not raw:
        return ""
    name = HONORIFIC_RE.sub("", raw.strip())
    return name.split()[0] if name else ""


def humanize(token: str) -> str:
    """snake_case / kebab to words: 'high_risk_adults' -> 'high-risk adults' (best effort)."""
    if not token:
        return ""
    t = str(token).replace("_", " ").replace("-", " ").strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\bhigh risk\b", "high-risk", t)
    return t


def parse_person(name: str | None) -> tuple[str, str | None]:
    """'Aanya (parent: Sneha)' -> ('Aanya', 'Sneha'); '(walk-in, no profile)' -> ('', None)."""
    if not name or name.strip().startswith("("):
        return "", None
    m = re.match(r"^\s*([^()]+?)\s*\(\s*parent\s*:\s*([^)]+)\)\s*$", name, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return name.strip(), None


# ---------- language ----------

def merchant_language(merchant: dict) -> str:
    """'hinglish' | 'en_light' (English with light Hindi touches) | 'en'."""
    ident = merchant.get("identity") or {}
    langs = [str(x).lower() for x in ident.get("languages") or []]
    city = str(ident.get("city") or "").lower()
    if "hi" in langs:
        return "hinglish" if city in HINDI_BELT or not city else "en_light"
    return "en"


def customer_language(customer: dict | None, merchant: dict) -> tuple[str, str | None]:
    """Returns (mode, native_greeting). mode: 'hinglish' | 'hindi' | 'en'."""
    if not customer:
        return merchant_language(merchant) if merchant_language(merchant) != "en_light" else "en", None
    pref = str((customer.get("identity") or {}).get("language_pref") or "").lower().strip()
    if pref in ("hi",  "hindi"):
        return "hindi", None
    if pref.startswith("hi"):
        return "hinglish", None
    m = re.match(r"^([a-z]{2})[-\s]?en", pref)
    if m and m.group(1) in NATIVE_GREETING:
        return "en", NATIVE_GREETING[m.group(1)]
    return "en", None


def month_in_range(month_range: str, month: int) -> bool:
    """'Nov-Feb' / 'Apr-Jun' / 'Jan' / 'Feb 14' -> whether month (1-12) falls inside."""
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    toks = re.findall(r"[A-Za-z]{3}", month_range or "")
    idx = [months.index(t.lower()) + 1 for t in toks if t.lower() in months]
    if not idx:
        return False
    if len(idx) == 1:
        return month == idx[0]
    a, b = idx[0], idx[1]
    return a <= month <= b if a <= b else (month >= a or month <= b)
