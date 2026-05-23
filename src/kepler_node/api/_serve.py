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
from kepler_node.agent.broker import IndiWebManagerBrokerBackend
from kepler_node.agent.claw import ClawController
from kepler_node.agent.ekos import DBusEkosAdapter
from kepler_node.agent.node_management import LocalNodeManagementBackend
from kepler_node.agent.session import ClawState, RuntimeSession
from kepler_node.api.app import build_app
from kepler_node.camera.gphoto2 import Gphoto2CameraBackend
from kepler_node.config import Settings
from kepler_node.imaging.astrometry import AstrometryNetSolverBackend
from kepler_node.mount.indi import INDIMountBackend
from kepler_node.storage.filesystem import FilesystemSessionStore


def _initialize_controller_for_api(controller: ClawController) -> ClawController:
    """Advance the pre-session lifecycle for the served API instance.

    The API should not come up stranded in BOOT. On startup, advance through
    the normal pre-session sequence until the controller reaches a stable
    externally visible state such as READY or PAUSED.
    """

    if controller.session.state == ClawState.BOOT:
        controller.boot()
    if controller.session.state == ClawState.DISCOVER:
        controller.discover()
    if controller.session.state == ClawState.CONNECT:
        controller.connect()
    return controller


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
        node_backend=LocalNodeManagementBackend(
            data_root=data_root,
            service_names=settings.managed_service_names,
        ),
        mount_backend=INDIMountBackend(),
        camera_backend=Gphoto2CameraBackend(),
        solver_backend=AstrometryNetSolverBackend(),
        store=FilesystemSessionStore(data_root=data_root),
        authorship_tracker=AuthorshipTracker(),
        verification_dir=verification_dir,
        ekos_adapter=DBusEkosAdapter(),
        broker_backend=IndiWebManagerBrokerBackend(
            host=settings.indiwebmanager_host,
            port=settings.indiwebmanager_port,
            timeout_seconds=settings.indiwebmanager_timeout_seconds,
        ),
    )

    _initialize_controller_for_api(controller)

    return build_app(controller=controller, ekos_output_dir=settings.ekos_output_dir)
