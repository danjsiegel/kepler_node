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
