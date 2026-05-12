from kepler_node import __version__
from kepler_node.config import Settings


def test_package_version() -> None:
    assert __version__ == "0.1.0"


def test_settings_defaults() -> None:
    settings = Settings()

    assert settings.data_dir.name == "data"
