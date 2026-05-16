---
title: "What I shipped in a weekend: 3 Python libraries, 6 articles, a Homebrew formula, and an upstream PR"
published: false
description: "A weekend log of how three small AWS Bedrock libraries went from research-doc to PyPI + Homebrew + GitHub Releases, plus six dev.to articles in adjacent challenges and an upstream PR to AWS Strands. What worked, what bottlenecked, and what I'd do differently."
tags: showdev, python, opensource, productivity
canonical_url:
cover_image:
---

Two days, end-to-end. Here is the log of what shipped, what blocked, and what the time actually went into.

## The list

Three Python libraries on PyPI:

- [`bedrockcache`](https://pypi.org/project/bedrockcache/): static auditor for Anthropic prompt caching across six Bedrock backends.
- [`bedrockstack`](https://pypi.org/project/bedrockstack/): Bedrock-aware retry policy, cost ledger, and AnthropicBedrock streaming-error normalizer.
- [`ragvitals`](https://github.com/MukundaKatta/ragvitals): five-dimensional production drift detection for RAG (still GitHub-only; PyPI was rate-limited at the time of writing).

A companion demo repo with synthetic + real-models harnesses: [`ragvitals-gemma-demo`](https://github.com/MukundaKatta/ragvitals-gemma-demo).

One Homebrew formula:

```bash
brew install MukundaKatta/tap/bedrockcache
```

Six articles on dev.to:

1. [Your RAG works on Claude. Does it work on Gemma 4?](https://dev.to/mukundakatta/your-rag-works-on-claude-does-it-work-on-gemma-4-drift-detection-across-model-families-3035)
2. [Fully open-source RAG with pgvector + pgai + Ollama](https://dev.to/mukundakatta/fully-open-source-rag-with-pgvector-pgai-ollama-and-ragvitals-watching-for-drift-2onk)
3. [Hardening a Notion MCP workflow with seven small utility MCP servers](https://dev.to/mukundakatta/hardening-a-notion-mcp-workflow-with-seven-small-utility-mcp-servers-1eho)
4. [Agents that monitor themselves on Tiger's Agentic Postgres](https://dev.to/mukundakatta/agents-that-monitor-themselves-a-self-auditing-rag-on-tigers-agentic-postgres-3661)
5. [Hardening Algolia MCP Server the same way I hardened Notion MCP](https://dev.to/mukundakatta/hardening-algolia-mcp-server-the-same-way-i-hardened-notion-mcp-seven-small-filters-8fo)
6. [A 60-line Redis sink for ragvitals](https://dev.to/mukundakatta/a-60-line-redis-sink-for-ragvitals-production-drift-in-the-same-redis-you-already-run-2p35)

One upstream PR, currently open: [strands-agents/sdk-python#2301](https://github.com/strands-agents/sdk-python/pull/2301). Adds an optional `s3_client=` kwarg to `S3SessionManager` so callers can reuse a pre-built boto3 client instead of paying for `boto3.Session(...)` + `session.client("s3", ...)` on every manager instance. Maintainable, additive, backward-compatible. 50/50 session tests pass locally.

## What actually consumed the time

If you guessed "writing code," you would be wrong. Here is where the hours actually went, sorted by surprise factor:

### 1. PyPI's "Too many new projects created" rate limit (lost about 8 hours)

I had three brand-new package names to register. PyPI has a per-account quota on first-time project creation that turns out to be tighter than the docs suggest. The rate limit is per-account, sliding-window, and shows up as `429 Too Many Requests` with body `Too many new projects created`. It does not respect `Retry-After` because it is not a per-second rate limit; it is a quota.

`bedrockcache` and `bedrockstack` got through after staggered retries. `ragvitals` still has not, despite 8+ attempts spread across hours. The unblock is to email admin@pypi.org with the project names; the response time is typically <24h.

**Lesson:** for any future PyPI-first launch, register the project names with `twine upload` of a `0.0.0a0` placeholder weeks before the real release. Names + versions both have per-account limits, but creating a project for the first time is the limit that bit me.

### 2. Name squatting on PyPI (lost 30 minutes + a rename)

The package I originally called `ragdrift` was squatted on PyPI by an unrelated person who uploaded a 0.1.0 in March and never touched it again. PyPI's PEP 541 takeover process takes weeks. The cheap fix was to rename to `ragvitals`, which is arguably a better name anyway because drift is only one of the five dimensions the library tracks.

**Lesson:** check `https://pypi.org/pypi/<name>/json` returns 404 BEFORE picking a name. I now do this as the first step of every new library scaffold.

### 3. dev.to's front-matter precedence over API fields (lost 15 minutes)

The dev.to REST API takes a `published: true` field on `POST /api/articles`. If your `body_markdown` contains YAML front-matter with `published: false`, the front-matter wins. My first publish came back as HTTP 201, looked like it worked, but the public URL 404'd because the front-matter still said `published: false`.

**Lesson:** when posting via the API, either set `published: true` in both the front-matter and the API field, or strip the `published` line from the front-matter entirely.

### 4. Em-dash stripping (recurring, low-cost)

I use em dashes naturally when writing. My voice rule excludes them from public posts. Every article needs a pre-publish `grep -c "—"` pass to confirm zero. Caught 3-5 stragglers per piece. Took 2 minutes per article; cost-of-living tax.

### 5. Cross-stack honesty (the real work)

The bulk of the actual hours went into making sure each article's claims were defensible:

- The Gemma 4 article reports `ResponseQuality.faithfulness=0.7858` when you swap from Claude to Gemma 4 in a synthetic harness. That exact number is **pinned by a pytest test** in the companion repo so CI catches it if the math drifts.
- The pgai + Ollama article describes a `demo/pgai_ollama_run.py` script that actually exists in the repo, with the SQL it shows in the prose.
- The Notion MCP hardening recipe references seven existing `@mukundakatta` MCP servers I had already shipped. None of them are vapor; the article's value is the composition, not the components.
- The Tiger Agentic Postgres article is the one place I explicitly flagged a non-runnable piece (the `drift_report.sql` is inline in the post, not committed to the repo yet). Naming the gap in the post itself is the only honest move.

The token-and-paste-velocity that makes "ship 6 articles in 2 days" possible is exactly what makes it tempting to fabricate one number, one repo link, one phantom function. Forcing each piece to have a runnable artifact in a public repo is the only thing that kept the velocity from outpacing the truth.

## What did not work

- **The Notion MCP article assumes the seven utility servers are individually well-tested.** They are CI-green at the package level, but I have not personally run the full 7-MCP stack against a live Notion workspace. The article's recipe is correct; my evidence is the component-level tests, not an end-to-end demo.
- **Amazon Q Developer Quack The Code Challenge: skipped.** The theme was specifically about Amazon Q Developer CLI usage. I have not actually used Q Developer in production for these libraries. Writing a fictional "I used Q to build bedrockcache" piece would have been dishonest, so I dropped it from the batch.
- **Trusted Publishing setup: deferred.** Once the PyPI quota clears, I want every future version to publish from a `git tag v0.x.y && git push --tags` via GitHub Actions, no token in chat. The workflow YAML is straightforward; the bottleneck is that PyPI's "pending publishers" UI requires a browser click I cannot script.

## What I'd do differently

1. **Pre-register PyPI names with a placeholder version** the moment I commit to the name. Eliminates the new-project rate-limit class of failure entirely.
2. **Write the article and the test that pins its numbers in the same commit.** I did this for the Gemma 4 piece but not for the others; the discipline made the Gemma article the strongest of the six.
3. **Stage the dev.to publishes 30 minutes apart**, not back-to-back. dev.to does not rate-limit posts the way PyPI rate-limits projects, but five articles in two hours feels spammy to followers. Cadence is its own form of quality.
4. **One repo for the demo, six article markdowns inside it.** I did this; recommend it. The article markdowns in the repo (`article-*.md`) double as draft history AND act as canonical source for re-publishing on other surfaces.

## What I'd do next

The libraries point at a wider gap I want to close: **a single open-source "AWS Bedrock + Claude in production" toolkit** that bundles `bedrockcache` + `bedrockstack` + `ragvitals` behind a coherent CLI and a documented pipeline pattern. Today they are three sibling libraries with parallel READMEs. A 200-page tutorial repo with a `cookiecutter-bedrock-rag` template would land them in users' projects with one command instead of three pip installs and a config decision.

If you are running Bedrock + Anthropic in production and any of these libraries would have caught a bug you actually hit, comment with what the bug was. The boring stuff (a $38k prompt-caching miss, a streaming-error parity gap, a re-indexed corpus that silently lost recall) is exactly the kind of signal worth feeding back into v0.2.

## Repos referenced

- [`bedrockcache`](https://github.com/MukundaKatta/bedrockcache) / [PyPI](https://pypi.org/project/bedrockcache/)
- [`bedrockstack`](https://github.com/MukundaKatta/bedrockstack) / [PyPI](https://pypi.org/project/bedrockstack/)
- [`ragvitals`](https://github.com/MukundaKatta/ragvitals) (PyPI pending)
- [`ragvitals-gemma-demo`](https://github.com/MukundaKatta/ragvitals-gemma-demo)
- [`homebrew-tap`](https://github.com/MukundaKatta/homebrew-tap)
- Upstream PR: [strands-agents/sdk-python#2301](https://github.com/strands-agents/sdk-python/pull/2301)

Two days, eleven public artifacts. Tools that turned the velocity from "tweet-thread" into "shippable" were uv, hatchling, the dev.to REST API, and a `grep -c "—"` line in my pre-publish checklist.
