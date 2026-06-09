"""Thin httpx-based client for the Kepler Node local API.

This module is the only place the Streamlit UI (and tests) need to
import the API response models.  All state-change decisions remain in
the API/controller layer; the client is purely a translation helper.
"""

from __future__ import annotations

from typing import Any

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "httpx is required for the Kepler UI client.  Install it with: uv pip install httpx"
    ) from exc


class KeplerApiClient:
    """Thin wrapper around the Kepler Node local HTTP API.

    All methods return raw dicts so the Streamlit layer does not depend on
    the API Pydantic models at runtime.  None is returned when the server
    responds with a 200 null body (e.g. no active session).
    """

    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)

    # ------------------------------------------------------------------ #
    # Health and status                                                    #
    # ------------------------------------------------------------------ #

    def get_health(self) -> dict[str, Any]:
        """GET /api/v1/health"""
        return self._get("/api/v1/health")

    def get_node_status(self) -> dict[str, Any]:
        """GET /api/v1/node/status"""
        return self._get("/api/v1/node/status")

    def get_readiness(self) -> dict[str, Any]:
        """GET /api/v1/readiness"""
        return self._get("/api/v1/readiness")

    def get_planner_mode(self) -> dict[str, Any]:
        """GET /api/v1/planner-mode — active planner mode from the install manifest."""
        return self._get("/api/v1/planner-mode")

    def post_time_confirm(self, confirmed_at_iso: str) -> dict[str, Any]:
        """POST /api/v1/time/confirm — apply an operator-confirmed timestamp."""
        return self._post("/api/v1/time/confirm", body={"confirmed_at": confirmed_at_iso})

    def post_calibrate(self) -> dict[str, Any]:
        """POST /api/v1/calibrate — transition to calibrate state."""
        return self._post("/api/v1/calibrate")

    def get_widefield_recommendations(
        self,
        *,
        focal_length_mm: float | None = None,
        aperture: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if focal_length_mm is not None:
            params["focal_length_mm"] = focal_length_mm
        if aperture is not None:
            params["aperture"] = aperture
        return self._get("/api/v1/widefield/recommendations", params=params)

    def post_focus_assist(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/v1/widefield/focus-assist", body=body)

    def post_widefield_condition_check(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/v1/widefield/evaluate", body=body)

    # ------------------------------------------------------------------ #
    # Session state                                                        #
    # ------------------------------------------------------------------ #

    def get_session_current(self) -> dict[str, Any] | None:
        """GET /api/v1/session/current — returns None when no session is active."""
        return self._get_nullable("/api/v1/session/current")

    def get_session_state(self) -> dict[str, Any] | None:
        """GET /api/v1/session/current/state — lightweight polling view."""
        return self._get_nullable("/api/v1/session/current/state")

    # ------------------------------------------------------------------ #
    # Session actions                                                      #
    # ------------------------------------------------------------------ #

    def post_session_stop(self) -> dict[str, Any]:
        """POST /api/v1/session/stop"""
        return self._post("/api/v1/session/stop")

    def post_session_pause(self) -> dict[str, Any]:
        """POST /api/v1/session/pause"""
        return self._post("/api/v1/session/pause")

    def post_session_resume(self) -> dict[str, Any]:
        """POST /api/v1/session/resume"""
        return self._post("/api/v1/session/resume")

    def post_camera_recover(self) -> dict[str, Any]:
        """POST /api/v1/camera/recover"""
        return self._post("/api/v1/camera/recover")

    def post_session_release_control(self) -> dict[str, Any]:
        """POST /api/v1/session/release-control"""
        return self._post("/api/v1/session/release-control")

    def post_session_acknowledge_complete(self) -> dict[str, Any]:
        """POST /api/v1/session/acknowledge-complete"""
        return self._post("/api/v1/session/acknowledge-complete")

    def post_session_clear_failure(self) -> dict[str, Any]:
        """POST /api/v1/session/clear-failure"""
        return self._post("/api/v1/session/clear-failure")

    def post_session_attach(self) -> dict[str, Any]:
        """POST /api/v1/session/attach — attach supervision to an Ekos-managed session.

        Transitions the node from READY → EKOS_WAIT and locks supervisory control.
        422 when supervision blockers are present (missing profile, Ekos unavailable, etc.).
        409 when the node is not in the ready state.
        """
        return self._post("/api/v1/session/attach")

    # ------------------------------------------------------------------ #
    # Review                                                               #
    # ------------------------------------------------------------------ #

    def get_session_frames(
        self,
        *,
        limit: int = 50,
        before_frame_id: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/session/current/frames"""
        params: dict[str, Any] = {"limit": limit}
        if before_frame_id is not None:
            params["before_frame_id"] = before_frame_id
        return self._get("/api/v1/session/current/frames", params=params)

    def get_session_artifacts(self) -> dict[str, Any]:
        """GET /api/v1/session/current/artifacts"""
        return self._get("/api/v1/session/current/artifacts")

    def get_session_outcome(self) -> dict[str, Any] | None:
        """GET /api/v1/session/current/outcome — None when session is not terminal."""
        return self._get_nullable("/api/v1/session/current/outcome")

    def get_session_intervention(self) -> dict[str, Any] | None:
        """GET /api/v1/session/current/intervention — None when no session is supervised."""
        return self._get_nullable("/api/v1/session/current/intervention")

    def get_session_events(
        self,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/session/current/events"""
        params: dict[str, Any] = {"limit": limit}
        if before_sequence is not None:
            params["before_sequence"] = before_sequence
        return self._get("/api/v1/session/current/events", params=params)

    # ------------------------------------------------------------------ #
    # Equipment profiles                                                   #
    # ------------------------------------------------------------------ #

    def get_equipment_profiles(self) -> dict[str, Any]:
        """GET /api/v1/equipment/profiles"""
        return self._get("/api/v1/equipment/profiles")

    def get_equipment_profile(self, profile_id: str) -> dict[str, Any]:
        """GET /api/v1/equipment/profiles/{profile_id}"""
        return self._get(f"/api/v1/equipment/profiles/{profile_id}")

    def post_equipment_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/equipment/profiles"""
        return self._post("/api/v1/equipment/profiles", body=body)

    def put_equipment_profile(self, profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """PUT /api/v1/equipment/profiles/{profile_id}"""
        resp = self._client.put(f"/api/v1/equipment/profiles/{profile_id}", json=body)
        resp.raise_for_status()
        return resp.json()

    def post_equipment_profile_select(self, profile_id: str) -> dict[str, Any]:
        """POST /api/v1/equipment/profiles/{profile_id}/select"""
        return self._post(f"/api/v1/equipment/profiles/{profile_id}/select")

    # ------------------------------------------------------------------ #
    # Target intake                                                        #
    # ------------------------------------------------------------------ #

    def get_target_current(self) -> dict[str, Any] | None:
        """GET /api/v1/target/current — None when no target staged."""
        return self._get_nullable("/api/v1/target/current")

    def post_target(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/target"""
        return self._post("/api/v1/target", body=body)

    def delete_target_current(self) -> dict[str, Any]:
        """DELETE /api/v1/target/current"""
        resp = self._client.delete("/api/v1/target/current")
        resp.raise_for_status()
        return resp.json()

    def post_session_start(self) -> dict[str, Any]:
        """POST /api/v1/session/start"""
        return self._post("/api/v1/session/start")

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _get_nullable(self, path: str) -> dict[str, Any] | None:
        resp = self._client.get(path)
        resp.raise_for_status()
        body = resp.json()
        return body  # may be None if server returned JSON null

    def _post(self, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> "KeplerApiClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
