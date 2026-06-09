"""Minimal headless Siril stack runner for widefield Milky Way lights."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SirilStackRequest:
    lights_dir: Path
    output_dir: Path
    sequence_name: str = "light"
    stacked_name: str = "milky_way_stacked"
    sigma_low: float = 3.0
    sigma_high: float = 3.0


@dataclass(slots=True)
class SirilStackResult:
    stacked_path: Path
    log: str
    working_dir: Path


def resolve_siril_binary(preferred: str = "siril-cli") -> str:
    for candidate in (preferred, "siril-cli", "siril"):
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError("Siril is not installed or not on PATH")


def build_milky_way_stack_script(request: SirilStackRequest) -> str:
    process_dir = request.output_dir / "process"
    script_lines = [
        "requires 1.0.0",
        f'cd "{request.lights_dir}"',
        f'convertraw {request.sequence_name} -out="{process_dir}"',
        f'cd "{process_dir}"',
        f'register {request.sequence_name} -2pass',
        f'seqapplyreg {request.sequence_name} -framing=min',
        (
            f'stack r_{request.sequence_name} rej w {request.sigma_low:g} {request.sigma_high:g} '
            f'-norm=addscale -output_norm -out={request.stacked_name}'
        ),
        "close",
    ]
    return "\n".join(script_lines) + "\n"


def run_milky_way_stack(
    request: SirilStackRequest,
    *,
    siril_binary: str = "siril-cli",
) -> SirilStackResult:
    if not request.lights_dir.exists():
        raise RuntimeError(f"lights directory does not exist: {request.lights_dir}")

    request.output_dir.mkdir(parents=True, exist_ok=True)
    process_dir = request.output_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)

    binary = resolve_siril_binary(siril_binary)
    script_text = build_milky_way_stack_script(request)
    log_path = request.output_dir / "siril-stack.log"

    proc = subprocess.run(
        [binary, "-d", str(request.output_dir), "-s", "-"],
        input=script_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    log_path.write_text(log, encoding="utf-8")

    stacked_candidates = [
        process_dir / f"{request.stacked_name}.fit",
        process_dir / f"{request.stacked_name}.fits",
        request.output_dir / f"{request.stacked_name}.fit",
        request.output_dir / f"{request.stacked_name}.fits",
    ]
    for candidate in stacked_candidates:
        if candidate.exists():
            return SirilStackResult(stacked_path=candidate, log=log, working_dir=request.output_dir)

    detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
    raise RuntimeError(f"Siril stacking failed: {detail}")