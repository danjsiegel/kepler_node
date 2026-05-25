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
- Kepler supervises the session: readiness gating, quality analysis, anomaly detection, intervention policy, and recovery. KStars/Ekos owns primary capture execution.

### Headless With Remote Planner

- The Pi hosts the Kepler services and hardware stack.
- KStars/Ekos runs on a laptop or another client on the same network.
- The remote client connects to the node-side services.
- Kepler supervises the session from the node side: conflict detection, control ownership rules, independent frame verification, intervention policy, and recovery.

## Supported Node Install And Upgrade

`bootstrap.sh` and `upgrade.sh` at the repo root are the supported install and upgrade entry points.

### Supported Profiles

| Profile | Use case |
|---|---|
| `headless-node` | INDI + Kepler on Pi; KStars/Ekos on a remote client over the local network |
| `field-fallback` | Same as headless-node, plus on-node KStars/Ekos accessible via xRDP (port 3389) |

The supported install posture is one provisioned node image with the full supported package set present. The selected profile controls the runtime posture, connection details, and profile-specific health expectations rather than choosing between two different package-install footprints.

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
1. Installs the full supported system package set (`astrometry.net`, `gpsd`, `gphoto2`, INDI server packages, KStars, xRDP, etc.)
2. Installs `uv` and syncs Python dependencies
3. Creates data directory structure
4. Writes an install manifest to `$DATA_DIR/install_manifest.json`
5. Disables the GVFS gphoto desktop monitor so GUI sessions do not auto-claim USB PTP cameras away from Kepler
6. Installs and starts the `kepler-node` systemd service
7. Configures INDI server service and the supported remote-planner services
8. Runs post-install health checks and prints a connection summary

Post-install summary shows the Kepler API URL, planner connection details (INDI port or xRDP port 3389), and any health-check warnings.

### GPS And RTC During Bootstrap

The supported bootstrap always installs `gpsd` and the optional `indi-gpsd` package when the distro ships it, but they serve different roles:

- Kepler trusted time and location use the node's `gpsd` path directly.
- The DS3231 RTC remains a Raspberry Pi OS clock source, not an INDI driver.
- The INDI GPSD driver is only needed when you want KStars/Ekos to ingest the node GPS over the managed INDI profile.

If you only want Kepler to trust GPS-backed time, the default bootstrap is enough.

If you also want the managed Ekos profile to expose GPS through INDI, enable it explicitly:

```bash
sudo env KEPLER_ENABLE_INDI_GPSD=true ./bootstrap.sh --profile headless-node

# Existing node: refresh services and keep the GPSD driver in the managed profile
sudo env KEPLER_ENABLE_INDI_GPSD=true ./upgrade.sh
```

That choice is persisted in the install manifest so later upgrades keep the same managed INDI driver list unless you override `KEPLER_INDI_PROFILE_DRIVERS` yourself.

### Upgrade Command

```bash
sudo ./upgrade.sh

# Upgrade to a specific release tag
sudo ./upgrade.sh --release v1.2.0
```

Upgrade steps:
1. Reads the existing install manifest (fails if none — run bootstrap first)
2. Stops managed services in dependency order (`kepler-ui` → `kepler-node` → `indiwebmanager`), then pulls latest code (or checks out the specified release)
3. Syncs Python dependencies
4. Refreshes managed service units and disables the GVFS gphoto desktop monitor so active GUI sessions release attached cameras
5. Updates the install manifest with new version and upgrade timestamp (`in-progress`)
6. Restarts managed services in dependency order (`indiwebmanager` → `kepler-node` → `kepler-ui` if present)
7. Runs post-upgrade health checks; on success, persists `success` outcome; on failure, persists `health-checks-failed` and exits 1

If a Fuji or other USB PTP camera appears in `lsusb` but Kepler still reports it disconnected, the usual cause is a desktop-session auto-mounter such as `gvfsd-gphoto2` claiming the device. The supported bootstrap and upgrade flow now masks that monitor so Kepler can claim the camera through `gphoto2`.

## Deployment Workflow

The supported deployment model is pull-on-node, not push-from-hosted-CI.

Why this is the supported posture:

- the live Kepler services run from the repo path captured in the `kepler-node` systemd unit `WorkingDirectory`
- a random GitHub Actions checkout is not the live install path
- the Pi may only be reachable on the tailnet or local network, so hosted runners are the wrong place to own deploy authority

Supported deployment paths:

1. First install: run `bootstrap.sh` manually on the Pi.
2. Routine updates: run `upgrade.sh` on the Pi or invoke it remotely over SSH against the live install path.
3. Automated updates on the Pi: use a self-hosted GitHub Actions runner on the node and run the manual deploy workflow in [.github/workflows/deploy-pi.yml](../.github/workflows/deploy-pi.yml).

`bootstrap.sh` is a first-install entry point only. If an install manifest already exists, treat the node as an upgrade case and use `upgrade.sh` instead.

The deploy helper in [scripts/deploy_pi.sh](../scripts/deploy_pi.sh) is intentionally Pi-local:

- it discovers the live repo path from `systemctl show kepler-node --property=WorkingDirectory --value`
- it runs `upgrade.sh` from that live install path
- it then runs [scripts/pi_smoke.py](../scripts/pi_smoke.py) as a post-deploy smoke check against the restarted services

Automated Pi deploys require passwordless `sudo` for the runner user. Without that, keep deploys manual over SSH and run `sudo ./upgrade.sh` yourself.

### Hardware Smoke Checks

Before bootstrap, or while validating clocks, GPS, and power on a bare Pi, run the smoke checker directly:

```bash
ssh <node> 'python3 - --require-gps-fix' < scripts/pi_smoke.py
```

After bootstrap, require the full Kepler stack as well:

```bash
ssh <node> 'python3 - --require-kepler-stack --expect-profile headless-node --require-gps-fix' < scripts/pi_smoke.py
```

To validate that a USB camera is not just visible on the bus but is actually in the remote-control mode Kepler requires, add the camera flag as well:

```bash
ssh <node> 'python3 - --require-kepler-stack --expect-profile headless-node --require-gps-fix --require-camera-remote-mode' < scripts/pi_smoke.py
```

If that check fails while `lsusb` still shows the camera, the usual causes are:

- the desktop GVFS gphoto monitor is still claiming the device
- the camera body is in storage/PTP mode instead of USB remote-control / tethered-shooting mode

The manual GitHub Actions hardware-smoke workflow in [.github/workflows/pi-hardware-smoke.yml](../.github/workflows/pi-hardware-smoke.yml) runs the same checks on a self-hosted Pi runner.

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
