#!/usr/bin/env bash
# upgrade.sh — Kepler Node v1 upgrade script
#
# Upgrades kepler-node to the specified release or latest from main.
#
# Usage:
#   ./upgrade.sh
#   ./upgrade.sh --release v1.2.0
#   ./upgrade.sh --skip-restart
#   ./upgrade.sh --help

set -euo pipefail

# ------------------------------------------------------------------ #
# Defaults                                                             #
# ------------------------------------------------------------------ #

TARGET_RELEASE=""
DATA_DIR="${KEPLER_DATA_DIR:-/var/lib/kepler}"
SKIP_RESTART=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEPLER_PORT=8000
UI_PORT=8501
INDI_PORT=7624
INDIWEBMANAGER_PORT=8624
INDI_PROFILE_NAME="${KEPLER_INDI_PROFILE_NAME:-Kepler-Starter-Rig}"
FUJI_CAMERA_DRIVER_LABEL="${KEPLER_FUJI_CAMERA_DRIVER_LABEL:-Kepler Fuji DSLR}"
INDI_GPHOTO_UPSTREAM_REF="${KEPLER_INDI_GPHOTO_UPSTREAM_REF:-f5fdc3a63014a8da84a70230c25bb5bc565e0dfd}"
INDI_PROFILE_DRIVERS="${KEPLER_INDI_PROFILE_DRIVERS:-ES iEXOS100 PMC-Eight,${FUJI_CAMERA_DRIVER_LABEL},Fuji Focus Bridge}"
INDIWEBMANAGER_HOME="${KEPLER_INDIWEBMANAGER_HOME:-/var/lib/indiwebmanager}"

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

log()  { echo "  [upgrade] $*"; }
ok()   { echo "  ✅ $*"; }
fail() { echo "  ❌ $*" >&2; exit 1; }
warn() { echo "  ⚠️  $*"; }

ensure_indiwebmanager_installed() {
    if command -v indi-web >/dev/null 2>&1 && indi-web --help >/dev/null 2>&1; then
        return 0
    fi

    uv tool install --force --with legacy-cgi indiweb || fail "indiweb installation failed"

    local indiweb_candidates=(
        "/usr/local/bin/indi-web"
        "/root/.local/bin/indi-web"
        "${HOME}/.local/bin/indi-web"
    )
    local candidate=""

    for candidate in "${indiweb_candidates[@]}"; do
        if [[ -x "${candidate}" ]]; then
            if [[ "${candidate}" != "/usr/local/bin/indi-web" ]]; then
                ln -sf "${candidate}" /usr/local/bin/indi-web
            fi
            export PATH="/usr/local/bin:${candidate%/indi-web}:${PATH}"
            break
        fi
    done

    command -v indi-web >/dev/null 2>&1 || fail "indiweb installation succeeded but the indi-web binary is not on PATH"
    indi-web --help >/dev/null 2>&1 || fail "indiweb installation succeeded but indi-web is not runnable"
}

write_indiwebmanager_service() {
    local service_path="$1"
    local indiweb_bin="$2"
    local indiweb_home="$3"
    cat > "${service_path}" <<INDIWEB
[Unit]
Description=INDI Web Manager
After=network.target

[Service]
Type=simple
Environment=HOME=${indiweb_home}
WorkingDirectory=${indiweb_home}
StateDirectory=indiwebmanager
ExecStartPre=/usr/bin/install -d -m 0755 ${indiweb_home}/.indi
ExecStart=${indiweb_bin}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
INDIWEB
}

build_and_install_fuji_focus_bridge() {
    local bridge_src_dir="${SCRIPT_DIR}/indi/fuji_focus_bridge"
    local bridge_build_dir="${bridge_src_dir}/build"

    [[ -d "${bridge_src_dir}" ]] || fail "Fuji focus bridge source directory is missing at ${bridge_src_dir}"

    cmake -S "${bridge_src_dir}" -B "${bridge_build_dir}" \
        || fail "Fuji focus bridge CMake configure failed"
    cmake --build "${bridge_build_dir}" -j"$(nproc)" \
        || fail "Fuji focus bridge build failed"
    cmake --install "${bridge_build_dir}" \
        || fail "Fuji focus bridge install failed"
}

