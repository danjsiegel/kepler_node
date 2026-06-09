from __future__ import annotations

from pathlib import Path

from kepler_node import config


def test_default_data_dir_prefers_nvme_mount(monkeypatch) -> None:
    class FakePath(type(Path())):
        def exists(self) -> bool:  # type: ignore[override]
            return str(self) in {"/media/nvme", "/data/kepler"}

        def is_mount(self) -> bool:  # type: ignore[override]
            return str(self) == "/media/nvme"

    monkeypatch.setattr(config, "Path", FakePath)

    assert config._default_data_dir() == FakePath("/media/nvme/kepler")


def test_default_data_dir_falls_back_to_legacy_data_dir(monkeypatch) -> None:
    class FakePath(type(Path())):
        def exists(self) -> bool:  # type: ignore[override]
            return str(self) == "/data/kepler"

        def is_mount(self) -> bool:  # type: ignore[override]
            return False

    monkeypatch.setattr(config, "Path", FakePath)

    assert config._default_data_dir() == FakePath("/data/kepler")