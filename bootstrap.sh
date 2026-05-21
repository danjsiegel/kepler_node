#!/usr/bin/env bash
# bootstrap.sh — Kepler Node v1 bootstrap script
#
# Supports two deployment profiles:
#   headless-node   — INDI/Kepler on Pi, KStars/Ekos on a remote client
#   field-fallback  — adds on-node KStars/Ekos with xRDP remote-desktop access
#
# Usage:
#   ./bootstrap.sh --profile headless-node
#   ./bootstrap.sh --profile field-fallback
#   ./bootstrap.sh --profile headless-node --data-dir /data/kepler
#   ./bootstrap.sh --help

set -euo pipefail

# ------------------------------------------------------------------ #
# Defaults                                                             #
# ------------------------------------------------------------------ #

PROFILE=""
DATA_DIR="${KEPLER_DATA_DIR:-/var/lib/kepler}"
KEPLER_PORT=8000
UI_PORT=8501
RDP_PORT=3389
INDI_PORT=7624
INDIWEBMANAGER_PORT=8624
INDI_PROFILE_NAME="${KEPLER_INDI_PROFILE_NAME:-Kepler-Starter-Rig}"
FUJI_CAMERA_DRIVER_LABEL="${KEPLER_FUJI_CAMERA_DRIVER_LABEL:-Kepler Fuji DSLR}"
INDI_GPHOTO_UPSTREAM_REF="${KEPLER_INDI_GPHOTO_UPSTREAM_REF:-99e26e1137dc32263f0470280e45ba3d9b53d3f8}"
INDI_PROFILE_DRIVERS="${KEPLER_INDI_PROFILE_DRIVERS:-ES iEXOS100 PMC-Eight,${FUJI_CAMERA_DRIVER_LABEL},Fuji Focus Bridge}"
INDIWEBMANAGER_HOME="${KEPLER_INDIWEBMANAGER_HOME:-/var/lib/indiwebmanager}"
SKIP_REBOOT_PROMPT=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEPLER_VERSION="$(grep '^version' "${SCRIPT_DIR}/pyproject.toml" 2>/dev/null | head -1 | sed 's/version = "\(.*\)"/\1/' || echo "dev")"

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

log()  { echo "  [kepler] $*"; }
ok()   { echo "  ✅ $*"; }
fail() { echo "  ❌ $*" >&2; exit 1; }
warn() { echo "  ⚠️  $*"; }

apt_package_available() {
    apt-cache show "$1" >/dev/null 2>&1
}

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

ensure_uv_installed() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi

    curl -Lsf https://astral.sh/uv/install.sh | bash

    local uv_candidates=(
        "/usr/local/bin/uv"
        "/root/.local/bin/uv"
        "/root/.cargo/bin/uv"
        "${HOME}/.local/bin/uv"
        "${HOME}/.cargo/bin/uv"
    )
    local uvx_candidates=(
        "/usr/local/bin/uvx"
        "/root/.local/bin/uvx"
        "/root/.cargo/bin/uvx"
        "${HOME}/.local/bin/uvx"
        "${HOME}/.cargo/bin/uvx"
    )
    local candidate=""
    local uvx_candidate=""

    for candidate in "${uv_candidates[@]}"; do
        if [[ -x "${candidate}" ]]; then
            if [[ "${candidate}" != "/usr/local/bin/uv" ]]; then
                ln -sf "${candidate}" /usr/local/bin/uv
            fi
            export PATH="/usr/local/bin:${candidate%/uv}:${PATH}"
            break
        fi
    done

    for uvx_candidate in "${uvx_candidates[@]}"; do
        if [[ -x "${uvx_candidate}" ]]; then
            if [[ "${uvx_candidate}" != "/usr/local/bin/uvx" ]]; then
                ln -sf "${uvx_candidate}" /usr/local/bin/uvx
            fi
            break
        fi
    done

    command -v uv >/dev/null 2>&1 || fail "uv installation succeeded but the uv binary is not on PATH"
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        fail "Bootstrap must be run as root (or via sudo)."
    fi
}

