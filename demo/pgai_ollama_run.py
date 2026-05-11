"""End-to-end RAG over Postgres + pgvector + pgai + Ollama, observed by ragvitals.

Architecture:
    Documents -> pgvector embeddings (via pgai.openai_embed or pgai.ollama_embed)
              -> SQL similarity search
              -> Ollama-served generator (default: gemma2:9b)
              -> Ollama-served judge (default: llama3.1:8b)
              -> ragvitals.Detector running the 5-dimensional drift report

This script is the runnable companion for the dev.to "Open Source AI Challenge
with pgai and Ollama" article. It exercises pgvector + pgai together.

Requirements (in addition to ragvitals):
    pip install 'ragvitals-gemma-demo[pgai]'      # adds psycopg + requests
    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres \\
        timescale/timescaledb-ha:pg17-all
    docker exec -it <container> psql -U postgres -c "CREATE EXTENSION vector;"
    docker exec -it <container> psql -U postgres -c "CREATE EXTENSION ai CASCADE;"
    ollama pull gemma2:9b   # generator
    ollama pull llama3.1:8b # judge
    ollama pull nomic-embed-text  # embedder

Then:
    python demo/pgai_ollama_run.py --setup       # one-time: create tables, ingest docs
    python demo/pgai_ollama_run.py --run         # run 50 queries + drift report

All heavy imports are guarded so `--help` works on a fresh checkout.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

CORPUS = [
    ("doc-prompt-cache-1", "Anthropic prompt caching: ephemeral cache_control on content blocks reduces input cost by 90% on cache reads."),
    ("doc-prompt-cache-2", "Bedrock cachePoint blocks require at least 1024 tokens of preceding content; system prompts under that threshold do not cache."),
    ("doc-prompt-cache-3", "LiteLLM v1.86 forwards cache_control on message dicts to Bedrock; earlier versions silently strip the directive."),
    ("doc-ragvitals-1", "ragvitals composes five drift dimensions: QueryDistribution, RetrievalRelevance, EmbeddingDrift, ResponseQuality, JudgeDrift."),
    ("doc-ragvitals-2", "Reference probes feed JudgeDrift; live traffic feeds ResponseQuality. Keeping these streams separate avoids false alarms during generator swaps."),
    ("doc-ragvitals-3", "Sinks: InMemorySink for tests, JSONLSink for cheap append-only logging, CloudWatchSink when boto3 is installed."),
    ("doc-pgai-1", "pgai brings AI workflows into PostgreSQL: ai.openai_embed, ai.ollama_generate, ai.ollama_embed all run inside the database."),
    ("doc-pgai-2", "pgvector stores high-dimensional vectors and supports cosine, L2, and inner-product similarity operators via <->, <#>, <=> respectively."),
    ("doc-pgai-3", "pgvectorscale adds StreamingDiskANN indexes that scale cosine-similarity search to billions of vectors without leaving Postgres."),
    ("doc-ollama-1", "Ollama serves quantized open-weight models locally over HTTP at port 11434; gemma2 and llama3 families are first-class."),
    ("doc-ollama-2", "Ollama's /api/generate endpoint returns streaming JSON; /api/embeddings returns a single embedding per prompt."),
    ("doc-bedrock-1", "Bedrock InvokeModel and Converse APIs have different cache-directive shapes; cache_control belongs to InvokeModel, cachePoint to Converse."),
]

# 50 representative queries reused from the synthetic experiment so reports line up.
QUERIES = json.loads((DATA_DIR / "queries.json").read_text())["queries"]


def _require(modules: list[str]) -> None:
    missing = []
    for m in modules:
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    if missing:
        print(
            "Missing deps:\n  " + "\n  ".join(missing) + "\nInstall with:\n  "
            "pip install 'ragvitals-gemma-demo[pgai]'",
            file=sys.stderr,
        )
        sys.exit(2)


def _connect(dsn: str):
    import psycopg  # type: ignore
    return psycopg.connect(dsn, autocommit=True)


SETUP_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS ai CASCADE;

CREATE TABLE IF NOT EXISTS rag_docs (
    id          text PRIMARY KEY,
    content     text NOT NULL,
    embedding   vector(768)
);

-- Vectorizer pattern: derive embedding from content using pgai's local Ollama embedder.
-- pgai.create_vectorizer would normally be a long-running worker; for the demo we
-- embed inline on insert. The challenge category 'Vectorizer Vibe' values teams
-- that set up a real vectorizer worker; this demo keeps it inline for clarity.
"""


def cmd_setup(conn) -> None:
    print("Creating extensions + tables...")
    with conn.cursor() as cur:
        for stmt in SETUP_SQL.strip().split(";"):
            s = stmt.strip()
            if not s:
                continue
            cur.execute(s)
        for doc_id, content in CORPUS:
            cur.execute(
                """
                INSERT INTO rag_docs (id, content, embedding)
                VALUES (
                    %s,
                    %s,
                    ai.ollama_embed('nomic-embed-text', %s)::vector
                )
                ON CONFLICT (id) DO UPDATE SET
                    content = excluded.content,
                    embedding = excluded.embedding
                """,
                (doc_id, content, content),
            )
    print(f"  ingested {len(CORPUS)} docs.")


