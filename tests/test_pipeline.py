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


def _ctx():
    cat = json.load(open(EXP / "categories" / "dentists.json"))
    m = json.load(open(EXP / "merchants" / "m_001_drmeera_dentist_delhi.json"))
    t = json.load(open(EXP / "triggers" / "trg_001_research_digest_dentists.json"))
    return cat, m, t


def _item(store):
    cat, m, t = _ctx()
    return pipeline.make_item(store, cat, m, t, None, None, set(), [])


def _run(monkeypatch, fake_writer, fake_critic=None, deadline=6.0):
    pipeline.CACHE.clear()
    store = Store(db_path="")
    it = _item(store)

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


def test_grounded_rewrite_is_used(monkeypatch):
    async def writer(prompt):
        return {"items": [{"id": _id(prompt), "bodies": [
            "Dr. Meera, JIDA Oct 2026, p.14 mein ek 2,100-patient trial aaya hai jo aapke 124 high-risk adult patients ke liye kaam ka hai: "
            "3-month fluoride varnish recall se caries recurrence 38% kam hua vs 6-month. 2-min summary aur patient WhatsApp draft bhej doon?"]}]}
    it = _run(monkeypatch, writer)
    assert it.chosen.source == "llm" and "2,100-patient" in it.chosen.body


def test_fabricated_rewrite_rejected(monkeypatch):
    async def writer(prompt):
        return {"items": [{"id": _id(prompt), "bodies": [
            "Dr. Meera, 87% of Delhi dentists already switched to 3-month recall per Dr. Kapoor. Want the summary?"]}]}
    it = _run(monkeypatch, writer)
    assert it.chosen.source == "template"


def test_slow_llm_falls_back_within_deadline(monkeypatch):
    async def writer(prompt):
        await asyncio.sleep(10)
        return None
    t0 = time.monotonic()
    it = _run(monkeypatch, writer, deadline=3.0)
    assert time.monotonic() - t0 < 3.5 and it.chosen.source == "template"


def test_critic_picks_best(monkeypatch):
    async def writer(prompt):
        return {"items": [{"id": _id(prompt), "bodies": [
            "Dr. Meera, JIDA Oct 2026, p.14 ka naya 2,100-patient trial: 3-month fluoride recall se caries 38% zyada kam hua vs 6-month — "
            "aapke 124 high-risk adult patients ke liye relevant hai. Summary aur ek forward karne layak patient WhatsApp bhej doon?"]}]}

    async def critic(prompt):
        rows = json.loads(prompt)
        return {"scores": [{"id": rows[0]["id"], "candidate": 0, "total": 38}, {"id": rows[0]["id"], "candidate": 1, "total": 46}]}
    it = _run(monkeypatch, writer, critic)
    assert it.chosen.source == "llm"