usage() {
    cat <<EOF
Kepler Node v1 Bootstrap

Usage:
    $0 --profile <headless-node|field-fallback> [options]

Profiles:
    headless-node   INDI/Kepler on Pi; connect KStars/Ekos from a remote client.
    field-fallback  Same as headless-node, plus on-node KStars/Ekos via xRDP remote desktop.

Options:
    --data-dir DIR      Data root (default: /var/lib/kepler)
    --port PORT         Kepler API port (default: 8000)
    --skip-reboot       Do not prompt for reboot at end
    --help              Show this help
EOF
    exit 0
}

# ------------------------------------------------------------------ #
# Argument parsing                                                     #
# ------------------------------------------------------------------ #

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)     PROFILE="$2";   shift 2 ;;
        --data-dir)    DATA_DIR="$2";  shift 2 ;;
        --port)        KEPLER_PORT="$2"; shift 2 ;;
        --skip-reboot) SKIP_REBOOT_PROMPT=true; shift ;;
        --help|-h)     usage ;;
        *) fail "Unknown argument: $1" ;;
    esac
done

[[ -z "${PROFILE}" ]] && { echo ""; warn "No --profile specified."; usage; }
[[ "${PROFILE}" == "headless-node" || "${PROFILE}" == "field-fallback" ]] \
    || fail "Profile must be one of: headless-node, field-fallback"

# ------------------------------------------------------------------ #
# Root check                                                           #
# ------------------------------------------------------------------ #

require_root

MANIFEST_PATH="${DATA_DIR}/install_manifest.json"
if [[ -f "${MANIFEST_PATH}" ]]; then
    fail "Existing install manifest found at ${MANIFEST_PATH}. Use upgrade.sh for updates instead of rerunning bootstrap.sh."
fi

# ------------------------------------------------------------------ #
# Step 1 — System prerequisites                                        #
# ------------------------------------------------------------------ #

log "Step 1: Installing system prerequisites..."

apt-get update -qq

COMMON_PACKAGES=(
    build-essential
    cmake
    pkg-config
    python3-pip
    python3-venv
    libcfitsio-dev
    libgphoto2-dev
    libindi-dev
    libjpeg-dev
    libraw-dev
    libusb-1.0-0-dev
    astrometry.net
    astrometry-data-tycho2
    gpsd
    gpsd-clients
    gphoto2
    kstars
    xrdp
    tigervnc-standalone-server
    curl
    git
    zlib1g-dev
)

if apt_package_available indi-full; then
    COMMON_PACKAGES+=(indi-full)
else
    COMMON_PACKAGES+=(indi-bin)
    for optional_indi_pkg in indi-gphoto indi-gpsd; do
        if apt_package_available "${optional_indi_pkg}"; then
            COMMON_PACKAGES+=("${optional_indi_pkg}")
        fi
    done
fi

apt-get install -y --no-install-recommends "${COMMON_PACKAGES[@]}" \
    || fail "System package installation failed"

ok "System prerequisites installed"

# ------------------------------------------------------------------ #
# Step 2 — Install uv and Python dependencies                          #
# ------------------------------------------------------------------ #

log "Step 2: Installing uv and kepler-node Python dependencies..."

ensure_uv_installed
ensure_indiwebmanager_installed

cd "${SCRIPT_DIR}"

EXTRAS="--extra local-api --extra ui"
uv sync ${EXTRAS} \
    || fail "Python dependency sync failed"

ok "Python dependencies installed"

log "Step 2b: Building and installing Kepler Fuji DSLR capture driver..."
build_and_install_kepler_fuji_ccd
ok "Kepler Fuji DSLR capture driver installed"

log "Step 2c: Building and installing Fuji focus bridge sidecar..."
build_and_install_fuji_focus_bridge
ok "Fuji focus bridge sidecar installed"

# ------------------------------------------------------------------ #
# Step 3 — Data directory                                              #
# ------------------------------------------------------------------ #

