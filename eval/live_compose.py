"""Compose the 30 canonical pairs through the live LLM pipeline; compare against templates.

Usage: set -a; . ./.env; set +a; uv run python -m eval.live_compose [--n 30]
"""
import argparse, asyncio, json, time
from pathlib import Path

from eval.render_all import load
from vera import pipeline
from vera.llm import POOL
from vera.store import Store
from vera.util import parse_dt

ROOT = Path(__file__).resolve().parent.parent


async def main(n):
    cats, ms, cs, ts = load(ROOT / "expanded")
    pairs = json.load(open(ROOT / "expanded" / "test_pairs.json"))["pairs"][:n]
    store = Store(db_path="")
    for slug, c in cats.items(): store.put("category", slug, 1, c)
    items = []
    for p in pairs:
        t = ts[p["trigger_id"]]; m = ms[t["merchant_id"]]
        c = cs.get(t.get("customer_id")) if t.get("customer_id") else None
        store.put("merchant", m["merchant_id"], 1, m); store.put("trigger", t["id"], 1, t)
        items.append((p["test_id"], pipeline.make_item(store, cats[m["category_slug"]], m, t, c, parse_dt("2026-09-28T10:00:00Z"), set(), [])))
    t0 = time.monotonic()
    for i in range(0, len(items), 10):          # emulate ticks of up to 10 fresh triggers
        chunk = [it for _, it in items[i:i + 10]]
        s = time.monotonic()
        await pipeline.compose_items(chunk, time.monotonic() + 7.5)
        print(f"chunk {i // 10}: {len(chunk)} items in {time.monotonic() - s:.1f}s")
    llm = sum(1 for _, it in items if it.chosen.source == "llm")
    print(f"\nchosen from LLM: {llm}/{len(items)}; total {time.monotonic() - t0:.1f}s; pool={json.dumps(POOL.status())[:400]}")
    print(f"rejected LLM candidates: {len(pipeline.REJECTS)}")
    for r in pipeline.REJECTS[:12]:
        print("  REJECT", r["id"], r["violations"], "|", r["body"][:160])
    out = ROOT / "eval" / "out"; out.mkdir(parents=True, exist_ok=True)
    with open(out / "live_compare.md", "w") as f:
        for tid, it in items:
            f.write(f"### {tid} {it.ctx.trigger['kind']} ({it.ctx.fs.lang}) -> {it.chosen.source}\n\n**template:** {it.template.body}\n\n")
            for c in it.candidates[1:]:
                f.write(f"**llm:** {c.body}\n\n")
    print("wrote eval/out/live_compare.md")

ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=30)
asyncio.run(main(ap.parse_args().n))
