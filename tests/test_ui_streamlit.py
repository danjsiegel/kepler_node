"""Headless smoke tests for the Kepler Node Streamlit UI.

Uses ``streamlit.testing.v1.AppTest`` with a mocked ``KeplerApiClient`` to
verify that all three tabs (Overview, Session, Review) render without raising
an exception in the common ``ready``/no-active-session posture.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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
    }
    # No active session
    client.get_session_current.return_value = None
    client.get_session_state.return_value = None
    client.get_session_outcome.return_value = None
    client.get_session_frames.return_value = {"frames": [], "next_before_frame_id": None}
    client.get_session_artifacts.return_value = {"artifacts": []}
    return client


# ------------------------------------------------------------------ #
# Smoke tests                                                          #
# ------------------------------------------------------------------ #

def _run_app_with_mock_client(mock_client: MagicMock) -> AppTest:
    """Run the Streamlit app in a headless AppTest with a patched KeplerApiClient."""
    with patch(
        "kepler_node.ui.api_client.KeplerApiClient",
        return_value=mock_client,
    ):
        at = AppTest.from_file(
            "src/kepler_node/ui/streamlit_app.py",
            default_timeout=10,
        )
        at.run()
    return at


def test_all_three_tabs_render_in_no_active_session_posture() -> None:
    """Overview, Session, and Review tabs all render without raising or
    calling st.stop() when the node is ready and no session is active."""
    at = _run_app_with_mock_client(_make_mock_client())

    # No exception during rendering
    assert not at.exception, f"Streamlit app raised: {at.exception}"

    # All three top-level tab headers must appear
    headers = [h.value for h in at.header]
    assert "Overview" in headers, f"Overview header missing; found: {headers}"
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
