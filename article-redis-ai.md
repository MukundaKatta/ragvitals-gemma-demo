---
title: "A 60-line Redis sink for ragvitals: production drift in the same Redis you already run"
published: false
description: "Most teams running RAG already have Redis sitting in front of their LLM for prompt caching or rate-limiting. Same Redis is a great place to land your drift reports. Sixty lines of Python implement a RedisSink for ragvitals that stores every Detector report as a Redis Stream entry you can query with one HGET."
tags: redischallenge, redis, ai, rag
canonical_url:
cover_image:
---

> Companion code: [MukundaKatta/ragvitals-gemma-demo](https://github.com/MukundaKatta/ragvitals-gemma-demo). The Redis sink described below is in `demo/redis_sink.py`.

`ragvitals` ships with three sinks out of the box: `InMemorySink` (good for tests), `JSONLSink` (cheap, append-only), and `CloudWatchSink` (boto3-optional). Last week I added a fourth, a sixty-line `RedisSink`. Here is why and how.

## The case for landing drift reports in Redis

Most production RAG stacks already have Redis. It is sitting in front of the LLM for prompt caching, in front of the embedder for query-embedding caching, or in front of the retriever as a Bloom filter for dedup. If you have Redis anyway, that is where your drift reports should land.

The alternatives are worse in specific ways:

- **CloudWatch / Datadog / New Relic**: separate infrastructure, separate auth, separate query language. Useful at $aggregator scale, overkill for "is my hit rate falling this hour".
- **JSONL on disk**: cheap, but on Lambda there is no persistent disk. On Kubernetes it lives only on the pod that wrote it.
- **A dedicated time-series DB**: Influx, Timescale, Prometheus. Right answer at $aggregator scale, wrong answer for a team that already has Redis.

Redis Streams is the right primitive for this. Each `Detector.report()` becomes one stream entry. The entry holds the per-dimension values, severities, and z-scores. A consumer reads them lazily with `XREAD` for alarming. Time-bucketed aggregates (last hour, last 24h) come from `XRANGE` with a millisecond filter, no extra query layer.

## The sink

```python
from __future__ import annotations
import json
from dataclasses import dataclass
import redis  # type: ignore

from ragvitals import Sink, DetectorReport


@dataclass
class RedisSink:
    """Redis Streams sink for ragvitals.

    Each call to Detector.report() appends one entry to a Redis Stream at
    `stream_key`. Each entry holds the JSON-encoded per-dimension report
    keyed by dimension name, plus aggregate metadata. Read back with
    XRANGE / XREAD.
    """
    client: redis.Redis
    stream_key: str = "ragvitals:reports"
    maxlen: int = 10_000   # cap stream size; older entries are trimmed

    def emit(self, report: DetectorReport) -> None:
        fields = {
            "window_start": report.window_start.isoformat(),
            "window_end":   report.window_end.isoformat(),
            "degraded":     ",".join(report.degraded),
            "warned":       ",".join(report.warned),
            "report":       json.dumps([
                {
                    "name": d.name,
                    "severity": d.severity.value,
                    "value": d.value,
                    "baseline": d.baseline,
                    "z_score": d.z_score,
                    "sample_size": d.sample_size,
                    "detail": d.detail,
                }
                for d in report.dimensions
            ]),
        }
        self.client.xadd(self.stream_key, fields,
                         maxlen=self.maxlen, approximate=True)


def latest_report(client: redis.Redis,
                  stream_key: str = "ragvitals:reports") -> dict | None:
    """Fetch the most recent report entry, decoded."""
    items = client.xrevrange(stream_key, count=1)
    if not items:
        return None
    entry_id, fields = items[0]
    return {
        "entry_id": entry_id.decode() if isinstance(entry_id, bytes) else entry_id,
        "window_start": fields[b"window_start"].decode(),
        "window_end":   fields[b"window_end"].decode(),
        "degraded":     fields[b"degraded"].decode().split(",") if fields[b"degraded"] else [],
        "warned":       fields[b"warned"].decode().split(",") if fields[b"warned"] else [],
        "dimensions":   json.loads(fields[b"report"]),
    }


def reports_in_window(client: redis.Redis,
                      since_ms: int,
                      stream_key: str = "ragvitals:reports") -> list[dict]:
    """All reports written since the given timestamp (ms epoch)."""
    items = client.xrange(stream_key, min=f"{since_ms}-0", max="+")
    return [
        {
            "entry_id": eid.decode() if isinstance(eid, bytes) else eid,
            "degraded": fields[b"degraded"].decode().split(",") if fields[b"degraded"] else [],
            "warned":   fields[b"warned"].decode().split(",") if fields[b"warned"] else [],
            "dimensions": json.loads(fields[b"report"]),
        }
        for eid, fields in items
    ]
```

Sixty lines including imports, dataclass scaffolding, and two helper queries. Drop into your project, point at any Redis 7+, done.

## Wiring it into the Detector

```python
import redis
from ragvitals import Detector, RetrievalRelevance, ResponseQuality
from your_project.redis_sink import RedisSink

r = redis.Redis.from_url("redis://localhost:6379/0")

det = Detector(
    dimensions=[
        RetrievalRelevance(metric="hit_rate", k=10),
        ResponseQuality(score_keys=["faithfulness", "relevance"]),
    ],
    sinks=[RedisSink(client=r, stream_key="myapp:rag:drift")],
)

for trace in your_trace_stream():
    det.ingest(trace)

det.report()       # emits to Redis
det.commit_window()
```

## Reading from Redis for alarms

```python
latest = latest_report(r, stream_key="myapp:rag:drift")
if latest and latest["degraded"]:
    page_oncall(latest)
```

Or via redis-cli for spot-checks:

```bash
redis-cli XREVRANGE myapp:rag:drift + - COUNT 1
# => 1715444321000-0
#    1) "window_start" "2026-05-11T08:00:00"
#    2) "window_end"   "2026-05-11T09:00:00"
#    3) "degraded"     "ResponseQuality.faithfulness"
#    4) ...
```

For a "show me everything that fired in the last 24h":

```bash
redis-cli XRANGE myapp:rag:drift "$(date -d '24 hours ago' +%s)000" "+"
```

## Why Streams over Pub/Sub

The two natural Redis primitives for this are Streams and Pub/Sub. Streams win for three reasons:

1. **Durable**: entries persist until you trim them. Pub/Sub messages vanish if no consumer is connected at the moment of publish.
2. **Queryable**: `XRANGE` with timestamp filters lets you compute "alarm rate over last hour" without a secondary store.
3. **Bounded**: `maxlen` with `approximate=True` caps growth without expensive trimming.

The downside of Streams is "no fan-out to multiple alarm-routing destinations without consumer groups", which is fine because you usually want exactly one consumer (your alarm router) anyway.

## What this stack does NOT solve

- **Aggregation across services**: if you run five separate RAG services in five separate Lambdas, each writes its own stream key. You still need a fan-in step in your alarm router. Trivial, not handled here.
- **Long-term retention**: Streams are for the hot window. For "show me drift in February" you want to roll up to a cold store. The pattern is to read the previous day's stream at end-of-day, aggregate, write to S3 / GCS / wherever, and trim the stream. Out of scope for this sink.
- **Multi-tenant isolation**: one stream key per tenant. Not handled in the sink itself; the caller picks the key.

## Why this fits Redis 8 specifically

Redis 8 added `RESP3` push messages and improved Stream consumer-group performance. If you are reading the stream with `XREADGROUP` for alarm routing, Redis 8 cuts per-consumer overhead noticeably; if you are just doing `XRANGE` snapshots for dashboards, either version is fine.

## Reproduce

```bash
git clone https://github.com/MukundaKatta/ragvitals-gemma-demo
cd ragvitals-gemma-demo
pip install -e ".[core]" redis

# Start Redis
docker run --rm -d -p 6379:6379 redis:8

# Run a synthetic detector with the Redis sink
python demo/redis_sink_example.py
redis-cli XREVRANGE ragvitals:reports + - COUNT 1
```

The full sink, the example script, and a tiny consumer that pages an alert when any dimension goes `degraded` are in the repo.

## Related work

- [`ragvitals`](https://github.com/MukundaKatta/ragvitals): the library this sink plugs into.
- [`bedrockcache`](https://github.com/MukundaKatta/bedrockcache) and [`bedrockstack`](https://github.com/MukundaKatta/bedrockstack): siblings of ragvitals for Anthropic-on-Bedrock teams.
- [`ragvitals-gemma-demo`](https://github.com/MukundaKatta/ragvitals-gemma-demo): the demo harness that exercises ragvitals across model swaps; the Redis sink lives here.

Sixty lines of Python, one durable Stream, your existing Redis. That is the right primitive for landing drift reports in production today.
