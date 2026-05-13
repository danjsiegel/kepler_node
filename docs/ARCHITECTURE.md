# Architecture

Kepler Node starts from one concrete deployment target and grows outward from stable capability boundaries.

## System View

```mermaid
flowchart LR
	Operator[Operator] --> UI[Streamlit UI]
	Operator --> CLI[Typer CLI]
	CLI --> API[FastAPI API]
	UI --> API
	API --> Claw[Claw Controller]
	Claw --> Node[Node Management]
	Claw --> Camera[Camera Adapter]
	Claw --> Mount[Mount Adapter]
	Claw --> Solver[Imaging and Solver]
	Claw --> Storage[(Filesystem Storage)]
```

## Current Posture

- Optimize first for Raspberry Pi 5 + iEXOS-100-02 PMC-Eight + Fuji X-T5
- Keep package boundaries generic so concrete adapters can be swapped later
- Prefer Python for orchestration, control flow, and data handling

## Client Model

Kepler is intended to run as the node-side control plane.

Remote planners such as KStars/Ekos can supply target intent and operator workflow around the node, but Kepler still owns local verification, correction, capture control, and recovery.

The Pi should stay a field-ready node first, not a desktop astronomy workstation by default.

## Domain Layout

- `kepler_node.agent`: orchestration and state-machine logic
- `kepler_node.camera`: camera adapters and exposure workflows
- `kepler_node.mount`: mount control and sync flows
- `kepler_node.imaging`: quality checks, solving, and image analysis
- `kepler_node.storage`: telemetry, artifacts, and session persistence

## Session Loop

```mermaid
flowchart TD
	Boot[boot] --> Discover[discover]
	Discover --> Connect[connect]
	Connect --> Ready[ready]
	Connect --> Paused[paused]

	Ready --> Calibrate[calibrate]
	Ready --> Target[target acquired]

	Calibrate --> TestCapture[test capture]
	Target --> TestCapture

	TestCapture --> Solve[solve]
	Solve --> Verify[center verify]

	Verify --> Ready
	Verify --> Correct[correct]
	Verify --> Capture[capture]

	Correct --> TestCapture
	Capture --> Guard[guard]
	Guard --> Capture
	Guard --> Recover[recover]
	Recover --> TestCapture
	Recover --> Connect

	Ready --> Paused
	Target --> Paused
	Capture --> Paused
	Recover --> Paused

	Guard --> Completed[completed]
	Paused --> Completed
	Paused --> Failed[failed]
```

## Near-Term Goal

Build enough internal structure to test automation ideas without prematurely committing to camera, mount, or model integrations.