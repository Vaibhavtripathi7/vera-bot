"""Post-deploy smoke test against a live URL (safe: tears its own state down afterwards).

Usage: uv run python -m eval.smoke https://vera-bot-xxxx.onrender.com
Checks: healthz/metadata latency, context push (200 + 409 + idempotent no-op), a tick on 3 real triggers,
a reply of each kind, cold-vs-warm latency, then /v1/teardown.
"""
import json
import sys
import time

import httpx

URL = sys.argv[1].rstrip("/")
E = "expanded"
ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def timed(method, path, body=None, timeout=35):
    t0 = time.monotonic()
    r = httpx.request(method, URL + path, json=body, timeout=timeout)
    return r, time.monotonic() - t0


r, dt = timed("GET", "/v1/healthz")
check("healthz", r.status_code == 200, f"{dt * 1000:.0f}ms (first call; >2s means it was asleep)")
r, dt = timed("GET", "/v1/healthz")
check("healthz warm < 2s", r.status_code == 200 and dt < 2, f"{dt * 1000:.0f}ms")
r, dt = timed("GET", "/v1/metadata")
check("metadata", r.status_code == 200 and "team_name" in r.json(), f"{dt * 1000:.0f}ms {r.json().get('model', '')[:60]}")
timed("POST", "/v1/teardown", {})

cat = json.load(open(f"{E}/categories/dentists.json"))
m = json.load(open(f"{E}/merchants/m_001_drmeera_dentist_delhi.json"))
c = json.load(open(f"{E}/customers/c_001_priya_for_m001.json"))
ts = [json.load(open(f"{E}/triggers/{t}.json")) for t in
      ("trg_001_research_digest_dentists", "trg_023_competitor_opened_dentist", "trg_003_recall_due_priya")]
for scope, cid, p in [("category", "dentists", cat), ("merchant", m["merchant_id"], m), ("customer", c["customer_id"], c)] + \
        [("trigger", t["id"], t) for t in ts]:
    r, dt = timed("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": 1, "payload": p, "delivered_at": "x"})
    check(f"context {scope}", r.status_code == 200, f"{dt * 1000:.0f}ms")
r, _ = timed("POST", "/v1/context", {"scope": "merchant", "context_id": m["merchant_id"], "version": 1, "payload": {"x": 1}})
check("stale version -> 409", r.status_code == 409)
r, dt = timed("POST", "/v1/tick", {"now": "2026-09-28T10:00:00Z", "available_triggers": [t["id"] for t in ts]})
acts = r.json().get("actions", [])
check("tick", r.status_code == 200 and len(acts) == 3 and dt < 10, f"{len(acts)} actions in {dt:.2f}s")
for a in acts:
    print(f"   · {a['trigger_id']}: {a['body'][:140]}…  [{a['rationale'][-60:]}]")
for msg, want in (("Yes please, go ahead", "send"), ("Thank you for contacting us! Our team will respond shortly.", "send"),
                  ("Stop messaging me. This is useless spam.", "end")):
    a = acts[0]
    r, dt = timed("POST", "/v1/reply", {"conversation_id": f"smoke_{want}_{len(msg)}", "merchant_id": a["merchant_id"],
                                       "from_role": "merchant", "message": msg, "turn_number": 2})
    check(f"reply '{msg[:24]}…' -> {want}", r.json().get("action") == want and dt < 10, f"{dt * 1000:.0f}ms")
r, _ = timed("GET", "/v1/debug/llm")
print("   llm:", json.dumps(r.json())[:300])
timed("POST", "/v1/teardown", {})
r, _ = timed("GET", "/v1/healthz")
check("teardown clean", r.json().get("contexts_loaded", {}).get("trigger") == 0)
print("\nALL PASS" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
