# Kepler Node Repo Instructions

## Project Identity
- Kepler Node is a Python-first control stack for an autonomous imaging node built around a Raspberry Pi.
- The current v1 implementation shape is driven by the private handoff spec in `lab/specs/V1_HANDOFF.md`.
- Preserve the separation between orchestration, hardware adapters, imaging analysis, and persistence rather than collapsing behavior into one module.

## Canonical Commands
- Use `uv` workflows for Python work in this repo.
- First-time environment setup: `uv sync --group dev`.
- Main validation path: `uv run pytest`.
- CLI inspection: `uv run kepler-node --help` and `uv run kepler-node info`.

## Change Boundaries
- Preserve the current Python package boundaries: `agent`, `camera`, `mount`, `imaging`, and `storage`.
- Keep `cli.py` thin and keep runtime settings in typed configuration modules.
- Treat solver behavior and node-management behavior as adapter concerns behind interfaces; do not create extra top-level packages unless implementation pressure clearly requires them.

## Spec-Driven Work
- For v1 behavior, API, state-machine, storage, or recovery decisions, consult `lab/specs/V1_HANDOFF.md` first.
- Treat `lab/` as private scratch and implementation-spec space. It is intentionally ignored by git, so spec changes there may not appear in normal git diff workflows.
- Promote conclusions from `lab/` into tracked docs only after they are stable.

## Commit And Safety Rules
- Never create commits, branches, or tags unless explicitly asked.
- Do not amend or rewrite history unless explicitly asked.
- Keep changes minimal and aligned with the current v1 scope rather than adding speculative architecture.
- Prefer reading `README.md`, `docs/ARCHITECTURE.md`, `pyproject.toml`, `src/kepler_node/cli.py`, `src/kepler_node/config.py`, and relevant tests before widening search.

## Current Repo Posture
- Python is managed with `uv`.
- The repo currently contains a thin CLI, typed settings, architecture notes, and a private lab/spec area.
- Add local API or UI scaffolding only when the task actually requires it; do not preemptively add broad surface area.

## Validation Guidance
- Use the smallest validation that proves the change.
- Default to `uv run pytest` when the scope touches Python behavior and no narrower check is obviously sufficient.