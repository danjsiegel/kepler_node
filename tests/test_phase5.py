"""Phase 5 focused tests — equipment profiles, target intake, session/start,
install manifest, and deployment-profile API surfaces.

These tests verify the new operator-facing endpoints and claw behaviors
introduced in Phase 5 without re-testing Phase 1–4 contracts.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.claw import ClawController
from kepler_node.agent.interfaces import (
    NetworkMode,
    PowerStatus,
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
    EquipmentProfileHardware,
    EquipmentProfileHardwareCamera,
    EquipmentProfileHardwareGps,
    EquipmentProfileHardwareLens,
    EquipmentProfileHardwareMount,
    EquipmentProfileSiteDefaults,
    EquipmentProfileSolvingHints,
    InstallManifest,
)


# ------------------------------------------------------------------ #
# Shared fakes (minimal subset from test_api_readiness pattern)        #
# ------------------------------------------------------------------ #


class FakeNodeBackend:
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
        return []

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
    ctrl = ClawController(
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
    return ctrl


def _make_api_client(tmp_path: Path) -> TestClient:
    ctrl = _make_controller(tmp_path)
    return TestClient(build_app(controller=ctrl))


# ------------------------------------------------------------------ #
# Profile factory helper                                               #
# ------------------------------------------------------------------ #


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


# ------------------------------------------------------------------ #
# Storage: EquipmentProfile CRUD                                       #
# ------------------------------------------------------------------ #


def test_equipment_profile_write_and_read(tmp_path: Path) -> None:
    store = FilesystemSessionStore(data_root=tmp_path)
    profile = _make_profile()
    store.write_profile(profile)

    retrieved = store.read_profile("test-profile")
    assert retrieved is not None
    assert retrieved.display_name == "Test Profile"
    assert retrieved.hardware.mount.model == "EQ6-R"


def test_equipment_profile_list(tmp_path: Path) -> None:
    store = FilesystemSessionStore(data_root=tmp_path)
    store.write_profile(_make_profile("p1", "Profile 1"))
    store.write_profile(_make_profile("p2", "Profile 2"))

    profiles = store.list_profiles()
    ids = {p.profile_id for p in profiles}
    assert ids == {"p1", "p2"}


def test_equipment_profile_delete(tmp_path: Path) -> None:
    store = FilesystemSessionStore(data_root=tmp_path)
    store.write_profile(_make_profile())
    store.delete_profile("test-profile")
    assert store.read_profile("test-profile") is None


def test_equipment_profile_default_flag_exclusivity(tmp_path: Path) -> None:
    """At most one profile may have is_default=True."""
    store = FilesystemSessionStore(data_root=tmp_path)
    store.write_profile(_make_profile("p1", "Profile 1", is_default=True))
    store.write_profile(_make_profile("p2", "Profile 2", is_default=True))

    profiles = store.list_profiles()
    defaults = [p for p in profiles if p.is_default]
    assert len(defaults) == 1, f"Expected exactly 1 default; got {[p.profile_id for p in defaults]}"
    assert defaults[0].profile_id == "p2"


# ------------------------------------------------------------------ #
# Storage: InstallManifest                                             #
# ------------------------------------------------------------------ #


def test_install_manifest_round_trip(tmp_path: Path) -> None:
    store = FilesystemSessionStore(data_root=tmp_path)
    assert store.read_install_manifest() is None

    manifest = InstallManifest(
        kepler_version="1.0.0",
        release_id="v1.0.0",
        bootstrap_profile="headless-node",
        installed_at=datetime.now(UTC),
    )
    store.write_install_manifest(manifest)

    retrieved = store.read_install_manifest()
    assert retrieved is not None
    assert retrieved.kepler_version == "1.0.0"
    assert retrieved.bootstrap_profile == "headless-node"
    assert retrieved.last_upgrade_at is None


# ------------------------------------------------------------------ #
# Claw: zoom-lens gate                                                 #
# ------------------------------------------------------------------ #


def test_zoom_lens_gate_blocks_without_assumption(tmp_path: Path) -> None:
    """check_readiness returns focal_length_assumption_required blocker for zoom lens."""
    ctrl = _make_controller(tmp_path)
    ctrl.active_equipment_profile = _make_profile(is_zoom=True, focal_length_assumption_mm=None)

    conditions = ctrl.check_readiness()
    blocker_names = [c.name for c in conditions]
    assert "focal_length_assumption_required" in blocker_names


def test_zoom_lens_gate_passes_with_assumption(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    ctrl.active_equipment_profile = _make_profile(is_zoom=True, focal_length_assumption_mm=200.0)

    conditions = ctrl.check_readiness()
    blocker_names = [c.name for c in conditions]
    assert "focal_length_assumption_required" not in blocker_names


def test_fixed_lens_no_zoom_gate(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    ctrl.active_equipment_profile = _make_profile(is_zoom=False)

    conditions = ctrl.check_readiness()
    blocker_names = [c.name for c in conditions]
    assert "focal_length_assumption_required" not in blocker_names


# ------------------------------------------------------------------ #
# Claw: stage_target_intake / clear_staged_target                      #
# ------------------------------------------------------------------ #


def test_stage_and_clear_target(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path)
    ctrl.stage_target_intake(
        target_label="M31",
        ra_hours=0.7122,
        dec_deg=41.269,
        target_source="manual",
        run_parameters={
            "exposure_seconds": 120,
            "camera_settings": {"gain": 100},
            "stop_condition": {"frame_count": 60},
        },
    )
    assert ctrl.session.staged_target_ra_hours == pytest.approx(0.7122)
    assert ctrl._staged_target_label == "M31"

    ctrl.clear_staged_target()
    assert ctrl.session.staged_target_ra_hours is None
    assert ctrl._staged_target_label is None


def test_start_session_requires_ready_state(tmp_path: Path) -> None:
    """start_session raises ValueError when state is not ready."""
    ctrl = _make_controller(tmp_path, state=ClawState.BOOT)
    with pytest.raises(ValueError, match="ready"):
        ctrl.start_session()


def test_start_session_requires_staged_target(tmp_path: Path) -> None:
    ctrl = _make_controller(tmp_path, state=ClawState.READY)
    with pytest.raises((ValueError, RuntimeError)):
        ctrl.start_session()


# ------------------------------------------------------------------ #
# API: GET /api/v1/equipment/profiles                                  #
# ------------------------------------------------------------------ #


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

    # Active profile now shown in list
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
    """PUT on the active profile must update in-memory state and clear accepted calibration.

    Spec line 1631: when an edit to the active profile succeeds outside a managed
    session, any affected accepted calibration must be cleared and updated blockers
    surfaced immediately.
    """
    ctrl = _make_controller(tmp_path)
    profile = _make_profile(profile_id="rig-active", is_default=True)
    ctrl.store.write_profile(profile)
    ctrl.active_equipment_profile = profile
    ctrl.session.calibration_accepted = True  # simulate prior accepted calibration

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

    # In-memory active profile must reflect the new data
    assert ctrl.active_equipment_profile is not None
    assert ctrl.active_equipment_profile.display_name == "Rig Active Updated"

    # Calibration must have been cleared
    assert ctrl.session.calibration_accepted is False


# ------------------------------------------------------------------ #
# API: GET/POST/DELETE /api/v1/target                                  #
# ------------------------------------------------------------------ #


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

    # GET reflects the staged target
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


# ------------------------------------------------------------------ #
# API: POST /api/v1/session/start                                      #
# ------------------------------------------------------------------ #


def test_api_session_start_without_staged_target_returns_error(tmp_path: Path) -> None:
    """session/start from READY with no staged target is a validation failure → 422."""
    client = _make_api_client(tmp_path)
    resp = client.post("/api/v1/session/start")
    assert resp.status_code == 422


def test_api_session_start_happy_path_creates_session(tmp_path: Path) -> None:
    """Stage a valid target with run parameters, then session/start should succeed.

    With calibration pre-accepted and the fake solver returning residual 2.0 arcmin
    (well within the 15.0 arcmin centering tolerance), the verification loop
    completes in one pass and the session transitions to CAPTURE.
    """
    ctrl = _make_controller(tmp_path)
    ctrl.session.calibration_accepted = True  # skip calibration gate
    ctrl.active_equipment_profile = _make_profile(profile_id="p1", is_default=True)

    client = TestClient(build_app(controller=ctrl))

    stage_resp = client.post(
        "/api/v1/target",
        json={
            "target_label": "M42",
            "ra_hours": 10.0,  # matches FakeSolverBackend output exactly
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
    # session_id must match canonical format: session-YYYYMMDDTHHMMSSZ-<6hex> (spec line 1260)
    import re
    assert re.fullmatch(r"session-\d{8}T\d{6}Z-[0-9a-f]{6}", ctrl.session.session_id), (
        f"session_id {ctrl.session.session_id!r} does not match canonical format"
    )
    # Session must have advanced past READY (centering loop ran successfully)
    assert ctrl.session.state not in {ClawState.READY, ClawState.FAILED}


# ------------------------------------------------------------------ #
# API: GET /api/v1/node/status — install manifest and planner mode     #
# ------------------------------------------------------------------ #

def test_api_session_start_without_active_profile_returns_422(tmp_path: Path) -> None:
    """POST /api/v1/session/start must return 422 when no active equipment profile is set."""
    ctrl = _make_controller(tmp_path)
    ctrl.session.calibration_accepted = True
    # Deliberately leave active_equipment_profile as None
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
    # Write an install manifest directly to the same store
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


# ------------------------------------------------------------------ #
# Claw: discover() auto-selects default equipment profile              #
# ------------------------------------------------------------------ #


def test_discover_auto_selects_single_default_profile(tmp_path: Path) -> None:
    """discover() loads the one profile marked is_default and sets it as active."""
    ctrl = _make_controller(tmp_path, state=ClawState.DISCOVER)
    profile = _make_profile(profile_id="rig-a", is_default=True)
    ctrl.store.write_profile(profile)

    result = ctrl.discover()

    assert result.next_state == ClawState.CONNECT
    assert ctrl.active_equipment_profile is not None
    assert ctrl.active_equipment_profile.profile_id == "rig-a"
    names = [c.name for c in result.degraded]
    assert "multiple_default_profiles" not in names
    assert "no_default_equipment_profile" not in names


def test_discover_degrades_on_multiple_default_profiles(tmp_path: Path) -> None:
    """discover() surfaces a degraded condition when more than one profile is default."""
    ctrl = _make_controller(tmp_path, state=ClawState.DISCOVER)
    # Write two profiles both marked is_default (bypassing _clear_default_flag)
    for pid in ("rig-a", "rig-b"):
        p = _make_profile(profile_id=pid, is_default=True)
        path = ctrl.store.profiles_root / f"{pid}.json"
        ctrl.store.profiles_root.mkdir(parents=True, exist_ok=True)
        path.write_text(p.model_dump_json(indent=2), encoding="utf-8")

    result = ctrl.discover()

    assert result.next_state == ClawState.PAUSED
    names = [c.name for c in result.degraded]
    assert "multiple_default_profiles" in names


def test_discover_degrades_when_profiles_exist_but_none_is_default(tmp_path: Path) -> None:
    """discover() surfaces a degraded condition when profiles exist but none is default."""
    ctrl = _make_controller(tmp_path, state=ClawState.DISCOVER)
    profile = _make_profile(profile_id="rig-a", is_default=False)
    ctrl.store.write_profile(profile)

    result = ctrl.discover()

    assert result.next_state == ClawState.PAUSED
    assert ctrl.active_equipment_profile is None
    names = [c.name for c in result.degraded]
    assert "no_default_equipment_profile" in names


def test_discover_no_profiles_pauses_with_no_default_equipment_profile(tmp_path: Path) -> None:
    """discover() with zero stored profiles pauses with no_default_equipment_profile."""
    ctrl = _make_controller(tmp_path, state=ClawState.DISCOVER)

    result = ctrl.discover()

    assert result.next_state == ClawState.PAUSED
    names = [c.name for c in result.degraded]
    assert "no_default_equipment_profile" in names
    assert ctrl.active_equipment_profile is None


# ------------------------------------------------------------------ #
# Upgrade path: manifest result timing and service restart behavior    #
# ------------------------------------------------------------------ #

_REPO_ROOT = Path(__file__).parent.parent


def test_install_manifest_accepts_in_progress_result(tmp_path: Path) -> None:
    """InstallManifest persists 'in-progress' as last_upgrade_result."""
    store = FilesystemSessionStore(data_root=tmp_path)
    manifest = InstallManifest(
        kepler_version="1.0.0",
        release_id="v1.0.0",
        bootstrap_profile="headless-node",
        installed_at=datetime.now(UTC),
        last_upgrade_result="in-progress",
    )
    store.write_install_manifest(manifest)
    retrieved = store.read_install_manifest()
    assert retrieved is not None
    assert retrieved.last_upgrade_result == "in-progress"


def test_install_manifest_accepts_health_checks_failed_result(tmp_path: Path) -> None:
    """InstallManifest persists 'health-checks-failed' as last_upgrade_result."""
    store = FilesystemSessionStore(data_root=tmp_path)
    manifest = InstallManifest(
        kepler_version="1.0.0",
        release_id="v1.0.0",
        bootstrap_profile="field-fallback",
        installed_at=datetime.now(UTC),
        last_upgrade_result="health-checks-failed",
    )
    store.write_install_manifest(manifest)
    retrieved = store.read_install_manifest()
    assert retrieved is not None
    assert retrieved.last_upgrade_result == "health-checks-failed"


def test_upgrade_sh_writes_in_progress_before_health_checks() -> None:
    """upgrade.sh must write 'in-progress' at step 4, not 'success'."""
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    # Step 4 manifest write must NOT contain "success" as the initial value
    # Find the manifest heredoc block and verify it uses in-progress
    assert '"last_upgrade_result": "in-progress"' in content, (
        "upgrade.sh Step 4 must write last_upgrade_result=in-progress, "
        "not success, before health checks run"
    )


def test_upgrade_sh_records_health_checks_failed_on_exit_1() -> None:
    """upgrade.sh must update manifest to health-checks-failed before exit 1."""
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    # The sed command must appear in the file AND before the exit 1 that follows it.
    # A bare string-presence check would pass even if the sed were placed after exit 1
    # (unreachable) or only happened to appear in a comment.
    hcf_pos = content.find('"health-checks-failed"')
    assert hcf_pos != -1, (
        "upgrade.sh must contain a sed command for 'health-checks-failed' manifest outcome"
    )
    exit_pos = content.find("exit 1", hcf_pos)
    assert exit_pos != -1, (
        "upgrade.sh must call 'exit 1' after the health-checks-failed sed"
    )
    assert hcf_pos < exit_pos, (
        "upgrade.sh must record health-checks-failed before exit 1 (sed must precede exit 1)"
    )


def test_upgrade_sh_stops_services_before_code_changes() -> None:
    """upgrade.sh must stop managed services before git pull (spec line 1800 step 4)."""
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    stop_pos = content.find("systemctl stop kepler-node")
    pull_pos = content.find("git pull")
    assert stop_pos != -1, "upgrade.sh must call 'systemctl stop kepler-node'"
    assert pull_pos != -1, "upgrade.sh must call 'git pull'"
    assert stop_pos < pull_pos, (
        "upgrade.sh must stop kepler-node before git pull (spec: stop services "
        "before applying changes)"
    )


def test_upgrade_sh_starts_service_unconditionally_when_not_skip_restart() -> None:
    """upgrade.sh Step 5 must start kepler-node without requiring it was previously active."""
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    # The new Step 5 must use 'systemctl start' not 'is-active && restart'
    assert re.search(r'systemctl start kepler-node', content), (
        "upgrade.sh Step 5 must use 'systemctl start kepler-node' so it works "
        "when the service was stopped or inactive before the upgrade"
    )
    # Must NOT gate the start on prior is-active state
    combined_pattern = r'is-active.*kepler-node.*\n.*systemctl (restart|start) kepler-node'
    assert not re.search(combined_pattern, content), (
        "upgrade.sh Step 5 must not condition service start on prior is-active state"
    )


def test_upgrade_sh_sets_health_fail_on_service_restart_failure() -> None:
    """upgrade.sh Step 5 must set HEALTH_FAIL=true when kepler-node fails to start.

    Without this, Step 6 reinitialises HEALTH_FAIL=false and runs the full 60-second
    API timeout even though the root cause (service start failure) was already recorded.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    # Find the service-restart-failed manifest sed
    srf_pos = content.find('"service-restart-failed"')
    assert srf_pos != -1, "upgrade.sh Step 5 must record service-restart-failed in manifest"
    # HEALTH_FAIL=true must appear after service-restart-failed sed and before the API wait
    api_wait_pos = content.find("Waiting for Kepler API")
    assert api_wait_pos != -1, "upgrade.sh must contain the API wait section"
    health_fail_true_pos = content.find("HEALTH_FAIL=true", srf_pos)
    assert health_fail_true_pos != -1, (
        "upgrade.sh must set HEALTH_FAIL=true after service-restart-failed sed so that "
        "Step 6 does not waste 60 seconds polling an API that will never respond"
    )
    assert health_fail_true_pos < api_wait_pos, (
        "upgrade.sh HEALTH_FAIL=true (service-restart path) must be set before the API "
        "wait loop so the loop is skipped when service start already failed"
    )


