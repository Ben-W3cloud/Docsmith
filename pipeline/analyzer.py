"""AST-based structural analysis producing an intermediate representation (IR).

This module walks Python source files using the built-in ``ast`` module and
extracts a **structured, JSON-serialisable intermediate representation (IR)**
of classes, functions, signatures, type hints, docstrings, and imports.

Why an IR?
----------
The LLM in ``generator.py`` is fed *only* the IR — never raw source code.
This has three benefits:

1.  **Token efficiency**: the IR is far more compact than raw source.
2.  **Privacy**: proprietary implementation details stay on the server.
3.  **Accuracy**: the model can't hallucinate APIs that aren't in the IR,
    because the IR is a faithful structural extract.

The IR is intentionally lossy — it drops function bodies, comments, and
decorator arguments.  What remains is the "public surface" needed to write
useful documentation.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from typing import Optional

from .fetcher import FetchResult


# ---------------------------------------------------------------------------
# IR dataclasses
# ---------------------------------------------------------------------------
# These form the "schema" of the intermediate representation.  Every field is
# intentionally simple (str, bool, int, list) so the whole thing can be
# serialised to JSON with ``dataclasses.asdict``.

@dataclass
class ParamIR:
    """A single function/method parameter."""
    name: str
    annotation: Optional[str] = None   # Type hint as a string (or None)
    default: Optional[str] = None      # Default value as a string (or None)
    kind: str = "positional"           # positional | keyword | vararg | kwarg


@dataclass
class FunctionIR:
    """A function or method definition."""
    name: str
    qualname: str                      # Fully-qualified name (module.Class.method)
    is_async: bool
    is_method: bool                     # True if defined inside a class
    is_classmethod: bool
    is_staticmethod: bool
    is_private: bool                    # True if name starts with ``_`` (but not dunder)
    params: list[ParamIR] = field(default_factory=list)
    returns: Optional[str] = None       # Return type hint as a string
    docstring: Optional[str] = None
    decorators: list[str] = field(default_factory=list)
    lineno: int = 0                     # Line number in the source file


@dataclass
class ClassIR:
    """A class definition."""
    name: str
    qualname: str
    bases: list[str] = field(default_factory=list)  # Base class names (as strings)
    docstring: Optional[str] = None
    methods: list[FunctionIR] = field(default_factory=list)
    lineno: int = 0


@dataclass
class ModuleIR:
    """A single Python module (file)."""
    path: str                           # Relative path in the repo
    module: str                         # Dotted module name (e.g. ``pkg.client``)
    docstring: Optional[str] = None
    classes: list[ClassIR] = field(default_factory=list)
    functions: list[FunctionIR] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    all_exports: list[str] = field(default_factory=list)  # From ``__all__``


@dataclass
class PackageIR:
    """The top-level IR for an entire fetched repo."""
    name: str                           # Repo name
    modules: list[ModuleIR] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise to a plain dict (useful for debugging / JSON output)."""
        return {"name": self.name, "modules": [asdict(m) for m in self.modules]}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _annot(node: Optional[ast.AST]) -> Optional[str]:
    """Unparse an AST node to a string, or return ``None`` on failure.

    Used for type annotations and default values.
    """
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return None


def _default(node: Optional[ast.AST]) -> Optional[str]:
    """Alias for ``_annot`` — used for default argument values."""
    return _annot(node)


def _decorators(node) -> list[str]:
    """Extract decorator names as strings from an AST node."""
    out = []
    for d in getattr(node, "decorator_list", []) or []:
        try:
            out.append(ast.unparse(d))
        except Exception:  # noqa: BLE001
            pass
    return out


def _module_name(path: str) -> str:
    """Convert a file path to a dotted module name.

    e.g. ``pkg/client.py`` → ``pkg.client``
         ``pkg/__init__.py`` → ``pkg``
    """
    p = path
    if p.endswith(".py"):
        p = p[:-3]
    p = p.replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
    return p


def _extract_all(module: ast.Module) -> list[str]:
    """Extract the ``__all__`` list from a module, if present."""
    for stmt in module.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    val = stmt.value
                    if isinstance(val, (ast.List, ast.Tuple)):
                        return [
                            e.value for e in val.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        ]
    return []


def _extract_imports(module: ast.Module) -> list[str]:
    """Extract all import statements as strings."""
    out = []
    for stmt in module.body:
        if isinstance(stmt, ast.Import):
            out.extend(a.name for a in stmt.names)
        elif isinstance(stmt, ast.ImportFrom):
            mod = stmt.module or ""
            out.extend(f"{mod}.{a.name}" for a in stmt.names)
    return out


# ---------------------------------------------------------------------------
# Function / method IR extraction
# ---------------------------------------------------------------------------

