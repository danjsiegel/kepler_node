# Kepler Node Task Workflows

## First Reads
Start here before broad repo exploration:
- `README.md`
- `docs/ARCHITECTURE.md`
- `pyproject.toml`
- `src/kepler_node/cli.py`
- `src/kepler_node/config.py`
- relevant files under `tests/`

If the task is about v1 behavior, state machine, API, recovery, or storage contracts, also read:
- `lab/specs/V1_HANDOFF.md`

## Common Tasks

### Spec-Driven Implementation
- Use `lab/specs/V1_HANDOFF.md` as the behavioral contract.
- Find the nearest owning package before editing.
- Keep implementation aligned with the current v1 scope instead of generalizing early.

### Package Placement Questions
- Put orchestration and policy in `agent`.
- Put camera backend and capture mechanics in `camera`.
- Put mount backend and pointing-control mechanics in `mount`.
- Put solve, frame analysis, and quality heuristics in `imaging`.
- Put session layout, artifacts, and metadata persistence in `storage`.
- Avoid creating new top-level packages unless there is repeated pressure that the current boundaries cannot absorb cleanly.

### Validation
- For Python behavior changes, default to `uv run pytest` unless a narrower check is enough.
- For CLI changes, `uv run kepler-node --help` or `uv run kepler-node info` can be a cheap targeted check.

### Spec Editing
- `lab/` is ignored by git, so use direct file reads to validate spec edits.
- After spec edits, reread the changed clauses and grep for contradictory wording instead of relying on git diff alone.