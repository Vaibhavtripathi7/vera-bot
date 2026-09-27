"""Versioned context store + conversation/suppression state, with SQLite write-through.

State is one session: it auto-wipes after SESSION_IDLE_RESET seconds of inactivity (a new judge
run must never see a previous run's versions/suppressions - spec R3). SQLite only exists so a
crash/restart *inside* a session loses nothing.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from . import config
from .util import iso_now

SCOPES = ("category", "merchant", "customer", "trigger")


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: str | None
    customer_id: str | None = None
    trigger_id: str | None = None
    family: str | None = None
    send_as: str = "vera"
    status: str = "open"                 # open | waiting | ended
    committed: bool = False
    language: str | None = None
    deliverable: str | None = None       # what we offered to do
    bodies: list[str] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    auto_replies: int = 0
    unanswered: int = 0
    offtopic: int = 0
    meta: dict = field(default_factory=dict)


class Store:
    def __init__(self, db_path: str | None = None):
        self.lock = threading.RLock()
        self.db_path = db_path if db_path is not None else config.DB_PATH
        self.started = time.time()
        self._reset_memory()
        self.db = None
        if self.db_path:
            self.db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            self._restore()

    # ---------- session handling ----------
    def _reset_memory(self):
        self.contexts: dict[tuple[str, str], dict] = {}
        self.convs: dict[str, Conversation] = {}
        self.suppressed: set[str] = set()
        self.merchant_state: dict[str, dict] = {}   # opted_out, hostile, auto_texts, sent_facts, bodies
        self.sent_triggers: set[str] = set()
        self.last_activity = time.time()
        self.generation = 0                           # bumps on any context change (cache keys)

    def touch(self):
        with self.lock:
            idle = time.time() - self.last_activity
            if config.SESSION_IDLE_RESET and idle > config.SESSION_IDLE_RESET and (self.contexts or self.convs):
                self.wipe()
            self.last_activity = time.time()

    def wipe(self):
        with self.lock:
            self._reset_memory()
            if self.db:
                self.db.execute("DELETE FROM kv")

    # ---------- persistence ----------
    def _persist(self, key: str, value):
        if self.db:
            self.db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    def _restore(self):
        rows = self.db.execute("SELECT k, v FROM kv").fetchall()
        meta = {}
        for k, v in rows:
            val = json.loads(v)
            if k.startswith("ctx|"):
                _, scope, cid = k.split("|", 2)
                self.contexts[(scope, cid)] = val
            elif k.startswith("conv|"):
                self.convs[k[5:]] = Conversation(**val)
            elif k.startswith("mstate|"):
                self.merchant_state[k[7:]] = val
            elif k == "meta":
                meta = val
        self.suppressed = set(meta.get("suppressed", []))
        self.sent_triggers = set(meta.get("sent_triggers", []))
        self.last_activity = meta.get("last_activity", time.time())

    def save_meta(self):
        self._persist("meta", {"suppressed": sorted(self.suppressed), "sent_triggers": sorted(self.sent_triggers),
                               "last_activity": self.last_activity})

    # ---------- contexts ----------
    def put(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[int, dict]:
        with self.lock:
            key = (scope, context_id)
            cur = self.contexts.get(key)
            if cur and cur["version"] >= version:
                return 409, {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
            self.contexts[key] = {"version": version, "payload": payload}
            self.generation += 1
            self._persist(f"ctx|{scope}|{context_id}", self.contexts[key])
            return 200, {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": iso_now()}

    def get(self, scope: str, context_id: str | None) -> dict | None:
        if not context_id:
            return None
        rec = self.contexts.get((scope, context_id))
        return rec["payload"] if rec else None

    def version(self, scope: str, context_id: str | None) -> int:
        rec = self.contexts.get((scope, context_id)) if context_id else None
        return rec["version"] if rec else 0

    def counts(self) -> dict:
        out = {s: 0 for s in SCOPES}
        for (scope, _), _v in self.contexts.items():
            out[scope] = out.get(scope, 0) + 1
        return out

    def customers_of(self, merchant_id: str) -> list[dict]:
        return [r["payload"] for (s, _), r in self.contexts.items()
                if s == "customer" and r["payload"].get("merchant_id") == merchant_id]

    # ---------- conversations / merchant state ----------
    def conv(self, conversation_id: str) -> Conversation | None:
        return self.convs.get(conversation_id)

    def save_conv(self, conv: Conversation):
        with self.lock:
            self.convs[conv.conversation_id] = conv
            self._persist(f"conv|{conv.conversation_id}", conv.__dict__)

    def mstate(self, merchant_id: str | None) -> dict:
        key = merchant_id or "_unknown"
        st = self.merchant_state.setdefault(key, {"opted_out": False, "hostile": False, "auto_texts": [],
                                                   "used_insights": [], "bodies": [], "unanswered": 0})
        return st

    def save_mstate(self, merchant_id: str | None):
        key = merchant_id or "_unknown"
        self._persist(f"mstate|{key}", self.merchant_state.get(key, {}))

    def uptime(self) -> int:
        return int(time.time() - self.started)
