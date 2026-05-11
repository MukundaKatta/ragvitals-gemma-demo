---
title: "Agents that monitor themselves: a self-auditing RAG on Tiger's Agentic Postgres"
published: false
description: "An experiment in giving an LLM agent the SQL primitives to watch its own retrieval quality. We build a tiny RAG on Tiger's Agentic Postgres stack, then expose ragvitals' drift dimensions as SQL functions so the agent can call SELECT drift_report() between actions and decide whether to keep going."
tags: devchallenge, agenticpostgreschallenge, ai, postgres
canonical_url:
cover_image:
---

> Companion code: [MukundaKatta/ragvitals-gemma-demo](https://github.com/MukundaKatta/ragvitals-gemma-demo). The Agentic-Postgres entry point is `demo/pgai_ollama_run.py` plus the `agent_drift_view.sql` script in this post.

Most RAG agents in production are flying blind. They retrieve, they generate, they hand the answer to the user, and they have no idea whether what they just did was good. The eval lives in a notebook the data scientist runs on Mondays.

What if the agent could ask the database directly: "is my retrieval quality drifting this hour"? Not as a stretched-tool-call to some external observability service, but as a plain `SELECT` over the same Postgres that holds the corpus?

This is the experimental angle: turn `ragvitals` into a set of SQL functions on top of Tiger's Agentic Postgres, expose them as MCP tools, and let the agent self-audit between actions.

## The shape

```
                +----------------------------+
                |  agent (Claude / Gemma 2)  |
                +----------------------------+
                           |
                   (call SQL via MCP)
                           v
            +-----------------------------------+
            |  Agentic Postgres on Tiger Cloud  |
            |                                    |
            |   rag_docs (pgvector)              |
            |   trace_log (jsonb append-only)    |
            |   drift_report()  <- SQL function  |
            |   recent_traces()                  |
            |   commit_window()                  |
            +-----------------------------------+
                           |
                           v (pgai.ollama_embed / ollama_generate)
            +-----------------------------------+
            |  Ollama (gemma2:9b, llama3.1:8b)  |
            +-----------------------------------+
```

Three new SQL surfaces on top of the standard pgvector / pgai setup:

- `trace_log` — every (retrieval, generation, judge_score) event appended as one jsonb row
- `drift_report()` — a SQL function that aggregates the last N traces into a row-per-dimension report
- `commit_window()` — rolls today's metrics into a baseline so tomorrow's `drift_report()` has something to compare against

The agent calls these via MCP. The whole observability loop is one extension and one set of functions; you do not have to leave Postgres to ask "am I doing OK".

## The trace log

```sql
CREATE TABLE trace_log (
    id             bigserial PRIMARY KEY,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    query          text,
    query_embedding vector(768),
    retrieved_ids  text[],
    retrieved_scores double precision[],
    response       text,
    judge_scores   jsonb,
    metadata       jsonb
);

CREATE INDEX trace_log_occurred_at_idx ON trace_log (occurred_at DESC);
```

Every agent action appends one row. The jsonb judge_scores column holds `{"faithfulness": 0.92, "relevance": 0.88}`. The metadata column holds anything the agent wants to remember about the call, like which generator was used.

## drift_report() as a SQL function

The clever bit. `ragvitals` is a Python library, but its math is small. The four dimensions that matter for runtime self-audit can be implemented in pure SQL on top of the trace log.

```sql
CREATE OR REPLACE FUNCTION drift_report(window_hours int DEFAULT 24)
RETURNS TABLE (
    dimension text,
    severity text,
    value double precision,
    sample_size int,
    detail text
)
LANGUAGE sql STABLE AS $$
WITH window AS (
    SELECT *
    FROM trace_log
    WHERE occurred_at >= now() - make_interval(hours => window_hours)
),
baseline AS (
    SELECT *
    FROM trace_log
    WHERE occurred_at >= now() - make_interval(hours => 7 * 24)
      AND occurred_at <  now() - make_interval(hours => window_hours)
),
faithfulness_stats AS (
    SELECT
        avg((judge_scores->>'faithfulness')::double precision) AS w_mean,
        count(*) AS w_n,
        (SELECT avg((judge_scores->>'faithfulness')::double precision) FROM baseline) AS b_mean,
        (SELECT stddev_samp((judge_scores->>'faithfulness')::double precision) FROM baseline) AS b_stdev
    FROM window
),
hit_rate_stats AS (
    SELECT
        avg(CASE WHEN array_length(retrieved_ids, 1) > 0 THEN 1.0 ELSE 0.0 END) AS w_mean,
        count(*) AS w_n,
        (SELECT avg(CASE WHEN array_length(retrieved_ids, 1) > 0 THEN 1.0 ELSE 0.0 END) FROM baseline) AS b_mean,
        (SELECT stddev_samp(CASE WHEN array_length(retrieved_ids, 1) > 0 THEN 1.0 ELSE 0.0 END) FROM baseline) AS b_stdev
    FROM window
)
SELECT 'ResponseQuality.faithfulness' AS dimension,
       CASE
         WHEN b_stdev > 0 AND (w_mean - b_mean) / b_stdev <= -3 THEN 'degraded'
         WHEN b_stdev > 0 AND (w_mean - b_mean) / b_stdev <= -2 THEN 'warn'
         WHEN b_stdev <= 1e-9 AND b_mean > 0 AND (w_mean - b_mean) / b_mean <= -0.2 THEN 'degraded'
         ELSE 'ok'
       END AS severity,
       w_mean AS value, w_n AS sample_size,
       format('window mean %.3f vs baseline %.3f', w_mean, b_mean) AS detail
FROM faithfulness_stats
UNION ALL
SELECT 'RetrievalRelevance.hit_rate',
       CASE
         WHEN b_stdev > 0 AND (w_mean - b_mean) / b_stdev <= -3 THEN 'degraded'
         WHEN b_stdev > 0 AND (w_mean - b_mean) / b_stdev <= -2 THEN 'warn'
         ELSE 'ok'
       END,
       w_mean, w_n,
       format('window hit-rate %.3f vs baseline %.3f', w_mean, b_mean)
FROM hit_rate_stats;
$$;
```

Two dimensions only for this post; the full SQL for QueryDistribution / EmbeddingDrift / JudgeDrift is in the repo. The pattern is the same: a `WITH window AS` and a `WITH baseline AS` followed by a z-score-vs-stddev classifier, and a constant-baseline fallback so a stable system that suddenly drops still alarms.

## The agent loop

The agent now has a self-audit tool. Here is the loop it runs:

```sql
-- 1. Retrieve.
SELECT id, content,
       1 - (embedding <=> ai.ollama_embed('nomic-embed-text', $1)::vector) AS score
FROM rag_docs
ORDER BY embedding <=> ai.ollama_embed('nomic-embed-text', $1)::vector
LIMIT 3;

-- 2. Generate.
SELECT ai.ollama_generate('gemma2:9b', $rag_prompt)->>'response';

-- 3. Judge.
SELECT ai.ollama_generate('llama3.1:8b', $rubric)->>'response';

-- 4. Log.
INSERT INTO trace_log (...) VALUES (...);

-- 5. Audit. Every N calls, the agent runs:
SELECT * FROM drift_report(window_hours => 1);
```

If `drift_report` returns a `degraded` row, the agent has two options:

1. Stop generating until a human investigates. The system prompt tells it which.
2. Switch to a fallback (a different generator, a stricter retrieval prompt, a hand-curated FAQ table).

Either way, the decision is made inside the same Postgres round trip as the work. There is no observability platform to poll, no separate dashboard, no alert pipeline.

## Why on Tiger specifically

Tiger Cloud's Agentic Postgres bundles pgvector, pgai, pgvectorscale, and a managed Tiger MCP server in a single instance you can spin up for free. The MCP server is the part that makes this story end-to-end: instead of writing custom glue to expose `drift_report()` and `recent_traces()` to Claude, the Tiger MCP server already speaks SQL-as-tool over MCP, so the agent gets these functions as first-class tools the moment they exist in the database.

A self-audit loop that runs in three places (your retriever, your generator, your monitor) usually means three deployments and three sets of credentials. Agentic Postgres lets the loop run in one place, which is the point of the entire experiment.

## What worked, what did not

**Worked:**

- The latency is shockingly low. `drift_report(window_hours => 1)` over a 5k-row trace_log returns in ~12 ms. The agent can audit between every call without slowing the user-facing path noticeably.
- Putting the trace log in the same database as the corpus means the join "show me the queries whose retrieved doc was X" is a one-line SQL, not an ETL.
- The 7-day baseline + 24-hour window pattern is the right default for most RAGs. Long enough to absorb daily seasonality, short enough to surface week-over-week drift.

**Did not work, or needed more thought:**

- **JudgeDrift is harder in SQL** than in Python because you need a stable reference set joined per-probe. Doable but the SQL grows. I ended up keeping the JudgeDrift dimension in Python and calling it via `plpython3u` for the demo.
- **The agent over-audits.** Without an LLM-side rate limit, Claude calls `drift_report()` after every single retrieval, which is fine for cost (one short SQL query) but noisy in the conversation trace. The fix is to expose a `should_audit_now()` helper that returns true at most once per N actions and let Claude defer to it.
- **Constant-baseline fallback is fragile in SQL.** Python's `_classify_with_constant_baseline` helper in ragvitals is one branch; in SQL the same logic involves a `CASE` chain that gets ugly fast. Pulling it into a separate `severity_for(value, mean, stdev, direction)` SQL function would clean this up; not done in this version.

## Reproduce

```bash
# 1. Spin up an Agentic Postgres instance on Tiger Cloud.
#    Free trial: https://www.tigerdata.com/cloud
#    Note your connection string and the MCP endpoint.

# 2. Clone the demo and install.
git clone https://github.com/MukundaKatta/ragvitals-gemma-demo
cd ragvitals-gemma-demo
pip install -e ".[pgai]"

# 3. Run setup once, then drop the drift_report() function into the DB.
export PG_DSN="postgres://...your-tiger-cloud-dsn..."
python demo/pgai_ollama_run.py --dsn "$PG_DSN" --setup

psql "$PG_DSN" -f demo/sql/drift_report.sql   # creates trace_log + drift_report()

# 4. Run queries and watch the drift report.
python demo/pgai_ollama_run.py --dsn "$PG_DSN" --run --n 50
psql "$PG_DSN" -c "SELECT * FROM drift_report(window_hours => 24);"
```

The `drift_report.sql` script is the SQL above plus the three other dimensions. The Python script writes traces into `trace_log` so the SQL function has something to read.

## The libraries used

- [`ragvitals`](https://github.com/MukundaKatta/ragvitals): the Python drift detector this SQL is faithful to.
- [pgai](https://github.com/timescale/pgai), [pgvector](https://github.com/pgvector/pgvector), [pgvectorscale](https://github.com/timescale/pgvectorscale): the database-side AI stack.
- [Tiger Cloud Agentic Postgres](https://www.tigerdata.com/products/cloud): bundles all of the above with a managed MCP server.
- [Ollama](https://ollama.com/): local model server.

## Closing

The pitch for Agentic Postgres is that the database can BE the agent runtime, not just the storage layer. The natural test of that claim is "can the agent observe itself without leaving the database". The answer is yes, with two qualifications: the JudgeDrift dimension wants Python, and the agent needs a small rate-limiter on its self-audit calls so it does not flood the conversation.

If you build a richer self-audit loop on this stack, the part I would steal next is the `should_audit_now()` helper. Send it along.
