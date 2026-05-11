---
title: "Fully open-source RAG with pgvector + pgai + Ollama, and ragvitals watching for drift"
published: false
description: "An end-to-end open-source RAG stack on Postgres: pgvector for storage, pgai for embedding and generation inside SQL, Ollama for serving Gemma 2 and Llama 3.1 locally, and a 5-dimensional drift detector wired over the whole thing. Code, schema, and reproducibility receipts included."
tags: devchallenge, pgaichallenge, database, ai
cover_image:
canonical_url:
---

> Companion code: [MukundaKatta/ragvitals-gemma-demo](https://github.com/MukundaKatta/ragvitals-gemma-demo). The pgai + Ollama path is the `demo/pgai_ollama_run.py` entry point.

If you have ever shipped RAG to production, you know the discovery: the day you swap a model, change an embedder, or re-index the corpus, **something will move that you did not expect**. Faithfulness drops. Retrieval recall sags. Query intent shifts under your nose. Most of the time you find out from a support ticket, not a dashboard.

The point of this post is to show that you can build the whole pipeline on open source and still have the observability part be a first-class citizen, not an afterthought you bolt on later. The stack:

- **pgvector** for vector storage
- **pgai** for embedding and generation, run inline as SQL functions
- **Ollama** for serving Gemma 2 9B (generator), Llama 3.1 8B (judge), and Nomic Embed (embedder), all locally
- **ragvitals** for a 5-dimensional drift report over every call

No managed embedding endpoint. No API key. The model weights live on your machine, the vectors live in your Postgres, and the alarm logic lives in a tiny Python library you can read end to end.

## Why Postgres for the vector layer

The temptation when you start a RAG project is to reach for a dedicated vector database. Pinecone, Weaviate, Qdrant, Milvus. They are good products. They are also another piece of infrastructure to operate, secure, and budget.

pgvector is good enough for most teams. pgvectorscale's StreamingDiskANN index scales to billions of vectors without leaving Postgres. pgai puts the embedding and generation calls inside the database itself, so your retrieval-and-generation step is one round trip, not three. Backups, migrations, observability, access control, and replication all use the same Postgres tooling you already know.

For a team that already runs Postgres, this is the lowest-friction RAG stack on the planet.

## The setup

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres \
    timescale/timescaledb-ha:pg17-all

# Inside the container:
psql -U postgres -c "CREATE EXTENSION vector;"
psql -U postgres -c "CREATE EXTENSION ai CASCADE;"

# Locally:
ollama pull gemma2:9b
ollama pull llama3.1:8b
ollama pull nomic-embed-text
```

That gets you a working Postgres with both extensions and three Ollama-served models ready on `http://localhost:11434`.

## The schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS ai CASCADE;

CREATE TABLE rag_docs (
    id         text PRIMARY KEY,
    content    text NOT NULL,
    embedding  vector(768)
);
```

That's it. One table, one vector column. No separate embedding service, no Redis cache, no message queue.

## Embed on insert with pgai

`pgai.ollama_embed` is a SQL function that calls your local Ollama server and returns the embedding as a `vector`. Insert with the embedding derived from the content directly:

```sql
INSERT INTO rag_docs (id, content, embedding)
VALUES (
    'doc-prompt-cache-1',
    'Anthropic prompt caching: ephemeral cache_control on content blocks reduces input cost by 90% on cache reads.',
    ai.ollama_embed('nomic-embed-text',
                    'Anthropic prompt caching: ephemeral cache_control on content blocks reduces input cost by 90% on cache reads.')::vector
);
```

The vectorizer pattern (`pgai.create_vectorizer`) wraps this in a background worker that watches a base table and keeps an embedding table in sync. For the demo I do it inline at insert time because the codebase is 12 docs and the workflow is "ingest once, retrieve forever." For a real production corpus the vectorizer is the right answer.

## Retrieve with one SQL statement

```sql
SELECT id, content,
       1 - (embedding <=> ai.ollama_embed('nomic-embed-text', $1)::vector) AS score
FROM rag_docs
ORDER BY embedding <=> ai.ollama_embed('nomic-embed-text', $1)::vector
LIMIT 3;
```

The `<=>` operator is cosine distance. `1 - cosine_distance` gives you a cosine similarity score you can rank by. The retrieval and the embedding both happen inside Postgres in a single round trip.

## Generate with pgai.ollama_generate

```sql
SELECT ai.ollama_generate('gemma2:9b',
    'Answer using only the context:' || E'\n' || $context || E'\n\nQ: ' || $query || E'\nA:'
)->>'response';
```

`ai.ollama_generate` returns a JSON object; `->>'response'` extracts the generated text. Wrap that with the standard "answer using only the context" RAG prompt and you have generation inside the same database round trip as retrieval.

## Wire it into ragvitals

The whole point of building this on open source is to **trust but verify**. Every retrieved row, every generation, every judge score becomes a `Trace` fed into a `Detector`:

```python
from ragvitals import (
    Detector, EmbeddingDrift, InMemorySink, JudgeDrift,
    QueryDistribution, ResponseQuality, RetrievalRelevance, Trace,
)

q = QueryDistribution(); q.set_reference(corpus_embeddings)
e = EmbeddingDrift();    e.set_reference(corpus_embeddings)
j = JudgeDrift(score_key="faithfulness")
j.set_reference({f"ref-{i}": 0.85 for i in range(10)})

det = Detector(dimensions=[
    q,
    RetrievalRelevance(metric="hit_rate", k=3),
    e,
    ResponseQuality(score_keys=["faithfulness", "relevance"]),
    j,
], sinks=[InMemorySink()])

for query in production_queries:
    retrieved = retrieve(conn, query, k=3)              # pgai inline retrieval
    answer    = generate(conn, query, [r[1] for r in retrieved], "gemma2:9b")
    scores    = judge(conn, query, answer, "llama3.1:8b")

    det.ingest(Trace(
        timestamp=now(),
        query=query,
        query_embedding=pgai_embed(conn, query),
        retrieval_scores=[r[2] for r in retrieved],
        relevance_labels=[1] + [0] * (len(retrieved) - 1),
        response=answer,
        judge_scores=scores,
        metadata={"model": "gemma2:9b"},
    ))

print(det.report())
```

Run this once a day, commit the window, and you have a rolling baseline of what "normal" looks like. The next time you swap to `llama3.1:8b` as the generator, you will see `ResponseQuality.faithfulness` move and the other four dimensions stay flat. If you swap to a different embedder, `QueryDistribution` and `EmbeddingDrift` move and the others stay flat. The dimension that fires identifies the cause.

## What this stack gets right

- **Single round trip**. The retrieval, the embedding of the live query, and the generation all happen inside Postgres via `pgai`. No fan-out, no marshaling between three services.
- **Open weights end to end**. Gemma 2 and Llama 3.1 are both open-source. Nomic Embed is open-source. No API key, no rate limit, no per-token billing.
- **Observability is a library, not a vendor**. `ragvitals` writes to whatever sink you configure. The default is an in-memory ring; the demo also ships a JSONL sink and a CloudWatch sink behind an optional dependency.
- **Everything is a SQL function**. If your ops team can debug Postgres, they can debug this stack.

## What this stack does NOT solve

- **GPU economics**. Running Gemma 2 9B locally takes a real GPU or at least an Apple-Silicon Mac with 32GB of RAM. For a production team this means a dedicated inference box, not the same machine running Postgres.
- **Multi-tenant isolation**. The demo uses a single `rag_docs` table. For multi-tenant SaaS you want row-level security or schema-per-tenant; pgvector indexes scale fine but the policy layer is on you.
- **Long-context retrieval**. Top-3 cosine is the right default for short docs. If your corpus is structured (code, contracts, papers), a hybrid BM25 + vector approach beats pure cosine. Look at TigerData's `pg_search` or a separate Elasticsearch shard.

## Reproduce

```bash
git clone https://github.com/MukundaKatta/ragvitals-gemma-demo
cd ragvitals-gemma-demo
pip install -e ".[pgai]"

# Start Postgres + pgai
docker run --rm -d -p 5432:5432 --name pgai \
    -e POSTGRES_PASSWORD=postgres timescale/timescaledb-ha:pg17-all

# Start Ollama (separate terminal) + pull models
ollama pull gemma2:9b
ollama pull llama3.1:8b
ollama pull nomic-embed-text

# One-time ingest
python demo/pgai_ollama_run.py --setup

# Run 20 queries and print the drift report
python demo/pgai_ollama_run.py --run --n 20
```

Output looks like this:

```
=== ragvitals report ===
  QueryDistribution                    ok         value= 0.0312       n=20
  RetrievalRelevance                   ok         value= 0.8000       n=20
  EmbeddingDrift                       ok         value= 0.0312       n=20
  ResponseQuality.faithfulness         ok         value= 0.7950       n=20
  JudgeDrift                           ok         value=    n/a       n=0
  degraded: []
  warned:   []
```

Now swap the generator to `llama3.1:8b`, re-run, and compare. If your `RetrievalRelevance` shifts, your queries shifted. If only `ResponseQuality` shifts, you changed the generator and the rest of the pipeline is invariant, which is exactly what you wanted to confirm.

## The libraries used

- [`ragvitals`](https://github.com/MukundaKatta/ragvitals): the 5-dimensional drift detector.
- [`bedrockcache`](https://github.com/MukundaKatta/bedrockcache) and [`bedrockstack`](https://github.com/MukundaKatta/bedrockstack): siblings of ragvitals for teams running Anthropic on AWS Bedrock instead of Ollama locally.
- [pgai](https://github.com/timescale/pgai) and [pgvector](https://github.com/pgvector/pgvector): the database side, built and maintained by Tiger Data (formerly Timescale) and Andrew Kane respectively.
- [Ollama](https://ollama.com/): the runner.

## Closing

The open-source AI stack is good enough for production RAG today. The piece most teams skip is monitoring, because it has historically meant adopting a platform. It does not have to. Five dimensions, a sink protocol, and a library you can read end to end is enough to catch the silent regressions before they become support tickets.

If you build something with this stack, comment with what your monitoring caught the first time you swapped a component. That is the highest-signal feedback there is.
