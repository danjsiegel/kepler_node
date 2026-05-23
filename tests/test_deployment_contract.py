"""Contract tests for bootstrap, upgrade, and release metadata scripts."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_PATCH_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _assert_patch_hunk_counts(content: str) -> None:
    lines = content.splitlines()
    saw_hunk = False
    idx = 0
    while idx < len(lines):
        match = _PATCH_HUNK_RE.match(lines[idx])
        if not match:
            idx += 1
            continue

        saw_hunk = True
        expected_old = int(match.group(2) or "1")
        expected_new = int(match.group(4) or "1")
        actual_old = 0
        actual_new = 0
        idx += 1

        while idx < len(lines):
            line = lines[idx]
            if line.startswith("@@ ") or line.startswith("diff --git "):
                break
            if line == r"\ No newline at end of file":
                idx += 1
                continue
            if not line.startswith("+"):
                actual_old += 1
            if not line.startswith("-"):
                actual_new += 1
            idx += 1

        assert actual_old == expected_old, (
            f"patch hunk old-count mismatch for {match.group(0)}: "
            f"expected {expected_old}, found {actual_old}"
        )
        assert actual_new == expected_new, (
            f"patch hunk new-count mismatch for {match.group(0)}: "
            f"expected {expected_new}, found {actual_new}"
        )

    assert saw_hunk, "expected at least one unified-diff hunk in the Fuji patch bundle"


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


def test_upgrade_sh_discards_unsupported_fuji_focus_bridge_changes_before_pull() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "discard_unsupported_local_git_changes()" in content, (
        "upgrade.sh must define a targeted cleanup helper for unsupported local git changes that block upgrades"
    )
    assert '"indi/fuji_focus_bridge"' in content, (
        "upgrade.sh must treat the old Fuji focus bridge tree as discardable upgrade debris now that it is not the supported deployment path"
    )
    cleanup_pos = content.find("discard_unsupported_local_git_changes")
    pull_pos = content.find("git pull")
    assert cleanup_pos != -1 and cleanup_pos < pull_pos, (
        "upgrade.sh must discard unsupported Fuji focus bridge changes before attempting git pull"
    )
    assert "Local repository has uncommitted changes outside unsupported Fuji focus bridge artifacts" in content, (
        "upgrade.sh must still fail clearly when unrelated local changes would be overwritten by upgrade"
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
    assert "INDI_PORT=7624" in content, (
        "upgrade.sh must retain the standard INDI port for broker-managed profiles"
    )
    assert "INDIWEBMANAGER_PORT=8624" in content, (
        "upgrade.sh must define the indiwebmanager API port for broker health checks"
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
    for package_name in ("build-essential", "cmake", "kstars", "xrdp", "tigervnc-standalone-server"):
        assert package_name in content, (
            f"bootstrap.sh must install {package_name} as part of the full supported package set"
        )
    assert (
        'if [[ "${PROFILE}" == "field-fallback" ]]'
        not in content[
            content.find("COMMON_PACKAGES=") : content.find('ok "System prerequisites installed"')
        ]
    ), "bootstrap.sh should not split the apt install footprint by profile"


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
    assert (
        'command -v uv >/dev/null 2>&1 || fail "uv installation succeeded but the uv binary is not on PATH"'
        in content
    ), "bootstrap.sh must fail clearly if uv still is not resolvable after installation"


def test_bootstrap_sh_installs_indiwebmanager_via_uv_tool() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "ensure_indiwebmanager_installed()" in content, (
        "bootstrap.sh must centralize indiwebmanager installation so the broker binary is provisioned deterministically"
    )
    assert "uv tool install --force --with legacy-cgi indiweb" in content, (
        "bootstrap.sh must install indiweb with legacy-cgi so the broker still runs on Python 3.13 systems where cgi was removed"
    )
    assert "/usr/local/bin/indi-web" in content, (
        "bootstrap.sh must stabilize the indi-web binary path for systemd"
    )
    assert "indi-web --help >/dev/null 2>&1" in content, (
        "bootstrap.sh must treat a present-but-broken indi-web binary as reinstallable instead of assuming it is healthy"
    )


def test_bootstrap_and_upgrade_do_not_build_fuji_focus_bridge_sidecar() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "build_and_install_fuji_focus_bridge" not in content, (
            f"{script_name} must not build the Fuji focus bridge as part of the supported starter-rig path now that focus is integrated into indi_kepler_fuji_ccd"
        )
        assert "Fuji Focus Bridge" not in content, (
            f"{script_name} must not require Fuji Focus Bridge in the managed indiwebmanager profile"
        )


def test_kepler_fuji_driver_bundle_exists() -> None:
    assert (_REPO_ROOT / "indi/kepler_fuji_ccd/kepler_fuji_ccd.xml").exists(), (
        "the repo must ship XML metadata for the custom Kepler Fuji DSLR driver so indiwebmanager can discover it"
    )
    assert (_REPO_ROOT / "indi/kepler_fuji_ccd/patches/0001-kepler-fuji-x-t5-hardening.patch").exists(), (
        "the repo must ship the upstream patchset for the custom Kepler Fuji DSLR build"
    )


def test_bootstrap_and_upgrade_build_kepler_fuji_camera_driver() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "KEPLER_FUJI_CAMERA_DRIVER_LABEL" in content, (
            f"{script_name} must make the custom Fuji camera driver label overridable"
        )
        assert "KEPLER_INDI_GPHOTO_UPSTREAM_REF" in content, (
            f"{script_name} must pin the upstream indi-gphoto revision used for the custom Fuji build"
        )
        assert "indi/kepler_fuji_ccd/patches/0001-kepler-fuji-x-t5-hardening.patch" in content, (
            f"{script_name} must apply the in-repo Kepler Fuji DSLR patchset to upstream indi-gphoto"
        )
        assert "git clone --depth 1 https://github.com/indilib/indi-3rdparty.git" in content, (
            f"{script_name} must build the custom Fuji driver from upstream indi-gphoto instead of checking in a forked code dump"
        )
        assert 'git -C "${repo_dir}" apply "${patch_file}"' in content, (
            f"{script_name} must apply the Kepler Fuji DSLR patchset before building the custom camera driver"
        )
        assert 'cmake -S "${repo_dir}/indi-gphoto" -B "${build_dir}"' in content, (
            f"{script_name} must configure the upstream indi-gphoto source tree before building the custom Fuji target"
        )
        assert "KEPLER_BUILD_CUSTOM_FUJI_ONLY=ON" in content, (
            f"{script_name} must build only the custom Fuji binary so the distro-provided stock gphoto drivers are not overwritten"
        )
        assert "/usr/share/indi/kepler_fuji_ccd.xml" in content, (
            f"{script_name} must install the Kepler Fuji DSLR XML metadata so indiwebmanager can discover the custom driver"
        )


def test_kepler_fuji_patch_bundle_includes_integrated_focus_support() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'find_widget(gphoto, "d171")' in content, (
        "the tracked Kepler Fuji DSLR patchset must teach the main driver to discover Fuji d171 as its focus widget"
    )
    assert 'Failed to set Fuji d171 focus delta %s: %s' in content, (
        "the tracked Kepler Fuji DSLR patchset must route manual focus through signed Fuji d171 writes in the active camera session"
    )
    assert 'int gphoto_read_focus_position(gphoto_driver *gphoto, int *value, char *errMsg);' in content, (
        "the tracked Kepler Fuji DSLR patchset must add a live Fuji focus-position read helper so achieved d171 readback becomes the driver truth"
    )
    assert 'IPState GPhotoCCD::MoveAbsFocuser(uint32_t targetTicks)' in content, (
        "the tracked Kepler Fuji DSLR patchset must expose an Ekos-readable absolute move path backed by Fuji d171"
    )
    assert 'static constexpr int32_t FUJI_D171_ABS_BIAS = 32768;' in content, (
        "the tracked Kepler Fuji DSLR patchset must map raw Fuji d171 positions into a non-negative virtual absolute step space"
    )


def test_kepler_fuji_patch_bundle_uses_settled_d171_readback_as_move_truth() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'const int FUJI_D171_SETTLE_POLL_US = 200000;' in content, (
        "the tracked Kepler Fuji DSLR patchset must poll d171 settling at a conservative 200 ms cadence"
    )
    assert 'const int FUJI_D171_SETTLE_MAX_POLLS = 25;' in content, (
        "the tracked Kepler Fuji DSLR patchset must allow up to five seconds for Fuji focus readback to settle"
    )
    assert 'const int FUJI_D171_SETTLE_TOLERANCE = 1;' in content, (
        "the tracked Kepler Fuji DSLR patchset must require a tight settle band before accepting d171 readback as final"
    )
    assert 'const int FUJI_D171_SUCCESS_TOLERANCE = 8;' in content, (
        "the tracked Kepler Fuji DSLR patchset must use an explicit success tolerance instead of pretending Fuji d171 echoes targets exactly"
    )
    assert 'Fuji d171 failed to settle after %d polls' in content, (
        "the tracked Kepler Fuji DSLR patchset must alert if Fuji d171 never settles after a write"
    )
    assert 'Fuji d171 settled at raw %d outside tolerance band from requested raw %d' in content, (
        "the tracked Kepler Fuji DSLR patchset must reject out-of-band settled readback instead of silently reporting target success"
    )
    assert 'FocusAbsPosN[0].value = fujiRawToAbsoluteTicks(settledMedian);' in content, (
        "the tracked Kepler Fuji DSLR patchset must publish the settled d171 median readback as the absolute focuser state"
    )
    assert 'FocusMaxPosN[0].value = FocusAbsPosN[0].max;' in content, (
        "the tracked Kepler Fuji DSLR patchset must publish a bounded absolute working window instead of a fake full-range max"
    )
    assert 'loadFujiFocusCalibration()' in content, (
        "the tracked Kepler Fuji DSLR patchset must load the projected Kepler focus calibration on connect before publishing bounds"
    )


def test_kepler_fuji_patch_bundle_clamps_ekos_absolute_moves_to_published_window() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'FocusRelPosN[0].max   = 4096;' in content, (
        "the tracked Kepler Fuji DSLR patchset must advertise a bounded relative move span instead of the full virtual axis"
    )
    assert 'if (targetTicks < publishedMinTicks || targetTicks > publishedMaxTicks)' in content, (
        "the tracked Kepler Fuji DSLR patchset must clamp Ekos absolute requests to the published Fuji working window"
    )
    assert 'Clamping Fuji absolute focus target from %u to %u within published range [%u, %u]' in content, (
        "the tracked Kepler Fuji DSLR patchset must log when an autofocus client asks for an absurd out-of-window move"
    )


def test_kepler_fuji_patch_bundle_reads_projected_runtime_calibration_file() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'KEPLER_FUJI_FOCUS_CALIBRATION_FILE' in content, (
        "the tracked Kepler Fuji DSLR patchset must allow the projected calibration file path to be overridden by environment"
    )
    assert 'return "data/runtime/fuji_focus_calibration.json";' in content, (
        "the tracked Kepler Fuji DSLR patchset must default to Kepler's runtime calibration projection path"
    )
    assert 'Loaded Kepler Fuji focus calibration from %s' in content, (
        "the tracked Kepler Fuji DSLR patchset must log when a projected focus calibration profile is loaded"
    )
    assert 'm_HaveFujiFocusCalibration && m_FujiFocusCalMaxRaw > m_FujiFocusCalMinRaw' in content, (
        "the tracked Kepler Fuji DSLR patchset must map between Fuji raw values and normalized absolute ticks only when calibration is valid"
    )


def test_kepler_fuji_patch_bundle_probes_focus_endpoints_on_connect_when_runtime_calibration_missing() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'bool GPhotoCCD::probeFujiFocusCalibration()' in content, (
        "the tracked Kepler Fuji DSLR patchset must define a broker-side Fuji endpoint probe when no projected calibration exists"
    )
    assert 'settleRawTarget(-10000, settledLow, errMsg)' in content, (
        "the tracked Kepler Fuji DSLR patchset must probe the empirically reachable Fuji low endpoint instead of a synthetic full-range minimum"
    )
    assert 'settleRawTarget(11000, settledHigh, errMsg)' in content, (
        "the tracked Kepler Fuji DSLR patchset must probe the empirically reachable Fuji high endpoint instead of a synthetic full-range maximum"
    )
    assert 'if (!m_HaveFujiFocusCalibration && !m_HaveFujiFocusState)' in content, (
        "the tracked Kepler Fuji DSLR patchset must only probe Fuji endpoints on the initial connect-time focus refresh when calibration is missing"
    )
    assert 'probeFujiFocusCalibration();' in content, (
        "the tracked Kepler Fuji DSLR patchset must trigger the Fuji endpoint probe during connect-time focus refresh when runtime calibration is absent"
    )
    assert 'Probed Fuji focus calibration range [%d, %d] from settled endpoints [%d, %d]' in content, (
        "the tracked Kepler Fuji DSLR patchset must log the probed Fuji working window discovered during broker-owned connect"
    )


def test_kepler_fuji_patch_bundle_has_consistent_hunk_counts() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    _assert_patch_hunk_counts(content)


def test_kepler_fuji_patch_bundle_does_not_advertise_sync_for_virtual_d171_axis() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'FI::SetCapability(FOCUSER_CAN_ABS_MOVE | FOCUSER_CAN_REL_MOVE);' in content, (
        "the tracked Kepler Fuji DSLR patchset must advertise only absolute and relative focus motion for the virtual d171 axis"
    )
    assert 'FOCUSER_CAN_SYNC' not in content, (
        "the tracked Kepler Fuji DSLR patchset must not advertise sync for the first-cut virtual Fuji d171 focuser contract"
    )
    assert 'virtual bool SyncFocuser(uint32_t ticks) override;' not in content, (
        "the tracked Kepler Fuji DSLR patchset must not declare SyncFocuser for the first-cut virtual Fuji d171 focuser contract"
    )
    assert 'bool GPhotoCCD::SyncFocuser(uint32_t ticks)' not in content, (
        "the tracked Kepler Fuji DSLR patchset must not implement SyncFocuser for the first-cut virtual Fuji d171 focuser contract"
    )


def test_kepler_fuji_patch_bundle_scales_event_timeout_with_requested_exposure() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'int exposure_event_timeout {10};' in content, (
        "the tracked Kepler Fuji DSLR patchset must persist a per-exposure event timeout budget on the driver state"
    )
    assert 'uint32_t exposure_wait_seconds = ((exptime_usec + 999999) / 1000000) + 30;' in content, (
        "the tracked Kepler Fuji DSLR patchset must derive Fuji event wait time from the requested exposure instead of a fixed retry ceiling"
    )
    assert 'if (exposure_wait_seconds < 90)' in content, (
        "the tracked Kepler Fuji DSLR patchset must keep a larger minimum wait budget for Fuji capture/download completion"
    )
    assert 'const int maxTimeoutCounter = gphoto->exposure_event_timeout > 0 ? gphoto->exposure_event_timeout : 10;' in content, (
        "the tracked Kepler Fuji DSLR patchset must consume the per-exposure timeout budget in the Fuji event loop"
    )


def test_kepler_fuji_patch_bundle_recovers_aborted_captures_gracefully() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'gphoto->is_aborted = false;' in content, (
        "the tracked Kepler Fuji DSLR patchset must clear stale abort state before starting the next exposure"
    )
    assert 'gphoto->is_aborted = true;' in content, (
        "the tracked Kepler Fuji DSLR patchset must mark an in-flight capture as aborted before draining late Fuji events"
    )
    assert 'if (gphoto->handle_sdcard_image != IGNORE_IMAGE || gphoto->is_aborted)' in content, (
        "the tracked Kepler Fuji DSLR patchset must still drain and clean up a late FILE_ADDED event after abort"
    )
    assert 'return result;' in content and 'int result = gphoto_read_exposure(gphoto);' in content, (
        "the tracked Kepler Fuji DSLR patchset must propagate the abort-drain result instead of blindly reporting success"
    )
    assert 'pthread_mutex_lock(&gphoto->mutex);' in content and 'pthread_mutex_unlock(&gphoto->mutex);' in content, (
        "the tracked Kepler Fuji DSLR patchset must clear the abort flag under the same mutex discipline used by the exposure path"
    )


def test_kepler_fuji_patch_bundle_slices_download_retries_for_abort_responsiveness() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'const int FUJI_SLICES_PER_RETRY = 20;' in content, (
        "the tracked Kepler Fuji DSLR patchset must break each Fuji download retry wait into fixed slices"
    )
    assert 'for (int slice = 0; slice < FUJI_SLICES_PER_RETRY; slice++)' in content, (
        "the tracked Kepler Fuji DSLR patchset must poll for abort between Fuji download retry sleep slices"
    )
    assert 'Fuji: image download retry aborted by user request after retry %d.' in content, (
        "the tracked Kepler Fuji DSLR patchset must log when a Fuji download retry loop exits for an abort"
    )


def test_kepler_fuji_patch_bundle_drops_pending_files_on_reconnect() -> None:
    content = (_REPO_ROOT / "indi" / "kepler_fuji_ccd" / "patches" / "0001-kepler-fuji-x-t5-hardening.patch").read_text()
    assert 'static void discard_fuji_pending_file(gphoto_driver *gphoto, const CameraFilePath *path, const char *reason)' in content, (
        "the tracked Kepler Fuji DSLR patchset must define a reconnect-safe helper that deletes a pending late Fuji file"
    )
    assert 'discard_fuji_pending_file(gphoto, &gphoto->camerapath, reason);' in content, (
        "the tracked Kepler Fuji DSLR patchset must delete the remembered pending Fuji file after session recovery"
    )
    assert 'drain_fuji_pending_events(gphoto, reason);' in content, (
        "the tracked Kepler Fuji DSLR patchset must drain late Fuji FILE_ADDED events after session recovery"
    )
    assert 'drain_fuji_pending_events(gphoto, "connect");' in content, (
        "the tracked Kepler Fuji DSLR patchset must also drain pending late Fuji files during reconnect"
    )


def test_bootstrap_and_upgrade_wipe_and_recreate_starter_rig_indi_profile() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "KEPLER_INDI_PROFILE_NAME" in content, (
            f"{script_name} must allow the managed indiwebmanager profile name to be overridden"
        )
        assert "KEPLER_INDI_PROFILE_DRIVERS" in content, (
            f"{script_name} must allow the managed indiwebmanager driver set to be overridden"
        )
        assert 'ES iEXOS100 PMC-Eight,${FUJI_CAMERA_DRIVER_LABEL}' in content, (
            f"{script_name} must default the managed profile to the Kepler Fuji DSLR driver without a separate focus bridge"
        )
        assert "/api/drivers" in content, (
            f"{script_name} must validate requested driver labels against indiwebmanager's documented driver catalog instead of bypassing the supported control surface"
        )
        assert "/api/server/stop" in content, (
            f"{script_name} must stop the active indiwebmanager server before wiping the managed profile so stale runtime state cannot survive the upgrade"
        )
        assert "-X DELETE \"http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/profiles/${encoded_name}\"" in content, (
            f"{script_name} must delete the existing indiwebmanager profile before recreating it"
        )
        assert "/api/profiles/${encoded_name}" in content, (
            f"{script_name} must recreate the indiwebmanager equipment profile through its REST API"
        )
        assert '"autostart": 1, "autoconnect": 1' in content, (
            f"{script_name} must configure the managed profile for autostart and autoconnect"
        )
        assert "systemctl restart indiwebmanager" in content, (
            f"{script_name} must restart indiwebmanager after wiping the profile so broker cache is cleared"
        )
        assert "/api/server/start/${encoded_name}" in content, (
            f"{script_name} must explicitly start the recreated profile after broker restart so upgrades end with a deterministic active driver set"
        )


def test_bootstrap_and_upgrade_write_indiwebmanager_with_real_home() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert 'INDIWEBMANAGER_HOME="${KEPLER_INDIWEBMANAGER_HOME:-/var/lib/indiwebmanager}"' in content, (
            f"{script_name} must give indiwebmanager a stable home directory so INDI drivers can persist configuration"
        )
        assert "Environment=HOME=${indiweb_home}" in content, (
            f"{script_name} must set HOME in indiwebmanager.service so drivers do not try to save config under (null)/.indi"
        )
        assert "WorkingDirectory=${indiweb_home}" in content, (
            f"{script_name} must run indiwebmanager from its state directory so relative config paths are stable"
        )
        assert "ExecStartPre=/usr/bin/install -d -m 0755 ${indiweb_home}/.indi" in content, (
            f"{script_name} must pre-create the .indi config directory before starting indiwebmanager"
        )


def test_bootstrap_and_upgrade_manage_indiwebmanager_service() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "indiwebmanager.service" in content, (
            f"{script_name} must provision indiwebmanager.service for the supported brokered INDI path"
        )
        assert "ExecStart=${indiweb_bin}" in content, (
            f"{script_name} must render the indiwebmanager unit using the resolved indi-web binary path"
        )
        assert "systemctl disable indiserver" in content, (
            f"{script_name} must disable any legacy indiserver service so it cannot conflict with the broker on port 7624"
        )


def test_bootstrap_and_upgrade_disable_gvfs_camera_auto_claimer() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "gvfs-gphoto2-volume-monitor.service" in content, (
            f"{script_name} must disable the GVFS gphoto monitor so desktop sessions do not steal USB cameras"
        )
        assert "gvfs-udisks2-volume-monitor.service" in content, (
            f"{script_name} must disable the GVFS UDisks volume monitor so desktop sessions do not auto-mount Fuji storage"
        )
        assert "gvfs-mtp-volume-monitor.service" in content, (
            f"{script_name} must disable the GVFS MTP volume monitor so desktop sessions do not claim the camera as removable media"
        )
        assert "systemctl --global mask" in content, (
            f"{script_name} must globally mask desktop camera claimers for future desktop sessions"
        )
        assert "systemctl --user mask --now" in content, (
            f"{script_name} must stop and mask desktop camera claimers in active user sessions"
        )
        assert "gsettings set org.gnome.desktop.media-handling automount false" in content, (
            f"{script_name} must disable GNOME media automount in active desktop sessions"
        )
        assert "pkill -x gvfsd-gphoto2" in content, (
            f"{script_name} must kill an already-running gvfsd-gphoto2 process so the camera is immediately releasable"
        )
        assert "pkill -f '/usr/libexec/gvfs-udisks2-volume-monitor'" in content, (
            f"{script_name} must kill the GVFS UDisks monitor so an already auto-mounted Fuji body is releasable immediately"
        )


def test_bootstrap_and_upgrade_mark_fuji_usb_devices_to_skip_udisks() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "99-kepler-camera.rules" in content, (
            f"{script_name} must manage the Fuji camera udev rule"
        )
        assert 'ENV{UDISKS_IGNORE}="1"' in content, (
            f"{script_name} must mark Fujifilm USB devices so UDisks ignores them"
        )
        assert 'ENV{UDISKS_AUTO}="0"' in content, (
            f"{script_name} must disable UDisks automount for Fujifilm USB devices"
        )


def test_bootstrap_and_upgrade_keepalive_yields_to_indi_camera_driver() -> None:
    for script_name in ("bootstrap.sh", "upgrade.sh"):
        content = (_REPO_ROOT / script_name).read_text()
        assert "pgrep -f 'indi_(fuji|gphoto)_ccd'" in content, (
            f"{script_name} must make the Fuji keepalive helper yield when an INDI camera driver is active so gphoto2 does not hold the USB interface busy during capture"
        )
        assert "indi camera driver active, keepalive exiting" in content, (
            f"{script_name} must log when the keepalive loop exits to avoid colliding with INDI capture"
        )


def test_upgrade_sh_refreshes_managed_service_units() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "Step 3b: Refreshing managed service units" in content, (
        "upgrade.sh should refresh managed service units before restart so legacy broken units are repaired"
    )
    assert 'write_indiwebmanager_service "/etc/systemd/system/indiwebmanager.service" "$(command -v indi-web)"' in content, (
        "upgrade.sh must rewrite the canonical indiwebmanager.service during upgrades"
    )
    assert "systemctl enable indiwebmanager" in content, (
        "upgrade.sh must enable indiwebmanager during upgrades so the broker survives reboot on existing installs"
    )
    assert "Step 3c: Preventing desktop camera auto-claimers" in content, (
        "upgrade.sh should disable GVFS camera auto-claimers before restarting services"
    )


def test_bootstrap_sh_field_fallback_includes_indiwebmanager_in_service_ordering() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "indiwebmanager.service" in content
    service_wants_match = re.search(r'SERVICE_WANTS="[^"]*indiwebmanager\.service[^"]*"', content)
    assert service_wants_match is not None, (
        "bootstrap.sh SERVICE_WANTS must include indiwebmanager.service for all profiles"
    )
    pre_block = content[: service_wants_match.start()]
    last_if = pre_block.rfind('if [[ "${PROFILE}"')
    last_elif = pre_block.rfind('elif [[ "${PROFILE}"')
    gating_pos = max(last_if, last_elif)
    if gating_pos != -1:
        gating_line = content[gating_pos : gating_pos + 60]
        assert "headless-node" not in gating_line, (
            "bootstrap.sh SERVICE_WANTS indiwebmanager.service must apply to all profiles, "
            "not be gated behind headless-node"
        )


def test_bootstrap_sh_health_check_verifies_indiwebmanager_service_active() -> None:
    content = (_REPO_ROOT / "bootstrap.sh").read_text()
    assert "systemctl is-active --quiet indiwebmanager" in content, (
        "bootstrap.sh health checks must verify indiwebmanager service is active, "
        "not only that the binary exists"
    )
    assert "/api/server/status" in content, (
        "bootstrap.sh health checks must verify the indiwebmanager HTTP API is reachable"
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


def test_upgrade_sh_stops_legacy_indiserver_and_restarts_indiwebmanager() -> None:
    content = (_REPO_ROOT / "upgrade.sh").read_text()
    assert "systemctl stop indiserver" in content, (
        "upgrade.sh must stop any legacy indiserver service during shutdown to avoid broker conflicts"
    )
    assert "systemctl stop indiwebmanager" in content, (
        "upgrade.sh must stop indiwebmanager as part of managed-service shutdown"
    )
    assert "systemctl start indiwebmanager" in content, (
        "upgrade.sh must start indiwebmanager as part of managed-service restart"
    )
    stop_indi_pos = content.find("systemctl stop indiserver")
    stop_broker_pos = content.find("systemctl stop indiwebmanager")
    start_broker_pos = content.find("systemctl start indiwebmanager")
    start_kepler_pos = content.find("systemctl start kepler-node")
    stop_kepler_pos = content.find("systemctl stop kepler-node")
    assert stop_indi_pos > stop_kepler_pos, (
        "upgrade.sh must stop kepler-node before any legacy indiserver service (dependency order)"
    )
    assert stop_broker_pos > stop_kepler_pos, (
        "upgrade.sh must stop kepler-node before indiwebmanager (dependency order)"
    )
    assert start_broker_pos < start_kepler_pos, (
        "upgrade.sh must start indiwebmanager before kepler-node (dependency order)"
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
    assert "scripts/deploy_pi.sh" in content, "deploy-pi.yml must invoke the Pi-local deploy helper"


# ---------------------------------------------------------------------------
# Fuji focus bridge sidecar contract checks
# ---------------------------------------------------------------------------


def test_fuji_focus_bridge_verify_script_exists() -> None:
    script = _REPO_ROOT / "scripts" / "fuji_focus_bridge_verify.sh"
    assert script.exists(), (
        "scripts/fuji_focus_bridge_verify.sh must exist as the reusable Pi-side "
        "verification script for the Fuji focus bridge"
    )
    content = script.read_text()
    assert "#!/usr/bin/env bash" in content, "fuji_focus_bridge_verify.sh must have a bash shebang"


def test_fuji_focus_bridge_verify_script_defaults_to_read_only() -> None:
    content = (_REPO_ROOT / "scripts" / "fuji_focus_bridge_verify.sh").read_text()
    assert "ALLOW_MOVE=false" in content, (
        "fuji_focus_bridge_verify.sh must default ALLOW_MOVE to false so the script "
        "is safe to run without flags"
    )
    assert "--allow-move" in content, (
        "fuji_focus_bridge_verify.sh must expose --allow-move flag for opt-in motion"
    )


def test_fuji_focus_bridge_verify_script_has_indi_probe_flag() -> None:
    content = (_REPO_ROOT / "scripts" / "fuji_focus_bridge_verify.sh").read_text()
    assert "PROBE_INDI=false" in content, (
        "fuji_focus_bridge_verify.sh must default PROBE_INDI to false"
    )
    assert "--probe-indi" in content, (
        "fuji_focus_bridge_verify.sh must expose --probe-indi flag so stock INDI "
        "focuser support can be disproved before the custom driver is expanded"
    )


def test_fuji_focus_bridge_verify_script_probes_known_surfaces() -> None:
    content = (_REPO_ROOT / "scripts" / "fuji_focus_bridge_verify.sh").read_text()
    for node in ("d171", "d262", "d209"):
        assert node in content, (
            f"fuji_focus_bridge_verify.sh must probe /main/other/{node} "
            "(script must read all focus-relevant Fuji nodes)"
        )


def test_fuji_focus_bridge_verify_script_ends_with_verdict() -> None:
    content = (_REPO_ROOT / "scripts" / "fuji_focus_bridge_verify.sh").read_text()
    assert "PASS" in content and "FAIL" in content and "INCONCLUSIVE" in content, (
        "fuji_focus_bridge_verify.sh must end with a human-readable PASS, FAIL, or "
        "INCONCLUSIVE verdict rather than raw command output only"
    )


def test_fuji_focus_bridge_indi_driver_files_exist() -> None:
    bridge_dir = _REPO_ROOT / "indi" / "fuji_focus_bridge"
    assert bridge_dir.exists(), (
        "indi/fuji_focus_bridge/ must exist as the standalone INDI focuser sidecar "
        "for the supported focus-bridge surface"
    )
    required_files = (
        "fuji_focus_bridge.cpp",
        "fuji_focus_bridge.h",
        "fuji_focus_bridge.xml",
        "CMakeLists.txt",
    )
    for fname in required_files:
        assert (bridge_dir / fname).exists(), (
            f"indi/fuji_focus_bridge/{fname} must exist for the sidecar to be buildable "
            "and discoverable by indiserver"
        )


def test_fuji_focus_bridge_xml_declares_focuser_device() -> None:
    xml_path = _REPO_ROOT / "indi" / "fuji_focus_bridge" / "fuji_focus_bridge.xml"
    content = xml_path.read_text()
    assert "Focusers" in content, (
        "fuji_focus_bridge.xml must declare devGroup group='Focusers' so Ekos "
        "discovers the sidecar as a focuser device"
    )
    assert "indi_fuji_focus_bridge" in content, (
        "fuji_focus_bridge.xml must reference the indi_fuji_focus_bridge driver binary"
    )


def test_fuji_focus_bridge_cpp_exposes_only_relative_move_contract() -> None:
    cpp_path = _REPO_ROOT / "indi" / "fuji_focus_bridge" / "fuji_focus_bridge.cpp"
    content = cpp_path.read_text()
    assert "FOCUSER_CAN_REL_MOVE" in content, (
        "fuji_focus_bridge.cpp must advertise FOCUSER_CAN_REL_MOVE capability "
        "for minimum viable relative focuser semantics"
    )
    assert "FOCUSER_CAN_ABS_MOVE" not in content, (
        "fuji_focus_bridge.cpp must NOT advertise FOCUSER_CAN_ABS_MOVE — the bridge "
        "does not provide absolute position guarantees"
    )


def test_fuji_focus_bridge_uses_d171_as_focus_primitive() -> None:
    cpp_path = _REPO_ROOT / "indi" / "fuji_focus_bridge" / "fuji_focus_bridge.cpp"
    content = cpp_path.read_text()
    assert "d171" in content, (
        "fuji_focus_bridge.cpp must use /main/other/d171 as the focus move primitive — "
        "the only proven writable focus surface on the XF55-200 + X-T5 posture"
    )


def test_fuji_focus_bridge_cmake_falls_back_without_indiconfig() -> None:
    content = (_REPO_ROOT / "indi" / "fuji_focus_bridge" / "CMakeLists.txt").read_text()
    assert "find_package(INDI QUIET)" in content, (
        "indi/fuji_focus_bridge/CMakeLists.txt must not require INDIConfig.cmake unconditionally; "
        "Debian/Raspberry Pi libindi-dev may not ship it"
    )
    assert (
        "find_path(FUJI_FOCUS_BRIDGE_INDI_INCLUDE_PARENT_DIR NAMES libindi/indifocuser.h)"
        in content
    ), (
        "indi/fuji_focus_bridge/CMakeLists.txt must fall back to direct INDI header lookup when "
        "INDIConfig.cmake is unavailable"
    )
    assert (
        "find_library(FUJI_FOCUS_BRIDGE_INDI_DRIVER_LIBRARY NAMES indidriver libindidriver)"
        in content
    ), (
        "indi/fuji_focus_bridge/CMakeLists.txt must fall back to direct INDI driver library lookup "
        "when INDIConfig.cmake is unavailable"
    )
    assert (
        "FUJI_FOCUS_BRIDGE_INDI_INCLUDE_PARENT_DIR}/libindi" in content
        or "FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIR})" in content
    ), (
        "indi/fuji_focus_bridge/CMakeLists.txt must add the libindi header directory itself to the "
        "include path so internal quoted headers like indidevapi.h resolve on Debian/Raspberry Pi"
    )


def test_fuji_focus_bridge_abort_uses_process_kill() -> None:
    cpp_path = _REPO_ROOT / "indi" / "fuji_focus_bridge" / "fuji_focus_bridge.cpp"
    content = cpp_path.read_text()
    assert "SIGTERM" in content, (
        "fuji_focus_bridge.cpp must send SIGTERM to the in-flight gphoto2 child process "
        "in AbortFocuser() — flag-only abort is not best-effort"
    )
    assert "kill(" in content, (
        "fuji_focus_bridge.cpp must call kill() to terminate the child PID; "
        "setting m_abort alone does not abort a blocking gphoto2 subprocess"
    )
    assert "m_movePid" in content, (
        "fuji_focus_bridge.cpp must track the child PID in m_movePid so AbortFocuser "
        "knows which process to terminate"
    )


def test_fuji_focus_bridge_reports_ips_busy_during_move() -> None:
    cpp_path = _REPO_ROOT / "indi" / "fuji_focus_bridge" / "fuji_focus_bridge.cpp"
    content = cpp_path.read_text()
    assert "IPS_BUSY" in content, (
        "fuji_focus_bridge.cpp must publish IPS_BUSY on FocusRelPosNP before the "
        "gphoto2 child process completes so Ekos sees the in-progress state immediately "
        "rather than waiting on a blocking INDI call"
    )
    assert "m_moveThread" in content, (
        "fuji_focus_bridge.cpp must use a background thread (m_moveThread) so "
        "MoveRelFocuser returns IPS_BUSY without blocking the INDI event loop"
    )


def test_fuji_focus_bridge_verify_script_parses_indi_getprop_correctly() -> None:
    """indi_getprop output is 'Device.Property.Element=value' (no leading dot).

    The script must extract device names by splitting on '=' first, then on '.'
    to get the first field — the same convention used by mount/indi.py.  The old
    broken pattern (grep '^[.]') would always produce an empty ALL_DEVICES list,
    causing the script to FAIL on a live INDI bus and making stock-support
    disproof impossible.
    """
    content = (_REPO_ROOT / "scripts" / "fuji_focus_bridge_verify.sh").read_text()
    # Must NOT use the broken leading-dot grep
    assert "grep '^\\.'" not in content and 'grep "^\\."' not in content, (
        "fuji_focus_bridge_verify.sh must NOT parse indi_getprop output with "
        "grep '^\\.' — indi_getprop output has no leading dot; the pattern "
        "always matches nothing, leaving ALL_DEVICES empty on a live bus"
    )
    # Must use the correct Device.Property.Element=value parse path
    assert "cut -d= -f1" in content, (
        "fuji_focus_bridge_verify.sh must extract the device name from "
        "indi_getprop output by cutting on '=' first (format is "
        "Device.Property.Element=value)"
    )
    assert "awk -F'.' '{print $1}'" in content, (
        "fuji_focus_bridge_verify.sh must extract the device name as the first "
        "dot-delimited field after stripping the '=value' suffix — consistent "
        "with how mount/indi.py parses the INDI bus"
    )


def test_fuji_focus_bridge_verify_script_uses_exact_focuser_property_matcher() -> None:
    """The stock-driver probe must only match real focuser property names.

    A broad substring such as FOC_ incorrectly matches unrelated properties like
    ACTIVE_FOCUSER, producing false warnings on a stock CCD driver that has no
    focuser interface. The probe should only match exact property names bounded
    by dots in indi_getprop output.
    """
    content = (_REPO_ROOT / "scripts" / "fuji_focus_bridge_verify.sh").read_text()
    assert "FOC_\\|" not in content and "|FOC_" not in content, (
        "fuji_focus_bridge_verify.sh must not use a broad FOC_ matcher because "
        "it falsely matches non-focuser properties like ACTIVE_FOCUSER"
    )
    assert (
        "grep -Ei '\\.(FOCUS_MOTION|FOCUS_STEPS|FOCUS_ABORT|FOCUS_STATUS|FOCUS_SPEED|REL_FOCUS|ABS_FOCUS)\\.'"
        in content
    ), (
        "fuji_focus_bridge_verify.sh must match only exact INDI focuser property "
        "names bounded by dots in Device.Property.Element output"
    )


def test_fuji_focus_bridge_verify_script_checks_gp2_set_exit_code() -> None:
    """gp2_set exit code must be captured and checked alongside text patterns.

    A non-zero gphoto2 --set-config exit code must be treated as failure even
    when stderr does not match the 'error|unsupported|failed' grep pattern.
    Relying only on text patterns produces false-pass results when gphoto2 exits
    non-zero silently or with an unrecognised error message.
    """
    content = (_REPO_ROOT / "scripts" / "fuji_focus_bridge_verify.sh").read_text()
    assert "INWARD_RC=$?" in content, (
        "fuji_focus_bridge_verify.sh must capture the gp2_set exit code for the "
        "inward move into INWARD_RC immediately after the command substitution"
    )
    assert "OUTWARD_RC=$?" in content, (
        "fuji_focus_bridge_verify.sh must capture the gp2_set exit code for the "
        "outward move into OUTWARD_RC immediately after the command substitution"
    )
    assert '${INWARD_RC}" -ne 0' in content, (
        "fuji_focus_bridge_verify.sh must check INWARD_RC for non-zero before "
        "deciding whether the inward move succeeded"
    )
    assert '${OUTWARD_RC}" -ne 0' in content, (
        "fuji_focus_bridge_verify.sh must check OUTWARD_RC for non-zero before "
        "deciding whether the outward move succeeded"
    )
