"""Example: drive a synthetic ragvitals detector and land reports in Redis.

Run:
    docker run --rm -d -p 6379:6379 redis:8
    pip install -e ".[core]" redis
    python demo/redis_sink_example.py
    redis-cli XREVRANGE ragvitals:reports + - COUNT 1
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta

try:
    import redis  # type: ignore[import-not-found]
except ImportError:
    print("Install redis: pip install redis", file=sys.stderr)
    sys.exit(2)

from ragvitals import (
    Detector,
    ResponseQuality,
    RetrievalRelevance,
    Trace,
)

from demo.redis_sink import RedisSink, latest_report


def main() -> None:
    r = redis.Redis.from_url("redis://localhost:6379/0")
    r.delete("ragvitals:reports")

    det = Detector(
        dimensions=[
            RetrievalRelevance(metric="hit_rate", k=5, warn_z=1.5, degraded_z=2.5),
            ResponseQuality(score_keys=["faithfulness", "relevance"]),
        ],
        sinks=[RedisSink(client=r, stream_key="ragvitals:reports")],
    )

    rng = random.Random(0)
    base_ts = datetime(2026, 5, 11)

    # 8 days of healthy baseline traffic
    for day in range(8):
        for _ in range(40):
            det.ingest(Trace(
                timestamp=base_ts + timedelta(days=day),
                relevance_labels=[1, 0, 0, 0, 0] if rng.random() < 0.85 else [0]*5,
                judge_scores={
                    "faithfulness": rng.gauss(0.92, 0.03),
                    "relevance":    rng.gauss(0.91, 0.03),
                },
            ))
        det.report()        # emit to Redis at end of day
        det.commit_window()

    # Today: faithfulness collapses
    for _ in range(40):
        det.ingest(Trace(
            timestamp=base_ts + timedelta(days=8),
            relevance_labels=[1, 0, 0, 0, 0] if rng.random() < 0.85 else [0]*5,
            judge_scores={"faithfulness": rng.gauss(0.55, 0.05),
                          "relevance":    rng.gauss(0.90, 0.03)},
        ))
    det.report()
    det.commit_window()

    snap = latest_report(r)
    print("latest report stored at:", snap["entry_id"])
    print("  degraded:", snap["degraded"])
    print("  warned:  ", snap["warned"])
    for d in snap["dimensions"]:
        if d["severity"] != "ok":
            print(f"  ! {d['name']}: severity={d['severity']} value={d['value']}")


if __name__ == "__main__":
    main()
