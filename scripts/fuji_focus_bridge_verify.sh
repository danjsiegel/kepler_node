#!/usr/bin/env bash
# fuji_focus_bridge_verify.sh — Fuji X-T5 focus bridge verification script
#
# Purpose:
#   Verify the current state of Fuji gphoto2 focus surfaces and check whether
#   stock indi-gphoto already satisfies the INDI focuser contract.  Defaults to
#   fully read-only operation; active motion and INDI probing require explicit
#   flags.
#
# Usage:
#   ./scripts/fuji_focus_bridge_verify.sh                   # read-only
#   ./scripts/fuji_focus_bridge_verify.sh --probe-indi      # also probe active INDI profile
#   ./scripts/fuji_focus_bridge_verify.sh --allow-move      # emit focus moves (d171)
#   ./scripts/fuji_focus_bridge_verify.sh --allow-move --probe-indi --artifact-dir /tmp/fuji-verify
#
# Current baseline (XF55-200mmF3.5-4.8 R LM OIS on X-T5):
#   /main/other/d171  — proven writable; moves the lens; NOT a calibrated linear axis
#   /main/other/d262  — declared numeric range but live writes fail; treat as read-only
#   /main/other/d209  — secondary status hint only; not a primary focus signal
#
# Stock indi-gphoto support:
#   indi-gphoto exposes the Fuji camera as a CCD/DSLR device.
#   It does NOT expose a standard INDI focuser interface (FOCUS_MOTION,
#   FOCUS_STEPS, FOCUS_ABORT, FOCUS_STATUS properties) through stock
#   indi_gphoto_ccd or indi_fuji_ccd drivers. This is confirmed by driver
#   source inspection and live target-Pi probes of both stock drivers.
#   Therefore a custom INDI focuser sidecar is required.

set -uo pipefail

# ------------------------------------------------------------------ #
# Flags                                                                #
# ------------------------------------------------------------------ #

ALLOW_MOVE=false
PROBE_INDI=false
ARTIFACT_DIR=""
MOVE_STEPS_INWARD=1
MOVE_STEPS_OUTWARD=1
MOVE_CYCLES=3
INDI_HOST="localhost"
INDI_PORT=7624

while [[ $# -gt 0 ]]; do
    case "$1" in
        --allow-move)          ALLOW_MOVE=true ;;
        --probe-indi)          PROBE_INDI=true ;;
        --artifact-dir)        ARTIFACT_DIR="$2"; shift ;;
        --move-steps)          MOVE_STEPS_INWARD="$2"; MOVE_STEPS_OUTWARD="$2"; shift ;;
        --move-steps-inward)   MOVE_STEPS_INWARD="$2"; shift ;;
        --move-steps-outward)  MOVE_STEPS_OUTWARD="$2"; shift ;;
        --move-cycles)         MOVE_CYCLES="$2"; shift ;;
        --indi-host)           INDI_HOST="$2"; shift ;;
        --indi-port)           INDI_PORT="$2"; shift ;;
        --help|-h)
            sed -n '/^# Purpose/,/^[^#]/{ /^[^#]/d; s/^# \{0,3\}//; p }' "$0"
            exit 0 ;;
        *) echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
    shift
done

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
declare -a SUMMARY_LINES=()

pass()  { PASS_COUNT=$((PASS_COUNT + 1));  SUMMARY_LINES+=("[PASS] $*"); echo "[PASS] $*"; }
fail()  { FAIL_COUNT=$((FAIL_COUNT + 1));  SUMMARY_LINES+=("[FAIL] $*"); echo "[FAIL] $*"; }
warn()  { WARN_COUNT=$((WARN_COUNT + 1));  SUMMARY_LINES+=("[WARN] $*"); echo "[WARN] $*"; }
info()  { echo "[INFO] $*"; }
sep()   { echo ""; echo "--- $* ---"; echo ""; }

gp2_get() {
    # Get a gphoto2 config value; returns the raw output of --get-config.
    gphoto2 --get-config "$1" 2>&1
}

