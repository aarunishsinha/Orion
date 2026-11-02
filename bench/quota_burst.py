"""
Verify the calendar worker quota is global across replicas.

Prerequisite (from the repo root):
    docker compose up -d --scale worker=3

Then:
    uv run --project servers/google_calendar python bench/quota_burst.py

Enqueues list_upcoming_events jobs directly through arq at a rate above the
quota (default 50 jobs, Poisson arrivals averaging 2/s, against a 10 per 10s
limit; --rate 0 sends them all at once). Without Google
credentials, admitted jobs fail later on the missing credentials.json; only
rejections by the rate limiter count against admission.

Reports admitted vs rejected, admissions per 10s window, the maximum seen in
any rolling 10s span (must be <= 10), and how many jobs each replica picked up.
"""
import argparse
import asyncio
import datetime
import os
import random
import re
import subprocess
from collections import Counter

from arq import create_pool
from arq.connections import RedisSettings

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RATE_LIMIT_KEY = "rate_limit:mcp_worker"
WINDOW_S = 10.0
LIMIT = 10

# docker compose log line: "worker-2  | 2026-11-02 10:00:00,123 - rate_limiter - INFO - Rate limit admitted: ..."
LOG_LINE = re.compile(
    r"^(?P<replica>\S+)\s+\|\s+(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) - rate_limiter - \w+ - "
    r"Rate limit (?P<outcome>admitted|exceeded)"
)


async def run_job(pool, delay: float):
    await asyncio.sleep(delay)
    job = await pool.enqueue_job("list_upcoming_events", 5)
    try:
        await job.result(timeout=60)
        return "admitted"
    except Exception as e:
        return "rejected" if "Rate limit exceeded" in str(e) else "admitted"


def worker_log_events(since: str) -> list[tuple[str, datetime.datetime, str]]:
    out = subprocess.run(
        ["docker", "compose", "logs", "worker", "--no-color", "--since", since],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    events = []
    for line in out.splitlines():
        m = LOG_LINE.match(line)
        if m:
            ts = datetime.datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S,%f")
            events.append((m["replica"], ts, m["outcome"]))
    return events


def max_in_rolling_window(times: list[datetime.datetime]) -> int:
    times = sorted(times)
    best, lo = 0, 0
    for hi, t in enumerate(times):
        while (t - times[lo]).total_seconds() >= WINDOW_S:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis", default="redis://localhost:6380/0")
    parser.add_argument("--jobs", type=int, default=50)
    parser.add_argument("--rate", type=float, default=2.0, help="jobs/sec; 0 = all at once")
    args = parser.parse_args()

    pool = await create_pool(RedisSettings.from_dsn(args.redis))
    await pool.delete(RATE_LIMIT_KEY)  # start from an empty window; touches no other keys

    since = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # Poisson arrivals: fixed spacing can phase-lock with arq's 0.5s poll interval
    # and send every job to the same replica.
    delays, t = [], 0.0
    for _ in range(args.jobs):
        delays.append(t)
        if args.rate > 0:
            t += random.expovariate(args.rate)
    outcomes = await asyncio.gather(*(run_job(pool, d) for d in delays))
    await pool.aclose()

    await asyncio.sleep(1)  # let container logs flush
    events = worker_log_events(since)
    admitted_at = [ts for _, ts, outcome in events if outcome == "admitted"]

    counts = Counter(outcomes)
    print(f"Jobs: {args.jobs} at {args.rate or 'burst'} jobs/s — "
          f"admitted {counts['admitted']}, rejected {counts['rejected']}")
    print(f"Worker log: {len(events)} rate-limit decisions, {len(admitted_at)} admitted")

    if admitted_at:
        t0 = min(admitted_at)
        buckets = Counter(int((t - t0).total_seconds() // WINDOW_S) for t in admitted_at)
        print("Admitted per 10s window: " + ", ".join(
            f"[{b * 10:>3.0f}s] {buckets[b]}" for b in range(max(buckets) + 1)))
        peak = max_in_rolling_window(admitted_at)
        print(f"Max admitted in any rolling 10s span: {peak} (limit {LIMIT}) — "
              f"{'OK' if peak <= LIMIT else 'QUOTA VIOLATED'}")

    per_replica = Counter(replica for replica, _, _ in events)
    print(f"Jobs per replica: {dict(sorted(per_replica.items()))} — {len(per_replica)} replica(s)")


if __name__ == "__main__":
    asyncio.run(main())
