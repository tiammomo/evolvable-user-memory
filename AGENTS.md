# Repository Guidelines

## Architecture

This is a Python 3.12+ project using a `src` layout. Keep dependency direction as:

```text
api/adapters -> application -> domain
```

`domain` must remain framework-free: no FastAPI, Pydantic, database driver, HTTP client, or provider SDK imports. Cross-boundary interaction belongs in typed application ports. Infrastructure adapters implement those ports and must not leak persistence entities into the domain.

The five conceptual planes are evidence, belief, experience, projection, and evolution. Preserve these distinctions:

- observations and evidence are append-only facts about input;
- memory revisions are immutable beliefs derived from evidence;
- contextual utility is learned from attributable outcomes, not reads;
- recall projections are disposable and reproducible from source state;
- evolution changes bounded strategy snapshots, never authorization, tenant isolation, erasure, or audit rules.

## Commands

- `uv sync`: create/update the local environment.
- `uv run pytest`: run tests and the coverage gate.
- `uv run ruff check .`: lint all code.
- `uv run ruff format --check .`: verify formatting.
- `uv run mypy`: run strict type checking.
- `uv run evolvable-memory`: start the development API.

## Style and Tests

Use 4-space indentation, complete type annotations, immutable `dataclass` domain values, timezone-aware UTC datetimes, and explicit tenant/subject scope on every operation. Keep functions small and name commands/results by business intent.

Place tests under `tests/` with names `test_*.py`. Every behavior change must cover its business rule, isolation boundary, idempotency behavior, and error path where applicable. A retrieval test must also assert that recall alone does not mutate belief or utility.

## Security

Never accept tenant or subject scope only from untrusted payloads in a production adapter; the initial API exposes them solely as a development contract. Do not log raw evidence by default. Do not make privacy, access-control, retention, suppression, or deletion policies self-modifying.