def test_release_json_exists_with_required_fields() -> None:
    """release.json must exist in the repo root and contain the required metadata fields.

    Spec lines 1762-1780: each supported release must ship with explicit metadata that
    the installer and upgrader can read.  Minimum required fields cover OS, architecture,
    free-space, managed services, and schema versions.
    """
    import json

    release_path = _REPO_ROOT / "release.json"
    assert release_path.exists(), (
        "release.json must be present in the repo root so upgrade.sh can read release "
        "metadata and run compatibility preflight checks (spec line 1762)"
    )
    data = json.loads(release_path.read_text())
    for field in (
        "release_id",
        "kepler_version",
        "required_os",
        "required_architecture",
        "required_free_space_mb",
        "managed_services",
    ):
        assert field in data, (
            f"release.json must contain '{field}' (spec line 1767: recommended release "
            "metadata fields include OS, arch, free-space, and managed services)"
        )
    assert isinstance(data["managed_services"], list), (
        "release.json managed_services must be a list"
    )
    assert data["required_free_space_mb"] > 0, (
        "release.json required_free_space_mb must be a positive integer"
    )


def test_upgrade_sh_reads_release_metadata() -> None:
    """upgrade.sh must read release.json before stopping services (spec line 1803 step 2).

    The minimum upgrade flow requires reading target release metadata before running
    preflight checks or stopping services.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "release.json" in content, (
        "upgrade.sh must read release.json as the target release metadata "
        "(spec line 1803: step 2 of the minimum upgrade flow)"
    )
    # release.json read must precede service stop
    release_pos = content.find("release.json")
    stop_pos = content.find("systemctl stop kepler-node")
    assert release_pos < stop_pos, (
        "upgrade.sh must read release.json before stopping services "
        "(spec: read metadata → preflight → stop services)"
    )


def test_upgrade_sh_preflight_checks_os_and_architecture() -> None:
    """upgrade.sh must check OS and architecture before making changes (spec lines 1792-1794).

    Minimum preflight checks include supported OS and supported architecture.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    # OS check: reads uname -s and compares to required_os
    assert "uname -s" in content or "required_os" in content, (
        "upgrade.sh must check the current OS against required_os from release.json "
        "(spec line 1793: supported OS is a required preflight check)"
    )
    # Architecture check: reads uname -m and compares to required_architecture
    assert "uname -m" in content or "required_architecture" in content, (
        "upgrade.sh must check the current architecture against required_architecture "
        "(spec line 1793: supported architecture is a required preflight check)"
    )
    # Both checks must precede service stop
    arch_pos = content.find("uname -m")
    stop_pos = content.find("systemctl stop kepler-node")
    assert arch_pos < stop_pos, (
        "upgrade.sh architecture check must run before stopping services"
    )


