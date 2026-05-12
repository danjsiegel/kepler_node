# Kepler Node Safety And Gotchas

## Git And Change Control
- Do not create commits, branches, or tags unless the user explicitly asks.
- `lab/` is ignored by git, so spec work there may not show up in normal change listings.

## Spec Gotchas
- `lab/specs/V1_HANDOFF.md` is intentionally private and more detailed than the tracked public docs.
- When public docs and the handoff spec differ, use the task context to decide which is authoritative. For v1 implementation work, the handoff spec usually wins.
- Avoid reintroducing removed concepts from the spec, such as reusable named capture presets, unless the user explicitly reopens that decision.

## Repo Shape Gotchas
- The repo is still early. Do not mistake missing folders for a requirement to scaffold them immediately.
- The spec treats node management and solver behavior as important adapter boundaries, but the current codebase does not yet need separate top-level packages for them.
- Keep `cli.py` thin and do not turn it into the orchestration layer.

## Operational Assumptions
- Prefer `uv` over ad hoc Python environment commands.
- Prefer the smallest validation step that proves the change.
- Preserve the current package boundaries unless the user explicitly asks for a restructure.