def _fn_ir(node: ast.AST, qualname_prefix: str, is_method: bool = False) -> Optional[FunctionIR]:
    """Convert an ``ast.FunctionDef`` / ``ast.AsyncFunctionDef`` to ``FunctionIR``.

    Parameters
    ----------
    node
        The AST node.
    qualname_prefix
        The parent qualname (e.g. ``pkg.MyClass`` for a method).
    is_method
        True if this function is defined inside a class.
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None

    args = node.args
    params: list[ParamIR] = []

    # Positional-only args (Python 3.8+)
    pos_only = list(args.posonlyargs)
    regular = list(args.args)
    total = pos_only + regular

    # Defaults align to the tail of ``total``; pad with None for params without defaults.
    defaults = list(args.defaults)
    pad = [None] * (len(total) - len(defaults))
    aligned_defaults = pad + defaults
    for a, d in zip(total, aligned_defaults):
        params.append(ParamIR(name=a.arg, annotation=_annot(a.annotation),
                              default=_default(d), kind="positional"))

    # *args
    if args.vararg:
        params.append(ParamIR(name=args.vararg.arg, annotation=_annot(args.vararg.annotation),
                              kind="vararg"))

    # Keyword-only args
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        params.append(ParamIR(name=a.arg, annotation=_annot(a.annotation),
                              default=_default(d), kind="keyword"))

    # **kwargs
    if args.kwarg:
        params.append(ParamIR(name=args.kwarg.arg, annotation=_annot(args.kwarg.annotation),
                              kind="kwarg"))

    decs = _decorators(node)
    return FunctionIR(
        name=node.name,
        qualname=f"{qualname_prefix}.{node.name}" if qualname_prefix else node.name,
        is_async=isinstance(node, ast.AsyncFunctionDef),
        is_method=is_method,
        is_classmethod=any(d.endswith("classmethod") for d in decs),
        is_staticmethod=any(d.endswith("staticmethod") for d in decs),
        is_private=node.name.startswith("_") and not (node.name.startswith("__") and node.name.endswith("__")),
        params=params,
        returns=_annot(node.returns),
        docstring=ast.get_docstring(node),
        decorators=decs,
        lineno=node.lineno,
    )


# ---------------------------------------------------------------------------
# Module-level analysis
# ---------------------------------------------------------------------------

def analyze_module(path: str, source: str) -> Optional[ModuleIR]:
    """Parse a single Python source file and return its ``ModuleIR``.

    Returns ``None`` if the file has a syntax error (can't be parsed).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    mod_name = _module_name(path)
    mir = ModuleIR(
        path=path,
        module=mod_name,
        docstring=ast.get_docstring(tree),
        imports=_extract_imports(tree),
        all_exports=_extract_all(tree),
    )

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cir = ClassIR(
                name=node.name,
                qualname=f"{mod_name}.{node.name}",
                bases=[_annot(b) or "" for b in node.bases],
                docstring=ast.get_docstring(node),
                lineno=node.lineno,
            )
            for child in node.body:
                fn = _fn_ir(child, cir.qualname, is_method=True)
                if fn:
                    cir.methods.append(fn)
            mir.classes.append(cir)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = _fn_ir(node, mod_name, is_method=False)
            if fn:
                mir.functions.append(fn)
    return mir


def analyze(result: FetchResult) -> PackageIR:
    """Analyse all fetched ``.py`` files and return a ``PackageIR``.

    This is the main entry point called by ``pipeline/jobs.py``.
    """
    pkg = PackageIR(name=result.ref.repo)
    for path, f in sorted(result.files.items()):
        if not path.endswith(".py"):
            continue
        mir = analyze_module(path, f.text)
        if mir is not None:
            pkg.modules.append(mir)
    return pkg


# ---------------------------------------------------------------------------
# Context extraction (README, changelog, examples)
# ---------------------------------------------------------------------------

def gather_context(result: FetchResult) -> dict:
    """Return a curated dict of text context safe to send to the LLM.

    Contains:
        - ``readme``: the first README file found (truncated to 20 KiB).
        - ``changelog``: the first CHANGELOG / HISTORY file (truncated to 6 KiB).
        - ``examples``: up to 6 example/sample Python files (each truncated to 4 KiB).

    Individual example files are truncated to keep the prompt bounded.
    """
    ctx: dict = {"readme": None, "changelog": None, "examples": []}
    for path, f in result.files.items():
        base = path.rsplit("/", 1)[-1].lower()
        if base.startswith("readme") and ctx["readme"] is None:
            ctx["readme"] = f.text[:20_000]
        elif base.startswith("changelog") or base.startswith("history"):
            if ctx["changelog"] is None:
                ctx["changelog"] = f.text[:6_000]

    for path, f in result.files.items():
        low = path.lower()
        if low.startswith(("examples/", "example/", "samples/")) and path.endswith(".py"):
            ctx["examples"].append({"path": path, "text": f.text[:4_000]})
            if len(ctx["examples"]) >= 6:
                break
    return ctx