"""Typed runtime settings for local development and future deployment."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Project settings loaded from environment variables when needed."""

    model_config = SettingsConfigDict(
        env_prefix="KEPLER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: Path = Field(default_factory=lambda: Path.cwd())
    data_dir: Path = Field(default_factory=lambda: Path.cwd() / "data")

    # Node-management knobs
    managed_service_names: list[str] = Field(
        default_factory=lambda: [
            "indiwebmanager",
            "indiserver",
            "gpsd",
            "kepler-node",
            "xrdp",
        ]
    )
    storage_warning_threshold_bytes: int = 20 * 1024 * 1024 * 1024  # 20 GiB
    storage_critical_threshold_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GiB

    # Local binary paths
    gphoto2_binary: str = "gphoto2"
    solve_field_binary: str = "solve-field"
    siril_binary: str = "siril-cli"

    # INDI server connection
    indiserver_host: str = "localhost"
    indiserver_port: int = 7624

    # INDI broker / semaphore (indiwebmanager)
    indiwebmanager_host: str = "localhost"
    indiwebmanager_port: int = 8624
    indiwebmanager_timeout_seconds: float = 3.0

    # Local API server
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Ekos output directory: the directory Ekos lands completed frames into.
    # When set, the API lifespan starts a FrameWatcher loop that ingests each
    # newly landed frame into the rolling quality session and feeds the
    # intervention policy engine.  Leave None to disable the watcher.
    ekos_output_dir: Path | None = Field(default=None)
