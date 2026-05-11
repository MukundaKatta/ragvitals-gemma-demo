# ragvitals-gemma-demo

[![ci](https://github.com/MukundaKatta/ragvitals-gemma-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/MukundaKatta/ragvitals-gemma-demo/actions/workflows/ci.yml)

Companion demo for the dev.to Gemma 4 Write-track entry **"Your RAG works on Claude. Does it work on Gemma 4? Drift detection across model families."**

This repo runs the experiment the article walks through:

1. Stand up a tiny RAG pipeline with a deterministic synthetic data shape (no GPU required).
2. Generate answers with **Claude Sonnet 4.5** for 8 days, build a baseline.
3. Swap the generator to **Gemma 4 9B** for a single day. Same retriever, same embedder, same judge, same queries.
4. Run [`ragvitals`](https://github.com/MukundaKatta/ragvitals) over both phases. The library should flag the response-quality drop without false-alarming on retrieval or query distribution.

## Two ways to run it

### Synthetic (fast, deterministic, no GPU)

```bash
git clone https://github.com/MukundaKatta/ragvitals-gemma-demo
cd ragvitals-gemma-demo
python -m venv .venv && source .venv/bin/activate
pip install -e ".[core]"
python demo/synthetic_run.py
```

This is what produces the article's headline numbers. The seed is fixed at `SEED = 20260524`. The output is pinned by pytest tests in `tests/test_synthetic_run.py` so CI catches any drift in the math.

### Real models (Gemma 4 9B + Claude Sonnet 4.5)

```bash
pip install -e ".[real]"
huggingface-cli login        # required for Gemma 4 (gated)
export ANTHROPIC_API_KEY=...  # Claude generator + judge

python demo/real_models_run.py --n 50
```

You'll need a GPU for Gemma 4 9B (or a smaller variant like 4B with `--gemma-model google/gemma-4-4b-it`). Traces stream to `traces.jsonl` so you can re-run ragvitals offline.

## What the synthetic experiment shows

After 8 days of Claude baseline, one day of Gemma 4 generator, same retriever:

| Dimension | Severity | Why |
|---|---|---|
| QueryDistribution | OK | Same queries, same users |
| EmbeddingDrift | OK | Same embedder |
| RetrievalRelevance | OK | Same retriever, same index |
| **ResponseQuality.faithfulness** | **DEGRADED** | Gemma's faithfulness mean ~0.78 vs baseline ~0.92 |
| JudgeDrift | OK | Judge unchanged; no false-positive |

Run `python demo/synthetic_run.py` to reproduce. The exact value of ResponseQuality.faithfulness lands at **0.7858** with the fixed seed — pinned by a pytest test.

## Repo layout

```
demo/
  synthetic_run.py        # deterministic experiment, no external deps beyond ragvitals
  real_models_run.py      # Gemma 4 + Claude + sentence-transformers (gated/expensive)
  data/queries.json       # 50 representative queries
tests/
  test_synthetic_run.py   # pins the article's headline numbers
.github/workflows/ci.yml  # runs synthetic on every push, all-Python 3.10-3.13
```

## License

MIT. See [LICENSE](LICENSE).

## Related

- [`ragvitals`](https://github.com/MukundaKatta/ragvitals) — the drift-detection library this demo exercises.
- [`bedrockcache`](https://github.com/MukundaKatta/bedrockcache) — prompt-caching auditor for Anthropic-on-Bedrock.
- [`bedrockstack`](https://github.com/MukundaKatta/bedrockstack) — Bedrock-aware retry policy + cost ledger + streaming-error normalization.
