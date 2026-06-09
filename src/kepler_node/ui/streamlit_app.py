"""Streamlit operator console for the Kepler Node.

Entrypoint: ``streamlit run src/kepler_node/ui/streamlit_app.py``

Five mobile-first surfaces are provided as tabs:
- **Overview**: node readiness, blockers, calibration action, planner mode, time, and power status.
- **Equipment**: active profile visibility and profile selection.
- **Target**: read-only view of the current target Kepler observes from KStars/Ekos, equipment profile context.
- **Session**: supervisory state, active owner, intervention status, and session controls.
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
    # v1.1 supervisory states
    "ekos_wait": "Waiting",
    "monitoring": "Healthy",
    "intervening": "Intervening",
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
                "Calibrate the node here (Overview tab → Calibrate), then start your capture"
                " sequence in KStars/Ekos.",
                "Return to the Session tab and click Attach Supervision Session to hand Kepler"
                " supervisory control over the running Ekos session.",
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
                "Use the on-node KStars/Ekos session to choose framing and sequence parameters.",
                "Calibrate the node here (Overview tab → Calibrate), then start your capture"
                " sequence in KStars/Ekos.",
                "Return to the Session tab and click Attach Supervision Session to hand Kepler"
                " supervisory control over the running Ekos session.",
            ],
            [
                ("Planner Transport", "On-node KStars/Ekos via xRDP"),
                ("RDP Port", str(planner_conn.get("rdp_port", "—")) if planner_conn else "—"),
            ],
        )

    return (
        planner_mode,
        "Planner Guidance",
        [
            planner_conn.get("summary", "Planner details unavailable")
            if planner_conn
            else "Planner details unavailable"
        ],
        [],
    )


# ------------------------------------------------------------------ #
# Tabs                                                                 #
# ------------------------------------------------------------------ #

overview_tab, equipment_tab, widefield_tab, target_tab, session_tab, review_tab = st.tabs(
    ["Overview", "Equipment", "Widefield", "Target", "Session", "Review"]
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
# WIDEFIELD                                                            #
# ===================================================================  #

with widefield_tab:
    st.header("Widefield Fuji")
    client = _client()

    try:
        node_status_w = client.get_node_status()
    except Exception as exc:
        st.error(f"Cannot reach Kepler API: {exc}")
        st.stop()

    active_prof_w = node_status_w.get("active_equipment_profile") or {}
    default_focal_length = active_prof_w.get("focal_length_mm") or 13.0

    st.caption(
        "This surface measures a real preview frame, then recommends exposure and ISO from current conditions."
    )

    rec_col1, rec_col2, rec_col3 = st.columns(3)
    with rec_col1:
        focal_length_mm = st.number_input(
            "Focal Length (mm)",
            min_value=1.0,
            value=float(default_focal_length),
            step=1.0,
            key="widefield_focal_length_mm",
        )
    with rec_col2:
        aperture = st.number_input(
            "Aperture (f/)",
            min_value=0.7,
            value=1.4,
            step=0.1,
            key="widefield_aperture",
        )
    with rec_col3:
        destination_dir = st.text_input(
            "Artifact Dir",
            value="/data/kepler/focus-assist/widefield",
            key="widefield_destination_dir",
        )

    eval_col1, eval_col2 = st.columns(2)
    with eval_col1:
        sample_exposure = st.number_input(
            "Sample Exposure (s)", min_value=0.5, value=2.0, step=0.5, key="widefield_sample_exposure"
        )
    with eval_col2:
        sample_iso = st.number_input(
            "Sample ISO", min_value=100, value=3200, step=100, key="widefield_sample_iso"
        )

    if st.button("🌌 Evaluate Conditions", key="widefield_evaluate_btn"):
        try:
            evaluation = client.post_widefield_condition_check(
                {
                    "destination_dir": destination_dir,
                    "sample_exposure_seconds": float(sample_exposure),
                    "sample_iso": int(sample_iso),
                    "focal_length_mm": float(focal_length_mm),
                    "aperture": float(aperture),
                }
            )
            st.markdown(
                f"**Status:** {evaluation.get('status')}  \n"
                f"**Summary:** {evaluation.get('summary')}"
            )
            met1, met2, met3, met4 = st.columns(4)
            with met1:
                st.metric("Stars", str(evaluation.get("star_count", 0)))
            with met2:
                st.metric("Background", f"{evaluation.get('background_adu', 0):.1f}")
            with met3:
                st.metric("Rec Exposure", f"{evaluation.get('recommended_exposure_seconds', 0):.1f} s")
            with met4:
                st.metric("Rec ISO", str(evaluation.get("recommended_iso", 0)))

            st.caption(f"Preview saved to {evaluation.get('image_path', '—')}")
            st.caption(
                f"Trailing ceiling estimate: {evaluation.get('trailing_ceiling_seconds', 0):.1f} s"
            )
            for note in evaluation.get("notes", []):
                st.caption(f"- {note}")
        except Exception as exc:
            st.error(str(exc))

    with st.expander("Show Rule-of-Thumb Ceiling"):
        try:
            rec = client.get_widefield_recommendations(
                focal_length_mm=focal_length_mm,
                aperture=aperture,
            )
            met1, met2, met3, met4 = st.columns(4)
            with met1:
                st.metric("Classic 500", f"{rec.get('classic_500_seconds', 0):.1f} s")
            with met2:
                st.metric("Crop 500", f"{rec.get('crop_500_seconds', 0):.1f} s")
            with met3:
                npf_seconds = rec.get("npf_seconds")
                st.metric("NPF Approx", f"{npf_seconds:.1f} s" if npf_seconds is not None else "—")
            with met4:
                st.metric("Ceiling", f"{rec.get('recommended_seconds', 0):.1f} s")
        except Exception as exc:
            st.warning(f"Could not load ceiling estimate: {exc}")

    st.divider()
    st.subheader("Focus Assist")
    focus_col1, focus_col2, focus_col3 = st.columns(3)
    with focus_col1:
        focus_exposure = st.number_input(
            "Focus Exposure (s)", min_value=0.5, value=3.0, step=0.5, key="focus_assist_exposure"
        )
        focus_iso = st.number_input(
            "Focus ISO", min_value=100, value=3200, step=100, key="focus_assist_iso"
        )
    with focus_col2:
        focus_min_raw = st.number_input(
            "Focus Min Raw", min_value=-1000, value=45, step=1, key="focus_assist_min_raw"
        )
        focus_max_raw = st.number_input(
            "Focus Max Raw", min_value=0, value=1497, step=1, key="focus_assist_max_raw"
        )
    with focus_col3:
        coarse_step = st.number_input(
            "Coarse Step", min_value=1, value=40, step=1, key="focus_assist_coarse"
        )
        fine_step = st.number_input(
            "Fine Step", min_value=0, value=10, step=1, key="focus_assist_fine"
        )

    if st.button("🔎 Run Focus Assist", key="run_focus_assist_btn"):
        try:
            focus_result = client.post_focus_assist(
                {
                    "destination_dir": destination_dir,
                    "exposure_seconds": focus_exposure,
                    "iso": int(focus_iso),
                    "aperture": aperture,
                    "focus_min_raw": int(focus_min_raw),
                    "focus_max_raw": int(focus_max_raw),
                    "coarse_step": int(coarse_step),
                    "fine_step": int(fine_step),
                }
            )
            if focus_result.get("status") == "success":
                st.success(focus_result.get("summary", "Focus assist succeeded"))
            else:
                st.warning(focus_result.get("summary", "Focus assist was inconclusive"))

            st.markdown(
                f"**Start:** {focus_result.get('started_raw')}  \n"
                f"**Best:** {focus_result.get('best_raw')}  \n"
                f"**Final:** {focus_result.get('final_raw')}"
            )

            all_samples = focus_result.get("coarse_samples", []) + focus_result.get("fine_samples", [])
            if all_samples:
                st.markdown("**Samples**")
                st.dataframe(all_samples, use_container_width=True)
        except Exception as exc:
            st.error(str(exc))


# ===================================================================  #
# TARGET                                                                #
# ===================================================================  #

with target_tab:
    st.header("Target Context")
    client = _client()

    try:
        node_status_t = client.get_node_status()
        current_target = client.get_target_current()
    except Exception as exc:
        st.error(f"Cannot reach Kepler API: {exc}")
        st.stop()

    active_prof_t = node_status_t.get("active_equipment_profile")

    # Equipment profile context
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

    # Kepler observes the current target from KStars/Ekos; this panel is read-only.
    st.info(
        "Target selection and sequence authoring are done in KStars/Ekos. "
        "Kepler observes the active target here and uses it for supervision context."
    )

    if current_target:
        st.subheader("Observed Target")
        col_tl, col_ra, col_dec = st.columns(3)
        with col_tl:
            st.metric("Target", current_target.get("target_label", "—"))
        with col_ra:
            st.metric("RA (h)", f"{current_target.get('ra_hours', 0):.4f}")
        with col_dec:
            st.metric("Dec (°)", f"{current_target.get('dec_deg', 0):.4f}")
        source = current_target.get("target_source", "—")
        st.caption(f"Source: {source}")
        if source == "kstars_ekos":
            st.success("✅ Target observed from KStars/Ekos")
        run_params = current_target.get("run_parameters", {})
        if run_params:
            st.markdown("**Sequence Summary (from Ekos)**")
            col_exp, col_cam, col_stop = st.columns(3)
            with col_exp:
                st.metric("Exposure", f"{run_params.get('exposure_seconds', '—')} s")
            with col_cam:
                st.metric(
                    "Camera Settings",
                    ", ".join(
                        f"{key}={value}"
                        for key, value in run_params.get("camera_settings", {}).items()
                    )
                    or "—",
                )
            with col_stop:
                stop_condition = run_params.get("stop_condition", {})
                st.metric(
                    "Stop Condition",
                    ", ".join(f"{key}={value}" for key, value in stop_condition.items()) or "—",
                )
    else:
        st.info(
            "No target observed yet.  "
            "Start a sequence in KStars/Ekos — Kepler will pick up the target automatically."
        )

_SUPERVISORY_NEXT_ACTION_LABEL: dict[str, str] = {
    "wait_for_ekos_session": "⏳ Waiting for KStars/Ekos session to start",
    "monitor_ekos_session": "✅ Monitoring Ekos session",
    "request_autofocus": "🔭 Requesting autofocus",
    "request_re_solve": "🔭 Requesting re-solve",
    "pause_and_review": "⏸ Pausing for operator review",
    "intervention_pending": "⚠️ Intervention in progress",
}

_ACTIVE_OWNER_LABEL: dict[str, str] = {
    "ekos": "KStars/Ekos",
    "kepler": "Kepler",
    "operator": "Operator",
    "unknown": "Unknown",
    "none": "None",
}


with session_tab:
    st.header("Session")
    client = _client()

    try:
        session_state = client.get_session_state()
        readiness_s = client.get_readiness()
        node_status_s = client.get_node_status()
    except Exception as exc:
        st.error(f"Cannot reach Kepler API: {exc}")
        st.stop()

    current_state_s = node_status_s.get("state", "")
    supervision_ready = readiness_s.get("supervision_ready", False)

    if session_state is None:
        st.info("No active managed session.")

        # v1.1 supervision attach path: operator starts Ekos, then attaches Kepler supervision
        if supervision_ready and current_state_s == "ready":
            st.info(
                "Node is ready to supervise. Start your capture sequence in KStars/Ekos, "
                "then attach Kepler supervision below."
            )
            if st.button("🔭 Attach Supervision Session", key="attach_session_btn"):
                try:
                    resp = client.post_session_attach()
                    st.success(
                        resp.get(
                            "message",
                            "Supervision attached — Kepler is waiting for your Ekos session",
                        )
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        elif current_state_s == "ready":
            supervision_blockers = readiness_s.get("supervision_blockers", [])
            if supervision_blockers:
                with st.expander("⚠️ Supervision blockers — resolve these before attaching"):
                    _show_blockers(supervision_blockers)
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

        # v1.1 canonical ownership signal
        active_owner = session_state.get("active_owner")
        if active_owner:
            st.metric(
                "Active Owner",
                _ACTIVE_OWNER_LABEL.get(active_owner, active_owner),
            )

        # v1.1 supervisory next action — tells the operator what Kepler is doing or waiting for
        next_action = session_state.get("supervisory_next_action")
        if next_action:
            st.info(
                f"🔭 {_SUPERVISORY_NEXT_ACTION_LABEL.get(next_action, next_action)}"
            )

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

        # v1.1 intervention summary
        intervention = session_state.get("intervention_summary")
        if intervention and intervention.get("active_kind"):
            with st.expander(f"⚠️ Active Intervention: {intervention['active_kind']}"):
                active_rec = intervention.get("active_record")
                if active_rec:
                    st.write(f"**Reason:** {active_rec.get('reason', '—')}")
                    st.write(f"**Retry count:** {active_rec.get('retry_count', 0)}")
                total = intervention.get("total_records", 0)
                if total:
                    st.caption(f"{total} total intervention records this session")

        st.divider()

        # --- Session controls ---
        st.subheader("Controls")
        col_p, col_r, col_s2, col_rc, col_rl = st.columns(5)

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

        with col_rc:
            if st.button(
                "🩹 Recover Camera",
                disabled=(state not in {"ready", "paused", "target_acquired"}),
            ):
                try:
                    resp = client.post_camera_recover()
                    st.success(resp.get("message", "Camera recovery completed"))
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
