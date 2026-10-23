"""Podcast service — feed refresh, downloads, position saving.

Runs background threads for network operations, emits Qt signals
for UI updates.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

import httpx

from quaver.core.library import LibraryDB
from quaver.core.podcast import (
    Episode,
    Podcast,
    PodcastDB,
    _ART_CACHE_DIR,
    download_episode,
    parse_feed,
)


def _download_artwork(podcast_id: int, url: str) -> None:
    """Download podcast artwork to the cache directory."""
    try:
        _ART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        # Detect format from content type or default to jpg.
        ext = ".jpg"
        ct = resp.headers.get("content-type", "")
        if "png" in ct:
            ext = ".png"
        path = _ART_CACHE_DIR / f"{podcast_id}{ext}"
        path.write_bytes(resp.content)
    except Exception:
        pass  # Non-critical — feed list will show placeholder.


class _RefreshWorker(QThread):
    """Refresh one or all podcast feeds in a background thread.

    Fetches RSS, upserts episodes, and applies retention cleanup (deleting
    old files). Does NOT download episodes — emits `downloads_needed` with
    a list of episode IDs so the service can queue them as separate workers.
    """

    progress = Signal(str)  # status message
    finished = Signal()
    downloads_needed = Signal(list)  # list of episode IDs to download
    error = Signal(str)

    def __init__(
        self, pdb: PodcastDB, podcast_id: int | None = None, parent=None
    ):
        super().__init__(parent)
        self._pdb = pdb
        self._podcast_id = podcast_id  # None = refresh all

    def run(self):
        try:
            if self._podcast_id is not None:
                podcasts = [self._pdb.get_podcast(self._podcast_id)]
                podcasts = [p for p in podcasts if p]
            else:
                podcasts = self._pdb.get_all_podcasts()

            needed: list[int] = []

            for podcast in podcasts:
                self.progress.emit(f"Refreshing {podcast.title}…")
                try:
                    title, episodes, artwork_url = parse_feed(podcast.feed_url)
                    self._pdb.upsert_episodes_batch(podcast.id, episodes)
                    self._pdb.update_last_fetched(podcast.id)
                    if artwork_url and artwork_url != podcast.artwork_url:
                        self._pdb.update_artwork_url(podcast.id, artwork_url)
                        _download_artwork(podcast.id, artwork_url)

                    # Apply retention: clean up old files, collect IDs to download.
                    needed.extend(self._apply_retention(podcast))
                except Exception as e:
                    self.error.emit(f"{podcast.title}: {e}")

            if needed:
                self.downloads_needed.emit(needed)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))

    def _apply_retention(self, podcast: Podcast) -> list[int]:
        """Clean up old downloads and return episode IDs that need downloading."""
        all_episodes = self._pdb.get_episodes(podcast.id)
        retain = podcast.retain_episodes
        to_download: list[int] = []

        for i, ep in enumerate(all_episodes):
            within_retention = i < retain

            if within_retention and podcast.auto_download:
                if not ep.downloaded and ep.url:
                    to_download.append(ep.id)
            elif not within_retention and ep.downloaded:
                # Outside retention window — delete the file.
                self._pdb.delete_episode_file(ep.id)

        return to_download


class _DownloadWorker(QThread):
    """Download a single episode in the background."""

    finished = Signal(int, str)  # episode_id, local_path
    progress = Signal(int, int, int)  # episode_id, bytes_done, bytes_total
    error = Signal(int, str)  # episode_id, error message

    def __init__(self, episode_id: int, url: str, parent=None):
        super().__init__(parent)
        self._episode_id = episode_id
        self._url = url

    def run(self):
        import time as _time
        last_emit = 0.0

        def _throttled_progress(done: int, total: int) -> None:
            nonlocal last_emit
            now = _time.monotonic()
            # Emit at most every 0.5 seconds to avoid flooding the UI.
            if now - last_emit >= 0.5 or done >= total:
                last_emit = now
                self.progress.emit(self._episode_id, done, total)

        try:
            local = download_episode(self._url, on_progress=_throttled_progress)
            self.finished.emit(self._episode_id, local)
        except Exception as e:
            self.error.emit(self._episode_id, str(e))


class _AddPodcastWorker(QThread):
    """Add a podcast (RSS fetch + DB insert + artwork) in the background."""

    finished = Signal()
    error = Signal(str)

    def __init__(
        self, pdb: PodcastDB, feed_url: str,
        default_retain: int = 2, default_refresh_hours: float = 24.0,
        parent=None,
    ):
        super().__init__(parent)
        self._pdb = pdb
        self._feed_url = feed_url
        self._default_retain = default_retain
        self._default_refresh_hours = default_refresh_hours

    def run(self):
        try:
            artwork_url = None
            try:
                title, episodes, artwork_url = parse_feed(self._feed_url)
            except Exception:
                title = self._feed_url
                episodes = []

            podcast_id = self._pdb.add_podcast(title, self._feed_url, artwork_url)
            if not podcast_id:
                self.finished.emit()
                return

            if artwork_url:
                _download_artwork(podcast_id, artwork_url)

            # Apply config defaults for new podcast.
            self._pdb.update_podcast_settings(
                podcast_id,
                auto_download=False,
                auto_delete_played=False,
                retain_episodes=self._default_retain,
                refresh_interval_hours=self._default_refresh_hours,
            )

            self._pdb.upsert_episodes_batch(podcast_id, episodes)
            self._pdb.update_last_fetched(podcast_id)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class _DeleteWorker(QThread):
    """Delete episode files in the background."""

    finished = Signal()

    def __init__(self, pdb: PodcastDB, episode_ids: list[int], parent=None):
        super().__init__(parent)
        self._pdb = pdb
        self._ids = episode_ids

    def run(self):
        for eid in self._ids:
            try:
                self._pdb.delete_episode_file(eid)
            except Exception:
                pass
        self.finished.emit()


class _RemovePodcastWorker(QThread):
    """Remove a podcast and all its files in the background."""

    finished = Signal()

    def __init__(self, pdb: PodcastDB, podcast_id: int, parent=None):
        super().__init__(parent)
        self._pdb = pdb
        self._podcast_id = podcast_id

    def run(self):
        for ep in self._pdb.get_episodes(self._podcast_id):
            if ep.local_path:
                try:
                    Path(ep.local_path).unlink(missing_ok=True)
                except Exception:
                    pass
        self._pdb.remove_podcast(self._podcast_id)
        self.finished.emit()


class PodcastService(QObject):
    """Coordinates podcast operations between core and UI."""

    feeds_updated = Signal()
    episodes_updated = Signal(int)  # podcast_id
    download_complete = Signal(int)  # episode_id
    download_progress = Signal(int, int, int)  # episode_id, bytes_done, bytes_total
    download_error = Signal(int, str)  # episode_id, error
    refresh_status = Signal(str)  # status message
    refresh_finished = Signal()

    def __init__(self, db: LibraryDB, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._pdb = PodcastDB(db)
        self._refresh_worker: _RefreshWorker | None = None
        self._download_workers: list[QThread] = []

        # Auto-refresh timer — checks every hour whether any podcast
        # is due for refresh based on its individual interval.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(60 * 60 * 1000)  # 1 hour check cycle
        self._refresh_timer.timeout.connect(self._check_due_refreshes)

        # Position save timer (10 seconds).
        self._position_save_timer = QTimer(self)
        self._position_save_timer.setInterval(10_000)

        # Current podcast episode being played.
        self._current_episode_id: int | None = None

    @property
    def pdb(self) -> PodcastDB:
        return self._pdb

    def _cleanup_workers(self) -> None:
        """Remove finished workers and schedule them for deletion."""
        alive = []
        for w in self._download_workers:
            if w.isRunning():
                alive.append(w)
            else:
                w.deleteLater()
        self._download_workers = alive

    # ------------------------------------------------------------------
    # Feed management
    # ------------------------------------------------------------------

    def add_podcast(self, feed_url: str) -> None:
        """Add a podcast by URL in the background."""
        from quaver.core.config import load_config
        config = load_config()
        worker = _AddPodcastWorker(
            self._pdb, feed_url,
            default_retain=config.podcast_default_retain,
            default_refresh_hours=float(config.podcast_default_refresh_hours),
            parent=self,
        )
        worker.finished.connect(lambda: self.feeds_updated.emit())
        worker.finished.connect(self._cleanup_workers)
        worker.error.connect(lambda e: self.refresh_status.emit(e))
        self._download_workers.append(worker)
        worker.start()

    def remove_podcast(self, podcast_id: int) -> None:
        """Remove a podcast and clean up files in the background."""
        worker = _RemovePodcastWorker(self._pdb, podcast_id, parent=self)
        worker.finished.connect(lambda: self.feeds_updated.emit())
        worker.finished.connect(self._cleanup_workers)
        self._download_workers.append(worker)
        worker.start()

    def refresh_all(self) -> None:
        """Refresh all feeds in the background."""
        if self._refresh_worker and self._refresh_worker.isRunning():
            return
        self._refresh_worker = self._make_refresh_worker()
        self._refresh_worker.start()

    def refresh_podcast(self, podcast_id: int) -> None:
        """Refresh a single feed in the background."""
        if self._refresh_worker and self._refresh_worker.isRunning():
            return
        self._refresh_worker = self._make_refresh_worker(podcast_id)
        self._refresh_worker.start()

    def _make_refresh_worker(self, podcast_id: int | None = None) -> _RefreshWorker:
        worker = _RefreshWorker(self._pdb, podcast_id, parent=self)
        worker.progress.connect(self.refresh_status.emit)
        worker.finished.connect(self._on_refresh_done)
        worker.downloads_needed.connect(self._on_downloads_needed)
        worker.error.connect(lambda e: self.refresh_status.emit(e))
        return worker

    def _on_refresh_done(self) -> None:
        self.feeds_updated.emit()
        self.refresh_finished.emit()

    def _on_downloads_needed(self, episode_ids: list[int]) -> None:
        """Queue downloads for episodes identified by retention policy."""
        for eid in episode_ids:
            self.download_episode(eid)

    def _check_due_refreshes(self) -> None:
        """Check which podcasts are due for refresh based on their
        individual refresh_interval_hours setting."""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        due_ids: list[int] = []

        for podcast in self._pdb.get_all_podcasts():
            if not podcast.last_fetched:
                due_ids.append(podcast.id)
                continue
            try:
                last = datetime.datetime.fromisoformat(podcast.last_fetched)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=datetime.timezone.utc)
                hours_since = (now - last).total_seconds() / 3600
                if hours_since >= podcast.refresh_interval_hours:
                    due_ids.append(podcast.id)
            except (ValueError, TypeError):
                due_ids.append(podcast.id)

        if due_ids:
            self.refresh_all()

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------

    def download_episode(self, episode_id: int) -> None:
        ep = self._pdb.get_episode(episode_id)
        if not ep or ep.downloaded:
            return
        worker = _DownloadWorker(episode_id, ep.url, parent=self)
        worker.finished.connect(self._on_download_done)
        worker.finished.connect(self._cleanup_workers)
        worker.progress.connect(self.download_progress.emit)
        worker.error.connect(self.download_error.emit)
        self._download_workers.append(worker)
        worker.start()

    def _on_download_done(self, episode_id: int, local_path: str) -> None:
        self._pdb.set_episode_downloaded(episode_id, local_path)
        self.download_complete.emit(episode_id)

    def delete_episode(self, episode_id: int) -> None:
        """Delete episode file in the background."""
        worker = _DeleteWorker(self._pdb, [episode_id], parent=self)
        worker.finished.connect(lambda: self.download_complete.emit(episode_id))
        worker.finished.connect(self._cleanup_workers)
        self._download_workers.append(worker)
        worker.start()

    # ------------------------------------------------------------------
    # Playback position tracking
    # ------------------------------------------------------------------

    def start_tracking_position(
        self, episode_id: int, get_position_ms: callable
    ) -> None:
        """Start saving playback position every 10 seconds."""
        self._current_episode_id = episode_id
        self._get_position = get_position_ms

        self._position_save_timer.timeout.disconnect() if self._position_save_timer.receivers(
            self._position_save_timer.timeout
        ) else None
        self._position_save_timer.timeout.connect(self._save_position)
        self._position_save_timer.start()

    def stop_tracking_position(self, save_final: bool = True) -> None:
        """Stop position saving, optionally saving one final time.

        Pass ``save_final=False`` when the engine has already moved on to
        a different track/state — querying it then would overwrite the
        episode's resume position with the new track's position.
        """
        self._position_save_timer.stop()
        if save_final:
            self._save_position()
        self._current_episode_id = None

    def mark_played(self, episode_id: int) -> None:
        self._pdb.set_episode_played(episode_id, True)
        # Auto-delete if configured.
        ep = self._pdb.get_episode(episode_id)
        if ep:
            podcast = self._pdb.get_podcast(ep.podcast_id)
            if podcast and podcast.auto_delete_played:
                self._pdb.delete_episode_file(episode_id)

    def _save_position(self) -> None:
        if self._current_episode_id and hasattr(self, "_get_position"):
            pos = self._get_position()
            self._pdb.save_playback_position(self._current_episode_id, pos)

    # ------------------------------------------------------------------
    # Timer management
    # ------------------------------------------------------------------

    def start_auto_refresh(self) -> None:
        self._refresh_timer.start()
        # Also do an immediate check on startup.
        self._check_due_refreshes()

    def shutdown(self) -> None:
        self._refresh_timer.stop()
        self._position_save_timer.stop()
        self._save_position()
        for w in self._download_workers:
            w.quit()
            w.wait(2000)

    # ------------------------------------------------------------------
    # Queries (convenience)
    # ------------------------------------------------------------------

    def get_all_podcasts(self) -> list[Podcast]:
        return self._pdb.get_all_podcasts()

    def get_episodes(self, podcast_id: int, limit: int = 0, offset: int = 0) -> list[Episode]:
        return self._pdb.get_episodes(podcast_id, limit=limit, offset=offset)

    def get_episode_count(self, podcast_id: int) -> int:
        return self._pdb.get_episode_count(podcast_id)

    def get_episode(self, episode_id: int) -> Episode | None:
        return self._pdb.get_episode(episode_id)

    def update_settings(
        self,
        podcast_id: int,
        auto_download: bool,
        auto_delete_played: bool,
        retain_episodes: int = 2,
        refresh_interval_hours: float = 24.0,
    ) -> None:
        self._pdb.update_podcast_settings(
            podcast_id, auto_download, auto_delete_played,
            retain_episodes, refresh_interval_hours,
        )
