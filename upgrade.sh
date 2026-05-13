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

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

log()  { echo "  [upgrade] $*"; }
ok()   { echo "  ✅ $*"; }
fail() { echo "  ❌ $*" >&2; exit 1; }
warn() { echo "  ⚠️  $*"; }

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

# Preflight: Managed service layout check (all managed services must already be bootstrapped)
for SVC in ${MANAGED_SVCS}; do
    if ! systemctl cat "${SVC}" &>/dev/null 2>&1; then
        fail "Expected managed service '${SVC}' is not present.  Run bootstrap.sh before upgrading."
    fi
done
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
    # Stop in dependency order: UI first, then kepler-node, then indiserver
    systemctl stop kepler-ui    2>/dev/null || true
    systemctl stop kepler-node  2>/dev/null || true
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

ok "Dependencies synced"

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
    # Start in dependency order: indiserver first, then kepler-node, then optional UI
    log "Step 5: Starting indiserver service..."
    systemctl start indiserver
    sleep 2
    if systemctl is-active --quiet indiserver; then
        ok "indiserver service started successfully"
    else
        warn "indiserver service did not start — check: journalctl -u indiserver"
        sed -i 's/"last_upgrade_result": "in-progress"/"last_upgrade_result": "service-restart-failed"/' \
            "${MANIFEST_PATH}" || true
        HEALTH_FAIL=true
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

if systemctl is-active --quiet indiserver; then
    ok "indiserver service is active"
else
    warn "indiserver service is not running — INDI device control may not be available"
    HEALTH_FAIL=true
fi

if command -v solve-field &>/dev/null; then
    ok "solve-field found at $(command -v solve-field)"
else
    warn "solve-field not found — astrometry plate solving will not work"
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