build_and_install_kepler_fuji_ccd() {
    local work_root="/tmp/kepler-indi-gphoto-${INDI_GPHOTO_UPSTREAM_REF:0:12}"
    local repo_dir="${work_root}/src"
    local build_dir="${work_root}/build"
    local patch_file="${SCRIPT_DIR}/indi/kepler_fuji_ccd/patches/0001-kepler-fuji-x-t5-hardening.patch"
    local xml_file="${SCRIPT_DIR}/indi/kepler_fuji_ccd/kepler_fuji_ccd.xml"

    [[ -f "${patch_file}" ]] || fail "Kepler Fuji DSLR patch file is missing at ${patch_file}"
    [[ -f "${xml_file}" ]] || fail "Kepler Fuji DSLR XML metadata is missing at ${xml_file}"

    rm -rf "${work_root}"

    git clone --depth 1 https://github.com/indilib/indi-3rdparty.git "${repo_dir}" \
        || fail "Failed to clone indi-3rdparty upstream source"
    git -C "${repo_dir}" fetch --depth 1 origin "${INDI_GPHOTO_UPSTREAM_REF}" \
        || fail "Failed to fetch indi-3rdparty revision ${INDI_GPHOTO_UPSTREAM_REF}"
    git -C "${repo_dir}" checkout --detach "${INDI_GPHOTO_UPSTREAM_REF}" \
        || fail "Failed to checkout indi-3rdparty revision ${INDI_GPHOTO_UPSTREAM_REF}"
    git -C "${repo_dir}" apply "${patch_file}" \
        || fail "Failed to apply the Kepler Fuji DSLR patchset"

    cmake -S "${repo_dir}/indi-gphoto" -B "${build_dir}" \
        -DCMAKE_INSTALL_PREFIX=/usr \
        -DKEPLER_BUILD_CUSTOM_FUJI_ONLY=ON \
        || fail "Kepler Fuji DSLR CMake configure failed"
    cmake --build "${build_dir}" -j"$(nproc)" \
        || fail "Kepler Fuji DSLR build failed"
    cmake --install "${build_dir}" \
        || fail "Kepler Fuji DSLR install failed"
    install -D -m 0644 "${xml_file}" /usr/share/indi/kepler_fuji_ccd.xml \
        || fail "Kepler Fuji DSLR XML install failed"
}

wait_for_indiwebmanager_api() {
    local attempts=0
    local max_attempts=12

    while ! curl -sf "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/server/status" &>/dev/null; do
        attempts=$((attempts + 1))
        if [[ ${attempts} -ge ${max_attempts} ]]; then
            return 1
        fi
        sleep 2
    done

    return 0
}

configure_indiwebmanager_profile() {
    local encoded_name
    local profile_payload
    local drivers_payload
    local available_drivers_json
    local installed_driver_labels
    local missing_drivers

    encoded_name="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "${INDI_PROFILE_NAME}")"
    profile_payload="$(python3 -c 'import json, sys; print(json.dumps({"port": int(sys.argv[1]), "autostart": 1, "autoconnect": 1}))' "${INDI_PORT}")"
    drivers_payload="$(python3 -c 'import json, sys; print(json.dumps([{"label": driver.strip()} for driver in sys.argv[1].split(",") if driver.strip()]))' "${INDI_PROFILE_DRIVERS}")"

    available_drivers_json="$(curl -sf "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/drivers")" \
        || fail "Could not read indiwebmanager driver catalog"

    installed_driver_labels="$(printf '%s' "${available_drivers_json}" | python3 -c 'import json, sys; print("\n".join(sorted({entry.get("label") for entry in json.load(sys.stdin) if entry.get("label")})))')"

    missing_drivers="$(printf '%s\n' "${installed_driver_labels}" | python3 -c 'import sys; wanted=[item.strip() for item in sys.argv[1].split(",") if item.strip()]; available={line.strip() for line in sys.stdin if line.strip()}; missing=[item for item in wanted if item not in available]; print("\n".join(missing)); raise SystemExit(1 if missing else 0)' "${INDI_PROFILE_DRIVERS}" 2>/dev/null || true)"

    if [[ -n "${missing_drivers}" ]]; then
        fail "indiwebmanager is missing required starter-rig drivers: ${missing_drivers//$'\n'/, }"
    fi

    curl -sf -X POST "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/server/stop" >/dev/null 2>&1 || true
    curl -sf -X DELETE "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/profiles/${encoded_name}" >/dev/null 2>&1 || true
    curl -sf -X POST "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/profiles/${encoded_name}" >/dev/null \
        || fail "Could not create indiwebmanager profile ${INDI_PROFILE_NAME}"
    curl -sf -H 'Content-Type: application/json' -X PUT -d "${profile_payload}" \
        "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/profiles/${encoded_name}" >/dev/null \
        || fail "Could not configure indiwebmanager profile ${INDI_PROFILE_NAME}"
    curl -sf -H 'Content-Type: application/json' -X POST -d "${drivers_payload}" \
        "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/profiles/${encoded_name}/drivers" >/dev/null \
        || fail "Could not assign drivers to indiwebmanager profile ${INDI_PROFILE_NAME}"
}