def test_upgrade_sh_preflight_checks_free_space() -> None:
    """upgrade.sh must check available free space before making changes (spec line 1796).

    Minimum preflight checks include required writable storage and minimum free space.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "required_free_space_mb" in content or "REQ_FREE_MB" in content, (
        "upgrade.sh must check available free space against required_free_space_mb "
        "(spec line 1796: free space is a required preflight check)"
    )
    free_pos = content.find("REQ_FREE_MB")
    stop_pos = content.find("systemctl stop kepler-node")
    assert free_pos != -1 and free_pos < stop_pos, (
        "upgrade.sh free-space check must run before stopping services"
    )


def test_upgrade_sh_preflight_checks_service_layout() -> None:
    """upgrade.sh must verify expected managed service layout before making changes.

    Spec line 1797: presence of the expected managed service layout is a required
    preflight check.  This ensures upgrade.sh is run on a bootstrapped node.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "systemctl cat" in content, (
        "upgrade.sh must call 'systemctl cat <service>' to verify the managed service "
        "layout exists before proceeding (spec line 1797)"
    )
    cat_pos = content.find("systemctl cat")
    stop_pos = content.find("systemctl stop kepler-node")
    assert cat_pos < stop_pos, (
        "upgrade.sh service-layout check must run before stopping services"
    )


