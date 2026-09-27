"""Exact merchant messages from the LLM-persona replay run (eval/out/personas.log) that exposed reply bugs."""
import os

os.environ["VERA_DB_PATH"] = ""
os.environ["LLM_ENABLED"] = "0"
os.environ["VERA_SESSION_IDLE_RESET"] = "0"

import pytest
from fastapi.testclient import TestClient

from vera import api, pipeline
from tests.test_api import push_all


@pytest.fixture(scope="module")
def client():
    api.STORE.wipe(); api.STORE.last_metadata = 0.0; pipeline.CACHE.clear()
    c = TestClient(api.app)
    push_all(c)
    return c


def start(c, tid):
    acts = c.post("/v1/tick", json={"now": "2026-09-28T10:00:00Z", "available_triggers": [tid]}).json()["actions"]
    assert acts, tid
    return acts[0]


def say(c, a, msg, turn=2):
    return c.post("/v1/reply", json={"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"], "from_role": "merchant",
                                     "message": msg, "turn_number": turn}).json()


def test_identity_question_answered(client):
    a = start(client, "trg_021_unverified_gbp_sunrise")
    r = say(client, a, "Aap kaun ho? Ye kya hai?")
    assert r["action"] == "send" and "Vera" in r["body"] and "magicpin" in r["body"]


def test_haggle_edit_acknowledged(client):
    a = start(client, "trg_013_corporate_thali_planning")
    r = say(client, a, "Arre Vera ji, ₹120 karo na 25+ ke liye aur delivery bhi free. Chalo theek hai, go ahead with the post.")
    assert r["action"] == "send" and "₹120" in r["body"]


def test_call_request_not_treated_as_later(client):
    a = start(client, "trg_020_summer_demand_shift")
    r = say(client, a, "Aap call pe samjha sakte ho kya? Main abhi thoda busy hoon.")
    assert r["action"] == "send" and "call" in r["body"].lower()


def test_abuse_then_calm_gst_stays_on_mission(client):
    a = start(client, "trg_056_competitor_opened_m_006_southindiancaf")
    r1 = say(client, a, "Abe dimag mat kha mera, pehle hi dhandha down hai. Mera GST return file karwa de jaldi, last date hai kal!")
    assert r1["action"] == "send" and ("maaf" in r1["body"].lower() or "sorry" in r1["body"].lower()) and "CA" in r1["body"]
    r2 = say(client, a, "Abe rona mat abhi, gussa nikal gaya mera. Chal chupchaap GST ka dekh, portal khul raha hai kya?", 3)
    assert r2["action"] == "send" and "CA" in r2["body"]


def test_loan_question_gets_loan_note_not_gst(client):
    a = start(client, "trg_096_curious_ask_due_m_006_southindiancaf")
    r = say(client, a, "Haan Thali hi chal raha hai... HDFC ya SBI me se kiska business loan ka interest rate kam hai? Baaki batao, kya post banana hai?")
    assert "loan" in r["body"].lower() and "gst" not in r["body"].lower()
    assert "Thali" in r["body"]                                     # post built from the merchant's own answer


def test_cost_question_answered_from_context(client):
    a = start(client, "trg_022_cde_webinar_dentists")
    r = say(client, a, "Cost kya hai iska? Ok go ahead. Next kya karna hai?")
    assert "₹500" in r["body"] and "confirm that detail" not in r["body"]


def test_followon_uses_artifact_not_unrelated_offer(client):
    a = start(client, "trg_016_kids_yoga_program_drafting")
    say(client, a, "haan bhej do", 2)
    say(client, a, "CONFIRM", 3)
    r = say(client, a, "haan dono bhej do", 4)
    assert "kids yoga" in r["body"].lower() and "First Month" not in r["body"]


def test_competitor_offer_quoted_as_is_and_catalog_offer_framed_as_suggestion(client):
    a = start(client, "trg_023_competitor_opened_dentist")
    assert "'Dental Cleaning @ ₹199' ke saath" in a["body"] or "leading with 'Dental Cleaning @ ₹199'" in a["body"]
    b = start(client, "trg_025_dormancy_glamour")          # Glamour has no active offers -> catalog offer must read as a suggestion
    assert "jaisa ek service+price offer" in b["body"] or "service+price offer like" in b["body"]
