"""Concurrency smoke test: bursts of mixed requests at >=10 req/s against a running bot."""
import asyncio, json, sys, time, glob
import httpx

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"


async def main():
    ts = [json.load(open(f)) for f in sorted(glob.glob("expanded/triggers/*.json"))][:40]
    lat, errs = [], 0
    async with httpx.AsyncClient(timeout=30) as c:
        async def one(method, path, body=None):
            nonlocal errs
            t0 = time.monotonic()
            try:
                r = await c.request(method, URL + path, json=body)
                if r.status_code >= 500:
                    errs += 1
            except httpx.HTTPError:
                errs += 1
            lat.append(time.monotonic() - t0)
        jobs = []
        for i, t in enumerate(ts):
            jobs.append(one("POST", "/v1/context", {"scope": "trigger", "context_id": t["id"] + "_load", "version": 1, "payload": {**t, "id": t["id"] + "_load"}}))
            jobs.append(one("GET", "/v1/healthz"))
            jobs.append(one("POST", "/v1/reply", {"conversation_id": f"load_{i}", "merchant_id": t["merchant_id"], "from_role": "merchant", "message": "yes please", "turn_number": 2}))
        jobs.append(one("POST", "/v1/tick", {"now": "2026-09-28T10:00:00Z", "available_triggers": [t["id"] + "_load" for t in ts[:20]]}))
        t0 = time.monotonic()
        await asyncio.gather(*jobs)
        dt = time.monotonic() - t0
    lat.sort()
    print(f"{len(lat)} requests in {dt:.2f}s ({len(lat)/dt:.0f} req/s), p50={lat[len(lat)//2]*1000:.0f}ms p99={lat[int(len(lat)*.99)-1]*1000:.0f}ms errors={errs}")

asyncio.run(main())
