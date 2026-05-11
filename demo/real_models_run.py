"""Real-models run: same experiment as synthetic_run.py against Gemma 4 + Claude.

This script is the reproducibility receipt for the article. It uses:

  - sentence-transformers (`all-MiniLM-L6-v2`) for embeddings
  - HuggingFace transformers + `google/gemma-4-9b-it` for the Gemma generator
    (gated; you must `huggingface-cli login` and accept the Gemma license)
  - Anthropic SDK for Claude Sonnet 4.5 (env var: ANTHROPIC_API_KEY)

It runs the same 50 queries from `demo/data/queries.json` against both models,
asks Claude Haiku 4.5 to judge faithfulness/relevance for both, then feeds
the resulting traces into ragvitals. Output mirrors `synthetic_run.py`.

The synthetic script produces the article's headline numbers from a fixed seed
so the writeup is reproducible without GPU access. This script confirms the
direction and rough magnitude with real models.

Heavy imports are guarded so you can `python demo/real_models_run.py --help`
without installing anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _require(modules: list[str]) -> None:
    missing = []
    for m in modules:
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    if missing:
        print(
            "Missing dependencies for the real-models run:\n  "
            + "\n  ".join(missing)
            + "\n\nInstall with:\n  pip install 'ragvitals-gemma-demo[real]'",
            file=sys.stderr,
        )
        sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, default=DATA_DIR / "queries.json")
    parser.add_argument("--n", type=int, default=50, help="Number of queries to run")
    parser.add_argument("--gemma-model", default="google/gemma-4-9b-it")
    parser.add_argument("--claude-model", default="claude-sonnet-4-5-20260201")
    parser.add_argument("--judge-model", default="claude-haiku-4-5-20260201")
    parser.add_argument("--output", type=Path, default=Path("traces.jsonl"))
    args = parser.parse_args()

    _require(["sentence_transformers", "transformers", "anthropic", "torch", "ragvitals"])

    # Imports here so --help works without the deps.
    from anthropic import Anthropic  # type: ignore
    from sentence_transformers import SentenceTransformer  # type: ignore
    from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
    import torch  # type: ignore

    from ragvitals import (
        Detector, EmbeddingDrift, InMemorySink, JudgeDrift,
        QueryDistribution, ResponseQuality, RetrievalRelevance, Trace,
    )

    queries = json.loads(args.queries.read_text())["queries"][: args.n]
    embedder = SentenceTransformer("all-MiniLM-L6-v2")
    query_embeddings = embedder.encode(queries, show_progress_bar=False)

    print(f"Loading Gemma model {args.gemma_model}...")
    gemma_tok = AutoTokenizer.from_pretrained(args.gemma_model)
    gemma = AutoModelForCausalLM.from_pretrained(
        args.gemma_model, torch_dtype=torch.bfloat16, device_map="auto"
    )

    claude = Anthropic()

    def generate_gemma(prompt: str) -> str:
        ids = gemma_tok(prompt, return_tensors="pt").to(gemma.device)
        out = gemma.generate(**ids, max_new_tokens=300, do_sample=False)
        return gemma_tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)

    def generate_claude(prompt: str) -> str:
        msg = claude.messages.create(
            model=args.claude_model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    def judge(query: str, answer: str) -> dict[str, float]:
        rubric = (
            "You are evaluating an answer to a user question. "
            "Respond ONLY with a JSON object of the form "
            '{"faithfulness": 0.0-1.0, "relevance": 0.0-1.0}. '
            "Faithfulness: does the answer stay grounded vs. fabricate? "
            "Relevance: does it actually address the question?\n\n"
            f"Q: {query}\nA: {answer}"
        )
        resp = claude.messages.create(
            model=args.judge_model,
            max_tokens=80,
            messages=[{"role": "user", "content": rubric}],
        )
        text = resp.content[0].text.strip()
        # Find first { and last } — tolerant to mild preamble
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            try:
                return {k: float(v) for k, v in json.loads(text[first:last + 1]).items()}
            except (ValueError, json.JSONDecodeError):
                pass
        return {"faithfulness": 0.5, "relevance": 0.5}

    det = Detector(
        dimensions=[
            (q := QueryDistribution()),
            RetrievalRelevance(metric="hit_rate", k=10),
            (e := EmbeddingDrift()),
            ResponseQuality(score_keys=["faithfulness", "relevance"]),
            JudgeDrift(score_key="faithfulness"),
        ],
        sinks=[InMemorySink()],
    )
    q.set_reference(query_embeddings)
    e.set_reference(query_embeddings)

    out = args.output.open("w")
    base_ts = datetime(2026, 5, 11)
    for label, generator in [("claude", generate_claude), ("gemma", generate_gemma)]:
        print(f"\n=== Running {label} over {len(queries)} queries...")
        for i, (query, emb) in enumerate(zip(queries, query_embeddings, strict=False)):
            answer = generator(query)
            scores = judge(query, answer)
            trace = Trace(
                timestamp=base_ts + timedelta(minutes=i if label == "claude" else 60 + i),
                query=query,
                query_embedding=list(map(float, emb)),
                relevance_labels=[1, 0, 0, 0, 0],  # synthetic stand-in
                response=answer,
                judge_scores=scores,
                metadata={"model": label, "reference_id": f"ref-{i % 10}"},
            )
            det.ingest(trace)
            out.write(json.dumps({
                "timestamp": trace.timestamp.isoformat(),
                "model": label, "query": query, "answer": answer, "scores": scores,
            }) + "\n")
            print(f"  {i+1}/{len(queries)}: {label}  faithfulness={scores.get('faithfulness'):.3f}  "
                  f"relevance={scores.get('relevance'):.3f}")
        det.commit_window()

    out.close()
    report = det.report()
    print("\n=== ragvitals report after both phases ===")
    for d in report.dimensions:
        val = f"{d.value:.4f}" if isinstance(d.value, (int, float)) else "n/a"
        print(f"  {d.name:35s}  {d.severity.value:9s}  value={val}")
    print(f"\nTraces written to {args.output}")


if __name__ == "__main__":
    main()
