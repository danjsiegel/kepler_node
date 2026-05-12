# Kepler Node

Kepler Node is a Python-first control stack for an autonomous imaging node built around a Raspberry Pi.

The initial focus is concrete: make a Pi 5, an iEXOS-100-02 PMC-Eight mount, and a Fuji X-T5 easier to operate in the field. The public shape stays generic so the project can grow into reusable OSS instead of a one-off hardware script pile.

## Current Direction

- Hardware-first, abstraction-friendly architecture
- Python managed with `uv`
- Thin CLI for local workflows and future automation hooks
- Typed settings for device paths, data roots, and runtime behavior
- OpenCV-ready foundation for image quality checks

## Quick Start

```bash
uv sync --group dev
uv run ruff check .
uv run kepler-node --help
uv run pytest
```

## Current Repo State

- uv-managed Python scaffold
- thin CLI and typed settings
- ignored lab workspace for discovery and brainstorming
- smoke tests and basic docs