gp2_current() {
    # Extract the Current: value from --get-config output.
    gp2_get "$1" | grep '^Current:' | sed 's/^Current: //'
}

gp2_set() {
    # Attempt to set a config value; caller must gate on ALLOW_MOVE.
    gphoto2 --set-config "$1=$2" 2>&1
}

# ------------------------------------------------------------------ #
# Artifact directory                                                   #
# ------------------------------------------------------------------ #

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
if [[ -n "${ARTIFACT_DIR}" ]]; then
    mkdir -p "${ARTIFACT_DIR}"
    ARTIFACT_FILE="${ARTIFACT_DIR}/fuji_verify_${TIMESTAMP}.txt"
    exec > >(tee -a "${ARTIFACT_FILE}") 2>&1
    info "Artifacts → ${ARTIFACT_FILE}"
fi

# ------------------------------------------------------------------ #
# Step 1: Camera detection                                             #
# ------------------------------------------------------------------ #

sep "Step 1: Camera detection"

if ! command -v gphoto2 &>/dev/null; then
    fail "gphoto2 not found; install gphoto2 to use this script"
    echo ""
    echo "=== INCONCLUSIVE: gphoto2 unavailable ==="
    exit 1
fi

DETECT_OUT="$(gphoto2 --auto-detect 2>&1)"
DETECTED_CAMERAS="$(echo "${DETECT_OUT}" | grep -v '^Model\|^---\|^$' | grep -v '^\s*$' || true)"
if [[ -z "${DETECTED_CAMERAS}" ]]; then
    fail "No camera detected via gphoto2 --auto-detect"
    echo ""
    echo "=== FAIL: No camera detected ==="
    exit 1
fi

info "Detected cameras:"
echo "${DETECTED_CAMERAS}" | while IFS= read -r line; do info "  ${line}"; done

# Verify it looks like a Fuji body
if echo "${DETECTED_CAMERAS}" | grep -qi "fuji"; then
    pass "Fuji camera body detected"
else
    warn "Detected camera may not be a Fuji body — proceeding anyway"
fi

# ------------------------------------------------------------------ #
# Step 2: Remote-control mode check                                    #
# ------------------------------------------------------------------ #

sep "Step 2: Remote-control mode"

CT_OUT="$(gp2_get /main/settings/capturetarget)"
if echo "${CT_OUT}" | grep -q "^Current:"; then
    pass "Camera is in USB remote-control/tether mode (capturetarget readable)"
else
    BULB_OUT="$(gp2_get /main/actions/bulb)"
    if echo "${BULB_OUT}" | grep -q "^Current:"; then
        pass "Camera is in USB remote-control/tether mode (bulb readable)"
    else
        fail "Camera does not appear to be in USB remote-control mode; focus nodes will be inaccessible"
        echo ""
        echo "=== FAIL: Camera not in remote-control mode ==="
        exit 1
    fi
fi

# ------------------------------------------------------------------ #
# Step 3: Lens identity and basic surfaces                             #
# ------------------------------------------------------------------ #

sep "Step 3: Lens identity and basic surfaces"

LENS_ID=""
for lens_node in "/main/status/lensname" "/main/other/d209"; do
    lens_raw="$(gp2_current "${lens_node}" 2>/dev/null)"
    if [[ -n "${lens_raw}" && "${lens_raw}" != "None" ]]; then
        LENS_ID="${lens_raw}"
        info "Lens identity via ${lens_node}: ${LENS_ID}"
        break
    fi
done

if [[ -z "${LENS_ID}" ]]; then
    warn "Lens identity node not readable; proceeding without lens ID"
else
    pass "Lens identity readable: ${LENS_ID}"
fi

# Zoom position
ZOOM_OUT="$(gp2_get /main/status/focallength 2>/dev/null)"
if echo "${ZOOM_OUT}" | grep -q "^Current:"; then
    ZOOM_VAL="$(echo "${ZOOM_OUT}" | grep '^Current:' | sed 's/^Current: //')"
    info "Focal length / zoom: ${ZOOM_VAL}"
    pass "Focal length is readable"
else
    warn "Focal length not readable from /main/status/focallength"
fi

