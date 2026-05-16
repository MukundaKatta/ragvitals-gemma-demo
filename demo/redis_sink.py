"""Redis Streams sink for ragvitals.

A drop-in `Sink` implementation that lands every `Detector.report()` into a
Redis Stream. Companion code for the dev.to Redis AI Challenge post.

Usage:
    import redis
    from ragvitals import Detector
    from demo.redis_sink import RedisSink, latest_report

    r = redis.Redis.from_url("redis://localhost:6379/0")
    det = Detector(dimensions=[...], sinks=[RedisSink(client=r)])
    ...
    latest = latest_report(r)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis  # type: ignore[import-not-found]

from ragvitals import DetectorReport


@dataclass
class RedisSink:
    """Append each Detector.report() into a Redis Stream entry.

    `stream_key` defaults to `ragvitals:reports`. `maxlen` caps the stream so
    a long-running process doesn't fill Redis; `approximate=True` lets Redis
    pick efficient trim points.
    """

    client: "redis.Redis"
    stream_key: str = "ragvitals:reports"
    maxlen: int = 10_000

    def emit(self, report: DetectorReport) -> None:
        fields = {
            "window_start": report.window_start.isoformat(),
            "window_end": report.window_end.isoformat(),
            "degraded": ",".join(report.degraded),
            "warned": ",".join(report.warned),
            "report": json.dumps([
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


def latest_report(client: "redis.Redis",
                  stream_key: str = "ragvitals:reports") -> dict | None:
    """Fetch the most recent report entry, decoded.

    Returns None if the stream is empty.
    """
    items = client.xrevrange(stream_key, count=1)
    if not items:
        return None
    entry_id, fields = items[0]
    return _decode(entry_id, fields)


def reports_in_window(client: "redis.Redis",
                      since_ms: int,
                      stream_key: str = "ragvitals:reports") -> list[dict]:
    """All reports written at or after `since_ms` (epoch ms)."""
    items = client.xrange(stream_key, min=f"{since_ms}-0", max="+")
    return [_decode(eid, f) for eid, f in items]


def _decode(entry_id, fields) -> dict:
    def _s(v):
        return v.decode() if isinstance(v, bytes) else v
    return {
        "entry_id": _s(entry_id),
        "window_start": _s(fields[b"window_start"]),
        "window_end": _s(fields[b"window_end"]),
        "degraded": _s(fields[b"degraded"]).split(",") if fields[b"degraded"] else [],
        "warned": _s(fields[b"warned"]).split(",") if fields[b"warned"] else [],
        "dimensions": json.loads(_s(fields[b"report"])),
    }
