from __future__ import annotations

b"""Flask entry point for the SDK Auto-Doc Generator.

This file is the single web server for both local development and Vercel's
serverless Python runtime.  All HTTP routes live here; the heavy lifting
(fetching, analysis, generation, building) is delegated to modules in the
``pipeline/`` package.

---------------------------------------------------------------------------
Vercel deployment notes
---------------------------------------------------------------------------
Vercel runs this file as a serverless function (``@vercel/python``).  The
working directory is not guaranteed, so we manually add the project root to
``sys.path`` before importing ``pipeline``.  See ``vercel.json`` for the
routing and runtime configuration.
"""

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
# When Vercel executes this file as a serverless function, the current working
# directory may differ from the project root.  We compute the project root
# (parent of ``api/``) and insert it at the front of ``sys.path`` so that
# ``import pipeline`` resolves correctly regardless of where the process starts.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# dotenv — load .env for local development
# ---------------------------------------------------------------------------
# In production (Vercel) environment variables are injected by the platform,
# so ``.env`` won't exist and this is a harmless no-op.  We import defensively
# so the app still boots even if ``python-dotenv`` is somehow unavailable.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
from flask import Flask, jsonify, render_template, request, send_file, abort

from pipeline import jobs

# Template and static folders live at the project root, not inside ``api/``,
# so we pass their absolute paths explicitly.
app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def landing():
    """Serve the marketing landing page."""
    return render_template("landing.html")


@app.route("/generate")
def generate_form():
    """Serve the repo-URL input form, optionally with a job_id to poll."""
    job_id = request.args.get("job")
    return render_template("index.html", initial_job_id=job_id)


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """Accept a repo URL + branch and kick off a background doc-generation job.

    Request body (JSON):
        { "repo_url": "https://github.com/owner/repo", "branch": "main" }

    Response (202 Accepted):
        { "job_id": "<12-char hex>", "status": "queued" }

    Validation:
        - ``repo_url`` is required and must contain ``github.com``.
        - ``branch`` defaults to ``"main"`` if omitted.
    """
    data = request.get_json(silent=True) or {}
    repo_url = (data.get("repo_url") or "").strip()
    branch = (data.get("branch") or "main").strip()

    # --- Input validation -------------------------------------------------
    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400
    if "github.com" not in repo_url:
        return jsonify({"error": "repo_url must be a GitHub URL"}), 400

    # --- Submit job -------------------------------------------------------
    # The pipeline runs in a daemon thread (see ``pipeline/jobs.py``).
    # We return immediately with the job ID so the client can poll status.
    job_id = jobs.submit(repo_url=repo_url, branch=branch)
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id: str):
    """Poll the status of a previously submitted job.

    Returns the job's public view (status, progress log, detection result,
    download URL if complete).  404 if the job ID is unknown.
    """
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify(job.public_view())


@app.route("/api/download/<job_id>", methods=["GET"])
def api_download(job_id: str):
    """Download the generated docs zip for a completed job.

    Returns 404 if the job doesn't exist, or 409 if the job exists but
    isn't finished yet.
    """
    job = jobs.get(job_id)
    if not job:
        abort(404)
    # Guard: only allow download once the build is complete.
    if job.status != "done" or not job.artifact_path:
        return jsonify({"error": "not ready", "status": job.status}), 409
    return send_file(
        job.artifact_path,
        as_attachment=True,
        download_name=f"{job.repo_slug}-docs.zip",
        mimetype="application/zip",
    )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def _not_found(_):
    """JSON 404 handler — keeps API responses consistent."""
    return jsonify({"error": "not found"}), 404


# ---------------------------------------------------------------------------
# Local dev entry point
# ---------------------------------------------------------------------------
# Run with:  python api/index.py
# In production, Vercel imports the ``app`` object directly (no __main__).
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True,
    )