def test_upgrade_sh_preflight_checks_manifest_writeability() -> None:
    """upgrade.sh must verify the install manifest is writable before making changes.

    Spec line 1797: ability to write the install manifest and upgrade outcome record
    is a required preflight check.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    # The writeability check uses 'touch' on the manifest path
    assert "touch" in content, (
        "upgrade.sh must use 'touch' to verify install manifest writeability "
        "before making changes (spec line 1797)"
    )
    touch_pos = content.find("touch")
    stop_pos = content.find("systemctl stop kepler-node")
    assert touch_pos < stop_pos, (
        "upgrade.sh manifest-writeability check must run before stopping services"
    )


def test_scripts_do_not_install_dev_dependencies() -> None:
    """bootstrap.sh and upgrade.sh must not install the dev dependency group on the node.

    The dev group includes ruff, pytest, and httpx — test/linting tools with no role
    on a deployed Pi.  Runtime installs need only --extra local-api --extra ui.
    """
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "--group dev" not in content, (
            f"{script_name} must not install the dev dependency group on deployed nodes; "
            "remove '--group dev' from the uv sync call"
        )





def test_bootstrap_sh_field_fallback_creates_indiserver_service() -> None:
    """bootstrap.sh must create indiserver.service for both profiles (spec line 1661, 1682)."""
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    # indiserver.service creation should NOT be inside an headless-node-only elif block
    # Verify the service file write is present outside a headless-only branch
    indiserver_write_pos = content.find("indiserver.service")
    assert indiserver_write_pos != -1, "bootstrap.sh must write indiserver.service"
    # Verify indiserver is NOT only written inside `elif headless-node` block
    # by checking that the block writing to INDI_SERVICE is not guarded by headless-node
    indi_section = content[max(0, indiserver_write_pos - 300) : indiserver_write_pos + 100]
    assert 'elif [[ "${PROFILE}" == "headless-node"' not in indi_section, (
        "bootstrap.sh must provision indiserver.service for all profiles, "
        "not only headless-node (field-fallback is a superset per spec line 1661)"
    )


def test_bootstrap_sh_field_fallback_includes_indiserver_in_service_ordering() -> None:
    """bootstrap.sh kepler-node.service must depend on indiserver for all profiles."""
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    # SERVICE_WANTS and SERVICE_AFTER must include indiserver for both profiles
    # (the old code conditionally excluded field-fallback)
    assert "indiserver.service" in content
    # Verify it's not only set for headless-node: the assignment must not be
    # inside a headless-node conditional
    service_wants_match = re.search(
        r'SERVICE_WANTS="[^"]*indiserver\.service[^"]*"', content
    )
    assert service_wants_match is not None, (
        "bootstrap.sh SERVICE_WANTS must include indiserver.service for all profiles"
    )
    # Check the assignment is not gated on headless-node profile
    pre_block = content[: service_wants_match.start()]
    # The last if/elif before this assignment must not be headless-node-only
    last_if = pre_block.rfind('if [[ "${PROFILE}"')
    last_elif = pre_block.rfind('elif [[ "${PROFILE}"')
    gating_pos = max(last_if, last_elif)
    if gating_pos != -1:
        gating_line = content[gating_pos : gating_pos + 60]
        assert "headless-node" not in gating_line, (
            "bootstrap.sh SERVICE_WANTS indiserver.service must apply to all profiles, "
            "not be gated behind headless-node"
        )


def test_bootstrap_sh_health_check_verifies_indiserver_service_active() -> None:
    """bootstrap.sh must check 'systemctl is-active indiserver' (spec line 1682)."""
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "systemctl is-active --quiet indiserver" in content, (
        "bootstrap.sh health checks must verify indiserver service is active, "
        "not only that the binary exists"
    )


def test_upgrade_sh_preflight_checks_supported_from_versions() -> None:
    """upgrade.sh must gate on supported_from_versions before stopping services.

    Spec line 1792: minimum preflight checks include supported current installed
    version.  Spec line 1784: upgrades are supported only between explicitly
    declared compatible releases.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "supported_from_versions" in content or "SUPPORTED_FROM" in content, (
        "upgrade.sh must read and enforce supported_from_versions from release.json "
        "(spec line 1792: supported current installed version is a required preflight check)"
    )
    supported_pos = content.find("SUPPORTED_FROM")
    stop_pos = content.find("systemctl stop kepler-node")
    assert supported_pos != -1 and supported_pos < stop_pos, (
        "upgrade.sh supported-version check must run before stopping services"
    )
    # The check must fail closed (call fail) when version is unsupported
    assert "fail " in content[supported_pos : stop_pos], (
        "upgrade.sh must call fail() when the current version is not in supported_from_versions"
    )


