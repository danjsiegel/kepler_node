from typer.testing import CliRunner

from kepler_node.cli import app


def test_cli_help() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Kepler Node development CLI." in result.stdout


def test_cli_info() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0
    assert "project_root=" in result.stdout
    assert "data_dir=" in result.stdout