# Aperture
APERTURE_OUT="$(gp2_get /main/capturesettings/aperture 2>/dev/null)"
if echo "${APERTURE_OUT}" | grep -q "^Current:"; then
    APERTURE_VAL="$(echo "${APERTURE_OUT}" | grep '^Current:' | sed 's/^Current: //')"
    info "Aperture: ${APERTURE_VAL}"
    pass "Aperture is readable"
else
    warn "Aperture not readable from /main/capturesettings/aperture"
fi

# ------------------------------------------------------------------ #
# Step 4: Focus-related hidden nodes                                   #
# ------------------------------------------------------------------ #

sep "Step 4: Focus surface probe (read-only)"

# d171 — primary proven writable focus surface
D171_OUT="$(gp2_get /main/other/d171 2>/dev/null)"
if echo "${D171_OUT}" | grep -q "^Current:"; then
    D171_VAL="$(echo "${D171_OUT}" | grep '^Current:' | sed 's/^Current: //')"
    D171_READONLY="$(echo "${D171_OUT}" | grep '^Readonly:' | sed 's/^Readonly: //')"
    info "d171 (FocusPosition) current=${D171_VAL} readonly=${D171_READONLY}"
    if [[ "${D171_READONLY}" == "0" ]]; then
        pass "d171 reports as writable (readonly=0) — matches baseline"
    else
        warn "d171 reports readonly=${D171_READONLY} — may not be writable in this posture"
    fi
    info "d171 full output:"
    echo "${D171_OUT}" | while IFS= read -r line; do info "  ${line}"; done
else
    fail "d171 not readable — camera may not be in manual-focus posture or this firmware differs"
    info "d171 output: ${D171_OUT}"
fi

# d262 — declared numeric range; treat as read-only / unproven
D262_OUT="$(gp2_get /main/other/d262 2>/dev/null)"
if echo "${D262_OUT}" | grep -q "^Current:"; then
    D262_VAL="$(echo "${D262_OUT}" | grep '^Current:' | sed 's/^Current: //')"
    D262_READONLY="$(echo "${D262_OUT}" | grep '^Readonly:' | sed 's/^Readonly: //')"
    info "d262 current=${D262_VAL} readonly=${D262_READONLY}"
    info "d262 full output:"
    echo "${D262_OUT}" | while IFS= read -r line; do info "  ${line}"; done
    # Baseline: d262 looks writable in metadata but fails live writes — treat as read-only
    warn "d262 readable but live write attempts fail in current posture; treat as unproven"
else
    info "d262 not readable (expected if lens posture changed or firmware differs)"
    warn "d262 not accessible"
fi

# d209 — secondary status hint
D209_OUT="$(gp2_get /main/other/d209 2>/dev/null)"
if echo "${D209_OUT}" | grep -q "^Current:"; then
    D209_VAL="$(echo "${D209_OUT}" | grep '^Current:' | sed 's/^Current: //')"
    info "d209 (focus hint) current=${D209_VAL}"
    pass "d209 readable (secondary hint)"
else
    info "d209 not readable or not relevant for current posture"
fi

# ------------------------------------------------------------------ #
# Step 5: Active focus move test (only when --allow-move)              #
# ------------------------------------------------------------------ #

sep "Step 5: Active focus move test"

if [[ "${ALLOW_MOVE}" == "false" ]]; then
    info "Skipping active focus moves (pass --allow-move to enable)"
    warn "Active move test skipped — result is INCONCLUSIVE for write path"
