"""Integration-oriented tests for the Kepler Node Streamlit UI.

Uses ``streamlit.testing.v1.AppTest`` with a mocked ``KeplerApiClient`` to
verify that the real UI renders without raising and that both supported planner
modes are actionable in the operator console.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import streamlit as st

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover
    pytest.skip("streamlit testing module not available", allow_module_level=True)


# ------------------------------------------------------------------ #
# Mock API client factory                                              #
# ------------------------------------------------------------------ #


def _make_mock_client() -> MagicMock:
    """Return a mock KeplerApiClient that represents a healthy ready node
    with no active session."""
    client = MagicMock()
    client.get_health.return_value = {"status": "healthy"}
    client.get_readiness.return_value = {
        "ready": True,
        "blockers": [],
        "degraded": [],
        "time_trusted": True,
        "calibrated": False,
        "external_control_summary": None,
    }
    client.get_node_status.return_value = {
        "state": "ready",
        "control_locked": False,
        "detected_devices": {
            "mount": {"connected": False, "status": "not_initialized"},
            "camera": {"connected": False, "status": "not_initialized"},
        },
        "storage_status": {},
        "power_status": {},
        "network_mode": "isolated",
        "time_certainty": {"trusted": True, "source": "ntp", "summary": "ok"},
        "power_integrity": {"healthy": True, "undervoltage_detected": False, "summary": "ok"},
        "build_summary": "kepler-node v1",
        "active_equipment_profile": None,
        "planner_mode": None,
        "planner_connection_details": None,
        "install_manifest": None,
    }
    # No active session
    client.get_session_current.return_value = None
    client.get_session_state.return_value = None
    client.get_session_outcome.return_value = None
    client.get_session_frames.return_value = {"frames": [], "next_before_frame_id": None}
    client.get_session_artifacts.return_value = {"artifacts": []}
    # Equipment profiles
    client.get_equipment_profiles.return_value = {"profiles": [], "active_profile_id": None}
    # Target
    client.get_target_current.return_value = None
    return client


def _make_mode_client(
    *, planner_mode: str, planner_connection_details: dict[str, object]
) -> MagicMock:
    client = _make_mock_client()
    client.get_node_status.return_value = {
        **client.get_node_status.return_value,
        "planner_mode": planner_mode,
        "planner_connection_details": planner_connection_details,
        "install_manifest": {
            "kepler_version": "1.2.3",
            "bootstrap_profile": planner_mode,
            "installed_at": "2026-05-13T00:00:00+00:00",
        },
        "active_equipment_profile": {
            "profile_id": "starter-rig",
            "display_name": "Starter Rig",
            "lens_is_zoom": False,
            "focal_length_mm": 135,
        },
    }
    client.get_target_current.return_value = {
        "target_label": "M31",
        "ra_hours": 0.7122,
        "dec_deg": 41.269,
        "target_source": "kstars_ekos" if planner_mode == "headless-node" else "manual",
        "run_parameters": {
            "exposure_seconds": 120,
            "camera_settings": {"gain": 100},
            "stop_condition": {"frame_count": 60},
        },
        "active_equipment_profile_id": "starter-rig",
    }
    return client


# ------------------------------------------------------------------ #
# Smoke tests                                                          #
# ------------------------------------------------------------------ #


def _run_app_with_mock_client(mock_client: MagicMock) -> AppTest:
    """Run the Streamlit app in a headless AppTest with a patched KeplerApiClient."""
    st.cache_resource.clear()
    with patch(
        "kepler_node.ui.api_client.KeplerApiClient",
        return_value=mock_client,
    ):
        at = AppTest.from_file(
            "src/kepler_node/ui/streamlit_app.py",
            default_timeout=10,
        )
        at.run()
    st.cache_resource.clear()
    return at


def test_all_three_tabs_render_in_no_active_session_posture() -> None:
    """All five tabs (Overview, Equipment, Target, Session, Review) render without
    raising or calling st.stop() when the node is ready and no session is active."""
    at = _run_app_with_mock_client(_make_mock_client())

    # No exception during rendering
    assert not at.exception, f"Streamlit app raised: {at.exception}"

    # All five top-level tab headers must appear
    headers = [h.value for h in at.header]
    assert "Overview" in headers, f"Overview header missing; found: {headers}"
    assert "Equipment Profiles" in headers, f"Equipment Profiles header missing; found: {headers}"
    assert "Target Context" in headers, (
        f"Target Context header missing; found: {headers}"
    )
    assert "Session" in headers, f"Session header missing; found: {headers}"
    assert "Review" in headers, f"Review header missing; found: {headers}"


def test_target_tab_is_supervision_first_no_start_session() -> None:
    """Target tab must not expose a Start Session or target staging form.
    In v1.1, target selection happens in KStars/Ekos; Kepler is read-only here."""
    client = _make_mode_client(
        planner_mode="headless-node",
        planner_connection_details={
            "mode": "remote_kstars_ekos",
            "host": "192.168.1.42",
            "summary": "Connect remotely",
            "indi_port": 7624,
        },
    )
    at = _run_app_with_mock_client(client)

    assert not at.exception, f"Streamlit app raised: {at.exception}"
    button_labels = [b.label for b in at.button]
    assert not any("Start Session" in lbl for lbl in button_labels), (
        f"Start Session button must not appear in v1.1 supervision-first UI; "
        f"found button labels: {button_labels}"
    )
    assert not any("Stage Target" in lbl or "📡" in lbl for lbl in button_labels), (
        f"Target staging submit button must not appear; found: {button_labels}"
    )
    headers = [h.value for h in at.header]
    assert "Target Context" in headers, (
        f"Target tab must use supervision-first header 'Target Context'; found: {headers}"
    )


def test_session_tab_shows_no_active_session_info() -> None:
    """Session tab shows an info message (not an error) when no session is active."""
    at = _run_app_with_mock_client(_make_mock_client())

    assert not at.exception
    info_values = [i.value for i in at.info]
    assert any("No active managed session" in v for v in info_values), (
        f"Expected no-session info message; info blocks found: {info_values}"
    )


def test_overview_renders_node_status_metrics() -> None:
    """Overview tab renders at least one metric (Node Status) in ready posture."""
    at = _run_app_with_mock_client(_make_mock_client())

    assert not at.exception
    metric_labels = [m.label for m in at.metric]
    metric_values = [str(m.value) for m in at.metric]
    assert any("Status" in lbl or "State" in lbl or "Health" in lbl for lbl in metric_labels), (
        f"Expected status/health metric in Overview; found labels: {metric_labels}"
    )
    assert any("Not initialized" in value for value in metric_values), metric_values


def test_overview_surfaces_card_reader_mode_warning() -> None:
    client = _make_mock_client()
    client.get_readiness.return_value = {
        "ready": False,
        "blockers": [
            {
                "name": "camera_remote_mode_required",
                "summary": "Camera is detected but only exposing status/card-reader controls",
                "operator_action_required": "Switch camera to USB tether/remote-control mode and retry",
            }
        ],
        "degraded": [],
        "time_trusted": True,
        "calibrated": False,
        "external_control_summary": None,
    }
    client.get_node_status.return_value = {
        **client.get_node_status.return_value,
        "detected_devices": {
            "mount": {"connected": False, "status": "not_initialized"},
            "camera": {
                "connected": False,
                "status": "card_reader_mode",
                "summary": "Camera is detected but only exposing status/card-reader controls",
            },
        },
    }

    at = _run_app_with_mock_client(client)

    assert not at.exception
    metric_values = [str(m.value) for m in at.metric]
    metric_labels = [m.label for m in at.metric]
    error_values = [e.value for e in at.error]
    assert any("Card Reader Mode" in value for value in metric_values), metric_values
    assert "Camera Readiness" in metric_labels, metric_labels
    assert "Camera Mode" in metric_labels, metric_labels
    assert any("Not Ready" in value for value in metric_values), metric_values
    assert any("SD Card Mode" in value for value in metric_values), metric_values
    assert any("card-reader controls" in value for value in error_values), error_values


def test_overview_surfaces_remote_ready_camera_state() -> None:
    client = _make_mock_client()
    client.get_node_status.return_value = {
        **client.get_node_status.return_value,
        "detected_devices": {
            "mount": {"connected": False, "status": "not_initialized"},
            "camera": {
                "connected": False,
                "status": "remote_control_ready",
                "summary": "Remote-control surface available via /main/actions/bulb",
                "usb_connected": True,
                "ready": True,
            },
        },
    }

    at = _run_app_with_mock_client(client)

    assert not at.exception
    metric_values = [str(m.value) for m in at.metric]
    assert any("Remote Ready" in value for value in metric_values), metric_values
    assert any("Ready" in value for value in metric_values), metric_values
    assert any("Remote Control" in value for value in metric_values), metric_values


def test_overview_surfaces_autocapture_mode_warning() -> None:
    client = _make_mock_client()
    client.get_readiness.return_value = {
        "ready": False,
        "blockers": [
            {
                "name": "camera_autocapture_mode_blocking",
                "summary": "Camera is in Still Capture Mode 'Self-timer'; exit self-timer/autocapture mode on the body before capture",
                "operator_action_required": "Exit self-timer/autocapture mode on the camera body and retry",
            }
        ],
        "degraded": [],
        "time_trusted": True,
        "calibrated": False,
        "external_control_summary": None,
    }
    client.get_node_status.return_value = {
        **client.get_node_status.return_value,
        "detected_devices": {
            "mount": {"connected": False, "status": "not_initialized"},
            "camera": {
                "connected": False,
                "status": "autocapture_mode",
                "summary": "Camera is in Still Capture Mode 'Self-timer'; exit self-timer/autocapture mode on the body before capture",
            },
        },
    }

    at = _run_app_with_mock_client(client)

    assert not at.exception
    metric_values = [str(m.value) for m in at.metric]
    error_values = [e.value for e in at.error]
    assert any("Auto Capture Mode" in value for value in metric_values), metric_values
    assert any("Not Ready" in value for value in metric_values), metric_values
    assert any("Auto Capture" in value for value in metric_values), metric_values
    assert any("Self-timer" in value for value in error_values), error_values


def test_headless_mode_flow_renders_actionable_planner_guidance() -> None:
    at = _run_app_with_mock_client(
        _make_mode_client(
            planner_mode="headless-node",
            planner_connection_details={
                "mode": "remote_kstars_ekos",
                "host": "192.168.1.42",
                "summary": "Connect KStars/Ekos remotely: set INDI server host to 192.168.1.42 and port 7624",
                "indi_port": 7624,
                "indi_reachable": True,
                "kepler_reachable": True,
            },
        )
    )

    assert not at.exception
    markdown_values = [m.value for m in at.markdown]
    info_values = [i.value for i in at.info]
    metric_values = [str(m.value) for m in at.metric]
    metric_labels = [m.label for m in at.metric]

    assert any("Headless Remote Planner" in value for value in markdown_values), markdown_values
    assert any("Use Remote KStars/Ekos" in value for value in markdown_values), markdown_values
    assert any("192.168.1.42" in value for value in info_values), info_values
    assert any("7624" in value for value in metric_values), metric_values
    assert any("Node Address" in lbl for lbl in metric_labels), metric_labels
    assert any("192.168.1.42" in v for v in metric_values), metric_values
    assert any("INDI Service" in lbl for lbl in metric_labels), metric_labels
    assert any("Kepler API" in lbl for lbl in metric_labels), metric_labels
    assert any("reachable" in v for v in metric_values), metric_values


def test_field_mode_flow_renders_actionable_local_planner_guidance() -> None:
    at = _run_app_with_mock_client(
        _make_mode_client(
            planner_mode="field-fallback",
            planner_connection_details={
                "mode": "on_node_kstars_ekos",
                "host": "192.168.1.42",
                "summary": "Launch KStars/Ekos on this node via xRDP remote desktop: connect to 192.168.1.42 on port 3389 (RDP)",
                "rdp_port": 3389,
                "indi_reachable": True,
                "kepler_reachable": True,
                "xrdp_reachable": True,
            },
        )
    )

    assert not at.exception
    markdown_values = [m.value for m in at.markdown]
    info_values = [i.value for i in at.info]
    metric_values = [str(m.value) for m in at.metric]
    metric_labels = [m.label for m in at.metric]

    assert any("Field Local-First" in value for value in markdown_values), markdown_values
    assert any("Open Local Planner Session" in value for value in markdown_values), markdown_values
    assert any("xRDP remote desktop" in value for value in info_values), info_values
    assert any("3389" in value for value in metric_values), metric_values
    assert any("Node Address" in lbl for lbl in metric_labels), metric_labels
    assert any("192.168.1.42" in v for v in metric_values), metric_values
    assert any("INDI Service" in lbl for lbl in metric_labels), metric_labels
    assert any("Kepler API" in lbl for lbl in metric_labels), metric_labels
    assert any("xRDP Service" in lbl for lbl in metric_labels), metric_labels
    assert any("reachable" in v for v in metric_values), metric_values


def test_session_tab_shows_attach_button_when_supervision_ready() -> None:
    """Session tab renders the Attach Supervision Session button when supervision_ready is True
    and the node is in the ready state with no active session."""
    client = _make_mock_client()
    client.get_readiness.return_value = {
        **client.get_readiness.return_value,
        "supervision_ready": True,
    }

    at = _run_app_with_mock_client(client)

    assert not at.exception, f"Streamlit app raised: {at.exception}"
    button_labels = [b.label for b in at.button]
    assert any("Attach Supervision Session" in lbl for lbl in button_labels), (
        f"Expected 'Attach Supervision Session' button; found button labels: {button_labels}"
    )


def test_session_tab_shows_supervisory_fields_when_session_is_monitoring() -> None:
    """Session tab renders Active Owner metric and supervisory_next_action info when an active
    session is in the 'monitoring' state."""
    client = _make_mock_client()
    client.get_session_state.return_value = {
        "state": "monitoring",
        "workflow_intent": "deep_sky_lp",
        "control_locked": True,
        "active_owner": "ekos",
        "supervisory_next_action": "monitor_ekos_session",
        "intervention_summary": {"active_kind": None, "total_records": 0, "active_record": None},
        "latest_message": None,
        "blockers": [],
        "degraded": [],
        "pause_summary": None,
    }

    at = _run_app_with_mock_client(client)

    assert not at.exception, f"Streamlit app raised: {at.exception}"
    metric_labels = [m.label for m in at.metric]
    assert "Active Owner" in metric_labels, (
        f"Expected 'Active Owner' metric; found: {metric_labels}"
    )
    info_values = [i.value for i in at.info]
    assert any("Monitoring" in v or "monitor" in v.lower() for v in info_values), (
        f"Expected supervisory next action info; found info blocks: {info_values}"
    )


def test_attach_button_calls_post_session_attach() -> None:
    """Clicking Attach Supervision Session invokes post_session_attach on the API client."""
    client = _make_mock_client()
    client.get_readiness.return_value = {
        **client.get_readiness.return_value,
        "supervision_ready": True,
    }
    client.post_session_attach.return_value = {
        "message": "Supervision attached — Kepler is waiting for your Ekos session"
    }

    st.cache_resource.clear()
    with patch("kepler_node.ui.api_client.KeplerApiClient", return_value=client):
        at = AppTest.from_file("src/kepler_node/ui/streamlit_app.py", default_timeout=10)
        at.run()

        attach_buttons = [b for b in at.button if "Attach Supervision Session" in b.label]
        assert attach_buttons, (
            f"Expected 'Attach Supervision Session' button; found: {[b.label for b in at.button]}"
        )
        attach_buttons[0].click().run()

    st.cache_resource.clear()

    client.post_session_attach.assert_called_once()
