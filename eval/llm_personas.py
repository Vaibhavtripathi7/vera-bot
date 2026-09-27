"""An LLM plays the merchant (like the real judge's sub-LLM) for multi-turn replay checks."""
import asyncio, glob, json, os
import httpx
from fastapi.testclient import TestClient

os.environ.setdefault("VERA_DB_PATH", "")
from vera import api
from vera.llm import Gemini, parse_json

PERSONAS = [
    "an engaged owner who likes the idea and agrees after one clarifying question",
    "a busy owner whose phone sends a WhatsApp Business auto-reply every time (always reply with the same canned auto-reply text)",
    "a skeptical owner who asks where the numbers come from, then agrees",
    "an owner who is rude/abusive at first, then asks for help filing GST",
    "an owner who replies in Hinglish and says haan karo after the first message",
    "an owner who says they are not interested, politely",
    "an owner who asks the price/cost, then says ok go ahead, then asks what's next",
    "an owner who says call me tomorrow, busy now",
    "an owner who switches from English to Hindi mid-conversation and agrees",
    "an owner who asks an unrelated question about a loan, then comes back to the topic",
]


async def main():
    P = Gemini("persona", os.environ.get("PERSONA_MODEL", "gemini-3.6-flash"), os.environ["GEMINI_API_KEY"], 100, 1000)
    c = TestClient(api.app)
    E = "expanded"
    for f in glob.glob(f"{E}/categories/*.json"):
        d = json.load(open(f)); c.post("/v1/context", json={"scope": "category", "context_id": d["slug"], "version": 1, "payload": d})
    for f in glob.glob(f"{E}/merchants/*.json"):
        d = json.load(open(f)); c.post("/v1/context", json={"scope": "merchant", "context_id": d["merchant_id"], "version": 1, "payload": d})
    pairs = json.load(open(f"{E}/test_pairs.json"))["pairs"]
    tids = [p["trigger_id"] for p in pairs if not p["customer_id"]][:10]
    for t in tids:
        d = json.load(open(f"{E}/triggers/{t}.json")); c.post("/v1/context", json={"scope": "trigger", "context_id": t, "version": 1, "payload": d})
    acts = []
    for i in range(4):
        acts += c.post("/v1/tick", json={"now": "2026-09-28T10:00:00Z", "available_triggers": tids}).json()["actions"]
    async with httpx.AsyncClient() as h:
        for a, persona in zip(acts, PERSONAS):
            hist = [("Vera", a["body"])]
            print(f"\n==== {persona}\nVERA: {a['body']}")
            for turn in range(2, 6):
                prompt = ("You are role-playing an Indian small-business owner on WhatsApp. Persona: " + persona +
                          "\nConversation so far:\n" + "\n".join(f"{w}: {m}" for w, m in hist) +
                          '\nWrite ONLY your next WhatsApp reply (short, realistic). JSON: {"reply": "..."}')
                d = {}
                for attempt in range(4):
                    try:
                        d = parse_json(await P.call(h, "You write realistic short WhatsApp replies.", prompt, 30, 200)) or {}
                        break
                    except Exception:
                        await asyncio.sleep(15 * (attempt + 1))
                msg = d.get("reply", "ok")
                r = c.post("/v1/reply", json={"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"], "from_role": "merchant",
                                               "message": msg, "turn_number": turn}).json()
                print(f"MERCHANT: {msg}\n  -> {r['action'].upper()}: {r.get('body') or r.get('rationale')}")
                hist += [("Merchant", msg), ("Vera", r.get("body") or f"[{r['action']}]")]
                if r["action"] != "send":
                    break

asyncio.run(main())
