import time
import math
import threading
from typing import List, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
import os
import statistics

"""
Latency Collector Service
Receives JSON events (POST /ingest) from agent workers.

Expected event schema (example):
{
  "type": "latency_turn",
  "room": "call-123",
  "identity": "pstn-1234",
  "timestamp": 1710000000000,
  "speech_start_ms": 1710000000123,
  "stt_first_partial_ms": 1710000000200,
  "stt_final_ms": 1710000000350,
  "agent_decision_ms": 1710000000365,
  "tts_first_byte_ms": 1710000000500,
  "playback_start_ms": 1710000000500,
  "round_trip_ms": 377
}

We store events in an in-memory ring buffer (window-based retention).
Provide:
  GET /summary -> aggregate stats (avg, p50, p95, p99) over window for round_trip_ms
  GET /events  -> raw recent events (limited)
Environment variables:
  METRICS_SUMMARY_PORT (default 9100)
  LATENCY_WINDOW_SECONDS (default 60)
"""

WINDOW_SECONDS = int(os.getenv("LATENCY_WINDOW_SECONDS", "60"))
MAX_EVENTS_RETURN = 200

app = FastAPI(title="LatencyCollector", version="0.1.0")

_lock = threading.Lock()
_events: List[Dict[str, Any]] = []

def _prune(now_ms: int):
    cutoff = now_ms - (WINDOW_SECONDS * 1000)
    # Keep only events with speech_start_ms or timestamp >= cutoff
    global _events
    _events = [e for e in _events if (e.get("speech_start_ms") or e.get("timestamp", 0)) >= cutoff]

def _percentile(data: List[float], pct: float) -> float:
    if not data:
        return math.nan
    k = (len(data) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    d0 = data[f] * (c - k)
    d1 = data[c] * (k - f)
    return d0 + d1

@app.post("/ingest")
async def ingest(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    now_ms = int(time.time() * 1000)
    with _lock:
        _events.append(payload)
        _prune(now_ms)
    return {"status": "ok"}

@app.get("/summary")
async def summary():
    now_ms = int(time.time() * 1000)
    with _lock:
        _prune(now_ms)
        rtts = [e["round_trip_ms"] for e in _events if isinstance(e.get("round_trip_ms"), (int, float))]
        rtts_sorted = sorted(rtts)
    count = len(rtts_sorted)
    if count == 0:
        return {
            "window_sec": WINDOW_SECONDS,
            "count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None
        }
    avg = sum(rtts_sorted) / count
    p50 = _percentile(rtts_sorted, 50)
    p95 = _percentile(rtts_sorted, 95)
    p99 = _percentile(rtts_sorted, 99)
    return {
        "window_sec": WINDOW_SECONDS,
        "count": count,
        "avg_ms": round(avg, 2),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "p99_ms": round(p99, 2)
    }

@app.get("/events")
async def events():
    now_ms = int(time.time() * 1000)
    with _lock:
        _prune(now_ms)
        recent = list(_events)[-MAX_EVENTS_RETURN:]
    return {"count": len(recent), "events": recent}

@app.get("/health")
async def health():
    return {"status": "ok", "time": int(time.time())}

@app.get("/")
async def root():
    return {"service": "latency-collector", "endpoints": ["/ingest", "/summary", "/events", "/health"]}

if __name__ == "__main__":
    port = int(os.getenv("METRICS_SUMMARY_PORT", "9100"))
    uvicorn.run("latency_collector:app", host="0.0.0.0", port=port, reload=False)
