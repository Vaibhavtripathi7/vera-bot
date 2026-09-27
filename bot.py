"""Challenge-brief section 7 artefacts.

- compose(category, merchant, trigger, customer) -> {body, cta, send_as, suppression_key, rationale}
  (deterministic template path - the same core that answers /v1/tick)
- respond(state, merchant_message) -> next move (the /v1/reply engine, section 7.4)
- `python bot.py` writes submission.jsonl for the 30 canonical test pairs.

The hosted bot itself is `vera.api:app` (uvicorn vera.api:app).
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

from vera.compose import compose  # noqa: F401  (re-exported contract)
from vera.conversation import ReplyEngine
from vera.store import Store

ROOT = Path(__file__).resolve().parent


def respond(state: dict, merchant_message: str) -> dict:
    """state: {conversation_id, merchant_id, customer_id?, category, merchant, trigger?, customer?, from_role?}."""
    store = Store(db_path="")
    for scope, key in (("category", "category"), ("merchant", "merchant"), ("trigger", "trigger"), ("customer", "customer")):
        obj = state.get(key)
        if obj:
            cid = obj.get("slug") or obj.get("merchant_id") or obj.get("id") or obj.get("customer_id")
            store.put(scope, cid, 1, obj)
    act, _conv, _k = ReplyEngine(store).handle({
        "conversation_id": state.get("conversation_id", "conv_offline"), "merchant_id": state.get("merchant_id"),
        "customer_id": state.get("customer_id"), "from_role": state.get("from_role", "merchant"),
        "message": merchant_message, "turn_number": state.get("turn_number", 2)})
    return act.to_json()


def make_submission(expanded: Path = ROOT / "expanded", out: Path = ROOT / "submission.jsonl") -> int:
    load = lambda sub, key: {json.load(open(f))[key]: json.load(open(f)) for f in glob.glob(str(expanded / sub / "*.json"))}
    cats = {json.load(open(f))["slug"]: json.load(open(f)) for f in glob.glob(str(expanded / "categories" / "*.json"))}
    ms, cs, ts = load("merchants", "merchant_id"), load("customers", "customer_id"), load("triggers", "id")
    pairs = json.load(open(expanded / "test_pairs.json"))["pairs"]
    with open(out, "w") as f:
        for p in pairs:
            t, m = ts[p["trigger_id"]], ms[p["merchant_id"]]
            c = cs.get(p.get("customer_id")) if p.get("customer_id") else None
            msg = compose(cats[m["category_slug"]], m, t, c)
            f.write(json.dumps({"test_id": p["test_id"], **msg}, ensure_ascii=False) + "\n")
    return len(pairs)


if __name__ == "__main__":
    n = make_submission(*(Path(a) for a in sys.argv[1:3]))
    print(f"wrote {n} lines to submission.jsonl")
