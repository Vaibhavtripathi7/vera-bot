"""Independent 4-criterion audit of submission.jsonl (12.5 points each, scored by pass rate).

1. no generic percentage discount (must use service@price / free offers)
2. exactly one primary CTA (a question + its "Reply X" instruction count as one ask; slot menus allowed for booking)
3. 24h window: first outbound carries an approved-template structure (template_name + template_params)
4. rationale explicitly names Context Anchors, Compulsion Lever and Compliance Guardrails

Usage: uv run python -m eval.audit_submission [submission.jsonl]
"""
import json
import re
import sys

PCT = re.compile(r"\d+\s*%\s*(off|discount)|flat\s+\d+\s*%|\b\d+\s*%\s*OFF", re.I)


def sentences(body):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", body) if s.strip()]


def cta_count(body, cta_type):
    sents = sentences(body)
    asks = [i for i, s in enumerate(sents) if "?" in s or re.match(r"^(reply|confirm|book|dispatch|batayein|.* reply karein)", s, re.I)
            or re.search(r"\breply (yes|confirm|1|haan)|\breply karein\b|\bconfirm reply\b", s, re.I)]
    if not asks:
        return 0
    # consecutive trailing ask sentences ("Want me to…? Reply YES — no commitment.") form one ask
    groups = 1
    for a, b in zip(asks, asks[1:]):
        if b != a + 1:
            groups += 1
    return groups


def audit(path):
    rows = [json.loads(l) for l in open(path)]
    fails = {1: [], 2: [], 3: [], 4: []}
    for r in rows:
        tid, body, rat = r.get("test_id"), r.get("body", ""), r.get("rationale", "")
        if PCT.search(body):
            fails[1].append(tid)
        n = cta_count(body, r.get("cta"))
        if n != 1 and not (r.get("cta") == "multi_choice_slot" and n >= 1):
            fails[2].append((tid, n))
        params = r.get("template_params")
        if not r.get("template_name") or not isinstance(params, list) or not params or not all(isinstance(p, str) and p for p in params):
            fails[3].append(tid)
        parts = {k: re.search(rf"{k}:\s*([^|]+)", rat) for k in ("Anchors", "Lever", "Guardrails")}
        if not all(p and len(p.group(1).strip(" .")) > 3 for p in parts.values()):
            fails[4].append(tid)
    n = len(rows)
    score = sum(12.5 * (n - len(f)) / n for f in fails.values())
    names = {1: "no % discount", 2: "one primary CTA", 3: "template window", 4: "rationale anchors/lever/guardrails"}
    for k, f in fails.items():
        print(f"{k}. {names[k]:38s} {n - len(f)}/{n}  {'FLAGS: ' + str(f) if f else ''}")
    print(f"\nSCORE: {score:.1f} / 50")
    return score


if __name__ == "__main__":
    audit(sys.argv[1] if len(sys.argv) > 1 else "submission.jsonl")