start_indiwebmanager_profile() {
    local encoded_name

    encoded_name="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "${INDI_PROFILE_NAME}")"

    curl -sf -X POST "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/server/start/${encoded_name}" >/dev/null \
        || fail "Could not start indiwebmanager profile ${INDI_PROFILE_NAME}"
}

disable_desktop_camera_claimers() {
    local user_units=(
        "gvfs-gphoto2-volume-monitor.service"
        "gvfs-udisks2-volume-monitor.service"
        "gvfs-mtp-volume-monitor.service"
    )
    local runtime_dir uid user_name user_unit

    if command -v systemctl >/dev/null 2>&1; then
        for user_unit in "${user_units[@]}"; do
            systemctl --global mask "${user_unit}" >/dev/null 2>&1 \
                || warn "Could not globally mask ${user_unit}; desktop sessions may still claim USB cameras"
        done
    fi

    if command -v runuser >/dev/null 2>&1; then
        for runtime_dir in /run/user/*; do
            [[ -d "${runtime_dir}" ]] || continue
            uid="$(basename "${runtime_dir}")"
            user_name="$(id -nu "${uid}" 2>/dev/null || true)"
            [[ -n "${user_name}" && -S "${runtime_dir}/bus" ]] || continue
            for user_unit in "${user_units[@]}"; do
                runuser -u "${user_name}" -- env \
                    XDG_RUNTIME_DIR="${runtime_dir}" \
                    DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime_dir}/bus" \
                    systemctl --user mask --now "${user_unit}" >/dev/null 2>&1 || true
            done
            runuser -u "${user_name}" -- env \
                XDG_RUNTIME_DIR="${runtime_dir}" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime_dir}/bus" \
                gsettings set org.gnome.desktop.media-handling automount false >/dev/null 2>&1 || true
            runuser -u "${user_name}" -- env \
                XDG_RUNTIME_DIR="${runtime_dir}" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime_dir}/bus" \
                gsettings set org.gnome.desktop.media-handling automount-open false >/dev/null 2>&1 || true
        done
    fi

    pkill -x gvfsd-gphoto2 >/dev/null 2>&1 || true
    pkill -f '/usr/libexec/gvfs-gphoto2-volume-monitor' >/dev/null 2>&1 || true
    pkill -f '/usr/libexec/gvfs-udisks2-volume-monitor' >/dev/null 2>&1 || true
    pkill -f '/usr/libexec/gvfs-mtp-volume-monitor' >/dev/null 2>&1 || true
    pkill -x gvfsd >/dev/null 2>&1 || true
    pkill -x gvfsd-fuse >/dev/null 2>&1 || true
}

install_fuji_camera_keepalive() {
    # Write the PTP keepalive loop script that the udev rule fires on camera attach.
    # It pings the Fuji body every 2 minutes to suppress the ~5-minute auto-power-off.
    cat > /usr/local/bin/kepler-camera-attach << 'ATTACH'
#!/bin/bash
# Kepler camera keepalive loop.
# Runs from the udev add rule when a Fujifilm camera is connected.
# Opens a PTP session every 2 minutes to suppress the camera's auto-power-off
# timer.  Exits when the camera is no longer reachable (disconnect/power-off).

LOGFILE=/var/log/kepler-camera-attach.log
INTERVAL=120

indi_camera_driver_active() {
    pgrep -f 'indi_(fuji|gphoto)_ccd' >/dev/null 2>&1
}

sleep 2

if indi_camera_driver_active; then
    echo "$(date -Iseconds) indi camera driver active, skipping keepalive startup" >> "$LOGFILE"
    exit 0
fi

echo "$(date -Iseconds) camera attached, starting keepalive loop (interval=${INTERVAL}s)" >> "$LOGFILE"

while true; do
    if indi_camera_driver_active; then
        echo "$(date -Iseconds) indi camera driver active, keepalive exiting" >> "$LOGFILE"
        exit 0
    fi

    if ! /usr/bin/gphoto2 --get-config /main/actions/bulb >> "$LOGFILE" 2>&1; then
        break
    fi

    sleep "$INTERVAL"
done

echo "$(date -Iseconds) camera unreachable, keepalive loop exiting" >> "$LOGFILE"
ATTACH
    chmod +x /usr/local/bin/kepler-camera-attach

    cat > /etc/udev/rules.d/99-kepler-camera.rules << 'RULES'
# Kepler: open a PTP session when a Fujifilm camera is connected so the body
# recognises an active host and suppresses its auto-power-off timer.
# Also keep desktop auto-mounters away from the body; INDI/gphoto own it.
SUBSYSTEM=="usb", ATTR{idVendor}=="04cb", ENV{UDISKS_IGNORE}="1", ENV{UDISKS_AUTO}="0", ACTION=="add", \
    RUN+="/usr/bin/systemd-run --no-block --unit=kepler-camera-attach /usr/local/bin/kepler-camera-attach"
RULES

    udevadm control --reload-rules
}

usage() {
    cat <<EOF
Kepler Node v1 Upgrade

Usage:
    $0 [options]

Options:
    --release TAG       Target git tag or branch (default: current HEAD / latest main)
    --data-dir DIR      Data root (default: /var/lib/kepler or \$KEPLER_DATA_DIR)
    --port PORT         Kepler API port (default: 8000)
    --skip-restart      Do not restart the systemd service after upgrade
    --help              Show this help
EOF
    exit 0
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        fail "Upgrade must be run as root (or via sudo)."
    fi
}

# ------------------------------------------------------------------ #
# Argument parsing                                                     #
# ------------------------------------------------------------------ #

while [[ $# -gt 0 ]]; do
    case "$1" in
        --release)      TARGET_RELEASE="$2"; shift 2 ;;
        --data-dir)     DATA_DIR="$2";       shift 2 ;;
        --port)         KEPLER_PORT="$2";    shift 2 ;;
        --skip-restart) SKIP_RESTART=true;   shift ;;
        --help|-h)      usage ;;
        *) fail "Unknown argument: $1" ;;
    esac
done

require_root

cd "${SCRIPT_DIR}"

# ------------------------------------------------------------------ #
# Step 1 — Preflight: read current install manifest                    #
# ------------------------------------------------------------------ #

log "Step 1: Preflight checks..."

MANIFEST_PATH="${DATA_DIR}/install_manifest.json"

if [[ ! -f "${MANIFEST_PATH}" ]]; then
    fail "No install manifest found at ${MANIFEST_PATH}.  Run bootstrap.sh first."
fi

PREV_VERSION="$(grep -o '"kepler_version": *"[^"]*"' "${MANIFEST_PATH}" | cut -d'"' -f4 || echo "unknown")"
BOOTSTRAP_PROFILE="$(grep -o '"bootstrap_profile": *"[^"]*"' "${MANIFEST_PATH}" | cut -d'"' -f4 || echo "")"

log "Current version: ${PREV_VERSION}"
log "Bootstrap profile: ${BOOTSTRAP_PROFILE}"

if [[ -z "${BOOTSTRAP_PROFILE}" ]]; then
    fail "Install manifest is missing bootstrap_profile.  Cannot continue safely."
fi

ok "Install manifest found (version: ${PREV_VERSION}, profile: ${BOOTSTRAP_PROFILE})"

# ------------------------------------------------------------------ #
# Step 1b — Read target release metadata                              #
# ------------------------------------------------------------------ #

log "Step 1b: Reading target release metadata..."

RELEASE_JSON_CONTENT=""
if [[ -n "${TARGET_RELEASE}" ]]; then
    # Fetch the target ref so we can read its release metadata before any changes
    # (spec line 1803 step 2: read target release metadata before preflight)
    git fetch --tags origin 2>/dev/null \
        || warn "Could not reach origin — checking local refs for ${TARGET_RELEASE}"
    RELEASE_JSON_CONTENT="$(git show "${TARGET_RELEASE}:release.json" 2>/dev/null)" \
        || fail "Cannot read release.json from target ref '${TARGET_RELEASE}'.  Verify the tag exists locally or ensure origin is reachable."
    log "Using release metadata from target ref: ${TARGET_RELEASE}"
else
    RELEASE_METADATA_PATH="${SCRIPT_DIR}/release.json"
    if [[ ! -f "${RELEASE_METADATA_PATH}" ]]; then
        fail "No release metadata found at ${RELEASE_METADATA_PATH}.  Cannot verify upgrade compatibility."
    fi
    RELEASE_JSON_CONTENT="$(cat "${RELEASE_METADATA_PATH}")"
    log "Using release metadata from local release.json"
fi

TARGET_RELEASE_ID="$(echo "${RELEASE_JSON_CONTENT}" | grep -o '"release_id": *"[^"]*"' | cut -d'"' -f4 || echo "unknown")"
REQ_OS="$(echo "${RELEASE_JSON_CONTENT}" | grep -o '"required_os": *"[^"]*"' | cut -d'"' -f4 || echo "")"
REQ_ARCH="$(echo "${RELEASE_JSON_CONTENT}" | grep -o '"required_architecture": *"[^"]*"' | cut -d'"' -f4 || echo "")"
REQ_FREE_MB="$(echo "${RELEASE_JSON_CONTENT}" | grep -o '"required_free_space_mb": *[0-9]*' | grep -o '[0-9]*$' || echo "0")"
# Extract supported_from_versions as a whitespace-separated list of version strings
SUPPORTED_FROM="$(echo "${RELEASE_JSON_CONTENT}" | grep -o '"supported_from_versions": *\[[^]]*\]' \
    | grep -o '"[0-9][^"]*"' | tr -d '"' | tr '\n' ' ' || echo "")"
# Extract managed_services as a whitespace-separated list (used for preflight and stop/start ordering)
MANAGED_SVCS="$(echo "${RELEASE_JSON_CONTENT}" | grep -o '"managed_services": *\[[^]]*\]' \
    | grep -o '"[^"]*"' | grep -v 'managed_services' | tr -d '"' | tr '\n' ' ' \
    || echo "kepler-node indiserver")"

log "Target release: ${TARGET_RELEASE_ID}"
log "Required OS: ${REQ_OS:-any}, architecture: ${REQ_ARCH:-any}, free space: ${REQ_FREE_MB} MB"
log "Supported upgrade sources: ${SUPPORTED_FROM:-any}"

ok "Release metadata read"

# ------------------------------------------------------------------ #
# Step 1c — Compatibility and resource preflight checks               #
# ------------------------------------------------------------------ #

log "Step 1c: Running preflight checks..."

# Preflight: Supported current version check (spec line 1792: supported installed version)
if [[ -n "${SUPPORTED_FROM}" ]]; then
    VERSION_OK=false
    for V in ${SUPPORTED_FROM}; do
        if [[ "${PREV_VERSION}" == "${V}" ]]; then
            VERSION_OK=true
            break
        fi
    done
    if ! ${VERSION_OK}; then
        fail "Current installed version '${PREV_VERSION}' is not in the supported upgrade sources for this release (${SUPPORTED_FROM% }).  Cannot upgrade safely."
    fi
    ok "Current version '${PREV_VERSION}' is a supported upgrade source"
fi

# Preflight: OS check
CURRENT_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
if [[ -n "${REQ_OS}" && "${CURRENT_OS}" != "${REQ_OS}" ]]; then
    fail "Unsupported OS: got '${CURRENT_OS}', required '${REQ_OS}'.  Kepler Node v1 requires Linux (64-bit Raspberry Pi OS)."
fi
ok "OS check passed (${CURRENT_OS})"

# Preflight: Architecture check
CURRENT_ARCH="$(uname -m)"
if [[ -n "${REQ_ARCH}" && "${CURRENT_ARCH}" != "${REQ_ARCH}" ]]; then
    fail "Unsupported architecture: got '${CURRENT_ARCH}', required '${REQ_ARCH}'.  Kepler Node v1 requires 64-bit Raspberry Pi OS (aarch64)."
fi
ok "Architecture check passed (${CURRENT_ARCH})"

# Preflight: Free space check
if [[ "${REQ_FREE_MB}" -gt 0 ]]; then
    AVAIL_MB="$(df --output=avail -m "${DATA_DIR}" 2>/dev/null | tail -1 | tr -d ' ' || echo "0")"
    if [[ "${AVAIL_MB}" -lt "${REQ_FREE_MB}" ]]; then
        fail "Insufficient free space in ${DATA_DIR}: ${AVAIL_MB} MB available, ${REQ_FREE_MB} MB required."
    fi
    ok "Free space check passed (${AVAIL_MB} MB available in ${DATA_DIR})"
fi

# Preflight: Managed service layout check.
if ! systemctl cat kepler-node &>/dev/null 2>&1; then
    fail "Expected managed service 'kepler-node' is not present.  Run bootstrap.sh before upgrading."
fi
if ! systemctl cat kepler-ui &>/dev/null 2>&1; then
    fail "Expected managed service 'kepler-ui' is not present.  Run bootstrap.sh before upgrading."
fi
if ! systemctl cat indiwebmanager &>/dev/null 2>&1 && ! systemctl cat indiserver &>/dev/null 2>&1; then
    fail "Expected either 'indiwebmanager' or legacy 'indiserver' managed service to be present.  Run bootstrap.sh before upgrading."
fi
ok "Managed service layout check passed"

# Preflight: Manifest writeability check
if ! touch "${MANIFEST_PATH}" 2>/dev/null; then
    fail "Cannot write to install manifest at ${MANIFEST_PATH}.  Check filesystem permissions."
fi
ok "Manifest writeability check passed"

ok "All preflight checks passed"

# ------------------------------------------------------------------ #
# Step 2 — Stop managed services, then pull latest code               #
# ------------------------------------------------------------------ #

log "Step 2: Stopping managed services before applying changes..."

if [[ "${SKIP_RESTART}" == "false" ]]; then
    # Stop in dependency order: UI first, then kepler-node, then broker and any legacy indiserver service
    systemctl stop kepler-ui    2>/dev/null || true
    systemctl stop kepler-node  2>/dev/null || true
    systemctl stop indiwebmanager 2>/dev/null || true
    systemctl stop indiserver   2>/dev/null || true
    log "Managed services stopped"
else
    log "Skipping service stop (--skip-restart)"
fi

log "Step 2 (cont): Pulling latest code..."

if [[ -n "${TARGET_RELEASE}" ]]; then
    # Already fetched in Step 1b; just check out the target ref
    git checkout "${TARGET_RELEASE}" \
        || fail "Could not check out release ${TARGET_RELEASE}"
    ok "Checked out release ${TARGET_RELEASE}"
else
    git fetch --tags origin || warn "Could not reach origin — upgrading from local state"
    git pull --ff-only origin main \
        || fail "Fast-forward pull failed.  Resolve divergence manually before upgrading."
    ok "Pulled latest main"
fi

NEW_VERSION="$(grep '^version' "${SCRIPT_DIR}/pyproject.toml" 2>/dev/null | head -1 | sed 's/version = "\(.*\)"/\1/' || echo "dev")"
log "Upgrading to: ${NEW_VERSION}"

# ------------------------------------------------------------------ #
# Step 3 — Update Python dependencies                                  #
# ------------------------------------------------------------------ #

log "Step 3: Syncing Python dependencies..."

uv sync --extra local-api --extra ui \
    || fail "Dependency sync failed — review uv output above"

ensure_indiwebmanager_installed

ok "Dependencies synced"

log "Step 3a: Ensuring Fuji camera driver build prerequisites..."
apt-get update -qq
apt-get install -y --no-install-recommends build-essential cmake pkg-config git gphoto2 libcfitsio-dev libgphoto2-dev libindi-dev libjpeg-dev libraw-dev libusb-1.0-0-dev zlib1g-dev \
    || fail "Fuji camera driver build prerequisites could not be installed"

log "Step 3aa: Building and installing Kepler Fuji DSLR capture driver..."
build_and_install_kepler_fuji_ccd
ok "Kepler Fuji DSLR capture driver installed"

log "Step 3ab: Building and installing Fuji focus bridge sidecar..."
build_and_install_fuji_focus_bridge
ok "Fuji focus bridge sidecar installed"

# ------------------------------------------------------------------ #
# Step 3b — Refresh managed service units                             #
# ------------------------------------------------------------------ #

log "Step 3b: Refreshing managed service units..."

write_indiwebmanager_service "/etc/systemd/system/indiwebmanager.service" "$(command -v indi-web)" "${INDIWEBMANAGER_HOME}"
systemctl daemon-reload
systemctl enable indiwebmanager >/dev/null 2>&1 || warn "Could not enable indiwebmanager service"
systemctl disable indiserver 2>/dev/null || true

ok "Managed service units refreshed"

log "Step 3c: Preventing desktop camera auto-claimers..."
disable_desktop_camera_claimers
ok "Desktop camera auto-claimers disabled"

log "Step 3d: Refreshing Fuji camera keepalive (udev rule + handler script)..."
install_fuji_camera_keepalive
ok "Fuji camera keepalive refreshed (/usr/local/bin/kepler-camera-attach + 99-kepler-camera.rules)"

# ------------------------------------------------------------------ #
# Step 4 — Update install manifest (in-progress)                      #
# ------------------------------------------------------------------ #

log "Step 4: Updating install manifest..."

NOW_ISO="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
INSTALLED_AT="$(grep -o '"installed_at": *"[^"]*"' "${MANIFEST_PATH}" | cut -d'"' -f4 || echo "${NOW_ISO}")"

cat > "${MANIFEST_PATH}" <<MANIFEST
{
  "kepler_version": "${NEW_VERSION}",
  "release_id": "${TARGET_RELEASE:-${NEW_VERSION}}",
  "bootstrap_profile": "${BOOTSTRAP_PROFILE}",
  "installed_at": "${INSTALLED_AT}",
  "last_upgrade_at": "${NOW_ISO}",
  "last_upgrade_result": "in-progress"
}
MANIFEST

ok "Install manifest updated (outcome recorded after health checks)"

# ------------------------------------------------------------------ #
# Step 5 — Start services                                              #
# ------------------------------------------------------------------ #

HEALTH_FAIL=false

if [[ "${SKIP_RESTART}" == "false" ]]; then
    # Start in dependency order: broker first, then kepler-node, then optional UI
    log "Step 5: Starting indiwebmanager service..."
    systemctl start indiwebmanager
    sleep 2
    if systemctl is-active --quiet indiwebmanager; then
        ok "indiwebmanager service started successfully"
    else
        warn "indiwebmanager service did not start — check: journalctl -u indiwebmanager"
        sed -i 's/"last_upgrade_result": "in-progress"/"last_upgrade_result": "service-restart-failed"/' \
            "${MANIFEST_PATH}" || true
        HEALTH_FAIL=true
    fi

    if ! ${HEALTH_FAIL}; then
        wait_for_indiwebmanager_api || fail "indiwebmanager API did not become reachable during upgrade"
        configure_indiwebmanager_profile
        systemctl restart indiwebmanager || fail "Could not restart indiwebmanager after profile update"
        wait_for_indiwebmanager_api || fail "indiwebmanager API did not recover after profile update"
        start_indiwebmanager_profile
    fi

    log "Step 5: Starting kepler-node service..."
    systemctl start kepler-node
    sleep 3
    if systemctl is-active --quiet kepler-node; then
        ok "kepler-node service started successfully"
    else
        warn "kepler-node service did not start — check: journalctl -u kepler-node"
        sed -i 's/"last_upgrade_result": "in-progress"/"last_upgrade_result": "service-restart-failed"/' \
            "${MANIFEST_PATH}" || true
        HEALTH_FAIL=true
    fi
    # Start UI service if it exists
    if systemctl is-enabled --quiet kepler-ui 2>/dev/null; then
        systemctl start kepler-ui || warn "kepler-ui service did not start — check: journalctl -u kepler-ui"
    fi
else
    log "Step 5: Skipping service start (--skip-restart)"
fi

# ------------------------------------------------------------------ #
# Step 6 — Post-upgrade health checks                                  #
# ------------------------------------------------------------------ #

log "Step 6: Post-upgrade health checks..."

NODE_IP="$(hostname -I | awk '{print $1}')"
KEPLER_URL="http://${NODE_IP}:${KEPLER_PORT}"
ATTEMPTS=0
MAX_ATTEMPTS=12

if ! ${HEALTH_FAIL}; then
    echo "  Waiting for Kepler API..."
    while ! curl -sf "${KEPLER_URL}/api/v1/health" &>/dev/null; do
        sleep 5
        ATTEMPTS=$((ATTEMPTS + 1))
        if [[ ${ATTEMPTS} -ge ${MAX_ATTEMPTS} ]]; then
            warn "Kepler API did not respond within 60 s"
            HEALTH_FAIL=true
            break
        fi
    done
fi

if ! ${HEALTH_FAIL}; then
    ok "Kepler API is healthy at ${KEPLER_URL}"

    REPORTED_VERSION="$(curl -sf "${KEPLER_URL}/api/v1/node/status" \
        | grep -o '"kepler_version": *"[^"]*"' | cut -d'"' -f4 || echo "")"
    if [[ "${REPORTED_VERSION}" == "${NEW_VERSION}" ]]; then
        ok "API is reporting upgraded version: ${REPORTED_VERSION}"
    else
        warn "Version mismatch: API reports '${REPORTED_VERSION}', expected '${NEW_VERSION}'"
    fi
fi

# Astronomy stack binary checks (spec line 1900)
echo "  Checking astronomy stack..."

if command -v indiserver &>/dev/null; then
    ok "indiserver found at $(command -v indiserver)"
else
    warn "indiserver not found — INDI device control will not be available"
    HEALTH_FAIL=true
fi

if command -v indi-web &>/dev/null; then
    ok "indi-web found at $(command -v indi-web)"
else
    warn "indi-web not found — brokered INDI control will not be available"
    HEALTH_FAIL=true
fi

if systemctl is-active --quiet indiwebmanager; then
    ok "indiwebmanager service is active"
else
    warn "indiwebmanager service is not running — brokered INDI control may not be available"
    HEALTH_FAIL=true
fi

if curl -sf "http://127.0.0.1:${INDIWEBMANAGER_PORT}/api/server/status" &>/dev/null; then
    ok "indiwebmanager API is reachable on port ${INDIWEBMANAGER_PORT}"
else
    warn "indiwebmanager API is not reachable on port ${INDIWEBMANAGER_PORT}"
    HEALTH_FAIL=true
fi

if command -v solve-field &>/dev/null; then
    ok "solve-field found at $(command -v solve-field)"
else
    warn "solve-field not found — astrometry plate solving will not work"
    HEALTH_FAIL=true
fi

if command -v gphoto2 &>/dev/null; then
	ok "gphoto2 found at $(command -v gphoto2)"
else
	warn "gphoto2 not found — direct camera control will not work"
	HEALTH_FAIL=true
fi

if ls /usr/share/astrometry/*.fits &>/dev/null 2>&1; then
    ok "Astrometry index files found in /usr/share/astrometry/"
else
    warn "No astrometry index files found — plate solving will not work without offline indexes"
    HEALTH_FAIL=true
fi

if command -v gpsd &>/dev/null; then
    ok "gpsd found at $(command -v gpsd)"
else
    warn "gpsd not found — GPS time/location integration will not be available"
fi

if [[ "${BOOTSTRAP_PROFILE}" == "field-fallback" ]]; then
    if command -v kstars &>/dev/null; then
        ok "kstars found at $(command -v kstars)"
    else
        warn "kstars not found — field-fallback profile requires KStars for local planner"
        HEALTH_FAIL=true
    fi
    if systemctl is-active --quiet xrdp; then
        ok "xrdp remote desktop service is active"
    else
        warn "xrdp is not running — remote desktop for KStars/Ekos may not be available"
        HEALTH_FAIL=true
    fi
fi

# Kepler UI service check
UI_URL="http://${NODE_IP}:${UI_PORT}"
UI_ATTEMPTS=0
UI_MAX_ATTEMPTS=12
echo "  Waiting for Kepler UI to be responsive..."
while ! curl -sf "${UI_URL}" &>/dev/null; do
    sleep 5
    UI_ATTEMPTS=$((UI_ATTEMPTS + 1))
    if [[ ${UI_ATTEMPTS} -ge ${UI_MAX_ATTEMPTS} ]]; then
        warn "Kepler UI did not respond within 60 s — check: journalctl -u kepler-ui"
        HEALTH_FAIL=true
        break
    fi
done

if [[ ${UI_ATTEMPTS} -lt ${UI_MAX_ATTEMPTS} ]]; then
    ok "Kepler UI is reachable at ${UI_URL}"
fi

# ------------------------------------------------------------------ #
# Summary                                                              #
# ------------------------------------------------------------------ #

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Kepler Node Upgrade Complete"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Previous version: ${PREV_VERSION}"
echo "  New version     : ${NEW_VERSION}"
echo "  Profile         : ${BOOTSTRAP_PROFILE}"
echo "  API URL         : ${KEPLER_URL}"
echo "  UI URL          : ${UI_URL}"
echo ""

if ${HEALTH_FAIL}; then
    warn "Health checks did not pass.  Review logs before operating: journalctl -u kepler-node"
    sed -i 's/"last_upgrade_result": "in-progress"/"last_upgrade_result": "health-checks-failed"/' \
        "${MANIFEST_PATH}" || true
    exit 1
fi

# All checks passed — persist success outcome (spec line 1808: step 9)
sed -i 's/"last_upgrade_result": "in-progress"/"last_upgrade_result": "success"/' \
    "${MANIFEST_PATH}" || true
ok "Upgrade outcome recorded as success"
