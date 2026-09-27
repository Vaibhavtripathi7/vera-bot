"""LLM path with a fake provider: grounded rewrites are accepted, fabricated ones rejected, slow ones time out."""
import asyncio
import glob
import json
import os
import time
from pathlib import Path

os.environ["VERA_DB_PATH"] = ""

from vera import pipeline
from vera.llm import POOL
from vera.store import Store

EXP = Path(__file__).resolve().parent.parent / "expanded"


HEAT = {"id": "trg_heat", "scope": "merchant", "kind": "weather_heatwave", "merchant_id": "m_009_apollo_pharmacy_jaipur",
        "payload": {"city": "Jaipur", "temp_c": 44, "days": 3, "advisory": "IMD orange alert"}, "urgency": 3, "suppression_key": "heat"}


def _ctx(kind="research"):
    if kind == "event":
        cat = json.load(open(EXP / "categories" / "pharmacies.json"))
        m = json.load(open(EXP / "merchants" / "m_009_apollo_pharmacy_jaipur.json"))
        return cat, m, HEAT
    cat = json.load(open(EXP / "categories" / "dentists.json"))
    m = json.load(open(EXP / "merchants" / "m_001_drmeera_dentist_delhi.json"))
    t = json.load(open(EXP / "triggers" / "trg_001_research_digest_dentists.json"))
    return cat, m, t


def _item(store, kind="research"):
    cat, m, t = _ctx(kind)
    return pipeline.make_item(store, cat, m, t, None, None, set(), [])


def _run(monkeypatch, fake_writer, fake_critic=None, deadline=6.0, kind="event"):
    pipeline.CACHE.clear()
    store = Store(db_path="")
    it = _item(store, kind)

    async def fake(system, prompt, role="writer", timeout=None, max_tokens=0):
        if role == "writer":
            return await fake_writer(prompt)
        return await fake_critic(prompt) if fake_critic else None

    monkeypatch.setattr(POOL, "complete_json", fake)
    monkeypatch.setattr(type(POOL), "enabled", property(lambda self: True))
    monkeypatch.setattr(POOL, "capacity", lambda role="writer": 10)
    asyncio.run(pipeline.compose_items([it], time.monotonic() + deadline))
    return it


def _id(prompt):
    return json.loads(prompt.split("Items:\n", 1)[1])[0]["id"]


def test_polish_rewrite_used_for_stiff_family(monkeypatch):
    captured = {}

    async def writer(prompt):
        captured["base"] = json.loads(prompt.split("Items:\n", 1)[1])[0]["baseline"]
        return {"items": [{"id": _id(prompt), "bodies": [
            "Ramesh, Jaipur mein 3 din tak 44°C ki garmi hai, IMD orange alert ke saath. ORS aur sunscreen counter pe aage rakhein, "
            "cold/cough peeche. 'Free Home Delivery > ₹499' ke saath customers ke liye ek quick update bhej doon?"]}]}
    it = _run(monkeypatch, writer)
    assert it.chosen.source == "llm", pipeline.REJECTS[-1:]


def test_research_family_keeps_template_without_llm_call(monkeypatch):
    calls = []

    async def writer(prompt):
        calls.append(prompt)
        return None
    it = _run(monkeypatch, writer, kind="research")
    assert it.chosen.source == "template" and not calls


def test_fabricated_rewrite_rejected(monkeypatch):
    async def writer(prompt):
        return {"items": [{"id": _id(prompt), "bodies": [
            "Ramesh, 87% of Jaipur pharmacies already stock ORS per Dr. Kapoor. Update bhej doon?"]}]}
    it = _run(monkeypatch, writer)
    assert it.chosen.source == "template"


def test_slow_llm_falls_back_within_deadline(monkeypatch):
    async def writer(prompt):
        await asyncio.sleep(10)
        return None
    t0 = time.monotonic()
    it = _run(monkeypatch, writer, deadline=3.0)
    assert time.monotonic() - t0 < 3.5 and it.chosen.source == "template"


def test_added_fact_in_polish_rejected(monkeypatch):
    async def writer(prompt):
        return {"items": [{"id": _id(prompt), "bodies": [
            "Ramesh, Jaipur 44°C pe hai, 3 din, IMD orange alert — aapke 240 chronic-Rx customers ke liye ORS stock karein. Update bhej doon?"]}]}
    it = _run(monkeypatch, writer)
    assert it.chosen.source == "template"


def test_critic_picks_best(monkeypatch):
    async def writer(prompt):
        return {"items": [{"id": _id(prompt), "bodies": [
            "Ramesh, Jaipur mein 3 din tak 44°C ki garmi hai, IMD orange alert ke saath. ORS aur sunscreen counter pe aage rakhein, "
            "cold/cough peeche. 'Free Home Delivery > ₹499' ke saath customers ke liye ek quick update bhej doon?"]}]}

    async def critic(prompt):
        rows = json.loads(prompt)
        return {"scores": [{"id": rows[0]["id"], "candidate": 0, "total": 38}, {"id": rows[0]["id"], "candidate": 1, "total": 46}]}
    it = _run(monkeypatch, writer, critic)
    assert it.chosen.source == "llm"


def test_non_latin_script_rejected(monkeypatch):
    async def writer(prompt):
        return {"items": [{"id": _id(prompt), "bodies": [
            "Ramesh, Jaipur mein 44°C ka IMD orange alert chal raha hai اگلے 3 din ke liye. ORS aur sunscreen aage rakhein. Update bhej doon?"]}]}
    it = _run(monkeypatch, writer)
    assert it.chosen.source == "template"
    assert any("non_latin_script" in r["violations"] for r in pipeline.REJECTS)
