"""GitHub tree fetch + selective raw-file fetch.

We never clone the repo. We hit:
  GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1  → the tree
  raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}  → file contents

Then we filter aggressively before fetching any content.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

import requests

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

# --- Filter rules ------------------------------------------------------------

# Keep patterns (fnmatch on full path OR basename)
KEEP_EXT = {".py"}
KEEP_BASENAMES = {
    "README", "README.md", "README.rst", "README.txt",
    "CHANGELOG", "CHANGELOG.md", "CHANGELOG.rst",
    "HISTORY.md", "HISTORY.rst",
    "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt",
}
KEEP_DIR_PREFIXES = ("examples/", "example/", "samples/", "docs/", "tests/", "test/")

# Drop patterns
DROP_DIR_PREFIXES = (
    ".github/", ".git/", "node_modules/", "dist/", "build/",
    ".venv/", "venv/", "__pycache__/", ".mypy_cache/", ".pytest_cache/",
    ".tox/", ".idea/", ".vscode/", "site/",  # mkdocs output
)
DROP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".pdf", ".zip", ".tar", ".gz", ".whl", ".so", ".dll",
    ".lock", ".mp4", ".mov", ".woff", ".woff2", ".ttf",
}
DROP_BASENAMES = {
    "poetry.lock", "package-lock.json", "yarn.lock", "Pipfile.lock",
    "uv.lock", ".DS_Store",
}

MAX_FILE_BYTES = 300 * 1024  # skip anything huge


@dataclass
class RepoRef:
    owner: str
    repo: str
    branch: str = "main"

    @property
    def slug(self) -> str:
        return f"{self.owner}-{self.repo}"


@dataclass
class TreeEntry:
    path: str
    size: int
    sha: str
    type: str  # "blob" | "tree"


@dataclass
class FetchedFile:
    path: str
    text: str


@dataclass
class FetchResult:
    ref: RepoRef
    entries: list[TreeEntry] = field(default_factory=list)
    files: dict[str, FetchedFile] = field(default_factory=dict)
    truncated: bool = False


# --- URL parsing -------------------------------------------------------------

_GITHUB_URL_RE = re.compile(
    r"^(?:https?://)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def parse_repo_url(url: str, branch: str = "main") -> RepoRef:
    url = url.strip().rstrip("/")
    m = _GITHUB_URL_RE.match(url)
    if not m:
        # Support pasting a full tree URL: github.com/o/r/tree/branch/...
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc.lower() == "github.com" and len(parts) >= 2:
            owner, repo = parts[0], parts[1].removesuffix(".git")
            if len(parts) >= 4 and parts[2] == "tree":
                branch = parts[3]
            return RepoRef(owner=owner, repo=repo, branch=branch)
        raise ValueError(f"Not a valid GitHub repo URL: {url!r}")
    return RepoRef(owner=m.group("owner"), repo=m.group("repo"), branch=branch)


# --- HTTP helpers ------------------------------------------------------------

def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "sdk-autodoc/1.0"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _resolve_default_branch(ref: RepoRef) -> str:
    r = requests.get(f"{GITHUB_API}/repos/{ref.owner}/{ref.repo}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json().get("default_branch", "main")


# --- Tree fetch --------------------------------------------------------------

def fetch_tree(ref: RepoRef) -> FetchResult:
    """Fetch the recursive tree, retrying with the default branch if needed."""
    url = f"{GITHUB_API}/repos/{ref.owner}/{ref.repo}/git/trees/{ref.branch}?recursive=1"
    r = requests.get(url, headers=_headers(), timeout=20)
    if r.status_code == 404:
        default = _resolve_default_branch(ref)
        if default != ref.branch:
            ref = RepoRef(ref.owner, ref.repo, default)
            url = f"{GITHUB_API}/repos/{ref.owner}/{ref.repo}/git/trees/{ref.branch}?recursive=1"
            r = requests.get(url, headers=_headers(), timeout=20)
    r.raise_for_status()
    payload = r.json()
    entries = [
        TreeEntry(path=n["path"], size=n.get("size", 0), sha=n["sha"], type=n["type"])
        for n in payload.get("tree", [])
    ]
    return FetchResult(ref=ref, entries=entries, truncated=payload.get("truncated", False))


# --- Filtering ---------------------------------------------------------------

def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _ext(path: str) -> str:
    b = _basename(path)
    if "." not in b:
        return ""
    return "." + b.rsplit(".", 1)[-1].lower()


def should_keep(path: str, size: int) -> bool:
    if size and size > MAX_FILE_BYTES:
        return False
    p = path.replace("\\", "/")
    for pref in DROP_DIR_PREFIXES:
        if p.startswith(pref) or f"/{pref}" in p:
            return False
    base = _basename(p)
    if base in DROP_BASENAMES:
        return False
    ext = _ext(p)
    if ext in DROP_EXT:
        return False

    # Keep list
    if ext in KEEP_EXT:
        return True
    if base in KEEP_BASENAMES:
        return True
    # README/CHANGELOG with any extension we didn't enumerate
    if re.match(r"^(README|CHANGELOG|HISTORY)(\.|$)", base, re.IGNORECASE):
        return True
    for pref in KEEP_DIR_PREFIXES:
        if p.startswith(pref):
            # Within keep-dir, still exclude by extension
            if ext in DROP_EXT:
                return False
            return True
    return False


def filter_tree(result: FetchResult) -> list[TreeEntry]:
    return [e for e in result.entries if e.type == "blob" and should_keep(e.path, e.size)]


# --- Raw file fetch ----------------------------------------------------------

def fetch_files(ref: RepoRef, entries: Iterable[TreeEntry], limit: int | None = None) -> dict[str, FetchedFile]:
    files: dict[str, FetchedFile] = {}
    session = requests.Session()
    session.headers.update({"User-Agent": "sdk-autodoc/1.0"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        session.headers["Authorization"] = f"Bearer {tok}"

    for i, e in enumerate(entries):
        if limit is not None and i >= limit:
            break
        url = f"{RAW_BASE}/{ref.owner}/{ref.repo}/{ref.branch}/{e.path}"
        try:
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                continue
            # Basic binary guard
            text = r.text
            if "\x00" in text[:2048]:
                continue
            files[e.path] = FetchedFile(path=e.path, text=text)
        except requests.RequestException:
            continue
    return files


def fetch_repo(repo_url: str, branch: str = "main", file_limit: int | None = 400) -> FetchResult:
    """One-shot: parse URL → fetch tree → filter → fetch selected file contents."""
    ref = parse_repo_url(repo_url, branch)
    result = fetch_tree(ref)
    kept = filter_tree(result)
    result.files = fetch_files(result.ref, kept, limit=file_limit)
    return result
