from __future__ import annotations

from unittest.mock import patch

from kepler_node import cli


def test_prepare_direct_fuji_camera_ownership_stops_known_claimers() -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> None:
        calls.append(command)
        return None

    with patch("subprocess.run", side_effect=fake_run):
        cli._prepare_direct_fuji_camera_ownership()

    assert ["systemctl", "stop", "kepler-camera-attach"] in calls
    assert ["pkill", "-x", "gvfsd-gphoto2"] in calls
    assert ["pkill", "-f", "/usr/local/bin/kepler-camera-attach"] in calls