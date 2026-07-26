"""In-memory job registry + background worker.

This module manages the lifecycle of documentation-generation jobs.  When a
client hits ``POST /api/generate``, a ``Job`` is created and a daemon thread
is spawned to run the full pipeline in the background.  The client polls
``GET /api/status/<job_id>`` for progress and ``GET /api/download/<job_id>``
for the final zip.

---------------------------------------------------------------------------
Data model
---------------------------------------------------------------------------
``Job`` — a dataclass holding all state for one generation request:
    id, repo_url, branch, status, progress log, error, detection result,
    repo_slug, artifact_path, timestamps.

``_REGISTRY`` — a module-level dict mapping job_id → Job.  Protected by
``_LOCK`` for thread safety.

---------------------------------------------------------------------------
Vercel / serverless caveats
---------------------------------------------------------------------------
* **In-memory state is ephemeral**.  On Vercel, cold starts destroy the
  process, so all jobs vanish.  For production use, replace ``_REGISTRY``
  with Redis / Vercel KV / Upstash.
* **Background threads may be killed** when the serverless function returns.
  Vercel Pro allows up to 60s; Hobby is 10s.  For very large repos, offload
  to a proper queue (Vercel Queues, Inngest, QStash).
* **Artifacts in /tmp are ephemeral**.  The zip is returned immediately via
  ``send_file``, so this is fine for the request/response cycle.  For
  long-term storage, use Vercel Blob or S3.
"""
from __future__ import annotations

import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------

@dataclass
class Job:
    """State for a single documentation-generation request."""
    id: str
    repo_url: str
    branch: str
    status: str = "queued"          # queued | running | done | error
    progress: list[str] = field(default_factory=list)
    error: Optional[str] = None
    detection: Optional[dict] = None
    repo_slug: str = "docs"
    artifact_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def public_view(self) -> dict:
        """Return a JSON-safe dict for API responses (hides internal fields)."""
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress[-20:],   # last 20 log lines
            "detection": self.detection,
            "error": self.error,
            "download_url": (f"/api/download/{self.id}" if self.status == "done" else None),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------
# In production, replace this with Redis / Vercel KV.

_REGISTRY: dict[str, Job] = {}
_LOCK = threading.Lock()


def get(job_id: str) -> Optional[Job]:
    """Retrieve a job by ID, or None if not found."""
    with _LOCK:
        return _REGISTRY.get(job_id)


def submit(repo_url: str, branch: str = "main") -> str:
    """Create a new job and start the pipeline in a background thread.

    Returns the job ID (12-char hex).
    """
    job = Job(id=uuid.uuid4().hex[:12], repo_url=repo_url, branch=branch)
    with _LOCK:
        _REGISTRY[job.id] = job
    # Daemon thread so it doesn't block process exit.
    t = threading.Thread(target=_run_pipeline, args=(job.id,), daemon=True)
    t.start()
    return job.id


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def _log(job: Job, msg: str) -> None:
    """Append a timestamped message to the job's progress log."""
    job.progress.append(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def _run_pipeline(job_id: str) -> None:
    """Execute the full doc-generation pipeline for a job.

    Any unhandled exception is caught and recorded on the job, setting
    status to ``error``.
    """
    # Lazy imports so the module stays cheap when Flask boots.
    from . import fetcher, detector, analyzer, generator, builder

    job = get(job_id)
    if not job:
        return
    job.status = "running"

    try:
        # ------------------------------------------------------------------
        # Step 1: Fetch the repo tree + file contents
        # ------------------------------------------------------------------
        _log(job, f"fetching repo tree: {job.repo_url} @ {job.branch}")
        result = fetcher.fetch_repo(job.repo_url, job.branch)
        job.repo_slug = result.ref.slug
        _log(job, f"fetched {len(result.files)} files after filtering "
                  f"(tree had {len(result.entries)} entries, truncated={result.truncated})")

        # ------------------------------------------------------------------
        # Step 2: Detect whether this is an SDK
        # ------------------------------------------------------------------
        _log(job, "running SDK detection")
        det = detector.detect(result)
        job.detection = det.as_dict()
        _log(job, f"detection: is_sdk={det.is_sdk} score={det.score:.2f} method={det.method}")
        if not det.is_sdk:
            _log(job, "not detected as an SDK — proceeding anyway with best-effort docs")

        # ------------------------------------------------------------------
        # Step 3: AST analysis → IR
        # ------------------------------------------------------------------
        _log(job, "analyzing Python source → IR")
        pkg = analyzer.analyze(result)
        _log(job, f"IR: {len(pkg.modules)} modules, "
                  f"{sum(len(m.classes) for m in pkg.modules)} classes")

        # ------------------------------------------------------------------
        # Step 4: Gather context (README, changelog, examples)
        # ------------------------------------------------------------------
        _log(job, "gathering README / examples / changelog context")
        context = analyzer.gather_context(result)

        # ------------------------------------------------------------------
        # Step 5: Generate documentation sections
        # ------------------------------------------------------------------
        _log(job, "generating documentation sections")
        docs = generator.generate_docs(
            pkg, context, progress=lambda m: _log(job, m)
        )
        _log(job, f"generated {len(docs)} markdown files")

        # ------------------------------------------------------------------
        # Step 6: Build MkDocs site + zip
        # ------------------------------------------------------------------
        _log(job, "building MkDocs site")
        # Use /tmp if available (Vercel provides it), else system temp.
        workdir = Path(tempfile.mkdtemp(prefix=f"sdkdoc_{job.id}_", dir="/tmp"
                                        if Path("/tmp").exists() else None))
        try:
            build = builder.assemble(docs, package_name=result.ref.repo,
                                     workdir=workdir)
        except Exception as build_err:  # noqa: BLE001
            # mkdocs build failed — fall back to zipping raw markdown.
            _log(job, f"mkdocs build failed ({build_err}); packaging source only")
            build = builder.assemble(docs, package_name=result.ref.repo,
                                     workdir=workdir, skip_build=True)

        job.artifact_path = str(build.zip_path)
        _log(job, f"artifact ready: {build.zip_path.name}")
        job.status = "done"
        job.finished_at = time.time()

    except Exception as e:  # noqa: BLE001
        # ------------------------------------------------------------------
        # Error path: record the failure and keep the job in the registry
        # so the client can retrieve the error details.
        # ------------------------------------------------------------------
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"
        job.finished_at = time.time()
        _log(job, f"ERROR: {job.error}")
        _log(job, traceback.format_exc(limit=3))