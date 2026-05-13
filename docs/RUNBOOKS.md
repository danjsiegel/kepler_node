# Runbooks

This document captures the intended operator runbooks for Kepler Node v1.

These runbooks are part of the Phase 5 product surface. They are here so setup and operation do not depend on internal notes or ad hoc memory.

## Runbook 1: First Boot

Use this after the node has been installed or freshly booted.

1. SSH into the node or open a maintenance shell.
2. Verify the kepler-node service is running:
   ```bash
   systemctl status kepler-node
   journalctl -u kepler-node -n 50
   ```
3. Find the node's IP address: `hostname -I`
4. Open the Kepler UI in a browser: `http://<NODE_IP>:8501`
   (or start it manually: `KEPLER_API_BASE_URL=http://<NODE_IP>:8000 uv run --extra ui streamlit run src/kepler_node/ui/streamlit_app.py`)
5. Check the Overview tab: readiness, storage, time source, power integrity, and detected devices.
6. Go to the Equipment tab and confirm or select the active equipment profile.
7. Resolve any blocking readiness issues before calibration.

## Runbook 2: Field Local-First Session

Use this when the Pi must host the full workflow on its own (field-fallback profile).

1. Open the Kepler UI (`http://<NODE_IP>:8501`).
2. Verify the Overview tab shows planner mode: **Field Fallback (on-node KStars + xRDP)**.
3. Connect an RDP client to `<NODE_IP>:3389` to reach the on-node KStars/Ekos.
   ```
   RDP client → <NODE_IP>:3389
   ```
4. In KStars/Ekos, plan the target (choose the object, framing, and run parameters).
5. Return to the Kepler UI → Target tab, stage the target with your planned run parameters, then review and confirm.
6. Go to the Overview tab and press **Calibrate** if not yet calibrated.
7. When calibration completes, go to the Target tab and press **Start Session**.
8. Monitor session state and recovery actions in the Session tab.

## Runbook 3: Headless Remote-Planner Session

Use this when a laptop or other client runs KStars/Ekos (headless-node profile).

1. Open the Kepler UI (`http://<NODE_IP>:8501`).
2. Check the Overview tab: planner mode should read **Headless Node (remote KStars/Ekos)** and show the INDI port (default: `7624`).
3. In KStars/Ekos on your laptop, add an INDI server pointing at the node:
   - Host: `<NODE_IP>`
   - Port: `7624`
4. Connect KStars/Ekos and plan the target on the remote client (choose the object, framing, and run parameters).
5. Return to the Kepler UI → Target tab, stage the target with your planned run parameters, then confirm or adjust as needed.
6. Go to the Overview tab and press **Calibrate** if not yet calibrated.
7. When calibration completes, go to the Target tab and press **Start Session**.
8. Monitor session state and recovery actions in the Session tab.

## Runbook 4: Calibration

1. Place the rig roughly facing north.
2. Open the Kepler UI.
3. Press `Calibrate`.
4. Wait for the bounded solve-and-correct loop to finish or pause.
5. If paused, follow the operator-facing guidance before retrying.

## Runbook 5: Pause, Stop, And Release Control

Use this when handing control back to the operator or an external client.

1. Pause the active managed session.
2. Review the current blocker or pause reason.
3. If Kepler should stop owning the workflow, use release-control from the paused state.
4. If the session should terminate normally, use stop.
5. Do not let external software take direct control during an active Kepler-managed session unless control has been released or the session has terminated.

## Runbook 6: Upgrade

1. SSH into the node.
2. Check the current install manifest in the Kepler UI (Overview tab → build info) or:
   ```bash
   cat $KEPLER_DATA_DIR/install_manifest.json
   ```
3. Run the supported upgrade command:
   ```bash
   sudo ./upgrade.sh
   # or for a specific release:
   sudo ./upgrade.sh --release v1.2.0
   ```
4. The upgrader runs preflight checks, stops managed services, pulls latest code, syncs dependencies, updates the manifest, restarts services, and runs post-upgrade health checks. On failure it persists a `health-checks-failed` outcome and exits 1.
5. Review the upgrade summary printed at the end.
6. Confirm the expected planner mode and node services still come up correctly in the Kepler UI.

## Runbook 7: Common Recovery Cases

### Time Uncertain

1. Review whether trusted time is available from GPS, network, RTC, or operator-confirmed fallback.
2. Apply the supported time confirmation flow only when the node is not in active motion or capture.
3. Recheck readiness before calibration or session start.

### Focal Length Assumption Required

1. Review the active profile and lens state.
2. Provide or confirm the focal-length assumption when live focal length is unavailable or untrusted.
3. Recalibrate or re-center if the prior centering assumption is no longer valid.

### External Control Conflict

1. Stop issuing motion or capture commands from external software.
2. Review the pause reason in the Kepler UI.
3. Decide whether to resume the Kepler-managed session or release control explicitly.

### Storage Critically Low

1. Do not start or continue capture until storage trust is restored.
2. Confirm the active data path and available space.
3. Resolve the storage issue before retrying.

## Scope

These are the intended v1 operator runbooks.

As Phase 5 lands, this file should be updated alongside the real bootstrap flow, planner-launch affordances, and upgrade tooling so the documented runbooks always match the supported product experience.