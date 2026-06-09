from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from kepler_node.imaging.siril_stack import (
    SirilStackRequest,
    build_milky_way_stack_script,
    run_milky_way_stack,
)


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_build_milky_way_stack_script_contains_expected_steps(tmp_path: Path) -> None:
    request = SirilStackRequest(lights_dir=tmp_path / "lights", output_dir=tmp_path / "stack")

    script = build_milky_way_stack_script(request)

    assert "convertraw light" in script
    assert "register light -2pass" in script
    assert "seqapplyreg light -framing=min" in script
    assert "stack r_light rej w 3 3" in script


def test_run_milky_way_stack_returns_stacked_file(tmp_path: Path) -> None:
    lights_dir = tmp_path / "lights"
    lights_dir.mkdir()
    (lights_dir / "frame1.raf").write_bytes(b"RAF")
    output_dir = tmp_path / "stack"

    def fake_run(cmd: list[str], **_: object) -> MagicMock:
        process_dir = output_dir / "process"
        process_dir.mkdir(parents=True, exist_ok=True)
        (process_dir / "milky_way_stacked.fit").write_bytes(b"FIT")
        return _proc(stdout="done", returncode=0)

    with patch("shutil.which", return_value="/usr/bin/siril-cli"), patch("subprocess.run", side_effect=fake_run):
        result = run_milky_way_stack(
            SirilStackRequest(lights_dir=lights_dir, output_dir=output_dir)
        )

    assert result.stacked_path.name == "milky_way_stacked.fit"
    assert result.stacked_path.exists()