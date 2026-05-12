# Kepler Node Architecture Context

## Current Repo Posture
- Kepler Node is a Python-first control stack for an autonomous imaging node.
- The current public repo shape is intentionally small: thin CLI, typed settings, tests, and architecture notes.
- The private v1 handoff spec in `lab/specs/V1_HANDOFF.md` is the most detailed implementation contract for current behavior decisions.

## Package Boundaries
- `kepler_node.agent`: orchestration and state-machine logic.
- `kepler_node.camera`: camera adapters and capture workflows.
- `kepler_node.mount`: mount control, sync, correction, and related adapters.
- `kepler_node.imaging`: image-quality checks, solve-related logic, and analysis.
- `kepler_node.storage`: session persistence, artifacts, and telemetry.

## Support Modules
- `kepler_node.cli`: local CLI entrypoints for development workflows.
- `kepler_node.config`: typed runtime settings and path configuration.

## Boundary Rules
- Keep orchestration in `agent` rather than spreading decision policy into adapters.
- Keep hardware specifics behind `camera` and `mount` interfaces.
- Keep solve and image-analysis logic in `imaging` even if an external solver backend is used.
- Keep persistence and session-record concerns in `storage`.
- Node-management and solver are important adapter concepts in the spec, but they do not yet require separate top-level packages unless implementation pressure justifies them.

## Implementation Posture
- Start concrete and hardware-aware, but keep interfaces generic enough to swap adapters later.
- Prefer explicit adapter seams over premature abstractions.
- Do not add API or UI surface area just because the spec mentions them; add them when the task actually requires them.