def test_upgrade_sh_stops_and_restarts_indiserver() -> None:
    """upgrade.sh must stop and restart indiserver as a managed service.

    Spec lines 1787-1788, 1808: upgrade script must stop and restart managed
    services in a known order.  release.json lists indiserver in managed_services.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "systemctl stop indiserver" in content, (
        "upgrade.sh must stop indiserver as part of managed-service shutdown "
        "(release.json lists indiserver in managed_services)"
    )
    assert "systemctl start indiserver" in content, (
        "upgrade.sh must start indiserver as part of managed-service restart "
        "(release.json lists indiserver in managed_services)"
    )
    # indiserver must be stopped before kepler-node is started, and started before kepler-node
    stop_indi_pos = content.find("systemctl stop indiserver")
    start_indi_pos = content.find("systemctl start indiserver")
    start_kepler_pos = content.find("systemctl start kepler-node")
    stop_kepler_pos = content.find("systemctl stop kepler-node")
    assert stop_indi_pos > stop_kepler_pos, (
        "upgrade.sh must stop kepler-node before indiserver (dependency order)"
    )
    assert start_indi_pos < start_kepler_pos, (
        "upgrade.sh must start indiserver before kepler-node (dependency order)"
    )


def test_bootstrap_sh_astrometry_index_check_fails_closed() -> None:
    """bootstrap.sh must set HEALTH_FAIL=true when astrometry index files are missing.

    Spec lines 1683-1688, 1900: required offline index files are a minimum
    post-bootstrap health check; missing indexes must fail the health check, not
    just warn.
    """
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    # Find the astrometry index check block
    fits_pos = content.find("astrometry/*.fits")
    assert fits_pos != -1, "bootstrap.sh must check for astrometry index files"
    # Find the HEALTH_FAIL=true that follows within the same else block
    block = content[fits_pos : fits_pos + 300]
    assert "HEALTH_FAIL=true" in block, (
        "bootstrap.sh must set HEALTH_FAIL=true when astrometry index files are missing "
        "(spec line 1683: required offline index files are a required health check)"
    )


def test_upgrade_sh_astrometry_index_check_fails_closed() -> None:
    """upgrade.sh must set HEALTH_FAIL=true when astrometry index files are missing.

    Spec line 1900: post-upgrade checks must prove presence of required offline index
    files; missing indexes must fail the health check, not just warn.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    fits_pos = content.find("astrometry/*.fits")
    assert fits_pos != -1, "upgrade.sh must check for astrometry index files"
    block = content[fits_pos : fits_pos + 300]
    assert "HEALTH_FAIL=true" in block, (
        "upgrade.sh must set HEALTH_FAIL=true when astrometry index files are missing "
        "(spec line 1900: required offline index files must be proven present)"
    )


