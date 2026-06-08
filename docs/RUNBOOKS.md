# Runbooks

This document captures the intended operator runbooks for Kepler Node v1.

These runbooks exist so setup and operation do not depend on internal notes or ad hoc memory.

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
4. In KStars/Ekos, plan and stage the target (choose the object, framing, and sequence parameters).
5. Go to the Kepler UI → Overview tab and press **Calibrate** if not yet calibrated.
6. Once calibration completes, return to KStars/Ekos and start your capture sequence.
7. Back in the Kepler UI → **Session tab**: when the node shows `supervision_ready`, click
   **🔭 Attach Supervision Session** to hand Kepler supervisory control over the running Ekos session.
8. Kepler transitions to `ekos_wait`, then `monitoring` once it detects the running sequence.
9. Monitor supervisory state, ownership, and any active interventions in the Session tab.
   The **Active Owner** metric shows which controller (Ekos, Kepler, or Operator) currently holds
   the workflow; the **supervisory next action** line describes what Kepler is doing or waiting for.

## Runbook 3: Headless Remote-Planner Session

Use this when a laptop or other client runs KStars/Ekos (headless-node profile).

1. Open the Kepler UI (`http://<NODE_IP>:8501`).
2. Check the Overview tab: planner mode should read **Headless Node (remote KStars/Ekos)** and show
   the INDI port (default: `7624`).
3. In KStars/Ekos on your laptop, add an INDI server pointing at the node:
   - Host: `<NODE_IP>`
   - Port: `7624`
4. Connect KStars/Ekos and plan and stage the target on the remote client (choose the object,
   framing, and sequence parameters).
5. Go to the Kepler UI → Overview tab and press **Calibrate** if not yet calibrated.
6. Once calibration completes, start the capture sequence in KStars/Ekos on your laptop.
7. Back in the Kepler UI → **Session tab**: when the node shows `supervision_ready`, click
   **🔭 Attach Supervision Session** to hand Kepler supervisory control over the running Ekos session.
8. Kepler transitions to `ekos_wait`, then `monitoring` once it detects the running sequence.
9. Monitor supervisory state, ownership, and any active interventions in the Session tab.
   The **Active Owner** metric shows which controller holds the workflow; the **supervisory next
   action** line describes what Kepler is doing or waiting for.

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

## Runbook 8: Fuji Milky Way Widefield On The Pi SSD

Use this when the goal is a simple widefield Milky Way session with the Fuji body and a wide lens, without relying on mount tracking or autofocus automation.

Assumptions:

- Fuji X-T5 with the Viltrox 13 mm lens
- Images should land on the Pi-attached SSD, not the camera SD card
- The workflow should stay simple and operator-driven rather than trying to force a full supervised session

### 1. Preflight

1. Confirm the external SSD is mounted on the Pi. The supported and simplest path is to mount it as the Kepler data root such as `/data/kepler`.
2. Create a capture directory on the SSD before opening Ekos:
   ```bash
   mkdir -p /data/kepler/captures/milky_way/$(date +%F)
   ```
3. Confirm the camera is in USB remote-control / tether mode and that the drive mode is `Single Shot`.
4. Keep the session local-first if possible so image transfer does not depend on weak Wi-Fi.

### 2. Camera Posture

1. Set the lens to manual focus.
2. Open the aperture fully for focus work.
3. Start with ISO `1600` to `3200`.
4. Start with exposure `5 s` for focus checks.
5. Do not use RAW+JPEG. Keep the capture mode simple and consistent.

### 3. Focus Sequence

1. Point near a dense star field or a bright star near the Milky Way target area.
2. In Ekos Capture, use a temporary focus exposure recipe:
   - exposure: `3 s` to `5 s`
   - binning: `1x1`
   - count: `1`
3. Take a single frame.
4. Open the returned image and zoom into the brightest stars.
5. Adjust focus manually in very small movements.
6. Repeat single captures until stars are smallest and roundest.
7. If you have a Bahtinov mask that fits this lens, use it; otherwise use repeated short captures and inspect star size manually.
8. Once focus is good, do not touch the focus ring again.

### 4. Capture Storage Settings

1. In the INDI camera tab, set upload mode to `Local` so frames are written on the Pi instead of being pushed back to the client.
2. In the Capture module, set the target directory to the SSD-backed folder created earlier, for example:
   ```text
   /data/kepler/captures/milky_way/YYYY-MM-DD
   ```
3. Do not use the camera SD card as the primary session destination.
4. If Ekos offers both client and local save paths, prefer `Local` for the real sequence and only use client-side transfer for spot checks.

### 5. Simple Milky Way Sequence

For a non-tracked 13 mm widefield run, start conservative:

1. exposure: `8 s`
2. ISO: `1600`
3. aperture: wide open or one stop down if star shapes are unacceptable wide open
4. count: `20` to `40`
5. delay: `1 s` to `2 s`

If star trailing is acceptable and the sky is dark enough, increase exposure toward `10 s` to `15 s`. If trailing becomes obvious, step back down.

### 6. Verification Before The Full Run

1. Take one test frame with the real capture settings.
2. Verify three things before starting the full sequence:
   - stars are acceptably sharp
   - framing is correct
   - the file landed on the Pi SSD path instead of the camera SD card or client machine
3. Only after that should you start the multi-frame run.

### 7. Recovery During The Run

1. If capture hangs, stop the sequence and confirm the camera is not stuck in an in-progress exposure state.
2. If frames start landing somewhere unexpected, stop immediately and fix upload mode and target directory before continuing.
3. If focus slips, pause the run and return to the single-frame manual focus loop rather than trying to recover with autofocus.

## Scope

These are the intended v1 operator runbooks.

Keep this file updated alongside the real bootstrap flow, planner-mode affordances, and upgrade tooling so the documented runbooks always match the supported product experience.