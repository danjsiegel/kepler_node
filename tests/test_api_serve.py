from __future__ import annotations

from types import SimpleNamespace

from kepler_node.agent.session import ClawState
from kepler_node.api._serve import _initialize_controller_for_api


class FakeController:
    def __init__(self, state: ClawState) -> None:
        self.session = SimpleNamespace(state=state)
        self.calls: list[str] = []

    def boot(self):
        self.calls.append("boot")
        self.session.state = ClawState.DISCOVER

    def discover(self):
        self.calls.append("discover")
        self.session.state = ClawState.CONNECT

    def connect(self):
        self.calls.append("connect")
        self.session.state = ClawState.READY


class PausingController(FakeController):
    def discover(self):
        self.calls.append("discover")
        self.session.state = ClawState.PAUSED


def test_initialize_controller_for_api_advances_boot_to_ready() -> None:
    controller = FakeController(ClawState.BOOT)

    result = _initialize_controller_for_api(controller)

    assert result is controller
    assert controller.calls == ["boot", "discover", "connect"]
    assert controller.session.state == ClawState.READY


def test_initialize_controller_for_api_stops_at_paused() -> None:
    controller = PausingController(ClawState.BOOT)

    _initialize_controller_for_api(controller)

    assert controller.calls == ["boot", "discover"]
    assert controller.session.state == ClawState.PAUSED
