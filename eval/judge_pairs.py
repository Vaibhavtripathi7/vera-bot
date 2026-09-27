"""Score the 30 canonical pairs (template/LLM path as configured) with the replica judge; print weakest dims.

Usage: set -a; . ./.env; set +a; LLM_ENABLED=0 uv run python -m eval.judge_pairs [--dim decision_quality]
"""
import argparse, asyncio, json, os
from pathlib import Path
import httpx
from eval.render_all import load
from eval.harness import JUDGE_SYSTEM
from vera.compose import compose_template, to_message
from vera.llm import Gemini, parse_json
from vera.util import parse_dt

ROOT = Path(__file__).resolve().parent.parent
DIMS = ["specificity", "category_fit", "merchant_fit", "decision_quality", "engagement_compulsion"]


def prompt_for(msg, cat, m, t, c):
    return f"""SCORE THIS MESSAGE:

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

Trigger Kind: {t.get('kind')}
Trigger Payload: {json.dumps(t.get('payload', {}))}
Trigger Urgency: {t.get('urgency')}

Customer: {json.dumps(c.get('identity', {})) if c else 'None (merchant-facing)'}

=== BOT'S MESSAGE ===
Body ({len(msg['body'])} chars): "{msg['body']}"
CTA: {msg.get('cta')}
Send As: {msg.get('send_as')}

Score each dimension 0-10 with clear reasoning. Be STRICT."""


async def main(dim, now):
    cats, ms, cs, ts = load(ROOT / "expanded")
    pairs = json.load(open(ROOT / "expanded" / "test_pairs.json"))["pairs"]
    P = Gemini("judge", os.environ.get("JUDGE_MODEL", "gemini-3.1-flash-lite"), os.environ["GEMINI_API_KEY"], 100, 1000)
    rows = []
    async with httpx.AsyncClient() as h:
        sem = asyncio.Semaphore(2)

        async def one(p):
            t = ts[p["trigger_id"]]; m = ms[t["merchant_id"]]; cat = cats[m["category_slug"]]
            c = cs.get(t.get("customer_id")) if t.get("customer_id") else None
            d, ctx, v = compose_template(cat, m, t, c, parse_dt(now))
            msg = to_message(d, t, c); msg["cta"] = d.cta
            async with sem:
                for a in range(4):
                    try:
                        j = parse_json(await P.call(h, JUDGE_SYSTEM, prompt_for(msg, cat, m, t, c), 40, 900))
                        if j and all(j.get(x) is not None for x in DIMS):
                            return p["test_id"], t["kind"], msg["body"], j
                    except Exception:
                        pass
                    await asyncio.sleep(12 * (a + 1))
            return p["test_id"], t["kind"], msg["body"], None
        rows = await asyncio.gather(*[one(p) for p in pairs])
    ok = [r for r in rows if r[3]]
    avg = {x: round(sum(float(r[3][x]) for r in ok) / len(ok), 2) for x in DIMS}
    print("judged", len(ok), "/", len(rows), avg, "total", round(sum(avg.values()), 2))
    for tid, kind, body, j in sorted(ok, key=lambda r: float(r[3][dim]))[:10]:
        key = dim.replace("engagement_compulsion", "engagement") + "_reason"
        print(f"\n{tid} {kind} {dim}={j[dim]} total={sum(float(j[x]) for x in DIMS)}\n  {body[:260]}\n  -> {j.get(key, '')[:220]}")
    json.dump([{"test_id": r[0], "kind": r[1], "body": r[2], "judge": r[3]} for r in rows], open(ROOT / "eval/out/pairs_judged.json", "w"), indent=1, ensure_ascii=False)

ap = argparse.ArgumentParser(); ap.add_argument("--dim", default="decision_quality"); ap.add_argument("--now", default="2026-09-28T10:00:00Z")
a = ap.parse_args(); asyncio.run(main(a.dim, a.now))
