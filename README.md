# SDK Auto-Doc Generator

A Flask tool that takes a GitHub repo URL, detects whether it is an SDK, and
generates a full navigable MkDocs (Material theme) documentation site for it.

## How it works

Pipeline (all data fetched via GitHub REST API — the repo is never cloned):

1. **Fetch** the repo tree (`/repos/{owner}/{repo}/git/trees/{branch}?recursive=1`).
2. **Filter** the tree — keep `.py`, `README*`, `CHANGELOG*`, `pyproject.toml`,
   `setup.py`, `examples/`, drop images/lockfiles/build artifacts.
3. **Selective fetch** of file contents via `raw.githubusercontent.com`.
4. **SDK detection** — heuristics first (Client classes, package naming,
   README API-key mentions). LLM classifier only as fallback.
5. **AST analysis** — Python's `ast` module extracts an intermediate
   representation (IR) of classes, methods, signatures, type hints, docstrings.
   Raw source is never sent to the LLM.
6. **Per-section doc generation** — one LLM call per section (Getting Started,
   Guides, Framework Integrations, API Reference, Advanced Usage,
   Troubleshooting), fed only the IR plus curated context.
7. **MkDocs assembly** — write markdown, generate `mkdocs.yml` with Material
   theme, run `mkdocs build`.
8. **Package** — zip the built static site and return it.

The API Reference section is built deterministically from the IR (templated),
not LLM-generated, so it stays accurate.

## Routes

- `POST /api/generate` — body `{ "repo_url": "...", "branch": "main" }` →
  returns `{ "job_id": "..." }`. The pipeline runs as a background job to
  avoid Vercel serverless time limits.
- `GET  /api/status/<job_id>` — poll for job status/progress.
- `GET  /api/download/<job_id>` — returns the generated docs as a zip.
- `GET  /` — single-page frontend form.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit
export FLASK_APP=api/index.py
flask run --port 5000
```

Open http://localhost:5000.

## Deploy to Vercel

```bash
vercel link
vercel env add GITHUB_TOKEN
vercel env add LLM_API_KEY
vercel --prod
```

## Vercel constraints (important)

- **No persistent filesystem** between invocations. All generated files are
  written under `/tmp` and returned as a zip. Job state is kept in an
  in-memory dict per warm instance — for production, back this with Redis
  or Vercel KV / Blob storage.
- **Execution time limits**: 10s on Hobby, 60s on Pro. The pipeline runs
  in a background thread and the client polls `/api/status`. For very large
  repos, offload to a proper queue (Vercel Queues, Upstash QStash, etc.).
- **Cold starts**: `mkdocs build` adds latency; consider caching the built
  environment if you upgrade the plan.

## Layout

```
api/index.py           Flask app (entry point for Vercel)
pipeline/
  fetcher.py           GitHub tree + raw file fetch
  detector.py          SDK heuristics + optional LLM classifier
  analyzer.py          AST → IR extraction
  generator.py         Per-section LLM doc generation + API-ref templating
  builder.py           MkDocs assembly + build + zip
  jobs.py              In-memory job registry with background execution
  llm.py               Thin LLM client wrapper (anthropic/openai)
templates/index.html   Single-page frontend
static/app.css         Frontend styles
```