def test_release_json_managed_services_includes_kepler_ui() -> None:
    """release.json managed_services must include kepler-ui.

    Spec line 1797: the upgrade preflight must verify the expected managed service
    layout, which includes kepler-ui.  bootstrap.sh always installs kepler-ui.service
    (bootstrap.sh:216-241), so a bootstrapped node always has all three services.
    """
    import json

    data = json.loads((_REPO_ROOT / "release.json").read_text())
    assert "kepler-ui" in data["managed_services"], (
        "release.json managed_services must include 'kepler-ui' so upgrade.sh preflight "
        "verifies the full bootstrapped service layout (spec line 1797)"
    )


def test_upgrade_sh_preflight_uses_release_metadata_managed_services() -> None:
    """upgrade.sh must read managed_services from release metadata for the preflight check.

    Spec line 1797: the upgrade preflight must verify the presence of the expected
    managed service layout.  Using the release metadata ensures the check stays
    in sync with the declared services rather than a hardcoded subset.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "MANAGED_SVCS" in content or "managed_services" in content, (
        "upgrade.sh must read managed_services from the release metadata "
        "(spec line 1797: preflight must verify the expected managed service layout)"
    )
    # The managed-service loop must reference the variable from release metadata
    assert "for SVC in ${MANAGED_SVCS}" in content or "MANAGED_SVCS" in content, (
        "upgrade.sh service-layout preflight must iterate MANAGED_SVCS extracted from "
        "release metadata rather than hardcoding a smaller service list"
    )
    # Preflight must run before stopping services
    managed_pos = content.find("MANAGED_SVCS")
    stop_pos = content.find("systemctl stop kepler-node")
    assert managed_pos < stop_pos, (
        "upgrade.sh managed-service preflight must run before stopping services"
    )


def test_upgrade_sh_reads_target_ref_release_metadata_for_release_flag() -> None:
    """upgrade.sh must read release.json from the target ref when --release is given.

    Spec lines 1800-1810 step 2: 'Read target release metadata' must use the target
    release's own release.json, not the current checkout's file.  When --release is
    specified, upgrade.sh must fetch the target ref and use 'git show' to read
    release.json from that ref before running preflight checks or stopping services.
    """
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "git show" in content, (
        "upgrade.sh must use 'git show <ref>:release.json' to read release metadata "
        "from the target ref when --release is specified (spec line 1803 step 2: "
        "read target release metadata before preflight)"
    )
    # git show must precede stopping services
    git_show_pos = content.find("git show")
    stop_pos = content.find("systemctl stop kepler-node")
    assert git_show_pos < stop_pos, (
        "upgrade.sh must read target ref release metadata (git show) before stopping "
        "services (spec: read metadata → preflight → stop services)"
    )
    # The fetch that enables git show must also precede stopping services
    fetch_pos = content.find("git fetch")
    assert fetch_pos < stop_pos, (
        "upgrade.sh must fetch the target ref before reading its release metadata "
        "and before stopping services"
    )
