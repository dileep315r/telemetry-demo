import asyncio
import os
import time
import uuid
import argparse
import json
import math
import random
from typing import List, Dict, Optional
import httpx
import numpy as np

"""
Load Test Script

Goals:
- Simulate N concurrent callers generating short speech bursts.
- Measure round trip latency (speech start -> agent playback) as reported by metrics collector OR synthetic fallback.
- Optionally push synthetic latency events if real agent/audio path not fully integrated yet (dry-run).

Approach:
1. Each simulated caller (task) obtains /token from orchestrator for a shared or per-call room.
2. (Future) Connect to LiveKit via official SDK / WebRTC to inject audio. For prototype we just:
   - Generate a pseudo 'speech' burst start timestamp.
   - Wait a configurable simulated processing delay (or poll metrics endpoint if real system running).
3. Collect latencies and aggregate (avg, p50, p95, p99) locally.
4. Optionally post events directly to METRICS_ENDPOINT to exercise metrics pipeline.

Environment Variables:
  ORCH_PUBLIC_URL (default http://localhost:8000)
  METRICS_ENDPOINT (default http://localhost:9100/ingest)
  LOADTEST_ROOM_PREFIX (default lt-)
  LOADTEST_PHRASE (default "testing one two three")
  LIVEKIT_HOST (unused in stub; placeholder for future direct media injection)
"""

DEFAULT_SIM_AGENT_LATENCY_MS = 420  # baseline synthetic mean
DEFAULT_JITTER_MS = 120

# Run-level variability knobs (to avoid identical aggregate stats across runs)
RUN_ID = os.getenv("RUN_ID", uuid.uuid4().hex[:8])
BASE_LATENCY_MS = int(os.getenv("BASE_LATENCY_MS", str(DEFAULT_SIM_AGENT_LATENCY_MS)))
RUN_DRIFT_MS = int(os.getenv("RUN_DRIFT_MS", "30"))  # each caller adds a random offset in [-RUN_DRIFT_MS, +RUN_DRIFT_MS]
JITTER_SCALE = float(os.getenv("JITTER_SCALE", "1.0"))  # scale jitter to further vary distribution

ORCH_URL = os.getenv("ORCH_PUBLIC_URL", "http://localhost:8000")
METRICS_ENDPOINT = os.getenv("METRICS_ENDPOINT", "http://localhost:9100/ingest")
ROOM_PREFIX = os.getenv("LOADTEST_ROOM_PREFIX", "lt-")
PHRASE = os.getenv("LOADTEST_PHRASE", "testing one two three")

