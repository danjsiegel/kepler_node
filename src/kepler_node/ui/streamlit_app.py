"""Streamlit operator console for the Kepler Node (Phase 4).

Entrypoint: ``streamlit run src/kepler_node/ui/streamlit_app.py``

Three mobile-first surfaces are provided as tabs:
- **Overview**: node readiness, blockers, calibration action, time/power status.
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
    raise ImportError(
        "streamlit is required.  Install with: uv pip install streamlit"
    ) from exc

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


def _show_blockers(blockers: list[dict[str, Any]]) -> None:
    for b in blockers:
        st.error(f"⛔ **{b['name']}**: {b['summary']}")
        if b.get("operator_action_required"):
            st.caption(f"Action required: {b['operator_action_required']}")


def _show_degraded(degraded: list[dict[str, Any]]) -> None:
    for d in degraded:
        st.warning(f"⚠️ **{d['name']}**: {d['summary']}")


# ------------------------------------------------------------------ #
# Tabs                                                                 #
# ------------------------------------------------------------------ #

overview_tab, session_tab, review_tab = st.tabs(["Overview", "Session", "Review"])


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
            mount_icon = "✅" if mount_info.get("connected") else "❌"
            st.metric("Mount", f"{mount_icon} {'Connected' if mount_info.get('connected') else 'Not connected'}")
        with col_c:
            camera_icon = "✅" if camera_info.get("connected") else "❌"
            st.metric("Camera", f"{camera_icon} {'Connected' if camera_info.get('connected') else 'Not connected'}")

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
                        st.error(f"Time confirmation failed: {resp.get('summary', 'unknown error')}")
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


# ===================================================================  #
# SESSION                                                               #
# ===================================================================  #

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
