"""CLI entrypoints for local development workflows."""

from datetime import UTC, datetime
from pathlib import Path

import typer

from kepler_node.camera.fuji_focus_assist import (
    FocusAssistRequest,
    FujiFocusAssistRunner,
    MilkyWaySequenceRequest,
    run_milky_way_sequence,
)
from kepler_node.camera.gphoto2 import Gphoto2CameraBackend
from kepler_node.config import Settings

app = typer.Typer(help="Kepler Node development CLI.")


@app.callback()
def main() -> None:
    """Run Kepler Node commands."""


@app.command("info")
def info() -> None:
    """Print the active project paths."""
    settings = Settings()
    typer.echo(f"project_root={settings.project_root}")
    typer.echo(f"data_dir={settings.data_dir}")


@app.command("lab-path")
def lab_path() -> None:
    """Print the private scratch area location."""
    typer.echo(Path("lab").resolve())


@app.command("fuji-focus-assist")
def fuji_focus_assist(
    destination_dir: Path | None = typer.Option(
        None,
        help="Directory for focus-assist artifacts (default: <data_dir>/focus-assist/<timestamp>)",
    ),
    exposure_seconds: float = typer.Option(3.0, help="Exposure time for focus frames."),
    iso: int = typer.Option(3200, help="ISO for focus frames."),
    aperture: float | None = typer.Option(None, help="Optional aperture value to enforce."),
    focus_min_raw: int = typer.Option(45, help="Lower focus bound for this lens posture."),
    focus_max_raw: int = typer.Option(1497, help="Upper focus bound for this lens posture."),
    coarse_step: int = typer.Option(40, help="Primary search step in raw d171 units."),
    fine_step: int = typer.Option(10, help="Refinement step in raw d171 units."),
    min_improvement_fraction: float = typer.Option(
        0.05,
        help="Required fractional improvement over the starting frame.",
    ),
) -> None:
    """Run a pure Kepler Fuji focus-assist search."""

    settings = Settings()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = destination_dir or settings.data_dir / "focus-assist" / timestamp

    camera = Gphoto2CameraBackend(
        gphoto2_bin=settings.gphoto2_binary,
        allow_focus_assist_surface_fallback=True,
    )
    runner = FujiFocusAssistRunner(camera)
    request = FocusAssistRequest(
        destination_dir=artifact_dir,
        exposure_seconds=exposure_seconds,
        iso=iso,
        aperture=aperture,
        focus_min_raw=focus_min_raw,
        focus_max_raw=focus_max_raw,
        coarse_step=coarse_step,
        fine_step=fine_step,
        min_improvement_fraction=min_improvement_fraction,
    )

    typer.echo(f"Artifacts: {artifact_dir}")
    camera.connect()
    try:
        result = runner.run(request)
    finally:
        camera.disconnect()

    typer.echo(
        f"status={result.status} start={result.started_raw} best={result.best_raw} final={result.final_raw}"
    )
    typer.echo(result.summary)
    for sample in result.coarse_samples:
        typer.echo(
            f"coarse raw={sample.raw_position} stars={sample.star_count} "
            f"hfr={sample.hfr_mean or 0:.2f} tenengrad={sample.tenengrad:.2f} "
            f"metric={sample.metric_source} path={sample.image_path}"
        )
    for sample in result.fine_samples:
        typer.echo(
            f"fine raw={sample.raw_position} stars={sample.star_count} "
            f"hfr={sample.hfr_mean or 0:.2f} tenengrad={sample.tenengrad:.2f} "
            f"metric={sample.metric_source} path={sample.image_path}"
        )


@app.command("fuji-milky-way-sequence")
def fuji_milky_way_sequence(
    destination_dir: Path | None = typer.Option(
        None,
        help="Directory for captured frames (default: <data_dir>/captures/milky-way/<timestamp>)",
    ),
    exposure_seconds: float = typer.Option(8.0, help="Exposure time for each frame."),
    iso: int = typer.Option(1600, help="ISO for each frame."),
    aperture: float | None = typer.Option(None, help="Optional aperture value to enforce."),
    frame_count: int = typer.Option(20, help="Number of frames to capture."),
    inter_frame_delay_seconds: float = typer.Option(1.0, help="Delay between frames."),
) -> None:
    """Capture a simple Milky Way sequence to the local data path."""

    settings = Settings()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    capture_dir = destination_dir or settings.data_dir / "captures" / "milky-way" / timestamp

    camera = Gphoto2CameraBackend(
        gphoto2_bin=settings.gphoto2_binary,
        allow_focus_assist_surface_fallback=True,
    )
    request = MilkyWaySequenceRequest(
        destination_dir=capture_dir,
        exposure_seconds=exposure_seconds,
        iso=iso,
        aperture=aperture,
        frame_count=frame_count,
        inter_frame_delay_seconds=inter_frame_delay_seconds,
    )

    typer.echo(f"Capture directory: {capture_dir}")
    camera.connect()
    try:
        frames = run_milky_way_sequence(camera, request)
    finally:
        camera.disconnect()

    for idx, frame in enumerate(frames, start=1):
        typer.echo(f"frame {idx}/{frame_count}: {frame.image_path}")


@app.command("serve")
def serve(
    host: str = typer.Option(None, help="API listen host (default: KEPLER_API_HOST)"),
    port: int = typer.Option(None, help="API listen port (default: KEPLER_API_PORT)"),
) -> None:
    """Start the local Kepler Node REST API server."""
    try:
        import uvicorn
    except ImportError:
        typer.echo(
            "uvicorn is required.  Install the local-api extras: "
            "uv pip install 'kepler-node[local-api]'",
            err=True,
        )
        raise typer.Exit(1)

    try:
        from kepler_node.api._serve import make_dev_app
    except ImportError:
        typer.echo("local API package not found", err=True)
        raise typer.Exit(1)

    settings = Settings()
    api_host = host or settings.api_host
    api_port = port or settings.api_port

    typer.echo(f"Starting Kepler Node API on http://{api_host}:{api_port}")
    uvicorn.run(make_dev_app(), host=api_host, port=api_port)
