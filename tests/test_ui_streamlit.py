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
            "mount": {"connected": False},
            "camera": {"connected": False},
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


def _make_mode_client(*, planner_mode: str, planner_connection_details: dict[str, object]) -> MagicMock:
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
    assert "Target & Session Start" in headers, (
        f"Target & Session Start header missing; found: {headers}"
    )
    assert "Session" in headers, f"Session header missing; found: {headers}"
    assert "Review" in headers, f"Review header missing; found: {headers}"


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
    assert any("Status" in lbl or "State" in lbl or "Health" in lbl for lbl in metric_labels), (
        f"Expected status/health metric in Overview; found labels: {metric_labels}"
    )


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
