import glob
import json
import os
from pathlib import Path

os.environ["VERA_DB_PATH"] = ""          # memory-only for tests
os.environ["LLM_ENABLED"] = "0"
os.environ["VERA_SESSION_IDLE_RESET"] = "0"

import pytest
from fastapi.testclient import TestClient

from vera import api, pipeline

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "expanded"


def _load(sub, key):
    return {json.load(open(f))[key]: json.load(open(f)) for f in glob.glob(str(EXP / sub / "*.json"))}


@pytest.fixture()
def client():
    api.STORE.wipe()
    pipeline.CACHE.clear()
    return TestClient(api.app)


def push_all(client, triggers=True):
    for f in glob.glob(str(EXP / "categories" / "*.json")):
        d = json.load(open(f))
        assert client.post("/v1/context", json={"scope": "category", "context_id": d["slug"], "version": 1, "payload": d,
                                                "delivered_at": "2026-04-26T10:00:00Z"}).status_code == 200
    for scope, sub, key in (("merchant", "merchants", "merchant_id"), ("customer", "customers", "customer_id")):
        for cid, d in _load(sub, key).items():
            r = client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": 1, "payload": d, "delivered_at": "x"})
            assert r.status_code == 200, r.text
    if triggers:
        for tid, d in _load("triggers", "id").items():
            client.post("/v1/context", json={"scope": "trigger", "context_id": tid, "version": 1, "payload": d, "delivered_at": "x"})


def test_health_and_metadata(client):
    r = client.get("/v1/healthz").json()
    assert r["status"] == "ok" and r["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    m = client.get("/v1/metadata").json()
    assert {"team_name", "team_members", "model", "approach", "version"} <= set(m)


def test_warmup_counts_and_versioning(client):
    push_all(client, triggers=False)
    assert client.get("/v1/healthz").json()["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}
    body = {"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 1, "payload": {"x": 1}}
    r = client.post("/v1/context", json=body)
    assert r.status_code == 409 and r.json() == {"accepted": False, "reason": "stale_version", "current_version": 1}
    body["version"] = 2
    assert client.post("/v1/context", json=body).json()["accepted"] is True
    bad = client.post("/v1/context", json={"scope": "nope", "context_id": "a", "version": 1, "payload": {}})
    assert bad.status_code == 400 and bad.json()["reason"] == "invalid_scope"


def test_no_content_type_header(client):
    r = client.post("/v1/tick", content=b'{"now": "2026-04-26T10:35:00Z", "available_triggers": []}')
    assert r.status_code == 200 and r.json() == {"actions": []}


def test_tick_all_canonical_pairs(client):
    push_all(client)
    pairs = json.load(open(EXP / "test_pairs.json"))["pairs"]
    r = client.post("/v1/tick", json={"now": "2026-09-27T10:00:00Z", "available_triggers": [p["trigger_id"] for p in pairs]})
    acts = r.json()["actions"]
    required = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
                "template_params", "body", "cta", "suppression_key", "rationale"}
    assert 0 < len(acts) <= 20
    for a in acts:
        assert required <= set(a) and a["body"].strip()
        assert "http" not in a["body"]
        if a["customer_id"]:
            assert a["send_as"] == "merchant_on_behalf"
    assert len({a["conversation_id"] for a in acts}) == len(acts)
    # remaining pairs go out on the next tick (deferred, not dropped); nothing is re-sent
    r2 = client.post("/v1/tick", json={"now": "2026-09-27T10:05:00Z", "available_triggers": [p["trigger_id"] for p in pairs]})
    sent = {a["trigger_id"] for a in acts} | {a["trigger_id"] for a in r2.json()["actions"]}
    r3 = client.post("/v1/tick", json={"now": "2026-09-27T10:10:00Z", "available_triggers": [p["trigger_id"] for p in pairs]})
    sent |= {a["trigger_id"] for a in r3.json()["actions"]}
    assert len(sent) == 30, f"only {len(sent)} of 30 canonical pairs produced a message"
    again = [a["trigger_id"] for a in r2.json()["actions"]]
    assert not set(again) & {a["trigger_id"] for a in acts}


def test_auto_reply_across_conversation_ids(client):
    push_all(client, triggers=False)
    mid = "m_001_drmeera_dentist_delhi"
    msg = "Thank you for contacting us! Our team will respond shortly."
    actions = []
    for i in range(1, 5):
        r = client.post("/v1/reply", json={"conversation_id": f"conv_auto_{i}", "merchant_id": mid, "customer_id": None,
                                           "from_role": "merchant", "message": msg, "received_at": "x", "turn_number": i + 1})
        actions.append(r.json()["action"])
    assert actions[:3] == ["send", "wait", "end"]


def test_intent_transition_passes_simulator_check(client):
    push_all(client, triggers=False)
    r = client.post("/v1/reply", json={"conversation_id": "conv_intent_1", "merchant_id": "m_001_drmeera_dentist_delhi",
                                       "customer_id": None, "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                                       "received_at": "x", "turn_number": 2}).json()
    body = r["body"].lower()
    assert r["action"] == "send"
    assert any(w in body for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"])
    assert not any(w in body for w in ["would you", "do you", "can you tell", "what if", "how about"])


def test_hostile_and_offtopic(client):
    push_all(client, triggers=False)
    base = {"merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None, "from_role": "merchant", "received_at": "x"}
    r = client.post("/v1/reply", json={**base, "conversation_id": "conv_hostile", "message": "Stop messaging me. This is useless spam.",
                                       "turn_number": 2}).json()
    assert r["action"] == "end"
    r = client.post("/v1/reply", json={**base, "conversation_id": "h2", "message": "Why are you bothering me, this is useless",
                                       "turn_number": 2}).json()
    assert r["action"] == "end" or "sorry" in r.get("body", "").lower()
    r = client.post("/v1/reply", json={**base, "conversation_id": "h3", "message": "Btw can you also help me with my GST filing this month?",
                                       "turn_number": 2}).json()
    assert r["action"] == "send" and "ca" in r["body"].lower()


def test_customer_slot_choice(client):
    push_all(client)
    acts = client.post("/v1/tick", json={"now": "2026-09-27T10:00:00Z", "available_triggers": ["trg_003_recall_due_priya"]}).json()["actions"]
    assert acts and acts[0]["send_as"] == "merchant_on_behalf"
    r = client.post("/v1/reply", json={"conversation_id": acts[0]["conversation_id"], "merchant_id": acts[0]["merchant_id"],
                                       "customer_id": acts[0]["customer_id"], "from_role": "customer", "message": "2",
                                       "received_at": "x", "turn_number": 2}).json()
    assert r["action"] == "send" and "Thu 6 Nov, 5pm" in r["body"]


def test_deterministic(client):
    push_all(client)
    ids = ["trg_001_research_digest_dentists", "trg_010_ipl_match_delhi"]
    a = client.post("/v1/tick", json={"now": "2026-09-27T10:00:00Z", "available_triggers": ids}).json()
    api.STORE.wipe(); pipeline.CACHE.clear()
    push_all(client)
    b = client.post("/v1/tick", json={"now": "2026-09-27T10:00:00Z", "available_triggers": ids}).json()
    assert [x["body"] for x in a["actions"]] == [x["body"] for x in b["actions"]]