log "Step 3: Setting up data directory at ${DATA_DIR}..."

mkdir -p "${DATA_DIR}/profiles" "${DATA_DIR}/sessions"
chmod 0750 "${DATA_DIR}"

ok "Data directory ready at ${DATA_DIR}"

# ------------------------------------------------------------------ #
# Step 4 — Write install manifest                                      #
# ------------------------------------------------------------------ #

log "Step 4: Writing install manifest..."
NOW_ISO="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

cat > "${MANIFEST_PATH}" <<MANIFEST
{
  "kepler_version": "${KEPLER_VERSION}",
  "release_id": "${KEPLER_VERSION}",
  "bootstrap_profile": "${PROFILE}",
  "installed_at": "${NOW_ISO}",
  "last_upgrade_at": null,
  "last_upgrade_result": null
}
MANIFEST

ok "Install manifest written to ${MANIFEST_PATH}"

log "Step 4b: Preventing desktop camera auto-claimers..."
disable_desktop_camera_claimers
ok "Desktop camera auto-claimers disabled"

log "Step 4c: Installing Fuji camera keepalive (udev rule + handler script)..."
install_fuji_camera_keepalive
ok "Fuji camera keepalive installed (/usr/local/bin/kepler-camera-attach + 99-kepler-camera.rules)"

# ------------------------------------------------------------------ #
# Step 5 — systemd service                                             #
# ------------------------------------------------------------------ #

log "Step 5: Installing kepler-node systemd service..."

SERVICE_FILE="/etc/systemd/system/kepler-node.service"
UV_BIN="$(command -v uv)"

# Both profiles require the INDI broker; field-fallback is a superset of headless-node
SERVICE_AFTER="network.target gpsd.service indiwebmanager.service"
SERVICE_WANTS="gpsd.service indiwebmanager.service"