def retrieve(conn, query: str, k: int = 3) -> list[tuple[str, str, float]]:
    """Top-k cosine-similar docs via pgai.ollama_embed + pgvector <=> operator."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content,
                   1 - (embedding <=> ai.ollama_embed('nomic-embed-text', %s)::vector) AS score
            FROM rag_docs
            ORDER BY embedding <=> ai.ollama_embed('nomic-embed-text', %s)::vector
            LIMIT %s
            """,
            (query, query, k),
        )
        return list(cur.fetchall())


def generate(conn, query: str, contexts: list[str], model: str) -> str:
    """Generate an answer with pgai.ollama_generate over the retrieved contexts."""
    context_block = "\n\n".join(f"- {c}" for c in contexts)
    prompt = (
        "Answer the question using ONLY the provided context. If the context does not "
        "contain the answer, say you don't know.\n\n"
        f"Context:\n{context_block}\n\nQuestion: {query}\nAnswer:"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ai.ollama_generate(%s, %s)->>'response'",
            (model, prompt),
        )
        return (cur.fetchone()[0] or "").strip()


def judge(conn, query: str, answer: str, judge_model: str) -> dict[str, float]:
    """Score faithfulness + relevance using a smaller Ollama-served judge model."""
    rubric = (
        "Score this Q/A pair on faithfulness (0-1) and relevance (0-1). "
        "Output ONLY a JSON object like {\"faithfulness\": 0.92, \"relevance\": 0.88}. "
        f"\n\nQ: {query}\nA: {answer}\nJSON:"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ai.ollama_generate(%s, %s)->>'response'",
            (judge_model, rubric),
        )
        text = (cur.fetchone()[0] or "").strip()
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        try:
            return {k: float(v) for k, v in json.loads(text[first:last + 1]).items()}
        except (ValueError, json.JSONDecodeError):
            pass
    return {"faithfulness": 0.5, "relevance": 0.5}


def cmd_run(conn, args) -> None:
    _require(["ragvitals"])
    from ragvitals import (
        Detector, EmbeddingDrift, InMemorySink, JudgeDrift,
        QueryDistribution, ResponseQuality, RetrievalRelevance, Trace,
    )

    # Snapshot the reference centroid from the docs we just ingested.
    with conn.cursor() as cur:
        cur.execute("SELECT embedding FROM rag_docs LIMIT 50")
        reference_embeddings = [list(row[0]) for row in cur.fetchall() if row[0] is not None]

    q = QueryDistribution(); q.set_reference(reference_embeddings)
    e = EmbeddingDrift();    e.set_reference(reference_embeddings)
    j = JudgeDrift(score_key="faithfulness")
    j.set_reference({f"ref-{i}": 0.85 for i in range(10)})

    det = Detector(
        dimensions=[
            q,
            RetrievalRelevance(metric="hit_rate", k=3),
            e,
            ResponseQuality(score_keys=["faithfulness", "relevance"]),
            j,
        ],
        sinks=[InMemorySink()],
    )

    base_ts = datetime(2026, 5, 11)
    queries = QUERIES[: args.n]

    out_path = Path(args.output)
    out = out_path.open("w")

    for i, query in enumerate(queries):
        retrieved = retrieve(conn, query, k=3)
        contexts = [content for _, content, _ in retrieved]
        scores  = [score for _, _, score in retrieved]
        answer  = generate(conn, query, contexts, args.gen_model)
        rubric  = judge(conn, query, answer, args.judge_model)

        # Query embedding via the same pgai inline embedder, so QueryDistribution
        # uses the same embedding space as the corpus did.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ai.ollama_embed('nomic-embed-text', %s)",
                (query,),
            )
            q_emb = list(cur.fetchone()[0])

        trace = Trace(
            timestamp=base_ts + timedelta(minutes=i),
            query=query,
            query_embedding=q_emb,
            retrieval_scores=scores,
            relevance_labels=[1] + [0] * (len(scores) - 1),  # top-1 deemed relevant
            response=answer,
            judge_scores=rubric,
            metadata={"model": args.gen_model},
        )
        det.ingest(trace)

        out.write(json.dumps({
            "timestamp": trace.timestamp.isoformat(),
            "query": query, "answer": answer,
            "scores": rubric, "retrieval_scores": scores,
        }) + "\n")
        print(f"  {i+1:2d}/{len(queries)}: faithfulness={rubric.get('faithfulness'):.2f} "
              f"relevance={rubric.get('relevance'):.2f}")

    out.close()
    report = det.report()
    print("\n=== ragvitals report ===")
    for d in report.dimensions:
        val = f"{d.value:.4f}" if isinstance(d.value, (int, float)) else "n/a"
        print(f"  {d.name:35s}  {d.severity.value:9s}  value={val:>7s}  n={d.sample_size}")
    print(f"  degraded: {report.degraded}")
    print(f"  warned:   {report.warned}")
    print(f"\nTraces written to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default="postgres://postgres:postgres@localhost:5432/postgres")
    parser.add_argument("--setup", action="store_true", help="Create tables + ingest the corpus")
    parser.add_argument("--run", action="store_true", help="Run queries + drift report")
    parser.add_argument("--n", type=int, default=20, help="Number of queries to run")
    parser.add_argument("--gen-model", default="gemma2:9b")
    parser.add_argument("--judge-model", default="llama3.1:8b")
    parser.add_argument("--output", default="pgai_traces.jsonl")
    args = parser.parse_args()

    if not (args.setup or args.run):
        parser.print_help()
        return

    _require(["psycopg"])
    with _connect(args.dsn) as conn:
        if args.setup:
            cmd_setup(conn)
        if args.run:
            cmd_run(conn, args)


if __name__ == "__main__":
    main()