def pct(values: List[float], p: float) -> float:
    if not values:
        return math.nan
    k = (len(values) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[f] * (c - k) + values[c] * (k - f)

async def fetch_token(client: httpx.AsyncClient, room: str, identity: str) -> str:
    resp = await client.post(f"{ORCH_URL}/token", json={
        "room": room,
        "identity": identity,
        "publish": True,
        "subscribe": True
    })
    resp.raise_for_status()
    return resp.json()["token"]

async def push_metric(client: httpx.AsyncClient, event: Dict):
    try:
        await client.post(METRICS_ENDPOINT, json=event, timeout=1.0)
    except Exception:
        pass

async def simulate_caller(index: int,
                          room: str,
                          identity: str,
                          bursts: int,
                          phrase: str,
                          synthetic: bool,
                          post_metrics: bool,
                          metrics_prefix: str,
                          results: List[float],
                          deterministic: bool,
                          seed: Optional[int] = None):
    if deterministic:
        rng = np.random.default_rng(seed if seed is not None else index)
    else:
        rng = np.random.default_rng()
    # Per-caller drift so each run's aggregate metrics shift
    caller_drift = random.randint(-RUN_DRIFT_MS, RUN_DRIFT_MS)
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            token = await fetch_token(client, room, identity)
        except Exception as e:
            print(f"[caller {index}] token fetch failed: {e}")
            return
        # NOTE: Would connect to LiveKit using token here.
        for b in range(bursts):
            speech_start = time.time()
            # Synthetic latency generation
            if synthetic:
                base = BASE_LATENCY_MS + caller_drift
                jitter = rng.normal(0, (DEFAULT_JITTER_MS * JITTER_SCALE) / 2)
                latency_ms = max(120, base + jitter)
                await asyncio.sleep(latency_ms / 1000.0)
            else:
                # In a real integration, we would monitor /events or track callback from media.
                # Placeholder wait approximating real pipeline
                await asyncio.sleep(0.4)
                latency_ms = 400.0
            round_trip = latency_ms
            results.append(round_trip)
            if post_metrics:
                event = {
                    "type": "latency_turn",
                    "room": room,
                    "identity": identity,
                    "timestamp": int(time.time() * 1000),
                    "speech_start_ms": int(speech_start * 1000),
                    "round_trip_ms": int(round_trip),
                    metrics_prefix + "simulated": True
                }
                await push_metric(client, event)

async def run_load(concurrency: int,
                   bursts: int,
                   shared_room: bool,
                   synthetic: bool,
                   post_metrics: bool,
                   phrase: str,
                   deterministic: bool):
    tasks = []
    results: List[float] = []
    room_base = ROOM_PREFIX + uuid.uuid4().hex[:6]
    start = time.time()
    for i in range(concurrency):
        room = room_base if shared_room else f"{room_base}-{i}"
        identity = f"caller-{i}"
        tasks.append(simulate_caller(
            index=i,
            room=room,
            identity=identity,
            bursts=bursts,
            phrase=phrase,
            synthetic=synthetic,
            post_metrics=post_metrics,
            metrics_prefix="synthetic_" if synthetic else "",
            results=results,
            deterministic=deterministic,
            seed=i if deterministic else None
        ))
    await asyncio.gather(*tasks)
    elapsed = time.time() - start
    values = sorted(results)
    count = len(values)
    if count == 0:
        print("No results.")
        return
    avg = sum(values) / count
    print(f"Completed {count} turns in {elapsed:.2f}s "
          f"(concurrency={concurrency}, bursts={bursts})")
    print(f"avg={avg:.2f}ms p50={pct(values,50):.2f}ms "
          f"p95={pct(values,95):.2f}ms p99={pct(values,99):.2f}ms max={values[-1]:.2f}ms")

def parse_args():
    ap = argparse.ArgumentParser(description="Concurrent call load tester")
    ap.add_argument("--concurrency", type=int, default=10, help="Number of concurrent simulated callers")
    ap.add_argument("--bursts", type=int, default=2, help="Speech bursts per caller")
    ap.add_argument("--shared-room", action="store_true", help="Use a single shared room")
    ap.add_argument("--real", action="store_true", help="Attempt real (non-synthetic) latency mode (placeholder)")
    ap.add_argument("--no-metrics", action="store_true", help="Do not post synthetic metrics")
    ap.add_argument("--phrase", type=str, default=PHRASE, help="Phrase to simulate")
    ap.add_argument("--deterministic", action="store_true", help="Deterministic seeded RNG (for reproducible runs)")
    return ap.parse_args()

def main():
    args = parse_args()
    print(f"[loadtest] RUN_ID={RUN_ID} base_latency={BASE_LATENCY_MS}ms drift=±{RUN_DRIFT_MS}ms "
          f"jitter={DEFAULT_JITTER_MS}ms scale={JITTER_SCALE} synthetic={not args.real} "
          f"concurrency={args.concurrency} bursts={args.bursts} shared_room={args.shared_room}")
    asyncio.run(run_load(
        concurrency=args.concurrency,
        bursts=args.bursts,
        shared_room=args.shared_room,
        synthetic=not args.real,
        post_metrics=not args.no_metrics,
        phrase=args.phrase,
        deterministic=args.deterministic
    ))

if __name__ == "__main__":
    main()
