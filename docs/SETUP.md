# Setup

This document describes the supported Kepler Node setup posture and the current v1 readiness boundary.

## Goal

The v1 install posture is a profile-based bootstrap flow on a 64-bit Raspberry Pi OS Lite install.

The intended operator experience is:

1. Flash a supported Raspberry Pi OS image.
2. Boot the Pi and connect over the local network or maintenance shell.
3. Clone the repo.
4. Run one supported bootstrap command with a profile selection.
5. Reboot if required.
6. Open the Kepler UI and continue guided setup.

## Supported Deployment Modes

Kepler Node is intended to support two operator modes.

### Field Local-First

- The Pi hosts the Kepler services.
- The Pi also has on-node KStars/Ekos installed.
- The operator reaches the planner session through the supported remote-desktop path (xRDP on port 3389).
- Kepler remains responsible for readiness, verification, correction, capture control, and recovery.

### Headless With Remote Planner

- The Pi hosts the Kepler services and hardware stack.
- KStars/Ekos runs on a laptop or another client on the same network.
- The remote client connects to the node-side services.
- Kepler still owns control-lock, verification, correction, and recovery.

## Supported Node Install And Upgrade

`bootstrap.sh` and `upgrade.sh` at the repo root are the supported install and upgrade entry points.

### Supported Profiles

| Profile | Use case |
|---|---|
| `headless-node` | INDI + Kepler on Pi; KStars/Ekos on a remote client over the local network |
| `field-fallback` | Same as headless-node, plus on-node KStars/Ekos accessible via xRDP (port 3389) |

### Bootstrap Command

```bash
# Headless mode (recommended for permanent observatory setups)
sudo ./bootstrap.sh --profile headless-node

# Field fallback (Pi is the sole computer in the field)
sudo ./bootstrap.sh --profile field-fallback

# With custom data directory
sudo ./bootstrap.sh --profile headless-node --data-dir /data/kepler
```

Bootstrap steps:
1. Installs system prerequisites (`indi-full`, `astrometry.net`, `gpsd`, etc.)
2. Installs `uv` and syncs Python dependencies
3. Creates data directory structure
4. Writes an install manifest to `$DATA_DIR/install_manifest.json`
5. Installs and starts the `kepler-node` systemd service
6. Configures INDI server service (`headless-node`) or xRDP/KStars (`field-fallback`)
7. Runs post-install health checks and prints a connection summary

Post-install summary shows the Kepler API URL, planner connection details (INDI port or xRDP port 3389), and any health-check warnings.

### Upgrade Command

```bash
sudo ./upgrade.sh

# Upgrade to a specific release tag
sudo ./upgrade.sh --release v1.2.0
```

Upgrade steps:
1. Reads the existing install manifest (fails if none — run bootstrap first)
2. Stops managed services in dependency order (`kepler-ui` → `kepler-node` → `indiserver`), then pulls latest code (or checks out the specified release)
3. Syncs Python dependencies
4. Updates the install manifest with new version and upgrade timestamp (`in-progress`)
5. Restarts managed services in dependency order (`indiserver` → `kepler-node` → `kepler-ui` if present)
6. Runs post-upgrade health checks; on success, persists `success` outcome; on failure, persists `health-checks-failed` and exits 1

## Current Readiness Posture

The repo now includes the main install, upgrade, profile, target-intake, session-start, and operator-runbook surfaces required by the v1 handoff.

What still needs to be proven before calling the product truly v1-ready is narrower:

- GPS-backed time and location need to be exercised as a real node capability rather than remaining mostly a contract and fallback path.
- Both supported planner modes need full end-to-end validation on a bootstrapped Raspberry Pi install, not just repo-local tests and docs.
- Bootstrap and upgrade health checks need continued real-hardware shakeout so the documented install story stays reproducible.

## Repo Development Quick Start

The current repo is still development-first rather than turnkey deployment-first.

```bash
uv sync --group dev --extra local-api --extra ui
uv run ruff check .
uv run pytest
uv run --extra local-api kepler-node serve
```

Start the Streamlit UI in another shell:

```bash
KEPLER_API_BASE_URL=http://127.0.0.1:8000 \
uv run --extra ui streamlit run src/kepler_node/ui/streamlit_app.py
```

On a bootstrapped node both services start automatically. For day-to-day operation see [RUNBOOKS.md](RUNBOOKS.md).
