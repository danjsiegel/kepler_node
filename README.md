# Kepler Node

![Kepler Node badge](kepler_node.png)

Kepler Node is a Python-first control stack for a field-ready astrophotography node built around a Raspberry Pi.

The current v1 implementation focuses on a concrete starter rig: a Raspberry Pi 5, an iEXOS-100-02 PMC-Eight mount, and a Fuji X-T5. The repo is organized so the control logic stays reusable even while the first release is optimized for that hardware.

The intended operating model is hybrid and local-first: the Pi must be able to host a usable field workflow with on-node KStars/Ekos when needed, while also supporting a headless mode where a laptop or other client runs KStars/Ekos remotely against the node.

Today the project includes a real Claw state machine, adapter-backed hardware and node-management boundaries, local filesystem session persistence, a local FastAPI control surface, and a thin Streamlit operator UI for the first mobile-first workflow screens.

## What It Does

- Python managed with `uv`
- Typed settings and a thin Typer CLI
- Adapter-backed node-management, camera, mount, solver, and storage layers
- Kepler Claw orchestration for boot, readiness, calibration, centering, capture, guard, recovery, pause, and terminal flows
- Local filesystem session records, event logs, frame metadata, and artifact summaries
- Local FastAPI endpoints for health, readiness, session state, session actions, and review data
- Streamlit Overview, Session, and Review screens as thin API consumers
- Conflict detection and control-lock ownership for managed sessions
- Bounded retry and recovery behavior for solve, reconnect, capture, and storage failure paths
- OpenCV-ready foundation for frame-quality heuristics

## Architecture

The codebase is split around the main runtime boundaries instead of collapsing orchestration into one module:

- `agent`: Claw state machine, session model, authorship tracking, and node-management policy
- `camera`: camera interfaces and the direct `gphoto2` path
- `mount`: mount interfaces and the INDI-backed path
- `imaging`: solver protocols and image-analysis boundaries
- `storage`: canonical filesystem persistence for sessions, events, frames, and artifacts
- `api`: local FastAPI app and response models
- `ui`: Streamlit operator console and API client

For diagrams of the runtime layout and session loop, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

For setup and operator runbooks, see [docs/SETUP.md](docs/SETUP.md) and [docs/RUNBOOKS.md](docs/RUNBOOKS.md).

## Workflow Coverage

- Boot, discover, connect, and readiness evaluation with named blockers such as time uncertainty, critically low storage, and power-integrity warnings
- Calibration, target-centering, and recovery verification through a shared `test_capture -> solve -> center_verify -> correct` loop
- Managed session ownership with `control_locked`, pause/resume semantics, release-control, stop, acknowledge-complete, and clear-failure flows
- Structured session and node events with persisted session outcome summaries
- Review surfaces for frames, artifacts, terminal outcome, stop reasons, and failure explanations
- Streamlit mobile-first Overview, Session, and Review tabs backed by the local API

## Optional Extras

- `local-api`: installs FastAPI and Uvicorn for the local REST server
- `ui`: installs Streamlit and HTTPX for the operator console
- `telemetry`: installs DuckDB for future local analytics or telemetry work
- `local-ai`: installs Ollama for optional advisory-only local model integrations

## Quick Start

```bash
uv sync --group dev --extra local-api --extra ui
uv run ruff check .
uv run kepler-node --help
uv run pytest
```

This is the current development quick start. The Phase 5 goal is a profile-based bootstrap flow for a complete Pi deployment and operator-ready runbooks.

## Local API And UI

Start the local API:

```bash
uv run --extra local-api kepler-node serve
```

Start the Streamlit UI against that API:

```bash
KEPLER_API_BASE_URL=http://127.0.0.1:8000 \
uv run --extra ui streamlit run src/kepler_node/ui/streamlit_app.py
```

## CLI

```bash
uv run kepler-node info
uv run --extra local-api kepler-node serve --help
```