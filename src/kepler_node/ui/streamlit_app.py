"""Streamlit operator console for the Kepler Node.

Entrypoint: ``streamlit run src/kepler_node/ui/streamlit_app.py``

Five mobile-first surfaces are provided as tabs:
- **Overview**: node readiness, blockers, calibration action, planner mode, time, and power status.
- **Equipment**: active profile visibility and profile selection.
- **Target**: staged target review, run-plan summary, and session start.
- **Session**: current Claw state, workflow intent, session controls.
- **Review**: latest frames, artifacts, outcome, and terminal acknowledgment actions.

The UI is a thin consumer of the local API; it owns no orchestration logic.
All state changes are made by calling the API through ``KeplerApiClient``.
"""

from __future__ import annotations

import os
from typing import Any

try:
    import streamlit as st
except ImportError as exc:  # pragma: no cover
    raise ImportError("streamlit is required.  Install with: uv pip install streamlit") from exc

from kepler_node.ui.api_client import KeplerApiClient

_API_BASE = os.environ.get("KEPLER_API_BASE_URL", "http://localhost:8000")

# ------------------------------------------------------------------ #
# Page config                                                          #
# ------------------------------------------------------------------ #

st.set_page_config(
    page_title="Kepler Node",
    page_icon="🔭",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ------------------------------------------------------------------ #
# Shared client                                                        #
# ------------------------------------------------------------------ #


@st.cache_resource
def _client() -> KeplerApiClient:
    return KeplerApiClient(base_url=_API_BASE)


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

_HEALTH_EMOJI = {
    "healthy": "🟢",
    "degraded": "🟡",
    "unhealthy": "🔴",
}

_STATE_VOCAB = {
    "ready": "Healthy",
    "calibrate": "Healthy",
    "target_acquired": "Healthy",
    "test_capture": "Healthy",
    "solve": "Healthy",
    "correct": "Healthy",
    "center_verify": "Healthy",
    "capture": "Healthy",
    "guard": "Healthy",
    "recover": "Recovering",
    "paused": "Paused",
    "completed": "Stopped",
    "failed": "Stopped",
    "boot": "Degraded",
    "discover": "Degraded",
    "connect": "Degraded",
}


def _vocab_label(state: str) -> str:
    return _STATE_VOCAB.get(state, state.capitalize())


def _device_status_display(device_info: dict[str, Any]) -> str:
    status = device_info.get("status")
    if status == "remote_control_ready":
        return "✅ Remote Ready"
    if device_info.get("connected"):
        return "✅ Connected"

    if status == "autocapture_mode":
        return "⚠ Auto Capture Mode"
    if status == "card_reader_mode":
        return "⚠ Card Reader Mode"
    if status == "detected_unknown_mode":
        return "⚠ USB Mode Unsupported"
    if status == "not_initialized":
        return "⏳ Not initialized"
    if status == "pending_connect":
        return "🟡 Pending connect"
    return "❌ Not connected"


def _camera_readiness_display(camera_info: dict[str, Any]) -> str:
    status = camera_info.get("status")
    if status == "remote_control_ready" or camera_info.get("ready"):
        return "✅ Ready"
    if status in {"autocapture_mode", "card_reader_mode", "detected_unknown_mode"}:
        return "❌ Not Ready"
    if status == "not_initialized":
        return "⏳ Not initialized"
    if status == "pending_connect":
        return "🟡 Pending connect"
    return "❌ Not connected"


def _camera_mode_display(camera_info: dict[str, Any]) -> str:
    status = camera_info.get("status")
    if status == "remote_control_ready":
        return "Remote Control"
    if status == "autocapture_mode":
        return "Auto Capture"
    if status == "card_reader_mode":
        return "SD Card Mode"
    if status == "detected_unknown_mode":
        return "Unknown USB Mode"
    if status == "not_initialized":
        return "Not initialized"
    if status == "pending_connect":
        return "Pending connect"
    return "Disconnected"


def _show_blockers(blockers: list[dict[str, Any]]) -> None:
    for b in blockers:
        st.error(f"⛔ **{b['name']}**: {b['summary']}")
        if b.get("operator_action_required"):
            st.caption(f"Action required: {b['operator_action_required']}")


def _show_degraded(degraded: list[dict[str, Any]]) -> None:
    for d in degraded:
        st.warning(f"⚠️ **{d['name']}**: {d['summary']}")


def _planner_mode_copy(
    planner_mode: str,
    planner_conn: dict[str, Any] | None,
) -> tuple[str, str, list[str], list[tuple[str, str]]]:
    if planner_mode == "headless-node":
        return (
            "Headless Remote Planner",
            "Use Remote KStars/Ekos",
            [
                "Open KStars/Ekos on a laptop or another trusted client.",
                "Add the node as a remote INDI server using the host and port below.",
                "Return here to review the staged target and start the managed session.",
            ],
            [
                ("Planner Transport", "Remote KStars/Ekos over INDI"),
                ("INDI Port", str(planner_conn.get("indi_port", "—")) if planner_conn else "—"),
            ],
        )

    if planner_mode == "field-fallback":
        return (
            "Field Local-First",
            "Open Local Planner Session",
            [
                "Connect to the node with an RDP client using the port below.",
                "Use the on-node KStars/Ekos session to choose framing and capture intent.",
                "Return here for calibration, verification, and managed session control.",
            ],
            [
                ("Planner Transport", "On-node KStars/Ekos via xRDP"),
                ("RDP Port", str(planner_conn.get("rdp_port", "—")) if planner_conn else "—"),
            ],
        )

    return (
        planner_mode,
        "Planner Guidance",
        [planner_conn.get("summary", "Planner details unavailable") if planner_conn else "Planner details unavailable"],
        [],
    )


# ------------------------------------------------------------------ #
# Tabs                                                                 #
# ------------------------------------------------------------------ #

overview_tab, equipment_tab, target_tab, session_tab, review_tab = st.tabs(
    ["Overview", "Equipment", "Target", "Session", "Review"]
)


# ===================================================================  #
# OVERVIEW                                                              #
# ===================================================================  #

with overview_tab:
    st.header("Overview")
    client = _client()

    # Fetch data
    try:
        health = client.get_health()
        readiness = client.get_readiness()
        node_status = client.get_node_status()
    except Exception as exc:
        st.error(f"Cannot reach Kepler API at {_API_BASE}: {exc}")
        st.stop()

    # --- Mobile-priority section: status and blockers above fold ---
    col_status, col_health = st.columns(2)
    with col_status:
        state_vocab = _vocab_label(node_status.get("state", ""))
        st.metric("Node Status", state_vocab)
        st.metric("State", node_status.get("state", "—"))
    with col_health:
        health_icon = _HEALTH_EMOJI.get(health.get("status", ""), "❓")
        st.metric("Health", f"{health_icon} {health.get('status', '—').capitalize()}")
        st.metric("Control Lock", "🔒 Locked" if node_status.get("control_locked") else "🔓 Free")

    # Blockers
    blockers = readiness.get("blockers", [])
    if blockers:
        st.subheader("⛔ Blocking Issues")
        _show_blockers(blockers)
    degraded_list = readiness.get("degraded", [])
    if degraded_list:
        st.subheader("⚠️ Warnings")
        _show_degraded(degraded_list)

    if not blockers:
        st.success("✅ No blocking issues")

    st.divider()

    # --- Readiness detail ---
    ready_icon = "✅" if readiness.get("ready") else "❌"
    cal_icon = "✅" if readiness.get("calibrated") else "⚠️"
    time_icon = "✅" if readiness.get("time_trusted") else "❌"

    col_r, col_c, col_t = st.columns(3)
    with col_r:
        st.metric("Ready", ready_icon)
    with col_c:
        st.metric("Calibrated", cal_icon)
    with col_t:
        st.metric("Time Trusted", time_icon)

    # Storage
    storage = readiness.get("storage_summary", {})
    if storage:
        free_gb = storage.get("free_bytes", 0) / (1024**3)
        total_gb = storage.get("total_bytes", 1) / (1024**3)
        st.metric("Storage Free", f"{free_gb:.1f} GB / {total_gb:.1f} GB")

    # Network mode
    st.metric("Network Mode", node_status.get("network_mode", "—"))

    # Time / power
    time_cert = node_status.get("time_certainty", {})
    power = node_status.get("power_integrity", {})
    col_tc, col_pw = st.columns(2)
    with col_tc:
        st.metric(
            "Time Source",
            time_cert.get("source", "—"),
            help=time_cert.get("summary"),
        )
    with col_pw:
        power_icon = "✅" if power.get("healthy") else "⚠️"
        st.metric("Power", f"{power_icon} {power.get('summary', '—')}")

    # Detected hardware (derived from adapter connection state)
    devices = node_status.get("detected_devices", {})
    if devices:
        mount_info = devices.get("mount", {})
        camera_info = devices.get("camera", {})
        col_m, col_c = st.columns(2)
        with col_m:
            st.metric(
                "Mount",
                _device_status_display(mount_info),
            )
        with col_c:
            st.metric(
                "Camera",
                _device_status_display(camera_info),
            )
            st.metric("Camera Readiness", _camera_readiness_display(camera_info))
            st.metric("Camera Mode", _camera_mode_display(camera_info))
            if camera_info.get("summary"):
                st.caption(camera_info["summary"])

    st.divider()

    # --- Overview actions (above fold on mobile) ---
    st.subheader("Actions")

    # Time confirm: show when time is not trusted
    if not readiness.get("time_trusted"):
        st.warning("⏰ Node time is not trusted.  Confirm the current time to unblock calibration.")
        confirmed_at = st.text_input(
            "Confirmed time (RFC 3339, e.g. 2025-06-01T22:00:00Z)",
            key="time_confirm_input",
        )
        if st.button("🕐 Confirm Time", key="time_confirm_btn"):
            if confirmed_at:
                try:
                    resp = client.post_time_confirm(confirmed_at)
                    if resp.get("applied"):
                        st.success(f"Time confirmed: {resp.get('summary', 'ok')}")
                    else:
                        st.error(
                            f"Time confirmation failed: {resp.get('summary', 'unknown error')}"
                        )
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            else:
                st.warning("Enter a timestamp before confirming.")

    # Calibrate: show when ready or target_acquired and not yet calibrated
    current_state = node_status.get("state", "")
    if current_state in {"ready", "target_acquired"} and not readiness.get("calibrated"):
        if not blockers:
            if st.button("🔭 Calibrate", key="calibrate_btn"):
                try:
                    resp = client.post_calibrate()
                    st.success(resp.get("message", "Calibration started"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        else:
            st.info("Resolve blocking issues above before starting calibration.")

    # --- Planner mode ---
    planner_mode = node_status.get("planner_mode")
    planner_conn = node_status.get("planner_connection_details")
    if planner_mode:
        st.divider()
        st.subheader("🔭 Planner Mode")
        mode_label = {
            "headless-node": "Headless Node (remote KStars/Ekos)",
            "field-fallback": "Field Fallback (on-node KStars + xRDP)",
        }.get(planner_mode, planner_mode)
        st.metric("Mode", mode_label)
        if planner_conn:
            st.info(planner_conn.get("summary", ""))
            if planner_conn.get("host"):
                st.metric("Node Address", planner_conn["host"])
            if planner_conn.get("indi_port"):
                st.code(f"INDI server port: {planner_conn['indi_port']}")
            if planner_conn.get("rdp_port"):
                st.code(f"xRDP port: {planner_conn['rdp_port']}")
            indi_r = planner_conn.get("indi_reachable")
            if indi_r is not None:
                st.metric("INDI Service", "✅ reachable" if indi_r else "⚠️ unreachable")
            kepler_r = planner_conn.get("kepler_reachable")
            if kepler_r is not None:
                st.metric("Kepler API", "✅ reachable" if kepler_r else "⚠️ unreachable")
            xrdp_r = planner_conn.get("xrdp_reachable")
            if xrdp_r is not None:
                st.metric("xRDP Service", "✅ reachable" if xrdp_r else "⚠️ unreachable")

        planner_title, planner_action, planner_steps, planner_metrics = _planner_mode_copy(
            planner_mode,
            planner_conn,
        )
        st.markdown(f"### {planner_title}")
        st.markdown(f"**Next Action:** {planner_action}")
        metric_columns = st.columns(max(1, len(planner_metrics)))
        for column, (label, value) in zip(metric_columns, planner_metrics, strict=False):
            with column:
                st.metric(label, value)
        for index, step in enumerate(planner_steps, start=1):
            st.markdown(f"{index}. {step}")

    # --- Node build info ---
    inst = node_status.get("install_manifest")
    if inst:
        st.divider()
        st.caption(
            f"Build: {inst.get('kepler_version', '—')} · "
            f"Profile: {inst.get('bootstrap_profile', '—')} · "
            f"Installed: {inst.get('installed_at', '—')}"
        )


# ===================================================================  #
# EQUIPMENT                                                             #
# ===================================================================  #

with equipment_tab:
    st.header("Equipment Profiles")
    client = _client()

    try:
        profiles_resp = client.get_equipment_profiles()
    except Exception as exc:
        st.error(f"Cannot reach Kepler API: {exc}")
        st.stop()

    profiles = profiles_resp.get("profiles", [])
    active_profile_id = profiles_resp.get("active_profile_id")

    if not profiles:
        st.info("No equipment profiles configured.  Add one below.")
    else:
        for prof in profiles:
            pid = prof.get("profile_id", "")
            name = prof.get("display_name", pid)
            is_active = pid == active_profile_id
            is_default = prof.get("is_default", False)

            label = f"{'✅ ' if is_active else ''}**{name}**"
            if is_default:
                label += " _(default)_"
            hw = prof.get("hardware_summary", {})
            detail = (
                f"Mount: {hw.get('mount_model', '—')} · "
                f"Camera: {hw.get('camera_make', '')} {hw.get('camera_model', '—')} · "
                f"Lens: {hw.get('lens_model', '—')}" + (" ⚠️ Zoom" if hw.get("lens_is_zoom") else "")
            )

            with st.expander(label):
                st.caption(detail)
                if not is_active:
                    if st.button(f"Select {name!r}", key=f"select_profile_{pid}"):
                        try:
                            client.post_equipment_profile_select(pid)
                            st.success(f"Profile {name!r} is now active")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                else:
                    st.success("Currently active profile")

    st.divider()
    st.subheader("Add Profile (JSON)")
    st.caption("Paste a valid EquipmentProfile JSON document to import a profile.")
    profile_json_input = st.text_area("Profile JSON", height=200, key="profile_json_input")
    if st.button("➕ Add Profile", key="add_profile_btn"):
        import json as _json

        try:
            body = _json.loads(profile_json_input)
            resp = client.post_equipment_profile(body)
            st.success(f"Profile {resp.get('profile', {}).get('display_name', 'imported')!r} added")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


# ===================================================================  #
# TARGET                                                                #
# ===================================================================  #

with target_tab:
    st.header("Target & Session Start")
    client = _client()

    try:
        node_status_t = client.get_node_status()
        current_target = client.get_target_current()
    except Exception as exc:
        st.error(f"Cannot reach Kepler API: {exc}")
        st.stop()

    current_state_t = node_status_t.get("state", "")
    active_prof_t = node_status_t.get("active_equipment_profile")

    # Show active equipment profile context
    if active_prof_t:
        st.caption(
            f"Active profile: **{active_prof_t.get('display_name', '—')}** · "
            f"Focal length: {active_prof_t.get('focal_length_mm', '—')} mm"
            + (
                " ⚠️ Zoom lens — focal length assumption required"
                if active_prof_t.get("lens_is_zoom")
                else ""
            )
        )
    else:
        st.warning("No equipment profile selected.  Go to the Equipment tab first.")

    # Current staged target
    if current_target:
        st.subheader("Staged Target")
        col_tl, col_ra, col_dec = st.columns(3)
        with col_tl:
            st.metric("Target", current_target.get("target_label", "—"))
        with col_ra:
            st.metric("RA (h)", f"{current_target.get('ra_hours', 0):.4f}")
        with col_dec:
            st.metric("Dec (°)", f"{current_target.get('dec_deg', 0):.4f}")
        st.caption(f"Source: {current_target.get('target_source', '—')}")
        run_params = current_target.get("run_parameters", {})
        if run_params:
            st.markdown("**Run Plan Summary**")
            col_exp, col_cam, col_stop = st.columns(3)
            with col_exp:
                st.metric("Exposure", f"{run_params.get('exposure_seconds', '—')} s")
            with col_cam:
                st.metric(
                    "Camera Settings",
                    ", ".join(
                        f"{key}={value}" for key, value in run_params.get("camera_settings", {}).items()
                    )
                    or "—",
                )
            with col_stop:
                stop_condition = run_params.get("stop_condition", {})
                st.metric(
                    "Stop Condition",
                    ", ".join(f"{key}={value}" for key, value in stop_condition.items()) or "—",
                )
            if current_target.get("target_source") == "kstars_ekos":
                st.info(
                    "Target intent came from KStars/Ekos. Kepler will still verify framing locally before capture."
                )
            else:
                st.info(
                    "This target was staged locally in Kepler. The node will still verify framing locally before capture."
                )

        if st.button("🗑 Clear Target", key="clear_target_btn"):
            try:
                client.delete_target_current()
                st.success("Target cleared")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        if current_state_t == "ready":
            st.divider()
            st.subheader("▶ Start Session")
            has_run_params = bool(
                run_params.get("exposure_seconds")
                and run_params.get("camera_settings")
                and run_params.get("stop_condition")
            )
            if not has_run_params:
                st.warning(
                    "Run parameters (exposure_seconds, camera_settings, stop_condition) are required."
                )
            else:
                if st.button("🚀 Start Session", key="start_session_btn"):
                    try:
                        resp = client.post_session_start()
                        st.success(resp.get("message", "Session started"))
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        elif current_state_t in {
            "target_acquired",
            "test_capture",
            "solve",
            "correct",
            "center_verify",
            "capture",
            "guard",
            "recover",
        }:
            st.info("Session in progress.  See the Session tab for controls.")
    else:
        st.info("No target staged.")

    st.divider()
    st.subheader("Stage a Target")

    with st.form("stage_target_form"):
        target_label = st.text_input("Target Name", placeholder="e.g. M31")
        ra_hours = st.number_input(
            "RA (hours)", min_value=0.0, max_value=24.0, step=0.001, format="%.4f"
        )
        dec_deg = st.number_input(
            "Dec (degrees)", min_value=-90.0, max_value=90.0, step=0.001, format="%.4f"
        )
        target_source = st.selectbox("Source", ["manual", "kstars_ekos", "catalog"], index=0)

        st.markdown("**Run Parameters**")
        exposure_seconds = st.number_input(
            "Exposure (seconds)", min_value=1.0, step=1.0, value=120.0
        )
        gain = st.number_input("Camera Gain", min_value=0, step=1, value=100)
        frame_count = st.number_input("Frame Count Limit", min_value=1, step=1, value=60)

        submitted = st.form_submit_button("📡 Stage Target")
        if submitted:
            if not target_label.strip():
                st.error("Target name is required.")
            else:
                body = {
                    "target_label": target_label.strip(),
                    "ra_hours": ra_hours,
                    "dec_deg": dec_deg,
                    "target_source": target_source,
                    "run_parameters": {
                        "exposure_seconds": exposure_seconds,
                        "camera_settings": {"gain": gain},
                        "stop_condition": {"frame_count": int(frame_count)},
                    },
                }
                try:
                    client.post_target(body)
                    st.success(f"Target {target_label!r} staged")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

with session_tab:
    st.header("Session")
    client = _client()

    try:
        session_state = client.get_session_state()
    except Exception as exc:
        st.error(f"Cannot reach Kepler API: {exc}")
        st.stop()

    if session_state is None:
        st.info("No active managed session.  Calibrate or start a session from the Overview.")
    else:
        # --- Mobile-priority: current state, problem, next action above fold ---
        state = session_state.get("state", "—")
        intent = session_state.get("workflow_intent") or "—"
        vocab = _vocab_label(state)

        col_s, col_i = st.columns(2)
        with col_s:
            st.metric("State", state)
            st.metric("Status", vocab)
        with col_i:
            st.metric("Intent", intent)
            lock_icon = "🔒" if session_state.get("control_locked") else "🔓"
            st.metric("Control", lock_icon)

        latest_msg = session_state.get("latest_message")
        if latest_msg:
            st.info(f"ℹ️ {latest_msg}")

        blockers = session_state.get("blockers", [])
        if blockers:
            st.subheader("⛔ Blockers")
            _show_blockers(blockers)

        degraded_list = session_state.get("degraded", [])
        if degraded_list:
            _show_degraded(degraded_list)

        # Pause summary
        pause = session_state.get("pause_summary")
        if pause:
            with st.expander("Pause Details"):
                st.write(f"**Reason:** {pause.get('pause_reason', '—')}")
                st.write(f"**Resume to:** {pause.get('resume_state', '—')}")
                if pause.get("operator_action_required"):
                    st.warning(f"Action required: {pause['operator_action_required']}")

        st.divider()

        # --- Session controls ---
        st.subheader("Controls")
        col_p, col_r, col_s2, col_rl = st.columns(4)

        with col_p:
            if st.button("⏸ Pause", disabled=(state == "paused")):
                try:
                    resp = client.post_session_pause()
                    st.success(resp.get("message", "Paused"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        with col_r:
            if st.button("▶ Resume", disabled=(state != "paused")):
                try:
                    resp = client.post_session_resume()
                    st.success(resp.get("message", "Resumed"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        with col_s2:
            if st.button("⏹ Stop"):
                try:
                    resp = client.post_session_stop()
                    st.success(resp.get("message", "Stopped"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        with col_rl:
            if state != "paused":
                st.caption(
                    "To hand control to external KStars/Ekos, pause the session first, "
                    "then Release Control."
                )
            if st.button("🔓 Release Control", disabled=(state != "paused")):
                try:
                    resp = client.post_session_release_control()
                    st.success(resp.get("message", "Control released"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))


# ===================================================================  #
# REVIEW                                                                #
# ===================================================================  #

with review_tab:
    st.header("Review")
    client = _client()

    try:
        outcome = client.get_session_outcome()
        frames_resp = client.get_session_frames(limit=10)
        artifacts_resp = client.get_session_artifacts()
    except Exception as exc:
        st.error(f"Cannot reach Kepler API: {exc}")
        st.stop()

    # --- Mobile-priority: latest frame and outcome above fold ---
    frames = frames_resp.get("frames", [])
    if frames:
        latest = frames[0]
        st.subheader("Latest Frame")
        col_fid, col_acc = st.columns(2)
        with col_fid:
            st.metric("Frame ID", latest.get("frame_id", "—"))
        with col_acc:
            st.metric("Decision", latest.get("acceptance_summary", "—"))
        q = latest.get("quality_summary", {})
        if q:
            st.caption("Quality Summary")
            st.json(q)
        solve = latest.get("solve_summary", {})
        if solve:
            with st.expander("Solve Summary"):
                st.json(solve)
    else:
        st.info("No frames recorded yet.")

    # Outcome
    if outcome:
        st.divider()
        st.subheader("Session Outcome")
        terminal = outcome.get("terminal_outcome", "—")
        state_val = outcome.get("state", "—")
        col_o1, col_o2 = st.columns(2)
        with col_o1:
            st.metric("Outcome", terminal)
        with col_o2:
            st.metric("Final State", state_val)

        # Stop / failure explanation above terminal actions
        stop_reason = outcome.get("stop_reason")
        failure_explanation = outcome.get("failure_explanation")
        if stop_reason:
            st.info(f"ℹ️ Stop reason: {stop_reason}")
        if failure_explanation:
            st.error(f"❌ Failure explanation: {failure_explanation}")

        # Terminal actions
        if state_val == "completed":
            if st.button("✅ Acknowledge Complete"):
                try:
                    resp = client.post_session_acknowledge_complete()
                    st.success(resp.get("message", "Acknowledged"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        if state_val == "failed":
            st.warning("Session ended in failure.  Review blockers before clearing.")
            if st.button("🗑 Clear Failure"):
                try:
                    resp = client.post_session_clear_failure()
                    st.success(resp.get("message", "Failure cleared"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    st.divider()

    # Full frame list
    if frames:
        st.subheader(f"Frames ({len(frames)})")
        for fr in frames:
            with st.expander(f"{fr.get('frame_id')} — {fr.get('acceptance_summary')}"):
                st.write(f"Captured: {fr.get('capture_timestamp')}")
                q = fr.get("quality_summary", {})
                if q:
                    st.json(q)

    # Artifacts
    artifacts = artifacts_resp.get("artifacts", [])
    if artifacts:
        st.subheader(f"Artifacts ({len(artifacts)})")
        for art in artifacts:
            st.write(f"- **{art.get('artifact_kind')}** — `{art.get('relative_path')}`")
