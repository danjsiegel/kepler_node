#!/usr/bin/env bash

set -euo pipefail

TARGET_REF=""
EXPECT_PROFILE=""
REQUIRE_GPS_FIX=false
REQUIRE_RTC_SYNC=false
API_BASE_URL="http://127.0.0.1:8000"

log()  { echo "  [deploy] $*"; }
ok()   { echo "  ✅ $*"; }
fail() { echo "  ❌ $*" >&2; exit 1; }

usage() {
	cat <<'EOF'
Kepler Node Pi deploy helper

Usage:
	./scripts/deploy_pi.sh [options]

Options:
	--target-ref REF         Target git tag or branch to deploy through upgrade.sh
	--expect-profile NAME    Expected bootstrap profile after deploy (headless-node or field-fallback)
	--require-gps-fix        Fail if the post-deploy smoke check does not observe a GPS TPV fix
	--require-rtc-sync       Fail if the post-deploy smoke check does not report RTCSynchronized=yes
	--api-base-url URL       Local API base URL for the smoke check (default: http://127.0.0.1:8000)
	--help                   Show this help
EOF
	exit 0
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--target-ref)      TARGET_REF="$2"; shift 2 ;;
		--expect-profile)  EXPECT_PROFILE="$2"; shift 2 ;;
		--require-gps-fix) REQUIRE_GPS_FIX=true; shift ;;
		--require-rtc-sync) REQUIRE_RTC_SYNC=true; shift ;;
		--api-base-url)    API_BASE_URL="$2"; shift 2 ;;
		--help|-h)         usage ;;
		*) fail "Unknown argument: $1" ;;
	esac
done

if ! sudo -n true >/dev/null 2>&1; then
	fail "Passwordless sudo is required for automated deploys on the Pi. Run bootstrap/upgrade manually or grant the runner user passwordless sudo for upgrade.sh."
fi

INSTALL_ROOT="$(systemctl show kepler-node --property=WorkingDirectory --value 2>/dev/null || true)"
[[ -n "${INSTALL_ROOT}" ]] || fail "Could not determine the live Kepler install path from the kepler-node systemd unit. Bootstrap the node first."
[[ -f "${INSTALL_ROOT}/upgrade.sh" ]] || fail "No upgrade.sh found under ${INSTALL_ROOT}."
[[ -f "${INSTALL_ROOT}/scripts/pi_smoke.py" ]] || fail "No scripts/pi_smoke.py found under ${INSTALL_ROOT}."

log "Using live install path: ${INSTALL_ROOT}"

upgrade_cmd=(sudo -n "${INSTALL_ROOT}/upgrade.sh")
if [[ -n "${TARGET_REF}" ]]; then
	upgrade_cmd+=(--release "${TARGET_REF}")
fi

log "Running upgrade..."
"${upgrade_cmd[@]}"
ok "Upgrade completed"

smoke_cmd=(python3 "${INSTALL_ROOT}/scripts/pi_smoke.py" --require-kepler-stack --api-base-url "${API_BASE_URL}")
if [[ -n "${EXPECT_PROFILE}" ]]; then
	smoke_cmd+=(--expect-profile "${EXPECT_PROFILE}")
fi
if [[ "${REQUIRE_GPS_FIX}" == "true" ]]; then
	smoke_cmd+=(--require-gps-fix)
fi
if [[ "${REQUIRE_RTC_SYNC}" == "true" ]]; then
	smoke_cmd+=(--require-rtc-sync)
fi

log "Running post-deploy smoke checks..."
"${smoke_cmd[@]}"
ok "Post-deploy smoke checks passed"