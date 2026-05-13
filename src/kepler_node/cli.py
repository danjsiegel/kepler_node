"""CLI entrypoints for local development workflows."""

from pathlib import Path

import typer

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
