#!/usr/bin/env python3
"""Bounded Fuji INDI capture probe for local or remote Pi testing.

This helper replaces ad hoc long-running shell commands with a short, explicit
probe that:

1. Reads the live INDI exposure, capturemode, and capturedelay surfaces.
2. Rejects invalid durations before trying to expose.
3. Optionally restarts the indiwebmanager profile.
4. Forces a known capture posture and bounded upload target.
5. Fires one exposure and polls for a proof file with a hard timeout.

Examples:
    python3 scripts/fuji_capture_probe.py --host danjsiegel@host
    python3 scripts/fuji_capture_probe.py --host danjsiegel@host --restart-profile
    python3 scripts/fuji_capture_probe.py --host danjsiegel@host --duration 8
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NoReturn


DEFAULT_DEVICE = "Kepler Fuji DSLR Fujifilm X-T5"
DEFAULT_PROFILE = "Kepler-Starter-Rig"
DEFAULT_UPLOAD_DIR = "/var/lib/indiwebmanager"
DEFAULT_LOG_PATH = "/tmp/indiserver.log"
DEFAULT_RESTART_TIMEOUT = 60.0


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class ExposureVector:
    minimum: float
    maximum: float
    current: float
    state: str


def info(message: str) -> None:
    print(f"[INFO] {message}")


def ok(message: str) -> None:
    print(f"[PASS] {message}")


def warn(message: str) -> None:
    print(f"[WARN] {message}")


def fail(message: str, *, exit_code: int = 1) -> "NoReturn":
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def run_command(command: list[str], *, timeout: int) -> CommandResult:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class Runner:
    def __init__(self, host: str | None) -> None:
        self.host = host

    def shell(self, script: str, *, timeout: int = 15) -> CommandResult:
        if self.host:
            command = ["ssh", self.host, script]
        else:
            command = ["/bin/sh", "-lc", script]
        return run_command(command, timeout=timeout)


def require_ok(result: CommandResult, context: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        fail(f"{context} failed: {detail}")
    return result.stdout


def fetch_number_vector(runner: Runner, device: str, name: str) -> str:
    script = (
        "printf '<getProperties version=\"1.7\" device=\"{device}\" name=\"{name}\"/>\\n' "
        "| nc -w 4 127.0.0.1 7624 2>/dev/null"
    ).format(device=device.replace('"', '\\"'), name=name)
    return require_ok(runner.shell(script, timeout=10), f"fetch {name}")


def fetch_switch_vector(runner: Runner, device: str, name: str) -> str:
    return fetch_number_vector(runner, device, name)


def parse_exposure_vector(xml_text: str) -> ExposureVector:
    state_match = re.search(r'name="CCD_EXPOSURE"[^>]*state="([^"]+)"', xml_text)
    min_match = re.search(r'name="CCD_EXPOSURE_VALUE"[^>]*min="([^"]+)"', xml_text)
    max_match = re.search(r'name="CCD_EXPOSURE_VALUE"[^>]*max="([^"]+)"', xml_text)
    value_match = re.search(r'<defNumber name="CCD_EXPOSURE_VALUE"[^>]*>\s*([^<\s]+)', xml_text)

    if not (state_match and min_match and max_match and value_match):
        fail("Could not parse CCD_EXPOSURE vector from INDI")

    return ExposureVector(
        minimum=float(min_match.group(1)),
        maximum=float(max_match.group(1)),
        current=float(value_match.group(1)),
        state=state_match.group(1),
    )


def getprop(runner: Runner, prop: str) -> str:
    result = runner.shell(f"indi_getprop {shlex.quote(prop)} 2>/dev/null || true", timeout=10)
    return result.stdout.strip()


def getprop_value(runner: Runner, prop: str) -> str:
    raw = getprop(runner, prop)
    if not raw:
        return ""
    if "=" not in raw:
        return raw
    return raw.rsplit("=", 1)[1].strip()


def fetch_profile_status(runner: Runner) -> str:
    result = runner.shell("curl -sf http://127.0.0.1:8624/api/server/status 2>/dev/null || true", timeout=10)
    return result.stdout.strip()


def fetch_fuji_processes(runner: Runner) -> list[str]:
    script = (
        "ps -eo pid,ppid,comm,args | "
        "grep -E 'indi_kepler_fuji_ccd|indiserver|indi_fuji_focus_bridge|indi_pmc8_telescope' | "
        "grep -v grep || true"
    )
    result = runner.shell(script, timeout=10)
    return [line.rstrip() for line in result.stdout.splitlines() if line.strip()]


def count_processes(process_lines: list[str], command_name: str) -> int:
    # ps -eo pid,ppid,comm,args: the comm field is truncated to 15 chars but the
    # args field always starts with the full binary name.  Match the first token
    # of the args column (column 4) so that wrapper processes like
    # "sh -c indiserver ..." don't inflate the indiserver count.
    pattern = re.compile(rf"^\s*\d+\s+\d+\s+\S+\s+{re.escape(command_name)}(?:\s|$)")
    return sum(1 for line in process_lines if pattern.search(line))


def wait_for_process_counts(
    runner: Runner,
    *,
    expected_fuji: int,
    expected_indiserver: int,
    timeout_seconds: float,
    phase: str,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_processes: list[str] = []

    while time.monotonic() < deadline:
        last_processes = fetch_fuji_processes(runner)
        fuji_count = count_processes(last_processes, "indi_kepler_fuji_ccd")
        indiserver_count = count_processes(last_processes, "indiserver")
        if fuji_count == expected_fuji and indiserver_count == expected_indiserver:
            return
        time.sleep(0.5)

    formatted = "\n".join(last_processes) if last_processes else "<none>"
    fail(
        f"profile {phase} did not reach expected process state "
        f"(want fuji={expected_fuji}, indiserver={expected_indiserver}).\n{formatted}"
    )


def wait_for_indi_ready(
    runner: Runner,
    device: str,
    *,
    timeout_seconds: float,
) -> None:
    """Wait until the INDI device is connected and CCD_EXPOSURE is not in Alert state."""
    deadline = time.monotonic() + timeout_seconds
    last_conn = ""
    last_state = ""

    while time.monotonic() < deadline:
        last_conn = check_connection(runner, device)
        if last_conn != "On":
            time.sleep(0.5)
            continue

        xml = runner.shell(
            "printf '<getProperties version=\"1.7\" device=\"{dev}\" name=\"CCD_EXPOSURE\"/>\\n'"
            " | nc -w 4 127.0.0.1 7624 2>/dev/null".format(dev=device.replace('"', '\\"')),
            timeout=10,
        )
        state_match = re.search(r'name="CCD_EXPOSURE"[^>]*state="([^"]+)"', xml.stdout)
        if state_match:
            last_state = state_match.group(1)
            if last_state != "Alert":
                return
        time.sleep(0.5)

    fail(
        f"INDI device not ready after {timeout_seconds:.0f}s "
        f"(CONNECTION.CONNECT={last_conn or 'missing'}, CCD_EXPOSURE state={last_state or 'missing'})"
    )


def connect_device(runner: Runner, device: str) -> str:
    setprop(runner, f"{device}.CONNECTION.CONNECT=On")
    return check_connection(runner, device)


def diagnose_disconnected_device(runner: Runner, device: str) -> None:
    profile_status = fetch_profile_status(runner)
    if profile_status:
        info(f"indiwebmanager status: {profile_status}")

    connection_vector = getprop(runner, f"{device}.CONNECTION.*")
    if connection_vector:
        info("connection:\n" + connection_vector)

    processes = fetch_fuji_processes(runner)
    if processes:
        info("processes:\n" + "\n".join(processes))
        fuji_count = sum("indi_kepler_fuji_ccd" in line for line in processes)
        if fuji_count > 1:
            warn(f"detected {fuji_count} indi_kepler_fuji_ccd processes; restart the profile before trusting results")


def setprop(runner: Runner, prop_assignment: str) -> None:
    result = runner.shell(f"indi_setprop {shlex.quote(prop_assignment)}", timeout=10)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        fail(f"indi_setprop {prop_assignment} failed: {detail}")


def restart_profile(runner: Runner, profile_name: str) -> None:
    encoded = shlex.quote(profile_name)
    require_ok(
        runner.shell("curl -sf -X POST http://127.0.0.1:8624/api/server/stop >/dev/null", timeout=15),
        "stop indiwebmanager profile",
    )
    wait_for_process_counts(
        runner,
        expected_fuji=0,
        expected_indiserver=0,
        timeout_seconds=DEFAULT_RESTART_TIMEOUT,
        phase="stop",
    )

    require_ok(
        runner.shell(f"curl -sf -X POST http://127.0.0.1:8624/api/server/start/{encoded} >/dev/null", timeout=15),
        "start indiwebmanager profile",
    )
    wait_for_process_counts(
        runner,
        expected_fuji=1,
        expected_indiserver=1,
        timeout_seconds=DEFAULT_RESTART_TIMEOUT,
        phase="start",
    )


def read_log_tail(runner: Runner, log_path: str, lines: int) -> str:
    return require_ok(runner.shell(f"tail -n {lines} {shlex.quote(log_path)}", timeout=10), "read log tail")


def list_probe_files(runner: Runner, upload_dir: str, prefix: str) -> list[str]:
    # Use find -name so the pattern is evaluated by find (not the shell).
    # shlex.quote wraps the pattern in single-quotes which prevents shell glob
    # expansion, making `ls -1 'dir/PROBE_*'` silently return nothing.
    dir_q = shlex.quote(upload_dir)
    name_q = shlex.quote(prefix + "*")
    result = runner.shell(
        f"find {dir_q} -maxdepth 1 -name {name_q} 2>/dev/null || true",
        timeout=10,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def clear_probe_files(runner: Runner, upload_dir: str, prefix: str) -> None:
    dir_q = shlex.quote(upload_dir)
    name_q = shlex.quote(prefix + "*")
    runner.shell(f"find {dir_q} -maxdepth 1 -name {name_q} -delete 2>/dev/null || true", timeout=10)


def check_connection(runner: Runner, device: str) -> str:
    return getprop_value(runner, f'{device}.CONNECTION.CONNECT')


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Fuji INDI capture probe")
    parser.add_argument("--host", help="SSH host for the Pi, e.g. danjsiegel@100.104.51.54")
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--duration", type=float, default=4.0)
    parser.add_argument("--force-capturemode", default="capturemode0")
    parser.add_argument("--upload-mode", choices=["client", "local", "both"], default="both")
    parser.add_argument("--upload-dir", default=DEFAULT_UPLOAD_DIR)
    parser.add_argument("--log-path", default=DEFAULT_LOG_PATH)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--restart-profile", action="store_true")
    parser.add_argument("--connect-if-needed", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--log-lines", type=int, default=40)
    args = parser.parse_args()

    runner = Runner(args.host)
    prefix = f"PROBE_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"

    info(f"device={args.device}")
    if args.host:
        info(f"host={args.host}")

    if args.restart_profile:
        info(f"Restarting indiwebmanager profile {args.profile}")
        restart_profile(runner, args.profile)
        info("Connecting device after restart")
        connect_device(runner, args.device)
        info("Waiting for INDI device to be ready (connection + non-Alert exposure)")
        wait_for_indi_ready(runner, args.device, timeout_seconds=DEFAULT_RESTART_TIMEOUT)

    connection = check_connection(runner, args.device)
    if connection != "On":
        diagnose_disconnected_device(runner, args.device)
        if args.connect_if_needed:
            info("Attempting INDI connection")
            connection = connect_device(runner, args.device)
        if connection != "On":
            action_hint = " rerun with --connect-if-needed or restart the profile" if not args.connect_if_needed else ""
            fail(
                f"camera is not connected in INDI (CONNECTION.CONNECT={connection or 'missing'})."
                f"{action_hint}"
            )
    ok("camera is connected")

    exposure_xml = fetch_number_vector(runner, args.device, "CCD_EXPOSURE")
    exposure = parse_exposure_vector(exposure_xml)
    if exposure.state == "Alert":
        info("CCD_EXPOSURE is Alert; waiting for the device to become ready")
        wait_for_indi_ready(runner, args.device, timeout_seconds=DEFAULT_RESTART_TIMEOUT)
        exposure_xml = fetch_number_vector(runner, args.device, "CCD_EXPOSURE")
        exposure = parse_exposure_vector(exposure_xml)
    info(
        f"CCD_EXPOSURE state={exposure.state} min={exposure.minimum:g} max={exposure.maximum:g} current={exposure.current:g}"
    )

    capturemode = getprop(runner, f"{args.device}.capturemode.*")
    capturedelay = getprop(runner, f"{args.device}.capturedelay.*")
    info("capturemode:\n" + (capturemode or "<missing>"))
    info("capturedelay:\n" + (capturedelay or "<missing>"))

    if args.preflight_only:
        ok("preflight completed")
        return 0

    if args.duration < exposure.minimum:
        fail(
            f"requested duration {args.duration:g}s is below the live INDI minimum {exposure.minimum:g}s",
            exit_code=2,
        )
    if args.duration > exposure.maximum:
        fail(
            f"requested duration {args.duration:g}s exceeds the live INDI maximum {exposure.maximum:g}s",
            exit_code=2,
        )

    if args.force_capturemode:
        info(f"Forcing capturemode to {args.force_capturemode}")
        setprop(runner, f"{args.device}.capturemode.{args.force_capturemode}=On")

    upload_prop = {
        "client": "UPLOAD_CLIENT",
        "local": "UPLOAD_LOCAL",
        "both": "UPLOAD_BOTH",
    }[args.upload_mode]
    setprop(runner, f"{args.device}.UPLOAD_MODE.{upload_prop}=On")
    setprop(runner, f"{args.device}.UPLOAD_SETTINGS.UPLOAD_DIR={args.upload_dir}")
    setprop(runner, f"{args.device}.UPLOAD_SETTINGS.UPLOAD_PREFIX={prefix}XXX")
    clear_probe_files(runner, args.upload_dir, prefix)

    info(f"Starting {args.duration:g}s exposure with prefix {prefix}")
    setprop(runner, f"{args.device}.CCD_EXPOSURE.CCD_EXPOSURE_VALUE={args.duration:g}")

    deadline = time.monotonic() + args.timeout_seconds
    files: list[str] = []
    while time.monotonic() < deadline:
        files = list_probe_files(runner, args.upload_dir, prefix)
        if files:
            break
        time.sleep(args.poll_seconds)

    exposure_after = getprop(runner, f"{args.device}.CCD_EXPOSURE.*")
    capturemode_after = getprop(runner, f"{args.device}.capturemode.*")
    log_tail = read_log_tail(runner, args.log_path, args.log_lines)

    print("[RESULT] files")
    if files:
        for path in files:
            print(path)
        ok(f"captured {len(files)} file(s)")
    else:
        warn("no proof file appeared before timeout")

    print("[RESULT] exposure")
    print(exposure_after or "<missing>")
    print("[RESULT] capturemode")
    print(capturemode_after or "<missing>")
    print("[RESULT] log tail")
    print(log_tail.rstrip())

    if len(files) == 1:
        return 0
    if len(files) > 1:
        return 3
    return 4


if __name__ == "__main__":
    raise SystemExit(main())