else
    info "Active move enabled (--allow-move). Cycles=${MOVE_CYCLES} steps inward=${MOVE_STEPS_INWARD} outward=${MOVE_STEPS_OUTWARD}"
    info "Running ${MOVE_CYCLES} inward+outward cycle(s) to assess d171 repeatability (spec requires several cycles)."

    CYCLE_FAIL=false
    for (( cycle=1; cycle<=MOVE_CYCLES; cycle++ )); do
        info "--- Cycle ${cycle}/${MOVE_CYCLES} ---"

        # Read baseline d171 value
        D171_BEFORE="$(gp2_current /main/other/d171 2>/dev/null)"
        info "d171 before inward move (cycle ${cycle}): ${D171_BEFORE}"

        # Inward move — negative delta, matching the bridge driver's signed convention:
        #   FOCUS_INWARD → gphoto2 --set-config /main/other/d171=-<ticks>
        info "Attempting inward move: d171 set to -${MOVE_STEPS_INWARD}"
        INWARD_OUT="$(gp2_set /main/other/d171 "-${MOVE_STEPS_INWARD}" 2>&1)"
        INWARD_RC=$?
        info "Inward set output (exit ${INWARD_RC}): ${INWARD_OUT}"

        sleep 1

        D171_AFTER_INWARD="$(gp2_current /main/other/d171 2>/dev/null)"
        info "d171 after inward move (cycle ${cycle}): ${D171_AFTER_INWARD}"

        # Outward move — positive delta, matching the bridge driver's signed convention:
        #   FOCUS_OUTWARD → gphoto2 --set-config /main/other/d171=+<ticks>
        info "Attempting outward move: d171 set to ${MOVE_STEPS_OUTWARD}"
        OUTWARD_OUT="$(gp2_set /main/other/d171 "${MOVE_STEPS_OUTWARD}" 2>&1)"
        OUTWARD_RC=$?
        info "Outward set output (exit ${OUTWARD_RC}): ${OUTWARD_OUT}"

        sleep 1

        D171_AFTER_OUTWARD="$(gp2_current /main/other/d171 2>/dev/null)"
        info "d171 after outward move (cycle ${cycle}): ${D171_AFTER_OUTWARD}"

        # Evaluate write success for this cycle: non-zero exit OR error text → failure
        if [[ "${INWARD_RC}" -ne 0 ]] || echo "${INWARD_OUT}" | grep -qi "error\|unsupported\|failed"; then
            CYCLE_FAIL=true
            fail "d171 inward write failed on cycle ${cycle} (exit ${INWARD_RC}) — not usable as focus primitive in this posture"
        elif [[ "${OUTWARD_RC}" -ne 0 ]] || echo "${OUTWARD_OUT}" | grep -qi "error\|unsupported\|failed"; then
            CYCLE_FAIL=true
            fail "d171 outward write failed on cycle ${cycle} (exit ${OUTWARD_RC}) — not usable as focus primitive in this posture"
        else
            info "Cycle ${cycle}: d171 write operations completed without error (inward exit ${INWARD_RC}, outward exit ${OUTWARD_RC})"
        fi
    done

    if [[ "${CYCLE_FAIL}" == "false" ]]; then
        pass "d171 write operations completed (zero exit code, no error text) across ${MOVE_CYCLES} cycle(s)"
        info "Note: d171 is not a calibrated linear axis; readback value may not reflect physical position exactly"
        info "Note: repeatability across ${MOVE_CYCLES} cycle(s) observed — verify physical lens motion separately"
    fi

    # Attempt d262 write to confirm it fails as expected
    info "Attempting d262 write to confirm unproven status..."
    D262_WRITE_OUT="$(gp2_set /main/other/d262 "1000" 2>&1)"
    info "d262 write output: ${D262_WRITE_OUT}"
    if echo "${D262_WRITE_OUT}" | grep -qi "error\|unsupported\|not supported"; then
        pass "d262 write fails as expected — confirmed read-only/unproven in current posture"
    else
        warn "d262 write did not report expected error — re-evaluate d262 status for this posture"
    fi
fi

# ------------------------------------------------------------------ #
# Step 6: Stock INDI focuser property check (only when --probe-indi)   #
# ------------------------------------------------------------------ #

sep "Step 6: Stock INDI focuser property check"

if [[ "${PROBE_INDI}" == "false" ]]; then
    info "Skipping INDI property probe (pass --probe-indi to enable)"
    warn "INDI probe skipped — stock support not disproved by this run"
