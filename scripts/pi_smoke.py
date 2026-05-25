#!/usr/bin/env python3
"""Minimal Raspberry Pi smoke checks for Kepler Node hardware and services.

This script is intentionally stdlib-only so it can run on a freshly provisioned
Pi before any project Python dependencies are installed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class CheckResult:
    level: str
    title: str
    detail: str


def _run(command: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def _fetch_json(url: str, timeout: int = 5) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _timedatectl_props() -> tuple[dict[str, str], str | None]:
    try:
        proc = _run(["timedatectl", "show", "--no-pager"])
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {}, str(exc)
    props: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key] = value
    return props, None


def _gpspipe_messages(limit: int = 20) -> tuple[list[dict[str, Any]], str | None]:
    try:
        proc = _run(["gpspipe", "-w", "-n", str(limit)], timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    messages: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return messages, None


def _service_state(name: str) -> str:
    try:
        proc = _run(["systemctl", "is-active", name], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    if proc.returncode == 0:
        return proc.stdout.strip() or "active"
    return proc.stdout.strip() or proc.stderr.strip() or "inactive"


def _append(results: list[CheckResult], level: str, title: str, detail: str) -> None:
    results.append(CheckResult(level=level, title=title, detail=detail))


def _camera_remote_mode_status() -> tuple[str, str]:
    try:
        detect = _run(["gphoto2", "--auto-detect"], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "fail", f"gphoto2 unavailable: {exc}"

    detected_lines = [
        line.rstrip()
        for line in detect.stdout.splitlines()
        if line.strip() and not line.startswith("Model") and not line.startswith("-")
    ]
    if detect.returncode != 0 or not detected_lines:
        detail = detect.stderr.strip() or detect.stdout.strip() or "no camera detected"
        return "fail", f"No camera detected via gphoto2 --auto-detect ({detail})"

    for config_path in (
        "/main/settings/capturetarget",
        "/main/actions/bulb",
    ):
        config = _run(["gphoto2", "--get-config", config_path], timeout=20)
        if config.returncode == 0:
            return (
                "pass",
                f"remote-control surface available via {config_path} ({detected_lines[0]})",
            )

    for status_path in (
        "/main/status/cameramodel",
        "/main/status/batterylevel",
        "/main/actions/autofocusdrive",
    ):
        config = _run(["gphoto2", "--get-config", status_path], timeout=20)
        if config.returncode == 0:
            return (
                "fail",
                "Camera detected in card-reader/status-only USB mode; "
                "switch the body to USB tether/remote-control mode",
            )

    detail = (
        config.stderr.strip() or config.stdout.strip() or "no supported control probe available"
    )
    return (
        "fail",
        "Camera detected but not in supported USB remote-control mode; "
        f"adapter probe failed: {detail}",
    )


def run_smoke(args: argparse.Namespace) -> int:
    results: list[CheckResult] = []

    gpsd_state = _service_state("gpsd")
    if gpsd_state == "active":
        _append(results, "pass", "gpsd service", "gpsd is active")
    else:
        _append(results, "fail", "gpsd service", f"gpsd is not active ({gpsd_state})")

    props, timedatectl_error = _timedatectl_props()
    ntp_sync = props.get("NTPSynchronized", "no").lower() == "yes"
    rtc_sync = props.get("RTCSynchronized", "no").lower() == "yes"
    timezone = props.get("Timezone", "unknown")
    if timedatectl_error is not None:
        _append(
            results, "fail", "time synchronization", f"timedatectl unavailable: {timedatectl_error}"
        )
    elif ntp_sync or rtc_sync:
        _append(
            results,
            "pass",
            "time synchronization",
            f"timezone={timezone}, ntp={ntp_sync}, rtc={rtc_sync}",
        )
    else:
        _append(
            results,
            "fail",
            "time synchronization",
            f"Neither NTP nor RTC reports synchronized time (timezone={timezone})",
        )

    if args.require_rtc_sync:
        if rtc_sync:
            _append(results, "pass", "rtc sync", "RTC is synchronized")
        else:
            _append(results, "fail", "rtc sync", "RTC synchronization is required but not active")
    elif rtc_sync:
        _append(results, "pass", "rtc visibility", "RTC is synchronized")
    else:
        _append(results, "warn", "rtc visibility", "RTC is not synchronized")

    gps_messages, gpspipe_error = _gpspipe_messages(limit=20)
    if gpspipe_error is not None:
        _append(results, "fail", "gpspipe", f"gpspipe unavailable: {gpspipe_error}")
    device_paths = sorted(
        {
            device.get("path")
            for msg in gps_messages
            if msg.get("class") == "DEVICES"
            for device in msg.get("devices", [])
            if device.get("path")
        }
        | {
            msg.get("path")
            for msg in gps_messages
            if msg.get("class") == "DEVICE" and msg.get("path")
        }
    )
    if gpspipe_error is None and device_paths:
        _append(results, "pass", "gps device", f"gpsd sees device(s): {', '.join(device_paths)}")
    elif gpspipe_error is None:
        _append(results, "fail", "gps device", "gpsd did not report any GPS devices")

    tpv_fix = next(
        (
            msg
            for msg in gps_messages
            if msg.get("class") == "TPV" and int(msg.get("mode", 0)) >= 2 and msg.get("time")
        ),
        None,
    )
    if tpv_fix is not None:
        _append(
            results,
            "pass",
            "gps fix",
            f"mode={tpv_fix.get('mode')} time={tpv_fix.get('time')}",
        )
    elif gpspipe_error is not None:
        _append(results, "fail", "gps fix", f"Could not inspect GPS fix state: {gpspipe_error}")
    elif args.require_gps_fix:
        _append(
            results, "fail", "gps fix", "GPS fix required, but no TPV fix with time was observed"
        )
    else:
        _append(results, "warn", "gps fix", "No TPV fix observed yet; this may be normal indoors")

    try:
        throttled = _run(["vcgencmd", "get_throttled"], timeout=5)
        raw = throttled.stdout.strip()
        undervoltage = False
        if "=" in raw:
            undervoltage = bool(int(raw.split("=", 1)[1], 16) & 0x1)
        if undervoltage:
            _append(results, "warn", "power integrity", f"undervoltage detected ({raw})")
        else:
            _append(results, "pass", "power integrity", raw or "no throttling reported")
    except FileNotFoundError:
        _append(results, "warn", "power integrity", "vcgencmd not available")

    if args.require_camera_remote_mode:
        camera_level, camera_detail = _camera_remote_mode_status()
        _append(results, camera_level, "camera remote mode", camera_detail)

    if args.require_kepler_stack:
        for service_name in ("indiwebmanager", "kepler-node", "kepler-ui"):
            state = _service_state(service_name)
            if state == "active":
                _append(results, "pass", f"service {service_name}", f"{service_name} is active")
            else:
                _append(
                    results,
                    "fail",
                    f"service {service_name}",
                    f"{service_name} is not active ({state})",
                )

        if args.expect_profile == "field-fallback":
            xrdp_state = _service_state("xrdp")
            if xrdp_state == "active":
                _append(results, "pass", "service xrdp", "xrdp is active")
            else:
                _append(results, "fail", "service xrdp", f"xrdp is not active ({xrdp_state})")

        try:
            health = _fetch_json(f"{args.api_base_url}/api/v1/health")
            status = _fetch_json(f"{args.api_base_url}/api/v1/node/status")
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            _append(results, "fail", "kepler api", f"Could not read Kepler API: {exc}")
        else:
            _append(results, "pass", "kepler api", f"health={health.get('status', 'unknown')}")
            time_certainty = status.get("time_certainty") or {}
            trusted = bool(time_certainty.get("trusted"))
            source = time_certainty.get("source", "unknown")
            if trusted:
                _append(results, "pass", "node time certainty", f"trusted source={source}")
            else:
                _append(results, "fail", "node time certainty", f"untrusted source={source}")

            if args.expect_profile:
                install_manifest = status.get("install_manifest") or {}
                manifest_profile = install_manifest.get("bootstrap_profile")
                planner_mode = status.get("planner_mode")
                if manifest_profile == args.expect_profile and planner_mode == args.expect_profile:
                    _append(
                        results,
                        "pass",
                        "planner profile",
                        f"bootstrap_profile={manifest_profile}, planner_mode={planner_mode}",
                    )
                else:
                    _append(
                        results,
                        "fail",
                        "planner profile",
                        (
                            f"Expected profile {args.expect_profile}, got "
                            f"bootstrap_profile={manifest_profile!r}, planner_mode={planner_mode!r}"
                        ),
                    )

    exit_code = 0
    for result in results:
        prefix = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[result.level]
        print(f"[{prefix}] {result.title}: {result.detail}")
        if result.level == "fail":
            exit_code = 1

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8000",
        help="Base URL for the local Kepler API when --require-kepler-stack is used",
    )
    parser.add_argument(
        "--require-gps-fix",
        action="store_true",
        help="Fail if gpspipe does not report a TPV fix with time",
    )
    parser.add_argument(
        "--require-rtc-sync",
        action="store_true",
        help="Fail if timedatectl does not report RTCSynchronized=yes",
    )
    parser.add_argument(
        "--require-kepler-stack",
        action="store_true",
        help="Require kepler-node, kepler-ui, indiwebmanager, and the local API to be active",
    )
    parser.add_argument(
        "--require-camera-remote-mode",
        action="store_true",
        help="Fail unless gphoto2 can detect the camera and read the capturetarget control used by the Kepler adapter",
    )
    parser.add_argument(
        "--expect-profile",
        choices=["headless-node", "field-fallback"],
        help="When requiring the Kepler stack, verify the node reports this bootstrap profile",
    )
    args = parser.parse_args()
    return run_smoke(args)


if __name__ == "__main__":
    sys.exit(main())
