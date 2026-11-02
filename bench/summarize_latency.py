"""
Summarise voice-to-response latency recorded by the orchestrator (ORION_BENCH=1).

Reads logs/latency.jsonl and reports p50/p90 per stage over warm runs, with the
first turn of the session (cold model load) reported separately.

    python bench/summarize_latency.py            # latest orchestrator session
    python bench/summarize_latency.py --pid 1234 # a specific session
"""
import argparse
import json
import os
import platform
import subprocess

LATENCY_LOG = os.path.join(os.path.dirname(__file__), "../logs/latency.jsonl")
STAGES = ["stt_s", "llm_s", "tts_s", "total_s"]


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (same as numpy's default)."""
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def tokens_per_sec(r: dict) -> float | None:
    if r.get("eval_count") and r.get("eval_duration"):
        return r["eval_count"] / (r["eval_duration"] / 1e9)
    return None


def cpu_model() -> str:
    try:
        return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return platform.processor()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=LATENCY_LOG)
    parser.add_argument("--pid", type=int, help="orchestrator session (default: latest)")
    args = parser.parse_args()

    with open(args.log) as f:
        records = [json.loads(line) for line in f if line.strip()]
    pid = args.pid or records[-1]["pid"]
    session = [r for r in records if r["pid"] == pid and r["llm_ok"]]

    cold = [r for r in session if r["turn"] == 1]
    warm = [r for r in session if r["turn"] > 1]

    print(f"CPU:      {cpu_model()}")
    print(f"Session:  pid {pid} — {len(warm)} warm run(s), {len(cold)} cold")
    if warm:
        audio = [r["audio_s"] for r in warm]
        print(f"Utterance length: {min(audio):.1f}–{max(audio):.1f}s")

    print(f"\n{'stage':<10}{'p50 (s)':>10}{'p90 (s)':>10}")
    for stage in STAGES:
        values = [r[stage] for r in warm]
        if values:
            print(f"{stage[:-2]:<10}{percentile(values, 50):>10.3f}{percentile(values, 90):>10.3f}")

    tps = [t for t in map(tokens_per_sec, warm) if t]
    if tps:
        print(f"\nLLM decode: p50 {percentile(tps, 50):.1f} tok/s, p90 {percentile(tps, 90):.1f} tok/s "
              f"(mean {sum(r['eval_count'] for r in warm) / len(warm):.0f} output tokens)")

    for r in cold:
        print(f"\nCold turn: stt {r['stt_s']:.3f}s, llm {r['llm_s']:.3f}s "
              f"(model load {(r.get('load_duration') or 0) / 1e9:.3f}s), "
              f"tts {r['tts_s']:.3f}s, total {r['total_s']:.3f}s")


if __name__ == "__main__":
    main()