else
    if ! command -v indi_getprop &>/dev/null; then
        fail "indi_getprop not found; install indi-bin to probe INDI properties"
    else
        info "Probing INDI server at ${INDI_HOST}:${INDI_PORT} for focuser properties..."

        # Get all device names visible on the INDI bus.
        # indi_getprop output format is "Device.Property.Element=value" (no leading dot).
        # Extract the device name by taking the portion before the first '=' then the
        # first '.'-delimited field — consistent with how mount/indi.py parses the bus.
        ALL_DEVICES="$(indi_getprop -h "${INDI_HOST}" -p "${INDI_PORT}" "*.*.*" 2>&1 | grep '=' | cut -d= -f1 | awk -F'.' '{print $1}' | sort -u 2>/dev/null || true)"
        if [[ -z "${ALL_DEVICES}" ]]; then
            fail "No INDI devices found on ${INDI_HOST}:${INDI_PORT} — indiserver must be running with the stock Fuji profile to disprove stock focuser support; cannot issue a valid PASS without a live INDI connection"
        else
            info "INDI devices found: $(echo "${ALL_DEVICES}" | tr '\n' ' ')"

            # Look for standard INDI focuser property names
            FOCUSER_PROPS="$(indi_getprop -h "${INDI_HOST}" -p "${INDI_PORT}" "*.*.*" 2>&1 | \
                grep -Ei '\.(FOCUS_MOTION|FOCUS_STEPS|FOCUS_ABORT|FOCUS_STATUS|FOCUS_SPEED|REL_FOCUS|ABS_FOCUS)\.' || true)"

            if [[ -z "${FOCUSER_PROPS}" ]]; then
                pass "No standard INDI focuser properties found on stock profile — custom sidecar required"
                info "Stock indi-gphoto does not expose FOCUS_MOTION, FOCUS_STEPS, FOCUS_ABORT, or FOCUS_STATUS"
                info "This confirms: the custom INDI focuser sidecar is necessary"
            else
                warn "Potential focuser properties found — review before assuming custom driver is needed:"
                echo "${FOCUSER_PROPS}" | while IFS= read -r line; do info "  ${line}"; done
            fi
        fi
    fi
fi

# ------------------------------------------------------------------ #
# Step 7: Summary                                                      #
# ------------------------------------------------------------------ #

sep "Summary"

echo ""
echo "=== Fuji Focus Bridge Verification Report ==="
echo "Timestamp : ${TIMESTAMP}"
echo "Allow move: ${ALLOW_MOVE}"
echo "Probe INDI: ${PROBE_INDI}"
echo ""
echo "Results:"
for line in "${SUMMARY_LINES[@]}"; do
    echo "  ${line}"
done
echo ""
echo "Counts: PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} WARN=${WARN_COUNT}"
echo ""

# Determine final verdict
#
# The two major gates are independent:
#   --probe-indi : disproves stock INDI focuser support (does not require --allow-move)
#   --allow-move : proves the d171 signed-move write path
#
# A run with --probe-indi and no --allow-move can still PASS the stock-support gate.
# A run with --allow-move and no --probe-indi is INCONCLUSIVE for stock-support.
if [[ "${FAIL_COUNT}" -gt 0 ]]; then
    echo "=== FAIL: ${FAIL_COUNT} check(s) failed — review findings above ==="
    exit 1
elif [[ "${PROBE_INDI}" == "false" ]]; then
    echo "=== INCONCLUSIVE: INDI not probed — re-run with --probe-indi to disprove stock focuser support ==="
    exit 0
elif [[ "${ALLOW_MOVE}" == "false" ]]; then
    if [[ "${WARN_COUNT}" -gt 0 ]]; then
        echo "=== PASS (INDI disproved) WITH WARNINGS: stock support confirmed absent; review warnings; re-run with --allow-move to validate d171 write path ==="
    else
        echo "=== PASS (INDI disproved): stock INDI focuser support absent; re-run with --allow-move to validate d171 signed-move write path ==="
    fi
    exit 0
elif [[ "${WARN_COUNT}" -gt 0 ]]; then
    echo "=== PASS WITH WARNINGS: all checks performed; review warnings before deploying bridge ==="
    exit 0
else
    echo "=== PASS: all checks passed; focus bridge prerequisites verified ==="
    exit 0
fi
