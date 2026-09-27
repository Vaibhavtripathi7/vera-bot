"""LLM provider pool: Gemini (writer + critic) and Groq (fallback), with per-provider RPM/RPD
buckets, circuit breakers, timeouts and a response cache. Every call is optional: callers must
treat None as "no LLM available" and fall back to templates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque

import httpx

from . import config
from .util import sha

log = logging.getLogger("vera.llm")


class Bucket:
    def __init__(self, rpm: int, rpd: int):
        self.rpm, self.rpd = rpm, rpd
        self.minute: deque[float] = deque()
        self.day_count, self.day_start = 0, time.time()

    def available(self, reserve: int = 0) -> bool:
        now = time.time()
        while self.minute and now - self.minute[0] > 60:
            self.minute.popleft()
        if now - self.day_start > 86400:
            self.day_count, self.day_start = 0, now
        return len(self.minute) < self.rpm and self.day_count < self.rpd - reserve

    def take(self):
        self.minute.append(time.time())
        self.day_count += 1

    def status(self) -> dict:
        self.available()
        return {"rpm_used": len(self.minute), "rpm": self.rpm, "rpd_used": self.day_count, "rpd": self.rpd}


class Provider:
    def __init__(self, name: str, model: str, key: str, rpm: int, rpd: int):
        self.name, self.model, self.key = name, model, key
        self.bucket = Bucket(rpm, rpd)
        self.failures, self.open_until = 0, 0.0

    @property
    def healthy(self) -> bool:
        return bool(self.key) and time.time() >= self.open_until

    def record(self, ok: bool):
        if ok:
            self.failures = 0
        else:
            self.failures += 1
            if self.failures >= 3:
                self.open_until = time.time() + 60
                self.failures = 0

    async def call(self, client: httpx.AsyncClient, system: str, prompt: str, timeout: float, max_tokens: int) -> str:
        raise NotImplementedError


class Gemini(Provider):
    async def call(self, client, system, prompt, timeout, max_tokens):
        gen = {"temperature": 0, "seed": 7, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}
        if "2.5" in self.model:  # disable "thinking" - it adds seconds of latency
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": gen}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        r = await client.post(url, json=body, headers={"x-goog-api-key": self.key}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])


class Groq(Provider):
    async def call(self, client, system, prompt, timeout, max_tokens):
        body = {"model": self.model, "temperature": 0, "seed": 7, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        r = await client.post("https://api.groq.com/openai/v1/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {self.key}"}, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class Pool:
    def __init__(self):
        self.writers: list[Provider] = []
        self.critics: list[Provider] = []
        if config.GEMINI_API_KEY:
            self.writers.append(Gemini("gemini-writer", config.GEMINI_WRITER_MODEL, config.GEMINI_API_KEY,
                                       config.GEMINI_WRITER_RPM, config.GEMINI_WRITER_RPD))
            self.critics.append(Gemini("gemini-critic", config.GEMINI_CRITIC_MODEL, config.GEMINI_API_KEY,
                                       config.GEMINI_CRITIC_RPM, config.GEMINI_CRITIC_RPD))
        if config.GROQ_API_KEY:
            g = Groq("groq", config.GROQ_MODEL, config.GROQ_API_KEY, config.GROQ_RPM, config.GROQ_RPD)
            self.writers.append(g)
            self.critics.append(g)
        self.cache: dict[str, dict] = {}
        self._client: httpx.AsyncClient | None = None
        self.stats = {"calls": 0, "ok": 0, "fail": 0, "cache_hits": 0}

    @property
    def enabled(self) -> bool:
        return config.LLM_ENABLED and bool(self.writers)

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(http2=False, limits=httpx.Limits(max_connections=20))
        return self._client

    def capacity(self, role: str = "writer") -> int:
        """How many calls could start right now (used to size best-of-N)."""
        provs = self.writers if role == "writer" else self.critics
        n = 0
        for p in provs:
            if p.healthy:
                p.bucket.available()
                n += max(0, p.bucket.rpm - len(p.bucket.minute))
        return n

    async def complete_json(self, system: str, prompt: str, role: str = "writer", timeout: float | None = None,
                            max_tokens: int = 1200) -> dict | None:
        if not config.LLM_ENABLED:
            return None
        key = sha(role + "\x00" + system + "\x00" + prompt)
        if key in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[key]
        provs = self.writers if role == "writer" else self.critics
        timeout = timeout or config.LLM_TIMEOUT
        for p in provs:
            if not p.healthy or not p.bucket.available():
                continue
            p.bucket.take()
            self.stats["calls"] += 1
            try:
                text = await asyncio.wait_for(p.call(self.client(), system, prompt, timeout, max_tokens), timeout + 0.5)
                data = parse_json(text)
                if data is None:
                    raise ValueError("unparseable json")
                p.record(True)
                self.stats["ok"] += 1
                self.cache[key] = data
                return data
            except Exception as e:  # noqa: BLE001 - any provider error just falls through
                p.record(False)
                self.stats["fail"] += 1
                log.warning("llm %s failed: %s", p.name, type(e).__name__)
        return None

    def status(self) -> dict:
        return {"enabled": self.enabled, **self.stats,
                "providers": {p.name: {**p.bucket.status(), "healthy": p.healthy} for p in {*self.writers, *self.critics}}}


def parse_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


POOL = Pool()
