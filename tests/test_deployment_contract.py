"""Contract tests for bootstrap, upgrade, and release metadata scripts."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent


def test_upgrade_sh_writes_in_progress_before_health_checks() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert '"last_upgrade_result": "in-progress"' in content, (
        "upgrade.sh Step 4 must write last_upgrade_result=in-progress, "
        "not success, before health checks run"
    )


def test_upgrade_sh_records_health_checks_failed_on_exit_1() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    hcf_pos = content.find('"health-checks-failed"')
    assert hcf_pos != -1, (
        "upgrade.sh must contain a sed command for 'health-checks-failed' manifest outcome"
    )
    exit_pos = content.find("exit 1", hcf_pos)
    assert exit_pos != -1, "upgrade.sh must call 'exit 1' after the health-checks-failed sed"
    assert hcf_pos < exit_pos, (
        "upgrade.sh must record health-checks-failed before exit 1 (sed must precede exit 1)"
    )


def test_upgrade_sh_stops_services_before_code_changes() -> None:
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
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert re.search(r"systemctl start kepler-node", content), (
        "upgrade.sh Step 5 must use 'systemctl start kepler-node' so it works "
        "when the service was stopped or inactive before the upgrade"
    )
    combined_pattern = r"is-active.*kepler-node.*\n.*systemctl (restart|start) kepler-node"
    assert not re.search(combined_pattern, content), (
        "upgrade.sh Step 5 must not condition service start on prior is-active state"
    )


def test_upgrade_sh_sets_health_fail_on_service_restart_failure() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    srf_pos = content.find('"service-restart-failed"')
    assert srf_pos != -1, "upgrade.sh Step 5 must record service-restart-failed in manifest"
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
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "release.json" in content, (
        "upgrade.sh must read release.json as the target release metadata "
        "(spec line 1803: step 2 of the minimum upgrade flow)"
    )
    release_pos = content.find("release.json")
    stop_pos = content.find("systemctl stop kepler-node")
    assert release_pos < stop_pos, (
        "upgrade.sh must read release.json before stopping services "
        "(spec: read metadata → preflight → stop services)"
    )


def test_upgrade_sh_defines_indi_port_for_service_template() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert 'INDI_PORT=7624' in content, (
        "upgrade.sh must define INDI_PORT before rendering the indiserver service template"
    )
    assert 'indiserver -f /run/kepler-indiserver/control.fifo -p ${INDI_PORT}' in content, (
        "upgrade.sh must render the indiserver unit with the configured INDI port"
    )


def test_upgrade_sh_preflight_checks_os_and_architecture() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "uname -s" in content or "required_os" in content, (
        "upgrade.sh must check the current OS against required_os from release.json "
        "(spec line 1793: supported OS is a required preflight check)"
    )
    assert "uname -m" in content or "required_architecture" in content, (
        "upgrade.sh must check the current architecture against required_architecture "
        "(spec line 1793: supported architecture is a required preflight check)"
    )
    arch_pos = content.find("uname -m")
    stop_pos = content.find("systemctl stop kepler-node")
    assert arch_pos < stop_pos, "upgrade.sh architecture check must run before stopping services"


def test_upgrade_sh_preflight_checks_free_space() -> None:
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
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "systemctl cat" in content, (
        "upgrade.sh must call 'systemctl cat <service>' to verify the managed service "
        "layout exists before proceeding (spec line 1797)"
    )
    cat_pos = content.find("systemctl cat")
    stop_pos = content.find("systemctl stop kepler-node")
    assert cat_pos < stop_pos, "upgrade.sh service-layout check must run before stopping services"


def test_upgrade_sh_preflight_checks_manifest_writeability() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
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
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "--group dev" not in content, (
            f"{script_name} must not install the dev dependency group on deployed nodes; "
            "remove '--group dev' from the uv sync call"
        )


def test_bootstrap_sh_installs_gphoto2_for_direct_camera_backend() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "gphoto2" in content, (
        "bootstrap.sh must install gphoto2 because the supported direct camera backend depends on it"
    )


def test_bootstrap_sh_installs_full_supported_runtime_once() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    for package_name in ("kstars", "xrdp", "tigervnc-standalone-server"):
        assert package_name in content, (
            f"bootstrap.sh must install {package_name} as part of the full supported package set"
        )
    assert 'if [[ "${PROFILE}" == "field-fallback" ]]' not in content[content.find("COMMON_PACKAGES="):content.find("ok \"System prerequisites installed\"")], (
        "bootstrap.sh should not split the apt install footprint by profile"
    )


def test_bootstrap_sh_falls_back_when_indi_full_is_unavailable() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "apt_package_available indi-full" in content, (
        "bootstrap.sh should detect whether indi-full exists instead of assuming the meta-package is present"
    )
    assert "COMMON_PACKAGES+=(indi-bin)" in content, (
        "bootstrap.sh must fall back to indi-bin when indi-full is unavailable"
    )
    assert "indi-gphoto" in content and "indi-gpsd" in content, (
        "bootstrap.sh should install available INDIGO/INDI support packages needed for the supported node posture"
    )


def test_bootstrap_sh_field_profile_does_not_require_ekos_package_name() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "ekos" not in content, (
        "bootstrap.sh should not require a separate ekos apt package when the distro ships KStars without that split"
    )


def test_bootstrap_sh_makes_uv_available_after_installer_runs() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "ensure_uv_installed()" in content, (
        "bootstrap.sh should centralize uv installation and path repair in a helper"
    )
    assert "ln -sf" in content and "/usr/local/bin/uv" in content, (
        "bootstrap.sh must make the discovered uv binary reachable from a stable system path"
    )
    assert 'command -v uv >/dev/null 2>&1 || fail "uv installation succeeded but the uv binary is not on PATH"' in content, (
        "bootstrap.sh must fail clearly if uv still is not resolvable after installation"
    )


def test_bootstrap_sh_field_fallback_creates_indiserver_service() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    indiserver_write_pos = content.find("indiserver.service")
    assert indiserver_write_pos != -1, "bootstrap.sh must write indiserver.service"
    indi_section = content[max(0, indiserver_write_pos - 300) : indiserver_write_pos + 100]
    assert 'elif [[ "${PROFILE}" == "headless-node"' not in indi_section, (
        "bootstrap.sh must provision indiserver.service for all profiles, "
        "not only headless-node (field-fallback is a superset per spec line 1661)"
    )


def test_bootstrap_and_upgrade_use_fifo_mode_for_generic_indiserver() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "RuntimeDirectory=kepler-indiserver" in content, (
            f"{script_name} must provision a runtime directory for the INDI control FIFO"
        )
        assert "ExecStartPre=/usr/bin/mkfifo /run/kepler-indiserver/control.fifo" in content, (
            f"{script_name} must create the INDI control FIFO before startup"
        )
        assert "ExecStart=/usr/bin/indiserver -f /run/kepler-indiserver/control.fifo -p ${INDI_PORT}" in content, (
            f"{script_name} must run indiserver in FIFO mode so the generic service stays up without hardcoded drivers"
        )


def test_bootstrap_and_upgrade_disable_gvfs_camera_auto_claimer() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "gvfs-gphoto2-volume-monitor.service" in content, (
            f"{script_name} must disable the GVFS gphoto monitor so desktop sessions do not steal USB cameras"
        )
        assert "systemctl --global mask" in content, (
            f"{script_name} must globally mask the GVFS gphoto monitor for future desktop sessions"
        )
        assert "systemctl --user mask --now" in content, (
            f"{script_name} must stop and mask the GVFS gphoto monitor in active user sessions"
        )
        assert "pkill -x gvfsd-gphoto2" in content, (
            f"{script_name} must kill an already-running gvfsd-gphoto2 process so the camera is immediately releasable"
        )


def test_upgrade_sh_refreshes_managed_service_units() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "Step 3b: Refreshing managed service units" in content, (
        "upgrade.sh should refresh managed service units before restart so legacy broken units are repaired"
    )
    assert 'write_indiserver_service "/etc/systemd/system/indiserver.service"' in content, (
        "upgrade.sh must rewrite the canonical indiserver.service during upgrades"
    )
    assert "Step 3c: Preventing desktop camera auto-claimers" in content, (
        "upgrade.sh should disable GVFS camera auto-claimers before restarting services"
    )


def test_bootstrap_sh_field_fallback_includes_indiserver_in_service_ordering() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "indiserver.service" in content
    service_wants_match = re.search(r'SERVICE_WANTS="[^"]*indiserver\.service[^"]*"', content)
    assert service_wants_match is not None, (
        "bootstrap.sh SERVICE_WANTS must include indiserver.service for all profiles"
    )
    pre_block = content[: service_wants_match.start()]
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
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "systemctl is-active --quiet indiserver" in content, (
        "bootstrap.sh health checks must verify indiserver service is active, "
        "not only that the binary exists"
    )


def test_bootstrap_sh_refuses_rerun_when_install_manifest_exists() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "Existing install manifest found" in content, (
        "bootstrap.sh should fail fast when an install manifest already exists"
    )
    assert "Use upgrade.sh for updates" in content, (
        "bootstrap.sh should direct existing installs to upgrade.sh instead of rebootstrap"
    )


def test_upgrade_sh_preflight_checks_supported_from_versions() -> None:
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
    assert "fail " in content[supported_pos:stop_pos], (
        "upgrade.sh must call fail() when the current version is not in supported_from_versions"
    )


def test_upgrade_sh_stops_and_restarts_indiserver() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "systemctl stop indiserver" in content, (
        "upgrade.sh must stop indiserver as part of managed-service shutdown "
        "(release.json lists indiserver in managed_services)"
    )
    assert "systemctl start indiserver" in content, (
        "upgrade.sh must start indiserver as part of managed-service restart "
        "(release.json lists indiserver in managed_services)"
    )
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
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    fits_pos = content.find("astrometry/*.fits")
    assert fits_pos != -1, "bootstrap.sh must check for astrometry index files"
    block = content[fits_pos : fits_pos + 300]
    assert "HEALTH_FAIL=true" in block, (
        "bootstrap.sh must set HEALTH_FAIL=true when astrometry index files are missing "
        "(spec line 1683: required offline index files are a required health check)"
    )


def test_upgrade_sh_astrometry_index_check_fails_closed() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    fits_pos = content.find("astrometry/*.fits")
    assert fits_pos != -1, "upgrade.sh must check for astrometry index files"
    block = content[fits_pos : fits_pos + 300]
    assert "HEALTH_FAIL=true" in block, (
        "upgrade.sh must set HEALTH_FAIL=true when astrometry index files are missing "
        "(spec line 1900: required offline index files must be proven present)"
    )


def test_bootstrap_and_upgrade_health_checks_verify_gphoto2() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "command -v gphoto2" in content, (
            f"{script_name} must verify the gphoto2 binary because the direct camera backend depends on it"
        )


def test_release_json_managed_services_includes_kepler_ui() -> None:
    import json

    data = json.loads((_REPO_ROOT / "release.json").read_text())
    assert "kepler-ui" in data["managed_services"], (
        "release.json managed_services must include 'kepler-ui' so upgrade.sh preflight "
        "verifies the full bootstrapped service layout (spec line 1797)"
    )


def test_upgrade_sh_preflight_uses_release_metadata_managed_services() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "MANAGED_SVCS" in content or "managed_services" in content, (
        "upgrade.sh must read managed_services from the release metadata "
        "(spec line 1797: preflight must verify the expected managed service layout)"
    )
    assert "for SVC in ${MANAGED_SVCS}" in content or "MANAGED_SVCS" in content, (
        "upgrade.sh service-layout preflight must iterate MANAGED_SVCS extracted from "
        "release metadata rather than hardcoding a smaller service list"
    )
    managed_pos = content.find("MANAGED_SVCS")
    stop_pos = content.find("systemctl stop kepler-node")
    assert managed_pos < stop_pos, (
        "upgrade.sh managed-service preflight must run before stopping services"
    )


def test_upgrade_sh_reads_target_ref_release_metadata_for_release_flag() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "git show" in content, (
        "upgrade.sh must use 'git show <ref>:release.json' to read release metadata "
        "from the target ref when --release is specified (spec line 1803 step 2: "
        "read target release metadata before preflight)"
    )
    git_show_pos = content.find("git show")
    stop_pos = content.find("systemctl stop kepler-node")
    assert git_show_pos < stop_pos, (
        "upgrade.sh must read target ref release metadata (git show) before stopping "
        "services (spec: read metadata → preflight → stop services)"
    )
    fetch_pos = content.find("git fetch")
    assert fetch_pos < stop_pos, (
        "upgrade.sh must fetch the target ref before reading its release metadata "
        "and before stopping services"
    )


def test_deploy_pi_sh_uses_live_systemd_working_directory() -> None:
    content = (_REPO_ROOT / "scripts" / "deploy_pi.sh").read_text()
    assert "systemctl show kepler-node --property=WorkingDirectory --value" in content, (
        "deploy_pi.sh must discover the live install path from the kepler-node systemd unit"
    )
    assert "sudo -n true" in content, (
        "deploy_pi.sh must fail fast when passwordless sudo is unavailable"
    )
    assert '"${INSTALL_ROOT}/upgrade.sh"' in content, (
        "deploy_pi.sh must run the installed upgrade.sh from the live repo path"
    )
    assert '"${INSTALL_ROOT}/scripts/pi_smoke.py"' in content, (
        "deploy_pi.sh must run the installed pi_smoke.py after upgrade"
    )


def test_deploy_pi_workflow_targets_self_hosted_pi_runner() -> None:
    content = (_REPO_ROOT / ".github" / "workflows" / "deploy-pi.yml").read_text()
    assert "workflow_dispatch" in content, "deploy-pi.yml must be manually triggerable"
    assert "self-hosted" in content and "kepler-pi" in content, (
        "deploy-pi.yml must target the self-hosted Pi runner labels"
    )
    assert "scripts/deploy_pi.sh" in content, (
        "deploy-pi.yml must invoke the Pi-local deploy helper"
    )