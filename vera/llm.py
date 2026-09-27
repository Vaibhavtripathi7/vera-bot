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
        self.last_error = ""

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
    """Gemini REST. Thinking must be off/minimal for latency; models differ in which knob they accept
    (3.x-lite: thinkingLevel=minimal only; 3.8-flash: thinkingBudget=0 only), so we adapt on a 400 and remember."""
    STYLES = ({"thinkingLevel": "minimal"}, {"thinkingBudget": 0}, None)

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.style = 1 if ("2.5" in self.model or "3.8" in self.model) else 0

    async def call(self, client, system, prompt, timeout, max_tokens):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        for _ in range(len(self.STYLES)):
            gen = {"temperature": 0, "seed": 7, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}
            if self.STYLES[self.style]:
                gen["thinkingConfig"] = self.STYLES[self.style]
            body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": gen}
            r = await client.post(url, json=body, headers={"x-goog-api-key": self.key}, timeout=timeout)
            if r.status_code == 400 and ("hinking" in r.text or "invalid argument" in r.text.lower()):
                self.style = (self.style + 1) % len(self.STYLES)
                continue
            r.raise_for_status()
            data = r.json()
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        raise RuntimeError("no accepted thinking config")


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
            shared: dict[str, Gemini] = {}                 # one Provider (one bucket) per model, even if used in both roles
            for role, models, rpm, rpd in (("w", config.GEMINI_WRITER_MODELS, config.GEMINI_WRITER_RPM, config.GEMINI_WRITER_RPD),
                                           ("c", config.GEMINI_CRITIC_MODELS, config.GEMINI_CRITIC_RPM, config.GEMINI_CRITIC_RPD)):
                for m in models:                           # separate per-model quotas -> pooled capacity
                    if m not in shared:
                        lim = config.GEMINI_LIMITS.get(m, (rpm, rpd))
                        shared[m] = Gemini(f"gemini:{m}", m, config.GEMINI_API_KEY, lim[0], lim[1])
                    (self.writers if role == "w" else self.critics).append(shared[m])
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
        deadline = time.monotonic() + timeout
        for p in provs:
            left = deadline - time.monotonic()
            if left < 1.5:                    # not enough time for another attempt
                break
            per_try = min(4.5, left - 0.3)
            if not p.healthy or not p.bucket.available():
                continue
            p.bucket.take()
            self.stats["calls"] += 1
            try:
                text = await asyncio.wait_for(p.call(self.client(), system, prompt, per_try, max_tokens), per_try + 0.2)
                data = parse_json(text)
                if data is None:
                    raise ValueError("unparseable json")
                p.record(True)
                self.stats["ok"] += 1
                self.cache[key] = data
                return data
            except Exception as e:  # noqa: BLE001 - any provider error just falls through
                p.record(False)
                resp = getattr(e, "response", None)
                status = getattr(resp, "status_code", None)
                detail = ""
                try:
                    detail = (resp.json().get("error") or {}).get("message", "")[:160] if resp is not None else str(e)[:160]
                except Exception:  # noqa: BLE001
                    detail = str(e)[:160]
                p.last_error = f"{status or type(e).__name__}: {detail}"
                if status in (429, 503):          # quota / overload: stop hammering this model for a while
                    p.open_until = time.time() + (60 if status == 429 else 20)
                elif isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
                    p.open_until = time.time() + 120  # slow right now: don't let it eat the next tick's budget
                self.stats["fail"] += 1
                log.warning("llm %s failed: %s", p.name, type(e).__name__)
        return None

    def status(self) -> dict:
        return {"enabled": self.enabled, **self.stats,
                "key_present": bool(config.GEMINI_API_KEY), "key_len": len(config.GEMINI_API_KEY),
                "providers": {p.name: {**p.bucket.status(), "healthy": p.healthy, "last_error": p.last_error}
                              for p in {*self.writers, *self.critics}}}


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
