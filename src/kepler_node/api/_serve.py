"""Bootstrap module for the Kepler Node local development server.

Wires the real hardware adapters and a fresh RuntimeSession into a
ClawController, then returns a FastAPI application via ``build_app()``.

Usage (via the CLI)::

    uv run kepler-node serve

or directly::

    uvicorn kepler_node.api._serve:make_dev_app --factory
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from kepler_node.agent.authorship import AuthorshipTracker
from kepler_node.agent.claw import ClawController
from kepler_node.agent.node_management import LocalNodeManagementBackend
from kepler_node.agent.session import RuntimeSession
from kepler_node.api.app import build_app
from kepler_node.camera.gphoto2 import Gphoto2CameraBackend
from kepler_node.config import Settings
from kepler_node.imaging.astrometry import AstrometryNetSolverBackend
from kepler_node.mount.indi import INDIMountBackend
from kepler_node.storage.filesystem import FilesystemSessionStore


def make_dev_app() -> FastAPI:
    """Create a FastAPI application bound to the local hardware adapters.

    Reads connection settings from ``Settings`` so environment variables
    (``KEPLER_DATA_DIR``, etc.) override defaults at runtime.
    """
    settings = Settings()
    data_root: Path = settings.data_dir
    data_root.mkdir(parents=True, exist_ok=True)

    verification_dir = data_root / "verify"
    verification_dir.mkdir(parents=True, exist_ok=True)

    controller = ClawController(
        session=RuntimeSession(),
        node_backend=LocalNodeManagementBackend(data_root=data_root),
        mount_backend=INDIMountBackend(),
        camera_backend=Gphoto2CameraBackend(),
        solver_backend=AstrometryNetSolverBackend(),
        store=FilesystemSessionStore(data_root=data_root),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=verification_dir,
    )

    return build_app(controller=controller)
