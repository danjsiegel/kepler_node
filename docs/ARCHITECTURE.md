# Architecture

Kepler Node starts from one concrete deployment target and grows outward from stable capability boundaries.

## Current Posture

- Optimize first for Raspberry Pi 5 + iEXOS-100-02 PMC-Eight + Fuji X-T5
- Keep package boundaries generic so concrete adapters can be swapped later
- Prefer Python for orchestration, control flow, and data handling

## Domain Layout

- `kepler_node.agent`: orchestration and state-machine logic
- `kepler_node.camera`: camera adapters and exposure workflows
- `kepler_node.mount`: mount control and sync flows
- `kepler_node.imaging`: quality checks, solving, and image analysis
- `kepler_node.storage`: telemetry, artifacts, and session persistence

## Near-Term Goal

Build enough internal structure to test automation ideas without prematurely committing to camera, mount, or model integrations.