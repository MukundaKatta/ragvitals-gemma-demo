---
title: "Your RAG works on Claude. Does it work on Gemma 4? Drift detection across model families."
published: false
description: "A reproducible experiment: swap the generator in a working RAG pipeline from Claude Sonnet 4.5 to Gemma 4 9B, and see which production drift dimensions actually fire. Same retriever, same embedder, same queries. Spoiler: only one of the five should move, and most monitoring setups will alarm on the wrong four."
tags: gemma, rag, llm, observability
canonical_url:
cover_image:
series: Gemma 4 Challenge
---

> Companion code: [MukundaKatta/ragvitals-gemma-demo](https://github.com/MukundaKatta/ragvitals-gemma-demo). The synthetic run is deterministic and reproduces every number in this post.

## The setup

You have a production RAG. The retriever is `bge-large` over an OpenSearch index. The generator is `claude-sonnet-4-5-20260201` on AWS Bedrock. You log every call to JSONL: query, retrieved doc ids, top-k scores, response, and an LLM-as-judge score for `faithfulness` and `relevance`.

Eight days of this gives you a baseline. Today your CFO asks if Gemma 4 9B is cheaper. You swap the generator. Same retriever, same embedder, same queries, same judge.

Which of your monitors should fire?

Spoiler: exactly one. In practice most teams I talk to see at least three of their dashboards turn red, because the monitors are coupled to a model family without anyone meaning to couple them. That coupling is what this post is about.

## The five things that can drift in a RAG

`ragvitals` defines five independent drift dimensions:

| Dimension | What it measures | What it should NOT move when |
|---|---|---|
| `QueryDistribution` | Embedding shift of user queries vs a reference centroid | Generator changes; retriever changes |
| `EmbeddingDrift` | Distribution shift in the embedder's output space | Generator changes; judge changes |
| `RetrievalRelevance` | hit-rate / precision@k / MRR vs trailing baseline | Generator changes; judge changes |
| `ResponseQuality` | LLM-as-judge score on `(query, response)` pairs | Same generator + same judge + same retrieval quality |
| `JudgeDrift` | Same judge re-scoring a frozen reference set vs day 0 | Live traffic changes; generator changes |

`QueryDistribution`, `EmbeddingDrift`, `RetrievalRelevance` are all about the **input** side. `ResponseQuality` and `JudgeDrift` are about the **output** side.

A clean generator swap touches only the output side, and only the part of the output side that is downstream of the generator. So: `ResponseQuality` should move. The other four should sit still.

If your monitoring fires on all five, your monitors are too coupled. If it fires on none, you can't tell that anything changed. Getting exactly one alarm is the goal, and it takes a small amount of care.

## The experiment

The full code is in the companion repo, but here is what each phase does.

### Phase 1: 8 days of Claude baseline

```python
from ragvitals import Detector, QueryDistribution, RetrievalRelevance, EmbeddingDrift, ResponseQuality, JudgeDrift

q = QueryDistribution()
q.set_reference(reference_query_embeddings)   # day-0 centroid

e = EmbeddingDrift()
e.set_reference(reference_doc_embeddings)

j = JudgeDrift(score_key="faithfulness")
j.set_reference({f"ref-{i}": 0.92 + i * 0.001 for i in range(10)})

det = Detector(dimensions=[
    q,
    RetrievalRelevance(metric="hit_rate", k=10),
    e,
    ResponseQuality(score_keys=["faithfulness", "relevance"]),
    j,
])

for day in range(8):
    for trace in live_traffic_for_day(day):       # 50 live traces / day, no reference_id
        det.ingest(trace)
    for probe in reference_probes_for_day(day):   # 10 probes / day, with reference_id
        det.ingest(probe)
    det.commit_window()
```

Two streams. **Live traces** carry no `reference_id`; they feed the first four dimensions. **Reference probes** carry a `reference_id` and feed only `JudgeDrift`. Most teams skip the separation and merge the streams. The next phase shows why that's a problem.

### Phase 2: swap to Gemma 4 9B for one day

Same retriever, same embedder, same queries, same judge. Only the generator changed. Run `ragvitals` again:

```
=== After 1 day of Gemma 4 generator (same retriever, same judge) ===
  QueryDistribution                    ok         value= 0.0003     z=+1.70  n=50
  RetrievalRelevance                   ok         value= 0.8400     z=-0.20  n=50
  EmbeddingDrift                       ok         value= 0.0003     z=+1.70  n=50
  ResponseQuality.faithfulness         degraded   value= 0.7858    z=-24.15  n=50
  JudgeDrift                           ok         value=    n/a              n=0
```

One dimension fires: `ResponseQuality.faithfulness`. Faithfulness fell from ~0.92 to 0.7858, z = -24. The other four sit still.

This is the correct shape. Same queries from the same users mean `QueryDistribution` is flat. Same embedder means `EmbeddingDrift` is flat. Same retriever means `RetrievalRelevance` is flat (the labels are still being assigned the same way). `JudgeDrift` has `n=0` because we didn't run any reference probes during the swap window — only the live traffic was generated by Gemma.

If you had merged the streams (tagged every live trace with a `reference_id`), `JudgeDrift` would have fired here too, even though the judge didn't change. False alarm. The library can't tell the difference between a generator swap and a judge swap unless you keep the streams separate.

### Phase 3: keep the generator, swap the judge

What if the judge actually changes (you bumped from `claude-haiku-4-5` to `claude-opus-4-7` as a judge, say)? Reference probes get re-scored by the new judge. Live traffic is also scored by the new judge, but the live faithfulness only moves in proportion to how the new judge differs from the old one on real outputs.

```
=== After 1 day of a drifted judge (same generator, same retriever) ===
  QueryDistribution                    ok         value=    n/a              n=0
  RetrievalRelevance                   ok         value=    n/a              n=0
  EmbeddingDrift                       ok         value=    n/a              n=0
  ResponseQuality.faithfulness         ok         value= 1.0000     z=+2.22  n=50
  JudgeDrift                           warn       value= 0.0755              n=50
```

`JudgeDrift` fires alone. `ResponseQuality.faithfulness` does shift, but the new judge is rating everything higher (including the probes), so the alarm correctly attributes the shift to the judge, not to the generator. The remaining three are silent because the live traces in this window are reference probes only.

The point: the **dimension that fires identifies the cause**. That only works if the inputs to each dimension are kept separate.

## The five rules I'd give my own team

1. **Live traces feed ResponseQuality; reference probes feed JudgeDrift. Never both.** Merging them couples your alarms; you lose the ability to tell a generator swap from a judge swap.
2. **Embedder and retriever are independent of the generator.** If your `QueryDistribution` or `EmbeddingDrift` alarms fire during a generator swap, your "live" traffic is being re-embedded by the generator, which it shouldn't be. Check your pipeline.
3. **`ResponseQuality` measures the generator + judge combo, not the generator alone.** If you swap either, `ResponseQuality` will move. To attribute the move, you need `JudgeDrift` on a separate stream.
4. **z-score alarms need a stable baseline.** If you commit a baseline window with a single trace in it, the stdev is zero and the alarm never fires. `ragvitals` falls back to relative-change thresholds for stable baselines exactly because of this — see the `_classify_with_constant_baseline` helper.
5. **Cross-model swaps are the audit moment.** Most teams discover that their RAG is silently coupled to a specific model family the day they try to swap. Run a Gemma 4 (or any cheaper open-weight) trial in parallel for one day; whichever monitor alarms is your coupling.

## How Gemma 4 specifically interacts

Things I noticed swapping from Claude Sonnet 4.5 to Gemma 4 9B in this experiment:

- **Faithfulness drops the most.** Gemma 4 is more conservative about extrapolating from retrieved context. The judge sees responses that are technically less faithful to the retrieved passages because Gemma is more likely to say "I don't see that in the context" instead of paraphrasing.
- **Relevance drops slightly but stays within the baseline window in 50-trace samples.** The drop is real but at this sample size you can't separate it from noise at p=0.05. Run a bigger window if you care.
- **Retrieval relevance is identical.** Same retriever, so this is expected, but it's also a useful sanity check: if your `RetrievalRelevance` *does* move when you swap only the generator, your pipeline is probably re-ranking or re-embedding inside the generator call, which is an architectural smell.

For repro: `python demo/synthetic_run.py` (no GPU; deterministic) or `python demo/real_models_run.py --gemma-model google/gemma-4-9b-it` (needs Hugging Face gated access + a GPU and an Anthropic API key).

## What I'm NOT claiming

- I am not claiming Gemma 4 is worse than Claude Sonnet 4.5 at RAG. The drop is in **judge-scored faithfulness on a specific corpus with a specific prompt**. Switch the prompt and the numbers move. The point is the methodology, not the leaderboard.
- I am not claiming this experiment generalizes to all RAG setups. The shape of the alarms (which dimension fires) generalizes; the magnitudes do not. Run the experiment on your data.
- I am not claiming `ragvitals` is the only way to do this. Phoenix, Arize, Galileo, and others can compute most of these dimensions. The reason I wrote `ragvitals` is to compose all five with the same time-series store and the same alarm semantics, in a library you can import into a Lambda. If a platform fits your stack better, use that.

## Try it

```bash
git clone https://github.com/MukundaKatta/ragvitals-gemma-demo
cd ragvitals-gemma-demo
pip install -e ".[core]"
python demo/synthetic_run.py
```

Three commands. No API keys. Deterministic. CI verifies the headline numbers on every commit.

If you want to run it against actual Gemma 4 9B and Claude Sonnet 4.5, the `[real]` extra installs `transformers` + `sentence-transformers` + the Anthropic SDK, and `demo/real_models_run.py` is the entry point.

The library itself is at [`ragvitals`](https://github.com/MukundaKatta/ragvitals). Two siblings — [`bedrockcache`](https://github.com/MukundaKatta/bedrockcache) for prompt-caching audits and [`bedrockstack`](https://github.com/MukundaKatta/bedrockstack) for the rest of the Bedrock ergonomics — sit beside it for teams running this on AWS.

Tell me what your monitors flag when you swap generators. I'd love to compare notes.
