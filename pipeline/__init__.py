"""SDK Auto-Doc pipeline package.

This package contains the core processing pipeline:

    fetcher.py    — GitHub tree + raw file fetch (no clone)
    detector.py   — SDK heuristics + optional LLM classifier
    analyzer.py   — AST → IR extraction
    generator.py  — Per-section LLM doc generation + API-ref templating
    builder.py    — MkDocs assembly + build + zip
    jobs.py       — In-memory job registry with background execution
    llm.py        — Thin OpenAI-compatible LLM client wrapper
"""