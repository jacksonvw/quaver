"""YouTube service — channel management, stream resolution, downloads.

Runs background threads for network operations, emits Qt signals
for UI updates.  Follows PodcastService patterns exactly.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

import httpx

from quaver.core.library import LibraryDB
from quaver.core.youtube import (
    Channel,
    Video,
    YouTubeDB,
    _THUMB_CACHE_DIR,
    download_video,
    fetch_channel_info,
    fetch_channel_videos,
    fetch_subscriptions,
    resolve_stream_url,
    search_youtube,
)

log = logging.getLogger(__name__)


def _download_channel_thumbnail(channel_db_id: int, url: str) -> None:
    """Download channel thumbnail to the cache directory."""
    try:
        _THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        full_url = url if url.startswith("http") else f"https:{url}"
        resp = httpx.get(full_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        ext = ".jpg"
        ct = resp.headers.get("content-type", "")
        if "png" in ct:
            ext = ".png"
        path = _THUMB_CACHE_DIR / f"channel_{channel_db_id}{ext}"
        path.write_bytes(resp.content)
    except Exception:
        pass  # Non-critical — channel list will show placeholder.


# ------------------------------------------------------------------
# Background workers
# ------------------------------------------------------------------


class _AddChannelWorker(QThread):
    """Add a channel (fetch info + initial videos) in the background."""

    finished = Signal()
    error = Signal(str)

    def __init__(
        self,
        ydb: YouTubeDB,
        channel_url: str,
        cookies_browser: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._ydb = ydb
        self._channel_url = channel_url
        self._cookies = cookies_browser

    def run(self):
        try:
            info = fetch_channel_info(self._channel_url, self._cookies)
            channel_db_id = self._ydb.add_channel(
                channel_id=info["channel_id"],
                title=info["title"],
                thumbnail_url=info.get("thumbnail_url"),
            )
            if not channel_db_id:
                self.finished.emit()
                return

            # Download channel thumbnail.
            thumb_url = info.get("thumbnail_url")
            if thumb_url:
                _download_channel_thumbnail(channel_db_id, thumb_url)

            # Fetch initial batch of videos.
            videos = fetch_channel_videos(
                self._channel_url, limit=30,
                cookies_browser=self._cookies,
                channel_id=info["channel_id"],
            )
            self._ydb.upsert_videos_batch(channel_db_id, videos)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class _RemoveChannelWorker(QThread):
    """Remove a channel and clean up downloaded files."""

    finished = Signal()

    def __init__(self, ydb: YouTubeDB, channel_db_id: int, parent=None):
        super().__init__(parent)
        self._ydb = ydb
        self._channel_db_id = channel_db_id

    def run(self):
        for video in self._ydb.get_videos(self._channel_db_id):
            if video.local_path:
                try:
                    Path(video.local_path).unlink(missing_ok=True)
                except Exception:
                    pass
        self._ydb.remove_channel(self._channel_db_id)
        self.finished.emit()


class _RefreshWorker(QThread):
    """Refresh one or all channels in the background."""

    progress = Signal(str)
    channel_done = Signal(int)  # channel_db_id — emitted after each channel finishes
    finished = Signal()
    error = Signal(str)

    def __init__(
        self,
        ydb: YouTubeDB,
        channel_db_id: int | None = None,
        cookies_browser: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._ydb = ydb
        self._channel_db_id = channel_db_id
        self._cookies = cookies_browser

    def run(self):
        try:
            if self._channel_db_id is not None:
                channels = [self._ydb.get_channel(self._channel_db_id)]
                channels = [c for c in channels if c]
            else:
                channels = self._ydb.get_all_channels()

            for ch in channels:
                self.progress.emit(f"Refreshing {ch.title}…")
                try:
                    url = f"https://www.youtube.com/channel/{ch.channel_id}/videos"
                    videos = fetch_channel_videos(
                        url, limit=30,
                        cookies_browser=self._cookies,
                        channel_id=ch.channel_id,
                    )
                    self._ydb.upsert_videos_batch(ch.id, videos)
                    # Re-download thumbnail (may have changed).
                    if ch.thumbnail_url:
                        _download_channel_thumbnail(ch.id, ch.thumbnail_url)
                    self.channel_done.emit(ch.id)
                except Exception as e:
                    self.error.emit(f"{ch.title}: {e}")

            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class _ResolveStreamWorker(QThread):
    """Resolve a video ID to playable stream URLs."""

    resolved = Signal(str, str, str)  # video_id, audio_url, video_url
    error = Signal(str, str)  # video_id, error message

    def __init__(
        self,
        video_id: str,
        quality: str = "best",
        cookies_browser: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._video_id = video_id
        self._quality = quality
        self._cookies = cookies_browser

    def run(self):
        try:
            audio_url, video_url = resolve_stream_url(
                self._video_id, self._quality, self._cookies
            )
            self.resolved.emit(
                self._video_id, audio_url, video_url or ""
            )
        except Exception as e:
            self.error.emit(self._video_id, str(e))


class _DownloadWorker(QThread):
    """Download a single video in the background."""

    finished = Signal(int, str)  # video_db_id, local_path
    progress = Signal(int, int, int)  # video_db_id, bytes_done, bytes_total
    error = Signal(int, str)  # video_db_id, error message

    def __init__(
        self,
        video_db_id: int,
        video_id: str,
        quality: str = "best",
        cookies_browser: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._video_db_id = video_db_id
        self._video_id = video_id
        self._quality = quality
        self._cookies = cookies_browser

    def run(self):
        last_emit = 0.0

        def _throttled_progress(done: int, total: int) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if now - last_emit >= 0.5 or (total > 0 and done >= total):
                last_emit = now
                self.progress.emit(self._video_db_id, done, total)

        try:
            local_path = download_video(
                self._video_id,
                quality=self._quality,
                cookies_browser=self._cookies,
                on_progress=_throttled_progress,
            )
            self.finished.emit(self._video_db_id, local_path)
        except Exception as e:
            self.error.emit(self._video_db_id, str(e))


class _SearchWorker(QThread):
    """Search YouTube in the background."""

    results = Signal(list)
    error = Signal(str)

    def __init__(
        self,
        query: str,
        limit: int = 25,
        cookies_browser: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._query = query
        self._limit = limit
        self._cookies = cookies_browser

    def run(self):
        try:
            results = search_youtube(
                self._query, self._limit, self._cookies
            )
            self.results.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class _SyncSubscriptionsWorker(QThread):
    """Fetch subscriptions from browser cookies."""

    finished = Signal(list)  # list of channel dicts
    error = Signal(str)

    def __init__(self, cookies_browser: str, parent=None):
        super().__init__(parent)
        self._cookies = cookies_browser

    def run(self):
        try:
            subs = fetch_subscriptions(self._cookies)
            self.finished.emit(subs)
        except Exception as e:
            self.error.emit(str(e))


# ------------------------------------------------------------------
# Service
# ------------------------------------------------------------------


class YouTubeService(QObject):
    """Coordinates YouTube operations between core and UI."""

    channels_updated = Signal()
    videos_updated = Signal(int)  # channel_db_id
    stream_resolved = Signal(str, str, str)  # video_id, audio_url, video_url
    resolve_error = Signal(str, str)  # video_id, error
    download_complete = Signal(int)  # video_db_id
    download_progress = Signal(int, int, int)  # video_db_id, bytes_done, total
    download_error = Signal(int, str)  # video_db_id, error
    search_results = Signal(list)  # list of result dicts
    search_error = Signal(str)
    refresh_status = Signal(str)
    refresh_finished = Signal()

    def __init__(self, db: LibraryDB, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._ydb = YouTubeDB(db)
        self._refresh_worker: _RefreshWorker | None = None
        self._workers: list[QThread] = []

        # Stream URL cache: video_id → (audio_url, video_url, expiry_time)
        self._stream_cache: dict[str, tuple[str, str, float]] = {}
        # Cache lifetime: 3 hours (YouTube URLs typically expire in 6h).
        self._cache_ttl = 3 * 3600

        # Auto-refresh timer — checks hourly.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(60 * 60 * 1000)
        self._refresh_timer.timeout.connect(self._check_due_refreshes)

    def _cleanup_workers(self) -> None:
        """Remove finished workers and schedule them for deletion."""
        alive = []
        for w in self._workers:
            if w.isRunning():
                alive.append(w)
            else:
                w.deleteLater()
        self._workers = alive

    @property
    def ydb(self) -> YouTubeDB:
        return self._ydb

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------

    def add_channel(self, channel_url: str) -> None:
        """Add a channel by URL in the background."""
        from quaver.core.config import load_config
        config = load_config()
        worker = _AddChannelWorker(
            self._ydb, channel_url,
            cookies_browser=config.youtube_auth_browser,
            parent=self,
        )
        worker.finished.connect(lambda: self.channels_updated.emit())
        worker.finished.connect(self._cleanup_workers)
        worker.error.connect(lambda e: self.refresh_status.emit(e))
        self._workers.append(worker)
        worker.start()

    def remove_channel(self, channel_db_id: int) -> None:
        """Remove a channel and clean up files in the background."""
        worker = _RemoveChannelWorker(self._ydb, channel_db_id, parent=self)
        worker.finished.connect(lambda: self.channels_updated.emit())
        worker.finished.connect(self._cleanup_workers)
        self._workers.append(worker)
        worker.start()

    def refresh_all(self) -> None:
        """Refresh all channel feeds in the background."""
        if self._refresh_worker and self._refresh_worker.isRunning():
            return
        self._refresh_worker = self._make_refresh_worker()
        self._refresh_worker.start()

    def refresh_channel(self, channel_db_id: int) -> None:
        """Refresh a single channel in the background."""
        if self._refresh_worker and self._refresh_worker.isRunning():
            return
        self._refresh_worker = self._make_refresh_worker(channel_db_id)
        self._refresh_worker.start()

    def _make_refresh_worker(
        self, channel_db_id: int | None = None
    ) -> _RefreshWorker:
        from quaver.core.config import load_config
        config = load_config()
        worker = _RefreshWorker(
            self._ydb, channel_db_id,
            cookies_browser=config.youtube_auth_browser,
            parent=self,
        )
        worker.progress.connect(self.refresh_status.emit)
        worker.channel_done.connect(self.videos_updated.emit)
        worker.finished.connect(self._on_refresh_done)
        worker.error.connect(lambda e: self.refresh_status.emit(e))
        return worker

    def _on_refresh_done(self) -> None:
        self.channels_updated.emit()
        self.refresh_finished.emit()

    def _check_due_refreshes(self) -> None:
        """Check which channels are due for refresh."""
        from quaver.core.config import load_config
        config = load_config()
        interval_hours = config.youtube_default_refresh_hours
        if interval_hours <= 0:
            return  # Manual refresh only.

        # For simplicity, refresh all channels on the global interval.
        # Individual per-channel intervals can be added later.
        self.refresh_all()

    # ------------------------------------------------------------------
    # Stream URL resolution
    # ------------------------------------------------------------------

    def resolve_stream(self, video_id: str) -> None:
        """Resolve a video_id to playable URLs. Checks cache first."""
        cached = self._stream_cache.get(video_id)
        if cached:
            audio_url, video_url, expiry = cached
            if time.time() < expiry:
                self.stream_resolved.emit(video_id, audio_url, video_url)
                return

        from quaver.core.config import load_config
        config = load_config()
        worker = _ResolveStreamWorker(
            video_id,
            quality=config.youtube_video_quality,
            cookies_browser=config.youtube_auth_browser,
            parent=self,
        )
        worker.resolved.connect(self._on_stream_resolved)
        worker.error.connect(self.resolve_error.emit)
        worker.finished.connect(self._cleanup_workers)
        self._workers.append(worker)
        worker.start()

    def _on_stream_resolved(
        self, video_id: str, audio_url: str, video_url: str
    ) -> None:
        self._stream_cache[video_id] = (
            audio_url, video_url, time.time() + self._cache_ttl
        )
        self.stream_resolved.emit(video_id, audio_url, video_url)

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------

    def download_video(self, video_db_id: int) -> None:
        """Download a video in the background."""
        video = self._ydb.get_video(video_db_id)
        if not video or video.downloaded:
            return
        from quaver.core.config import load_config
        config = load_config()
        worker = _DownloadWorker(
            video_db_id, video.video_id,
            quality=config.youtube_video_quality,
            cookies_browser=config.youtube_auth_browser,
            parent=self,
        )
        worker.finished.connect(self._on_download_done)
        worker.finished.connect(self._cleanup_workers)
        worker.progress.connect(self.download_progress.emit)
        worker.error.connect(self.download_error.emit)
        self._workers.append(worker)
        worker.start()

    def _on_download_done(self, video_db_id: int, local_path: str) -> None:
        self._ydb.set_downloaded(video_db_id, local_path)
        self.download_complete.emit(video_db_id)

    def delete_download(self, video_db_id: int) -> None:
        """Delete the downloaded file for a video."""
        self._ydb.clear_download(video_db_id)
        self.download_complete.emit(video_db_id)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str) -> None:
        """Search YouTube in the background."""
        from quaver.core.config import load_config
        config = load_config()
        worker = _SearchWorker(
            query, limit=25,
            cookies_browser=config.youtube_auth_browser,
            parent=self,
        )
        worker.results.connect(self.search_results.emit)
        worker.error.connect(self.search_error.emit)
        worker.finished.connect(self._cleanup_workers)
        self._workers.append(worker)
        worker.start()

    # ------------------------------------------------------------------
    # Subscription sync
    # ------------------------------------------------------------------

    def sync_subscriptions(self) -> None:
        """Fetch subscriptions from browser cookies and add as channels."""
        from quaver.core.config import load_config
        config = load_config()
        if not config.youtube_auth_browser:
            return
        worker = _SyncSubscriptionsWorker(
            config.youtube_auth_browser, parent=self
        )
        worker.finished.connect(self._on_subscriptions_fetched)
        worker.finished.connect(self._cleanup_workers)
        worker.error.connect(lambda e: self.refresh_status.emit(e))
        self._workers.append(worker)
        worker.start()

    def _on_subscriptions_fetched(self, subs: list[dict]) -> None:
        for sub in subs:
            channel_db_id = self._ydb.add_channel(
                channel_id=sub["channel_id"],
                title=sub["title"],
                thumbnail_url=sub.get("thumbnail_url"),
                is_subscription=True,
            )
            thumb_url = sub.get("thumbnail_url")
            if channel_db_id and thumb_url:
                _download_channel_thumbnail(channel_db_id, thumb_url)
        self.channels_updated.emit()
        self._workers = [w for w in self._workers if w.isRunning()]
        # Fetch videos for all newly synced channels.
        self.refresh_all()

    # ------------------------------------------------------------------
    # Queries (convenience, for UI)
    # ------------------------------------------------------------------

    def get_all_channels(self) -> list[Channel]:
        return self._ydb.get_all_channels()

    def get_videos(
        self, channel_db_id: int, limit: int = 0, offset: int = 0
    ) -> list[Video]:
        return self._ydb.get_videos(channel_db_id, limit=limit, offset=offset)

    def get_video_count(self, channel_db_id: int) -> int:
        return self._ydb.get_video_count(channel_db_id)

    def get_video(self, video_db_id: int) -> Video | None:
        return self._ydb.get_video(video_db_id)

    def get_channel(self, channel_db_id: int) -> Channel | None:
        return self._ydb.get_channel(channel_db_id)

    def mark_watched(self, video_db_id: int) -> None:
        self._ydb.set_watched(video_db_id, True)

    # ------------------------------------------------------------------
    # Timer management
    # ------------------------------------------------------------------

    def start_auto_refresh(self) -> None:
        self._refresh_timer.start()

    def shutdown(self) -> None:
        self._refresh_timer.stop()
        for w in self._workers:
            w.quit()
            w.wait(2000)