cat > "${SERVICE_FILE}" <<SERVICE
[Unit]
Description=Kepler Node API Service
After=${SERVICE_AFTER}
Wants=${SERVICE_WANTS}

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
Environment=KEPLER_DATA_DIR=${DATA_DIR}
ExecStart=${UV_BIN} run --extra local-api kepler-node serve --host 0.0.0.0 --port ${KEPLER_PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable kepler-node
systemctl start kepler-node || warn "Service did not start immediately — check logs with: journalctl -u kepler-node"

ok "kepler-node service installed and started"

# Install Kepler UI service (Streamlit)
UI_SERVICE_FILE="/etc/systemd/system/kepler-ui.service"
cat > "${UI_SERVICE_FILE}" <<UISERVICE
[Unit]
Description=Kepler Node UI Service
After=network.target kepler-node.service
Wants=kepler-node.service

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
Environment=KEPLER_DATA_DIR=${DATA_DIR}
Environment=KEPLER_API_BASE_URL=http://127.0.0.1:${KEPLER_PORT}
ExecStart=${UV_BIN} run --extra ui streamlit run src/kepler_node/ui/streamlit_app.py --server.port ${UI_PORT} --server.address 0.0.0.0 --server.headless true
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UISERVICE

systemctl daemon-reload
systemctl enable kepler-ui
systemctl start kepler-ui || warn "UI service did not start immediately — check logs with: journalctl -u kepler-ui"

ok "kepler-ui service installed and started"

# ------------------------------------------------------------------ #
# Step 6 — INDI + profile-specific configuration                       #
# ------------------------------------------------------------------ #

# The supported path brokers INDI through indiwebmanager, which owns indiserver lifecycle.
log "Step 6: Configuring INDI broker service..."
INDIWEBMANAGER_SERVICE="/etc/systemd/system/indiwebmanager.service"
INDIWEBMANAGER_BIN="$(command -v indi-web)"
write_indiwebmanager_service "${INDIWEBMANAGER_SERVICE}" "${INDIWEBMANAGER_BIN}" "${INDIWEBMANAGER_HOME}"
systemctl daemon-reload
systemctl stop indiserver 2>/dev/null || true
systemctl disable indiserver 2>/dev/null || true
systemctl enable indiwebmanager || warn "INDI broker service setup failed"
systemctl start indiwebmanager || warn "INDI broker did not start — check: journalctl -u indiwebmanager"
wait_for_indiwebmanager_api || warn "indiwebmanager API did not become reachable during setup"
configure_indiwebmanager_profile
systemctl restart indiwebmanager || warn "INDI broker did not restart after profile update — check: journalctl -u indiwebmanager"
wait_for_indiwebmanager_api || warn "indiwebmanager API did not recover after profile update"
start_indiwebmanager_profile
ok "INDI broker service configured"

if [[ "${PROFILE}" == "field-fallback" ]]; then
    log "Step 6 (field-fallback): Configuring remote desktop (xrdp)..."
    # Enable graphical target so KStars/Ekos can run a desktop session
    systemctl set-default graphical.target || true
    systemctl enable xrdp || warn "Could not enable xrdp service"
    systemctl start xrdp || warn "xrdp did not start — remote desktop may not be available immediately"
    ok "xrdp configured for remote desktop access to KStars/Ekos (RDP port ${RDP_PORT})"
fi

# ------------------------------------------------------------------ #
# Step 7 — Post-install health checks                                  #
# ------------------------------------------------------------------ #

log "Step 7: Running post-install health checks..."

HEALTH_FAIL=false
NODE_IP="$(hostname -I | awk '{print $1}')"
KEPLER_URL="http://${NODE_IP}:${KEPLER_PORT}"
ATTEMPTS=0
MAX_ATTEMPTS=12

echo "  Waiting for Kepler API to be responsive..."
while ! curl -sf "${KEPLER_URL}/api/v1/health" &>/dev/null; do
    sleep 5
    ATTEMPTS=$((ATTEMPTS + 1))
    if [[ ${ATTEMPTS} -ge ${MAX_ATTEMPTS} ]]; then
        warn "Kepler API did not respond within 60 s — check: journalctl -u kepler-node"
        HEALTH_FAIL=true
        break
    fi
done

if ! ${HEALTH_FAIL}; then
    ok "Kepler API is healthy at ${KEPLER_URL}"

    # Check /api/v1/node/status
    STATUS="$(curl -sf "${KEPLER_URL}/api/v1/node/status" || echo '{}')"
    MANIFEST_PROFILE="$(echo "${STATUS}" | grep -o '"bootstrap_profile":"[^"]*"' | cut -d'"' -f4 || echo "")"
    if [[ "${MANIFEST_PROFILE}" == "${PROFILE}" ]]; then
        ok "Install manifest profile matches: ${MANIFEST_PROFILE}"
    else
        warn "Install manifest profile mismatch (got: '${MANIFEST_PROFILE}', expected: '${PROFILE}')"
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

if [[ "${PROFILE}" == "field-fallback" ]]; then
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
echo "  Kepler Node Bootstrap Complete"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Profile       : ${PROFILE}"
echo "  Kepler version: ${KEPLER_VERSION}"
echo "  API URL       : ${KEPLER_URL}"
echo "  UI URL        : ${UI_URL}"
echo "  Data dir      : ${DATA_DIR}"
if [[ "${PROFILE}" == "headless-node" ]]; then
    echo "  INDI port     : ${INDI_PORT} (connect remote KStars/Ekos to this node)"
elif [[ "${PROFILE}" == "field-fallback" ]]; then
    echo "  xRDP port     : ${RDP_PORT} (connect RDP client to reach KStars/Ekos on node)"
fi
echo ""

if ${HEALTH_FAIL}; then
    warn "One or more health checks failed.  Review logs before operating."
    exit 1
fi

if [[ "${SKIP_REBOOT_PROMPT}" == "false" ]]; then
    read -rp "Reboot now to apply all changes? [y/N] " REBOOT_ANSWER
    if [[ "${REBOOT_ANSWER}" =~ ^[Yy]$ ]]; then
        systemctl reboot
    fi
fi
