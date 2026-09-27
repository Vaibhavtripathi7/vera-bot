"""Render every dataset trigger through the template composer and report validator violations.

Usage: uv run python -m eval.render_all [--pairs] [--now 2026-04-26T10:00:00Z] [--show]
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

from vera.compose import compose_template
from vera.util import parse_dt

ROOT = Path(__file__).resolve().parent.parent


def load(expanded: Path):
    def rd(sub, key):
        return {json.load(open(f))[key]: json.load(open(f)) for f in glob.glob(str(expanded / sub / "*.json"))}
    cats = {json.load(open(f))["slug"]: json.load(open(f)) for f in glob.glob(str(expanded / "categories" / "*.json"))}
    return cats, rd("merchants", "merchant_id"), rd("customers", "customer_id"), rd("triggers", "id")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", action="store_true", help="only the 30 canonical test pairs")
    ap.add_argument("--now", default="2026-04-26T10:00:00Z")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--expanded", default=str(ROOT / "expanded"))
    a = ap.parse_args()
    cats, ms, cs, ts = load(Path(a.expanded))
    ids = sorted(ts)
    labels = {}
    if a.pairs:
        pairs = json.load(open(Path(a.expanded) / "test_pairs.json"))["pairs"]
        ids = [p["trigger_id"] for p in pairs]
        labels = {p["trigger_id"]: p["test_id"] for p in pairs}
    now = parse_dt(a.now)
    bad = 0
    for tid in ids:
        t = ts[tid]
        m = ms[t["merchant_id"]]
        c = cs.get(t.get("customer_id")) if t.get("customer_id") else None
        d, ctx, v = compose_template(cats[m["category_slug"]], m, t, c, now)
        bad += bool(v)
        if a.show or v:
            print(f"--- {labels.get(tid, '')} {tid} [{ctx.family}/{ctx.fs.lang}] {'VIOLATIONS: ' + str(v) if v else ''}")
            print(d.body)
            if a.show:
                print(f"   cta={d.cta} | {d.rationale}")
    print(f"\n{len(ids) - bad}/{len(ids)} clean")


if __name__ == "__main__":
    main()
