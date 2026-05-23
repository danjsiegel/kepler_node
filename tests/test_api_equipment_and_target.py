"""Tests for equipment-profile, target-intake, session-start, and planner-mode API surfaces."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.claw import ClawController
from kepler_node.agent.interfaces import (
    NetworkMode,
    PowerStatus,
    ServiceHealth,
    StorageStatus,
    TimeSource,
    TimeStatus,
)
from kepler_node.agent.session import ClawState, RuntimeSession
from kepler_node.api.app import build_app
from kepler_node.camera.protocols import CameraSettings, CaptureRequest, CaptureResult
from kepler_node.imaging.protocols import SolveResult
from kepler_node.mount.protocols import MountPosition
from kepler_node.storage.filesystem import FilesystemSessionStore
from kepler_node.storage.models import (
    EquipmentProfile,
    EquipmentProfileBackendPreferences,
    EquipmentProfileFocusCalibration,
    EquipmentProfileHardware,
    EquipmentProfileHardwareCamera,
    EquipmentProfileHardwareGps,
    EquipmentProfileHardwareLens,
    EquipmentProfileHardwareMount,
    EquipmentProfileSiteDefaults,
    EquipmentProfileSolvingHints,
    FujiFocusCalibrationProfile,
    InstallManifest,
)


class FakeNodeBackend:
    def __init__(self, *, service_healths: list | None = None) -> None:
        self._service_healths = service_healths or []

    def network_mode(self) -> NetworkMode:
        return NetworkMode.FIELD_HOTSPOT

    def time_status(self) -> TimeStatus:
        return TimeStatus(trusted=True, source=TimeSource.NETWORK, summary="ok")

    def storage_status(self) -> StorageStatus:
        return StorageStatus(
            data_root=Path("/tmp"),
            free_bytes=50_000_000_000,
            total_bytes=100_000_000_000,
            writable=True,
            summary="ok",
        )

    def power_status(self) -> PowerStatus:
        return PowerStatus(healthy=True, summary="ok", undervoltage_detected=False)

    def service_health(self) -> list:
        return self._service_healths

    def confirm_time(self, timestamp: datetime) -> TimeStatus:
        return self.time_status()


class FakeMountBackend:
    connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def current_position(self) -> MountPosition:
        return MountPosition(ra_hours=0.0, dec_deg=0.0)

    def slew_to(self, position: MountPosition) -> None:
        pass

    def sync_to(self, position: MountPosition) -> None:
        pass

    def activity_events(self) -> list:
        return []

    def poll_activity(self) -> None:
        pass


class FakeCameraBackend:
    def connect(self, settings: CameraSettings) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capture(self, request: CaptureRequest) -> CaptureResult:
        return CaptureResult(image_path=Path("/tmp/test.jpg"), metadata={})

    def activity_events(self) -> list:
        return []


class FakeSolverBackend:
    def solve(self, image_path: Path, **_: object) -> SolveResult:
        return SolveResult(success=True, ra_hours=10.0, dec_deg=45.0, residual_arcmin=2.0)


def _make_controller(tmp_path: Path, *, state: ClawState = ClawState.READY) -> ClawController:
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    session = RuntimeSession()
    session.state = state
    return ClawController(
        session=session,
        node_backend=FakeNodeBackend(),
        mount_backend=FakeMountBackend(),
        camera_backend=FakeCameraBackend(),
        solver_backend=FakeSolverBackend(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
        test_exposure_seconds=1.0,
    )


def _make_api_client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(controller=_make_controller(tmp_path)))


def _make_profile(
    profile_id: str = "test-profile",
    display_name: str = "Test Profile",
    is_default: bool = False,
    is_zoom: bool = False,
    focal_length_assumption_mm: float | None = None,
) -> EquipmentProfile:
    return EquipmentProfile(
        profile_id=profile_id,
        display_name=display_name,
        is_default=is_default,
        hardware=EquipmentProfileHardware(
            mount=EquipmentProfileHardwareMount(model="EQ6-R"),
            camera=EquipmentProfileHardwareCamera(make="ZWO", model="ASI294MC"),
            lens=EquipmentProfileHardwareLens(
                model="Rokinon 135mm f/2",
                is_zoom=is_zoom,
                default_focal_length_mm=None if is_zoom else 135,
            ),
            gps=EquipmentProfileHardwareGps(),
        ),
        site_defaults=EquipmentProfileSiteDefaults(),
        solving_hints=EquipmentProfileSolvingHints(
            focal_length_assumption_mm=focal_length_assumption_mm,
        ),
        backend_preferences=EquipmentProfileBackendPreferences(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _make_fuji_focus_calibration(*, focal_length_mm: float, raw_min: int, raw_max: int) -> dict:
    profile_id = f"xf55-200@{int(focal_length_mm)}mm-mf"
    calibration = EquipmentProfileFocusCalibration(
        active_profile_id=profile_id,
        profiles={
            profile_id: FujiFocusCalibrationProfile(
                profile_id=profile_id,
                camera_model="X-T5",
                lens_model="XF55-200mmF3.5-4.8 R LM OIS",
                focal_length_mm=focal_length_mm,
                focus_mode="manual",
                raw_min=raw_min,
                raw_max=raw_max,
                calibrated_at=datetime(2026, 5, 23, tzinfo=UTC),
            )
        },
    )
    return calibration.model_dump(mode="json")


def test_api_get_equipment_profiles_empty(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    resp = client.get("/api/v1/equipment/profiles")
    assert resp.status_code == 200
    data = resp.json()
    assert data["profiles"] == []
    assert data["active_profile_id"] is None


def test_api_post_and_get_equipment_profile(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    payload = {
        "profile_id": "my-rig",
        "display_name": "My Rig",
        "hardware": {
            "mount": {"model": "EQ6-R"},
            "camera": {"make": "ZWO", "model": "ASI294MC"},
            "lens": {"model": "Rokinon 135", "is_zoom": False},
            "gps": {},
        },
        "site_defaults": {},
        "solving_hints": {},
        "backend_preferences": {},
    }
    resp = client.post("/api/v1/equipment/profiles", json=payload)
    assert resp.status_code == 201

    resp2 = client.get("/api/v1/equipment/profiles/my-rig")
    assert resp2.status_code == 200
    assert resp2.json()["profile"]["display_name"] == "My Rig"


def test_api_post_and_get_equipment_profile_preserves_fuji_focus_calibration(
    tmp_path: Path,
) -> None:
    client = _make_api_client(tmp_path)
    payload = {
        "profile_id": "fuji-rig",
        "display_name": "Fuji Rig",
        "hardware": {
            "mount": {"model": "iEXOS-100"},
            "camera": {
                "make": "Fujifilm",
                "model": "X-T5",
                "fuji_focus_calibration": _make_fuji_focus_calibration(
                    focal_length_mm=55.0,
                    raw_min=-428,
                    raw_max=10442,
                ),
            },
            "lens": {"model": "XF55-200mmF3.5-4.8 R LM OIS", "is_zoom": True},
            "gps": {},
        },
        "site_defaults": {},
        "solving_hints": {"focal_length_assumption_mm": 55.0},
        "backend_preferences": {},
    }
    resp = client.post("/api/v1/equipment/profiles", json=payload)
    assert resp.status_code == 201

    resp2 = client.get("/api/v1/equipment/profiles/fuji-rig")
    assert resp2.status_code == 200
    calibration = resp2.json()["profile"]["hardware"]["camera"]["fuji_focus_calibration"]
    assert calibration["active_profile_id"] == "xf55-200@55mm-mf"
    stored = calibration["profiles"]["xf55-200@55mm-mf"]
    assert stored["raw_min"] == -428
    assert stored["raw_max"] == 10442
    assert stored["normalized_max"] == 10000


def test_api_put_equipment_profile_updates_fuji_focus_calibration(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    payload = {
        "profile_id": "rig-fuji",
        "display_name": "Rig Fuji",
        "hardware": {
            "mount": {"model": "iEXOS-100"},
            "camera": {"make": "Fujifilm", "model": "X-T5"},
            "lens": {"model": "XF55-200mmF3.5-4.8 R LM OIS", "is_zoom": True},
            "gps": {},
        },
        "site_defaults": {},
        "solving_hints": {"focal_length_assumption_mm": 55.0},
        "backend_preferences": {},
    }
    client.post("/api/v1/equipment/profiles", json=payload)

    updated = {
        **payload,
        "hardware": {
            **payload["hardware"],
            "camera": {
                "make": "Fujifilm",
                "model": "X-T5",
                "fuji_focus_calibration": _make_fuji_focus_calibration(
                    focal_length_mm=135.0,
                    raw_min=1498,
                    raw_max=10234,
                ),
            },
        },
    }
    resp = client.put("/api/v1/equipment/profiles/rig-fuji", json=updated)
    assert resp.status_code == 200
    calibration = resp.json()["profile"]["hardware"]["camera"]["fuji_focus_calibration"]
    assert calibration["active_profile_id"] == "xf55-200@135mm-mf"
    assert calibration["profiles"]["xf55-200@135mm-mf"]["focal_length_mm"] == 135.0


def test_api_select_equipment_profile_writes_fuji_focus_runtime_projection(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    profile = _make_profile(profile_id="fuji-runtime", display_name="Fuji Runtime")
    profile.hardware.camera = EquipmentProfileHardwareCamera(
        make="Fujifilm",
        model="X-T5",
        fuji_focus_calibration=EquipmentProfileFocusCalibration.model_validate(
            _make_fuji_focus_calibration(focal_length_mm=55.0, raw_min=-428, raw_max=10442)
        ),
    )
    profile.hardware.lens = EquipmentProfileHardwareLens(
        model="XF55-200mmF3.5-4.8 R LM OIS",
        is_zoom=True,
        default_focal_length_mm=55.0,
    )
    ctrl.store.write_profile(profile)
    client = TestClient(build_app(controller=ctrl))

    resp = client.post("/api/v1/equipment/profiles/fuji-runtime/select")
    assert resp.status_code == 200

    runtime_path = tmp_path / "runtime" / "fuji_focus_calibration.json"
    assert runtime_path.exists()
    projection = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert projection["equipment_profile_id"] == "fuji-runtime"
    assert projection["calibration"]["active_profile_id"] == "xf55-200@55mm-mf"
    assert projection["active_calibration"]["raw_min"] == -428
    assert projection["lens"]["active_focal_length_mm"] == 55.0


def test_api_put_active_equipment_profile_refreshes_fuji_focus_runtime_projection(
    tmp_path: Path,
) -> None:
    ctrl = _make_controller(tmp_path)
    profile = _make_profile(profile_id="rig-active", is_default=True)
    profile.hardware.camera = EquipmentProfileHardwareCamera(
        make="Fujifilm",
        model="X-T5",
        fuji_focus_calibration=EquipmentProfileFocusCalibration.model_validate(
            _make_fuji_focus_calibration(focal_length_mm=55.0, raw_min=-428, raw_max=10442)
        ),
    )
    profile.hardware.lens = EquipmentProfileHardwareLens(
        model="XF55-200mmF3.5-4.8 R LM OIS",
        is_zoom=True,
        default_focal_length_mm=55.0,
    )
    ctrl.store.write_profile(profile)
    ctrl.set_active_equipment_profile(profile)
    ctrl.session.calibration_accepted = True

    client = TestClient(build_app(controller=ctrl))

    updated_payload = {
        "profile_id": "rig-active",
        "display_name": "Rig Active Updated",
        "hardware": {
            "mount": {"model": "EQ6-R"},
            "camera": {
                "make": "Fujifilm",
                "model": "X-T5",
                "fuji_focus_calibration": _make_fuji_focus_calibration(
                    focal_length_mm=135.0,
                    raw_min=1498,
                    raw_max=10234,
                ),
            },
            "lens": {
                "model": "XF55-200mmF3.5-4.8 R LM OIS",
                "is_zoom": True,
                "default_focal_length_mm": 135.0,
            },
            "gps": {},
        },
        "site_defaults": {},
        "solving_hints": {"focal_length_assumption_mm": 135.0},
        "backend_preferences": {},
        "is_default": True,
    }
    resp = client.put("/api/v1/equipment/profiles/rig-active", json=updated_payload)
    assert resp.status_code == 200

    runtime_path = tmp_path / "runtime" / "fuji_focus_calibration.json"
    projection = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert projection["calibration"]["active_profile_id"] == "xf55-200@135mm-mf"
    assert projection["lens"]["active_focal_length_mm"] == 135.0


def test_api_post_equipment_profile_duplicate_returns_409(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    payload = {
        "profile_id": "dup",
        "display_name": "Dup",
        "hardware": {"mount": {"model": "EQ6"}, "camera": {}, "lens": {}, "gps": {}},
        "site_defaults": {},
        "solving_hints": {},
        "backend_preferences": {},
    }
    client.post("/api/v1/equipment/profiles", json=payload)
    resp = client.post("/api/v1/equipment/profiles", json=payload)
    assert resp.status_code == 409


def test_api_get_equipment_profile_not_found(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    resp = client.get("/api/v1/equipment/profiles/does-not-exist")
    assert resp.status_code == 404


def test_api_select_equipment_profile(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    payload = {
        "profile_id": "sel",
        "display_name": "Sel",
        "hardware": {"mount": {"model": "EQ6"}, "camera": {}, "lens": {}, "gps": {}},
        "site_defaults": {},
        "solving_hints": {},
        "backend_preferences": {},
    }
    client.post("/api/v1/equipment/profiles", json=payload)
    resp = client.post("/api/v1/equipment/profiles/sel/select")
    assert resp.status_code == 200

    list_resp = client.get("/api/v1/equipment/profiles")
    assert list_resp.json()["active_profile_id"] == "sel"


def test_api_select_nonexistent_profile_returns_404(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    resp = client.post("/api/v1/equipment/profiles/ghost/select")
    assert resp.status_code == 404


def test_api_put_equipment_profile(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    payload = {
        "profile_id": "rig1",
        "display_name": "Rig 1",
        "hardware": {"mount": {"model": "HEQ5"}, "camera": {}, "lens": {}, "gps": {}},
        "site_defaults": {},
        "solving_hints": {},
        "backend_preferences": {},
    }
    client.post("/api/v1/equipment/profiles", json=payload)

    updated = {**payload, "display_name": "Rig 1 Updated"}
    resp = client.put("/api/v1/equipment/profiles/rig1", json=updated)
    assert resp.status_code == 200
    assert resp.json()["profile"]["display_name"] == "Rig 1 Updated"


def test_api_put_active_equipment_profile_refreshes_in_memory_state(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    profile = _make_profile(profile_id="rig-active", is_default=True)
    ctrl.store.write_profile(profile)
    ctrl.active_equipment_profile = profile
    ctrl.session.calibration_accepted = True

    client = TestClient(build_app(controller=ctrl))

    updated_payload = {
        "profile_id": "rig-active",
        "display_name": "Rig Active Updated",
        "hardware": {"mount": {"model": "EQ6-R"}, "camera": {}, "lens": {}, "gps": {}},
        "site_defaults": {},
        "solving_hints": {},
        "backend_preferences": {},
        "is_default": True,
    }
    resp = client.put("/api/v1/equipment/profiles/rig-active", json=updated_payload)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True
    assert ctrl.active_equipment_profile is not None
    assert ctrl.active_equipment_profile.display_name == "Rig Active Updated"
    assert ctrl.session.calibration_accepted is False


def test_api_get_target_current_empty(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    resp = client.get("/api/v1/target/current")
    assert resp.status_code == 200
    assert resp.json() is None


def test_api_post_and_get_target(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    payload = {
        "target_label": "M31",
        "ra_hours": 0.7122,
        "dec_deg": 41.269,
        "target_source": "manual",
        "run_parameters": {
            "exposure_seconds": 120,
            "camera_settings": {"gain": 100},
            "stop_condition": {"frame_count": 60},
        },
    }
    resp = client.post("/api/v1/target", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_label"] == "M31"
    assert data["ra_hours"] == pytest.approx(0.7122)

    get_resp = client.get("/api/v1/target/current")
    assert get_resp.status_code == 200
    assert get_resp.json()["target_label"] == "M31"


def test_api_delete_target_current(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    payload = {
        "target_label": "M31",
        "ra_hours": 0.7122,
        "dec_deg": 41.269,
        "run_parameters": {},
    }
    client.post("/api/v1/target", json=payload)
    del_resp = client.delete("/api/v1/target/current")
    assert del_resp.status_code == 200

    get_resp = client.get("/api/v1/target/current")
    assert get_resp.json() is None


def test_api_session_start_without_staged_target_returns_error(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    resp = client.post("/api/v1/session/start")
    assert resp.status_code == 422


def test_api_session_start_happy_path_creates_session(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    ctrl.session.calibration_accepted = True
    ctrl.active_equipment_profile = _make_profile(profile_id="p1", is_default=True)

    client = TestClient(build_app(controller=ctrl))

    stage_resp = client.post(
        "/api/v1/target",
        json={
            "target_label": "M42",
            "ra_hours": 10.0,
            "dec_deg": 45.0,
            "target_source": "manual",
            "run_parameters": {
                "exposure_seconds": 120,
                "camera_settings": {"iso": 800},
                "stop_condition": {"type": "frame_count", "count": 20},
            },
        },
    )
    assert stage_resp.status_code == 200

    start_resp = client.post("/api/v1/session/start")
    assert start_resp.status_code == 200
    assert ctrl.session.session_id is not None
    assert re.fullmatch(r"session-\d{8}T\d{6}Z-[0-9a-f]{6}", ctrl.session.session_id)
    assert ctrl.session.state not in {ClawState.READY, ClawState.FAILED}


def test_api_session_start_without_active_profile_returns_422(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    ctrl.session.calibration_accepted = True
    assert ctrl.active_equipment_profile is None

    client = TestClient(build_app(controller=ctrl))
    client.post(
        "/api/v1/target",
        json={
            "target_label": "M42",
            "ra_hours": 10.0,
            "dec_deg": 45.0,
            "target_source": "manual",
            "run_parameters": {
                "exposure_seconds": 120,
                "camera_settings": {"iso": 800},
                "stop_condition": {"type": "frame_count", "count": 20},
            },
        },
    )
    resp = client.post("/api/v1/session/start")
    assert resp.status_code == 422
    assert "equipment profile" in resp.json()["detail"].lower()


def test_api_node_status_includes_install_manifest_fields(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    manifest = InstallManifest(
        kepler_version="1.2.3",
        release_id="v1.2.3",
        bootstrap_profile="headless-node",
        installed_at=datetime.now(UTC),
    )
    ctrl.store.write_install_manifest(manifest)

    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["install_manifest"] is not None
    assert data["install_manifest"]["kepler_version"] == "1.2.3"
    assert data["install_manifest"]["bootstrap_profile"] == "headless-node"
    assert data["planner_mode"] == "headless-node"
    conn = data["planner_connection_details"]
    assert conn is not None
    assert conn["indi_port"] == 7624


def test_api_node_status_field_fallback_planner_mode(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    manifest = InstallManifest(
        kepler_version="1.0.0",
        release_id="v1.0.0",
        bootstrap_profile="field-fallback",
        installed_at=datetime.now(UTC),
    )
    ctrl.store.write_install_manifest(manifest)

    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    data = resp.json()
    assert data["planner_mode"] == "field-fallback"
    assert data["planner_connection_details"]["rdp_port"] == 3389


def test_api_node_status_no_manifest_returns_none(tmp_path: Path) -> None:
    client = _make_api_client(tmp_path)
    resp = client.get("/api/v1/node/status")
    data = resp.json()
    assert data["planner_mode"] is None
    assert data["install_manifest"] is None


def test_api_node_status_planner_connection_includes_service_reachability(
    tmp_path: Path,
) -> None:
    """planner_connection_details exposes indi_reachable and kepler_reachable
    when the node backend reports those service states."""
    node = FakeNodeBackend(
        service_healths=[
            ServiceHealth(name="indiserver", healthy=True, summary="active"),
            ServiceHealth(name="kepler-node", healthy=False, summary="inactive"),
        ]
    )
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    ctrl = ClawController(
        session=RuntimeSession(),
        node_backend=node,
        mount_backend=FakeMountBackend(),
        camera_backend=FakeCameraBackend(),
        solver_backend=FakeSolverBackend(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
    )
    manifest = InstallManifest(
        kepler_version="1.0.0",
        release_id="v1.0.0",
        bootstrap_profile="headless-node",
        installed_at=datetime.now(UTC),
    )
    ctrl.store.write_install_manifest(manifest)

    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    conn = resp.json()["planner_connection_details"]
    assert conn is not None
    assert conn["indi_reachable"] is True
    assert conn["kepler_reachable"] is False


def test_api_node_status_field_fallback_includes_xrdp_reachability(
    tmp_path: Path,
) -> None:
    """field-fallback planner_connection_details exposes xrdp_reachable (spec §237)."""
    node = FakeNodeBackend(
        service_healths=[
            ServiceHealth(name="indiserver", healthy=True, summary="active"),
            ServiceHealth(name="kepler-node", healthy=True, summary="active"),
            ServiceHealth(name="xrdp", healthy=True, summary="active"),
        ]
    )
    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    vdir = base / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    ctrl = ClawController(
        session=RuntimeSession(),
        node_backend=node,
        mount_backend=FakeMountBackend(),
        camera_backend=FakeCameraBackend(),
        solver_backend=FakeSolverBackend(),
        store=FilesystemSessionStore(data_root=base),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=vdir,
    )
    manifest = InstallManifest(
        kepler_version="1.0.0",
        release_id="v1.0.0",
        bootstrap_profile="field-fallback",
        installed_at=datetime.now(UTC),
    )
    ctrl.store.write_install_manifest(manifest)

    client = TestClient(build_app(controller=ctrl))
    resp = client.get("/api/v1/node/status")
    assert resp.status_code == 200
    conn = resp.json()["planner_connection_details"]
    assert conn is not None
    assert conn["indi_reachable"] is True
    assert conn["kepler_reachable"] is True
    assert conn["xrdp_reachable"] is True


def test_settings_default_managed_service_names_includes_kepler_and_xrdp() -> None:
    """Settings default includes kepler-node and xrdp so _serve.py wires them correctly."""
    from kepler_node.config import Settings

    settings = Settings()
    assert "kepler-node" in settings.managed_service_names
    assert "xrdp" in settings.managed_service_names
    assert "indiserver" in settings.managed_service_names
