"""Pin the headline numbers from the synthetic experiment.

These are exactly the numbers cited in the dev.to article. If they drift, the
article is wrong, and CI tells us before publish.
"""

from __future__ import annotations

import random

from demo.synthetic_run import (
    SEED,
    _baseline_phase,
    _build_detector,
    _gemma_phase,
    _judge_drift_phase,
)


def test_gemma_swap_only_flags_response_quality():
    rng = random.Random(SEED)
    det = _build_detector()
    _baseline_phase(det, rng)
    det.commit_window()
    _gemma_phase(det, rng)
    report = det.report()

    flagged = {d.name for d in report.dimensions if d.severity.value != "ok"}
    assert flagged == {"ResponseQuality.faithfulness"}, (
        f"Gemma swap should flag ONLY ResponseQuality.faithfulness, got {flagged}"
    )

    fr = next(d for d in report.dimensions if d.name == "ResponseQuality.faithfulness")
    assert fr.value is not None
    # Gemma in the synthetic harness produces ~0.78-0.80 mean faithfulness.
    # This is the article's headline number.
    assert 0.77 <= fr.value <= 0.81, fr.value


def test_judge_drift_only_flags_judge_drift():
    rng = random.Random(SEED)
    det = _build_detector()
    _baseline_phase(det, rng)
    det.commit_window()
    _judge_drift_phase(det, rng)
    report = det.report()

    flagged = {d.name for d in report.dimensions if d.severity.value != "ok"}
    assert flagged == {"JudgeDrift"}, (
        f"Judge swap should flag ONLY JudgeDrift, got {flagged}"
    )

    jd = next(d for d in report.dimensions if d.name == "JudgeDrift")
    assert jd.value is not None
    # New judge rates +0.15 higher on the same probes; the realized delta lands
    # near +0.07 because the score is clipped at 1.0.
    assert 0.05 <= jd.value <= 0.10, jd.value


def test_gemma_swap_does_not_alarm_on_retrieval_or_query_distribution():
    """The whole point: same retriever, same queries, no false alarms."""
    rng = random.Random(SEED)
    det = _build_detector()
    _baseline_phase(det, rng)
    det.commit_window()
    _gemma_phase(det, rng)
    report = det.report()

    quiet = {"QueryDistribution", "RetrievalRelevance", "EmbeddingDrift"}
    for d in report.dimensions:
        if d.name in quiet:
            assert d.severity.value == "ok", (
                f"{d.name} alarmed during a generator-only swap: {d.severity}"
            )
