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

    # Phase 2: node-management knobs
    managed_service_names: list[str] = Field(default_factory=lambda: ["indiserver", "gpsd"])
    storage_warning_threshold_bytes: int = 20 * 1024 * 1024 * 1024  # 20 GiB
    storage_critical_threshold_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GiB

    # Phase 2: local binary paths
    gphoto2_binary: str = "gphoto2"
    solve_field_binary: str = "solve-field"

    # Phase 2: INDI server connection
    indiserver_host: str = "localhost"
    indiserver_port: int = 7624
