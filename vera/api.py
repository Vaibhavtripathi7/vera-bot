"""HTTP surface: the 5 judge endpoints (+ teardown). Bodies are parsed from raw bytes so a missing
Content-Type header never causes a 422, and no handler can surface a 500 to the judge.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .conversation import ReplyEngine
from .llm import POOL
from .scheduler import Scheduler
from .store import SCOPES, Store
from .validator import CASE_STUDY_BODIES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vera.api")

app = FastAPI(title="Vera bot", docs_url=None, redoc_url=None)
STORE = Store()
SCHED = Scheduler(STORE)
REPLIES = ReplyEngine(STORE)
LOCK = asyncio.Lock()


def _load_case_studies():
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "case-studies.md")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return
    import re
    for block in re.findall(r"```\n([\s\S]*?)```", text):
        if len(block) > 80:
            CASE_STUDY_BODIES.append(" ".join(block.split()))


_load_case_studies()


async def _json(request: Request):
    raw = await request.body()
    if len(raw) > config.MAX_BODY_BYTES:
        return None, "payload_too_large"
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    return data, None


@app.get("/")
@app.head("/")
async def root():
    return {"service": "vera-bot", "status": "ok"}


@app.get("/v1/healthz")
@app.head("/v1/healthz")
async def healthz():
    STORE.touch()
    return {"status": "ok", "uptime_seconds": STORE.uptime(), "contexts_loaded": STORE.counts()}


@app.get("/v1/metadata")
async def metadata():
    import time as _t
    STORE.last_metadata = _t.time()
    return {
        "team_name": config.TEAM_NAME,
        "team_members": config.TEAM_MEMBERS,
        "model": f"{config.GEMINI_WRITER_MODEL} (writer) + {config.GEMINI_CRITIC_MODEL} (critic); deterministic template core" if POOL.enabled
        else "deterministic grounded composer (no LLM in hot path)",
        "approach": "fact-sheet + insight engine -> per-trigger playbooks (EN/Hinglish) -> validator gate -> "
                    "batched LLM rewrite + critic (best-of-N, deadline-bounded) -> rule-based reply state machine with artifacts",
        "contact_email": config.CONTACT_EMAIL,
        "version": config.COMPOSER_VERSION,
        "submitted_at": config.SUBMITTED_AT,
    }


@app.post("/v1/context")
async def context(request: Request):
    data, err = await _json(request)
    if err:
        return JSONResponse({"accepted": False, "reason": err, "details": "body must be a JSON object under 600KB"}, 400)
    STORE.touch()
    scope, cid, payload = data.get("scope"), data.get("context_id"), data.get("payload")
    if scope not in SCOPES:
        return JSONResponse({"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {list(SCOPES)}"}, 400)
    if not isinstance(cid, str) or not cid or not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_payload", "details": "context_id (str) and payload (object) required"}, 400)
    try:
        version = int(data.get("version", 1))
    except (TypeError, ValueError):
        return JSONResponse({"accepted": False, "reason": "invalid_version", "details": "version must be an integer"}, 400)
    code, body = STORE.put(scope, cid, version, payload)
    return JSONResponse(body, code)


@app.post("/v1/tick")
async def tick(request: Request):
    t0 = time.monotonic()
    data, err = await _json(request)
    if err:
        return JSONResponse({"actions": []})
    STORE.touch()
    ids = [str(x) for x in (data.get("available_triggers") or []) if x]
    try:
        async with LOCK:
            out = await asyncio.wait_for(SCHED.tick(data.get("now"), ids), timeout=config.TICK_DEADLINE + 3)
    except Exception as e:  # noqa: BLE001
        log.exception("tick failed: %s", e)
        out = {"actions": []}
    log.info("tick: %d listed -> %d actions in %.2fs", len(ids), len(out["actions"]), time.monotonic() - t0)
    return out


@app.post("/v1/reply")
async def reply(request: Request):
    t0 = time.monotonic()
    data, err = await _json(request)
    if err:
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Unparseable reply payload; waiting."}
    STORE.touch()
    try:
        async with LOCK:
            act, conv, klass = REPLIES.handle(data)
        out = act.to_json()
        if out.get("action") == "send" and not out.get("body"):
            out = {"action": "wait", "wait_seconds": 1800, "rationale": "Nothing grounded to add; waiting."}
    except Exception as e:  # noqa: BLE001
        log.exception("reply failed: %s", e)
        out = {"action": "wait", "wait_seconds": 1800, "rationale": "Temporary issue composing reply; backing off."}
        klass = "error"
    log.info("reply: class=%s -> %s in %.2fs", klass, out["action"], time.monotonic() - t0)
    return out


@app.post("/v1/teardown")
async def teardown():
    STORE.wipe()
    return {"status": "wiped"}


@app.get("/v1/debug/llm")
async def debug_llm():
    return POOL.status()
