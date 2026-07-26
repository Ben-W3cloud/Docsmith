"""MkDocs site assembly: write markdown + mkdocs.yml, build, zip.

This module takes the generated markdown files and turns them into a static
website using MkDocs with the Material theme.  The final output is a zip
archive containing the built site.

---------------------------------------------------------------------------
Why MkDocs?
---------------------------------------------------------------------------
MkDocs is a fast, Python-friendly static-site generator.  The Material theme
gives us a polished, searchable docs site with minimal configuration.  The
``mkdocs build`` command produces a ``site/`` directory of plain HTML/CSS/JS
that can be served by any static host.

---------------------------------------------------------------------------
Vercel constraint
---------------------------------------------------------------------------
On Vercel's serverless runtime, ``mkdocs build`` may fail if the environment
lacks the necessary binaries or if the build exceeds the function's max
duration (60s on Pro).  To handle this gracefully, ``assemble()`` accepts a
``skip_build=True`` flag that zips the raw markdown + ``mkdocs.yml`` instead
of the built site.  The caller (``jobs.py``) uses this fallback automatically
when the build fails.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml  # PyYAML is a transitive dep of mkdocs


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    """Outcome of the build step."""
    project_dir: Path   # The temp project dir (markdown + mkdocs.yml)
    site_dir: Path      # The built site/ directory (same as project_dir if skip_build)
    zip_path: Path      # Path to the final zip archive


# ---------------------------------------------------------------------------
# MkDocs navigation generation
# ---------------------------------------------------------------------------

def _nav_for(files: dict[str, str], package_name: str) -> list:
    """Build the MkDocs ``nav`` config from the generated files.

    The navigation structure is:
        - Home (index.md)
        - Getting Started
        - Guides
        - Framework Integrations
        - Advanced Usage
        - Troubleshooting
        - API Reference (sub-nav with per-module pages)
    """
    nav: list = [{"Home": "index.md"}]
    labels = {
        "getting_started.md": "Getting Started",
        "guides.md": "Guides",
        "framework_integrations.md": "Framework Integrations",
        "advanced_usage.md": "Advanced Usage",
        "troubleshooting.md": "Troubleshooting",
    }
    for fname, label in labels.items():
        if fname in files:
            nav.append({label: fname})

    # API reference sub-nav
    api_entries = sorted(k for k in files if k.startswith("api/") and k != "api/index.md")
    if "api/index.md" in files or api_entries:
        api_nav: list = []
        if "api/index.md" in files:
            api_nav.append({"Overview": "api/index.md"})
        for path in api_entries:
            # api/foo/bar.md → "foo.bar"
            label = path[len("api/"):-len(".md")].replace("/", ".")
            api_nav.append({label: path})
        nav.append({"API Reference": api_nav})
    return nav


# ---------------------------------------------------------------------------
# MkDocs configuration
# ---------------------------------------------------------------------------

def _mkdocs_config(package_name: str, nav: list) -> str:
    """Generate the ``mkdocs.yml`` content as a string.

    Uses the Material theme with a curated set of features and extensions.
    """
    cfg = {
        "site_name": f"{package_name} — SDK docs",
        "site_description": f"Auto-generated documentation for {package_name}",
        "theme": {
            "name": "material",
            "features": [
                "navigation.instant",
                "navigation.tracking",
                "navigation.sections",
                "navigation.top",
                "content.code.copy",
                "search.highlight",
                "search.suggest",
            ],
            "palette": [
                {"scheme": "default", "primary": "indigo", "accent": "indigo",
                 "toggle": {"icon": "material/weather-night", "name": "Dark mode"}},
                {"scheme": "slate", "primary": "indigo", "accent": "indigo",
                 "toggle": {"icon": "material/weather-sunny", "name": "Light mode"}},
            ],
        },
        "markdown_extensions": [
            "admonition",
            "pymdownx.details",
            "pymdownx.superfences",
            "pymdownx.highlight",
            "pymdownx.inlinehilite",
            "pymdownx.tabbed",
            "tables",
            "toc",
        ],
        "nav": nav,
    }
    return yaml.safe_dump(cfg, sort_keys=False)


# ---------------------------------------------------------------------------
# Project writing
# ---------------------------------------------------------------------------

def write_project(files: dict[str, str], package_name: str,
                  parent_dir: str | os.PathLike | None = None) -> Path:
    """Write docs/ + mkdocs.yml into a temp project dir. Returns the dir."""
    parent = Path(parent_dir) if parent_dir else Path(tempfile.mkdtemp(prefix="sdkdoc_"))
    project = parent / "project"
    docs = project / "docs"
    if project.exists():
        shutil.rmtree(project)
    docs.mkdir(parents=True)

    for rel, content in files.items():
        target = docs / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    nav = _nav_for(files, package_name)
    (project / "mkdocs.yml").write_text(_mkdocs_config(package_name, nav), encoding="utf-8")
    return project


# ---------------------------------------------------------------------------
# MkDocs build
# ---------------------------------------------------------------------------

def build_site(project_dir: Path) -> Path:
    """Run ``mkdocs build`` and return the ``site/`` directory.

    Raises ``RuntimeError`` if the build fails (non-zero exit code).
    """
    site_dir = project_dir / "site"
    if site_dir.exists():
        shutil.rmtree(site_dir)
    # We don't use --strict because it fails on missing cross-references, which
    # is too harsh for auto-generated content.
    proc = subprocess.run(
        ["mkdocs", "build", "--clean", "--site-dir", str(site_dir)],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"mkdocs build failed (rc={proc.returncode})\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return site_dir


# ---------------------------------------------------------------------------
# Zip packaging
# ---------------------------------------------------------------------------

def zip_site(site_dir: Path, out_path: Path) -> Path:
    """Create a zip archive of the built site."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, filenames in os.walk(site_dir):
            for name in filenames:
                full = Path(root) / name
                zf.write(full, arcname=full.relative_to(site_dir))
    return out_path


# ---------------------------------------------------------------------------
# One-shot assembly
# ---------------------------------------------------------------------------

def assemble(files: dict[str, str], package_name: str,
             workdir: str | os.PathLike | None = None,
             skip_build: bool = False) -> BuildResult:
    """One-shot: write project, run mkdocs build, produce a zip.

    Parameters
    ----------
    files
        ``{ relative_docs_path: markdown_content }`` from ``generator.generate_docs()``.
    package_name
        Used for the site title and the zip filename.
    workdir
        Parent directory for the temp project.  Defaults to a new temp dir.
    skip_build
        If True, zip the raw markdown + mkdocs.yml instead of the built site.
        Used as a fallback when ``mkdocs build`` fails or is unavailable.

    Returns
    -------
    BuildResult
        Contains paths to the project dir, site dir, and zip archive.
    """
    project = write_project(files, package_name, parent_dir=workdir)
    if skip_build:
        zip_path = project.parent / f"{package_name}-docs-src.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in project.rglob("*"):
                if p.is_file():
                    zf.write(p, arcname=p.relative_to(project))
        return BuildResult(project_dir=project, site_dir=project, zip_path=zip_path)

    site_dir = build_site(project)
    zip_path = project.parent / f"{package_name}-docs.zip"
    zip_site(site_dir, zip_path)
    return BuildResult(project_dir=project, site_dir=site_dir, zip_path=zip_path)