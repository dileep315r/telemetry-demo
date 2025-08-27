#!/usr/bin/env python3
import argparse
import csv
import os
import sys
import time
from datetime import datetime
from typing import List, Dict, Any

import httpx

"""
Latency Report Script

Functions:
1. Fetch rolling summary from metrics collector (/summary).
2. Fetch recent raw events (/events) and write to CSV.
3. Compute offline aggregate (avg, p50, p95, p99) from fetched events (round_trip_ms).
4. Optionally run in watch mode to periodically print updated stats.
5. Optionally generate a simple ASCII sparkline of recent round trips.

Usage:
  python scripts/latency_report.py --metrics-url http://localhost:9100 --csv out.csv
  python scripts/latency_report.py --watch 5
"""

def percentile(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    if p <= 0:
        return values[0]
    if p >= 100:
        return values[-1]
    k = (len(values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return d0 + d1

def ascii_sparkline(values: List[float], width: int = 40) -> str:
    if not values:
        return ""
    take = values[-width:]
    mn = min(take)
    mx = max(take)
    chars = "▁▂▃▄▅▆▇█"
    span = mx - mn if mx > mn else 1
    out = []
    for v in take:
        idx = int((v - mn) / span * (len(chars) - 1))
        out.append(chars[idx])
    return "".join(out)

def fetch(metrics_url: str) -> Dict[str, Any]:
    with httpx.Client(timeout=3.0) as client:
        summary = client.get(f"{metrics_url}/summary").json()
        events = client.get(f"{metrics_url}/events").json()
    return {"summary": summary, "events": events}

def offline_stats(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    rtts = sorted([e["round_trip_ms"] for e in events if isinstance(e.get("round_trip_ms"), (int, float))])
    if not rtts:
        return {}
    return {
        "count": len(rtts),
        "avg": sum(rtts) / len(rtts),
        "p50": percentile(rtts, 50),
        "p95": percentile(rtts, 95),
        "p99": percentile(rtts, 99),
        "min": rtts[0],
        "max": rtts[-1],
    }

def write_csv(path: str, events: List[Dict[str, Any]]):
    if not events:
        print("No events to write")
        return
    keys = sorted({k for e in events for k in e.keys()})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for e in events:
            w.writerow(e)
    print(f"Wrote {len(events)} events to {path}")

def print_report(data: Dict[str, Any], show_events: bool, sparkline: bool):
    summary = data["summary"]
    events = data["events"]["events"]
    off = offline_stats(events)
    print(f"\n=== Latency Report {datetime.utcnow().isoformat()}Z ===")
    print("Rolling Window Summary (/summary):")
    print(summary)
    if off:
        print("Offline recomputed from raw events (/events):")
        print({k: round(v, 2) if isinstance(v, float) else v for k, v in off.items()})
    if sparkline and off:
        rtts = [e["round_trip_ms"] for e in events if "round_trip_ms" in e]
        print("Sparkline (most recent):", ascii_sparkline(rtts))
    if show_events:
        print("\nRecent Events:")
        for e in events[-10:]:
            print(e)

def parse_args():
    ap = argparse.ArgumentParser(description="Latency aggregation & report tool")
    ap.add_argument("--metrics-url", default=os.getenv("METRICS_URL", "http://localhost:9100"), help="Base URL for metrics service")
    ap.add_argument("--csv", help="Write raw events to CSV path")
    ap.add_argument("--show-events", action="store_true", help="Print last 10 events")
    ap.add_argument("--watch", type=int, help="Watch mode interval seconds")
    ap.add_argument("--sparkline", action="store_true", help="Display ASCII sparkline")
    return ap.parse_args()

def main():
    args = parse_args()
    interval = args.watch
    try:
        while True:
            data = fetch(args.metrics_url)
            print_report(data, show_events=args.show_events, sparkline=args.sparkline)
            if args.csv:
                write_csv(args.csv, data["events"]["events"])
            if not interval:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("Interrupted.")

if __name__ == "__main__":
    main()
