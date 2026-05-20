"""EKOS output directory watcher for Kepler frame quality guardrails.

Watches the directory EKOS writes completed frames to, runs frame quality
analysis on each new file, and yields results as an async stream. Designed
to run as an asyncio background task inside the Kepler API lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator, Callable

from kepler_node.imaging.frame_quality import FrameQualityAnalyzer, FrameQualitySession
from kepler_node.imaging.protocols import QualityAnalyzer, QualityCheckResult, QualityClassification

_logger = logging.getLogger(__name__)

# Extensions EKOS and gphoto2 produce that we want to analyze
_FRAME_EXTENSIONS = frozenset(
    {".fits", ".fit", ".fts", ".tiff", ".tif", ".jpg", ".jpeg", ".png", ".raf"}
)


class FrameWatcher:
    """Watches an EKOS output directory for new frames and analyzes each one.

    Polls on a configurable interval (default 2 s). New files are detected by
    comparing the current directory listing against the known set. Each new
    file is analyzed in a thread pool so the event loop stays free.

    Typical usage as a background task::

        watcher = FrameWatcher(output_dir, session=quality_session)
        task = asyncio.create_task(_run_watcher(watcher))
        ...
        watcher.stop()

    Or consumed directly::

        async for path, result in watcher.watch():
            if quality_session.recommendation() == TRIGGER_AUTOFOCUS:
                await ekos.trigger_autofocus()
    """

    def __init__(
        self,
        directory: Path,
        analyzer: QualityAnalyzer | None = None,
        session: FrameQualitySession | None = None,
        *,
        poll_interval_seconds: float = 2.0,
        on_new_frame: Callable[[Path, QualityCheckResult], None] | None = None,
    ) -> None:
        self._directory = directory
        self._analyzer: QualityAnalyzer = analyzer or FrameQualityAnalyzer()
        self._session = session
        self._poll_interval = poll_interval_seconds
        self._on_new_frame = on_new_frame
        self._seen: set[Path] = set()
        self._running = False

    async def watch(self) -> AsyncIterator[tuple[Path, QualityCheckResult]]:
        """Yield (path, result) for each new frame that lands in the directory.

        Runs until ``stop()`` is called or the task is cancelled.
        """
        self._running = True
        self._seen = self._snapshot()
        _logger.info("FrameWatcher started on %s (%d existing files)", self._directory, len(self._seen))

        while self._running:
            await asyncio.sleep(self._poll_interval)
            current = self._snapshot()
            new_files = current - self._seen
            # Mark pre-existing files as seen immediately; new files are only
            # added to _seen after successful analysis so a partial-write or
            # transient analysis failure is retried on the next poll cycle.
            self._seen.update(current - new_files)

            for path in sorted(new_files):
                # Small settle delay — give the writer time to finish flushing
                await asyncio.sleep(0.15)
                try:
                    result = await asyncio.to_thread(self._analyzer.analyze, path)
                except Exception:
                    _logger.exception("frame quality analysis failed for %s", path)
                    continue

                # A load-FAIL means the file could not be opened at all (partial
                # write / transient lock). Treat it as a retriable non-event:
                # do not mark as seen, do not mutate the quality session, do
                # not fire the callback, and do not yield — let the next poll
                # cycle try again once the writer finishes.
                if result.checks.get("load") == QualityClassification.FAIL:
                    _logger.debug("frame %s load failed, will retry on next poll", path.name)
                    continue

                self._seen.add(path)

                if self._session is not None:
                    self._session.add(result)

                if self._on_new_frame is not None:
                    try:
                        self._on_new_frame(path, result)
                    except Exception:
                        _logger.exception("on_new_frame callback raised for %s", path)

                _logger.debug(
                    "frame %s → %s (%s)", path.name, result.overall, result.summary
                )
                yield path, result

    def stop(self) -> None:
        """Signal the watch loop to exit after the current poll cycle."""
        self._running = False
        _logger.info("FrameWatcher stop requested")

    def set_session(self, session: FrameQualitySession | None) -> None:
        """Replace the rolling quality session used for accumulation.

        Call this when the managed session changes so that a new session starts
        with a fresh quality baseline rather than inheriting history from a
        previous session.
        """
        self._session = session

    def _snapshot(self) -> set[Path]:
        """Return the set of frame paths currently in the watched directory."""
        if not self._directory.exists():
            return set()
        return {
            p
            for p in self._directory.rglob("*")
            if p.is_file() and p.suffix.lower() in _FRAME_EXTENSIONS
        }
