"""Provider-agnostic metadata enrichment service.

Routes to TVMaze or TMDb based on per-tab configuration.
Replaces the old TMDb-only service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal

from quaver.core.library import LibraryDB, Track

log = logging.getLogger(__name__)


@dataclass
class EnrichmentJob:
    """One tab's enrichment request."""

    provider: str  # "tvmaze" or "tmdb"
    folder_paths: list[str]  # folders belonging to this tab
    tmdb_api_key: str = ""  # only needed when provider == "tmdb"
    include_failed: bool = True  # retry :not_found tracks (False = new only)


@dataclass
class ShowEnrichmentJob:
    """Enrich a single show by show_id."""

    show_id: int
    provider: str  # "tvmaze" or "tmdb"
    tmdb_api_key: str = ""


class _EnrichWorker(QObject):
    """Background worker that enriches video tracks with metadata."""

    progress = Signal(int, int)  # (done, total)
    track_enriched = Signal(int)  # track_id
    finished = Signal()
    error = Signal(str)

    def __init__(
        self, db: LibraryDB, jobs: list[EnrichmentJob],
        show_jobs: list[ShowEnrichmentJob] | None = None,
    ) -> None:
        super().__init__()
        self._db = db
        self._jobs = jobs
        self._show_jobs = show_jobs or []
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            self._run_jobs()
        except Exception as e:
            log.error("Metadata enrichment failed: %s", e, exc_info=True)
            self.error.emit(str(e))
        finally:
            self.finished.emit()

    def _run_jobs(self) -> None:
        # Per-show jobs first (targeted scraping).
        for sj in self._show_jobs:
            if self._cancelled:
                break
            self._enrich_show(sj)

        # Bulk per-tab jobs.
        for job in self._jobs:
            if self._cancelled:
                break
            tracks = self._db.get_unenriched_video_tracks(
                job.folder_paths, include_failed=job.include_failed,
            )
            if not tracks:
                continue

            if job.provider == "tvmaze":
                self._enrich_tvmaze(tracks)
            elif job.provider == "tmdb":
                self._enrich_tmdb(tracks, job.tmdb_api_key)

    # ------------------------------------------------------------------
    # Per-show enrichment
    # ------------------------------------------------------------------

    def _enrich_show(self, sj: ShowEnrichmentJob) -> None:
        """Enrich all episodes for a single show."""
        show = self._db.get_show(sj.show_id)
        if not show:
            return

        # Clear metadata_provider for this show's episodes so they get re-processed.
        episode_ids = self._db.get_show_episode_ids(sj.show_id)
        if not episode_ids:
            return
        self._db.clear_metadata_provider(episode_ids)

        # Get the now-unenriched tracks.
        tracks = [
            t for t in self._db.get_unenriched_video_tracks()
            if t.show_id == sj.show_id
        ]
        if not tracks:
            return

        log.info("Per-show enrichment: %s (%d episodes)", show["title"], len(tracks))

        if sj.provider == "tvmaze":
            self._enrich_tvmaze(tracks, show_hint=show)
        elif sj.provider == "tmdb":
            self._enrich_tmdb(tracks, sj.tmdb_api_key, show_hint=show)

    # ------------------------------------------------------------------
    # TVMaze enrichment
    # ------------------------------------------------------------------

    def _enrich_tvmaze(self, tracks: list[Track], show_hint: dict | None = None) -> None:
        from quaver.core.tvmaze import TVMazeClient

        client = TVMazeClient()
        try:
            self._enrich_with_tvmaze(client, tracks, show_hint=show_hint)
        finally:
            client.close()

    def _enrich_with_tvmaze(
        self, client: "TVMazeClient", tracks: list[Track],
        show_hint: dict | None = None,
    ) -> None:
        from quaver.core.tvmaze import TVMazeClient  # noqa: F811 — type hint

        total = len(tracks)
        log.info("TVMaze enrichment: %d tracks to process", total)

        series_cache: dict[str, int | None] = {}
        series_details_cache: dict[str, object] = {}

        # Pre-seed cache from video_shows scraper bindings.
        self._seed_series_cache(client, series_cache, series_details_cache,
                                tracks, "tvmaze", show_hint)

        done = 0

        for track in tracks:
            if self._cancelled:
                log.info("TVMaze enrichment cancelled at %d/%d", done, total)
                break

            try:
                if track.media_type == "series_episode":
                    self._tvmaze_enrich_episode(
                        client, track, series_cache, series_details_cache,
                    )
                elif track.media_type == "movie":
                    # TVMaze has no movie support — mark as not found so
                    # TMDb enrichment can pick it up later.
                    self._db.upsert_video_metadata(
                        track.id, metadata_provider="tvmaze:not_found",
                    )
                    log.debug(
                        "TVMaze skip movie: %s (use TMDb for movies)",
                        track.title,
                    )
            except Exception:
                log.warning(
                    "Failed to enrich track %d (%s)",
                    track.id, track.title, exc_info=True,
                )

            done += 1
            self.track_enriched.emit(track.id)
            if done % 10 == 0 or done == total:
                self.progress.emit(done, total)

    def _seed_series_cache(
        self, client, series_cache, series_details_cache,
        tracks, scraper_name, show_hint=None,
    ) -> None:
        """Pre-seed the series lookup cache from video_shows scraper bindings."""
        # Collect unique show_ids from tracks.
        show_ids = {t.show_id for t in tracks if t.show_id}
        if show_hint and show_hint.get("id"):
            show_ids.add(show_hint["id"])

        for sid in show_ids:
            show = self._db.get_show(sid) if sid != (show_hint or {}).get("id") else show_hint
            if not show:
                continue
            title = show["folder_title"]
            if show.get("scraper") == scraper_name and show.get("scraper_id"):
                # Already bound — seed cache with stored ID.
                series_cache[title] = show["scraper_id"]
                # Fetch details for this known ID.
                try:
                    if scraper_name == "tvmaze":
                        details = client.get_series_details(show["scraper_id"])
                    else:
                        details = client.get_series_details(show["scraper_id"])
                    if details:
                        series_details_cache[title] = details
                except Exception:
                    pass
                log.debug("Cache seeded from binding: %s → %s %d", title, scraper_name, show["scraper_id"])

    def _tvmaze_enrich_episode(
        self,
        client: "TVMazeClient",
        track: Track,
        series_cache: dict[str, int | None],
        series_details_cache: dict[str, object],
    ) -> None:
        """Enrich one episode using bulk episode list (1 API call per show)."""
        series_title = track.artist
        season = track.disc_number or 1
        episode = track.track_number or 0

        # Resolve series ID from cache or DB binding.
        if series_title not in series_cache and track.show_id:
            show = self._db.get_show(track.show_id)
            if show and show.get("scraper") == "tvmaze" and show.get("scraper_id"):
                series_cache[series_title] = show["scraper_id"]
                try:
                    details = client.get_series_details(show["scraper_id"])
                    if details:
                        series_details_cache[series_title] = details
                except Exception:
                    pass

        if series_title not in series_cache:
            series_id = client.search_series(series_title, track.year)
            series_cache[series_title] = series_id
            if series_id is not None:
                details = client.get_series_details(series_id)
                if details:
                    series_details_cache[series_title] = details
                if track.show_id:
                    self._db.update_show(
                        track.show_id,
                        scraper="tvmaze", scraper_id=series_id,
                    )

        series_id = series_cache.get(series_title)
        if series_id is None:
            self._db.upsert_video_metadata(
                track.id, metadata_provider="tvmaze:not_found",
            )
            return

        # Fetch all episodes in one call (cached after first call per show).
        all_episodes = client.get_all_episodes(series_id)
        ep_result = None
        if episode > 0:
            for ep in all_episodes:
                if ep.season == season and ep.episode == episode:
                    ep_result = ep
                    break

        details = series_details_cache.get(series_title)

        self._db.upsert_video_metadata(
            track.id,
            metadata_provider="tvmaze",
            plot=ep_result.plot if ep_result else "",
            genre=details.genre if details else "",
            poster_path=(
                ep_result.still_path
                if ep_result and ep_result.still_path
                else (details.poster_path if details else "")
            ),
            backdrop_path="",
        )

        updates: dict[str, str | int | None] = {}
        if ep_result and ep_result.title:
            updates["title"] = ep_result.title
        if details and details.title:
            updates["artist"] = details.title
            updates["album_artist"] = details.title
        if details and details.year:
            updates["year"] = details.year
        if updates:
            self._db.update_track_info(track.id, **updates)

        if track.show_id and details:
            show_updates: dict[str, str | int | None] = {}
            if details.title:
                show_updates["title"] = details.title
            if details.year:
                show_updates["year"] = details.year
            if details.genre:
                show_updates["genre"] = details.genre
            if details.poster_path:
                show_updates["poster_path"] = details.poster_path
            if hasattr(details, "plot") and details.plot:
                show_updates["plot"] = details.plot
            if show_updates:
                self._db.update_show(track.show_id, **show_updates)

        log.debug(
            "Enriched episode (TVMaze): %s S%02dE%02d",
            series_title, season, episode,
        )

    # ------------------------------------------------------------------
    # TMDb enrichment
    # ------------------------------------------------------------------

    def _enrich_tmdb(self, tracks: list[Track], api_key: str, show_hint: dict | None = None) -> None:
        if not api_key:
            self.error.emit("No TMDb API key configured")
            return

        from quaver.core.tmdb import TMDbClient

        client = TMDbClient(api_key)
        try:
            self._enrich_with_tmdb(client, tracks, show_hint=show_hint)
        finally:
            client.close()

    def _enrich_with_tmdb(
        self, client: "TMDbClient", tracks: list[Track],
        show_hint: dict | None = None,
    ) -> None:
        total = len(tracks)
        log.info("TMDb enrichment: %d tracks to process", total)

        series_cache: dict[str, int | None] = {}
        series_details_cache: dict[str, object] = {}

        # Pre-seed cache from video_shows scraper bindings.
        self._seed_series_cache(client, series_cache, series_details_cache,
                                tracks, "tmdb", show_hint)

        done = 0

        for track in tracks:
            if self._cancelled:
                log.info("TMDb enrichment cancelled at %d/%d", done, total)
                break

            try:
                if track.media_type == "movie":
                    self._tmdb_enrich_movie(client, track)
                elif track.media_type == "series_episode":
                    self._tmdb_enrich_episode(
                        client, track, series_cache, series_details_cache,
                    )
            except Exception:
                log.warning(
                    "Failed to enrich track %d (%s)",
                    track.id, track.title, exc_info=True,
                )

            done += 1
            self.track_enriched.emit(track.id)
            if done % 10 == 0 or done == total:
                self.progress.emit(done, total)

    def _tmdb_enrich_movie(self, client: "TMDbClient", track: Track) -> None:
        result = client.lookup_movie(track.title, track.year)
        if not result:
            self._db.upsert_video_metadata(
                track.id, tmdb_id=0, metadata_provider="tmdb:not_found",
            )
            log.debug("No TMDb match for movie: %s (%s)", track.title, track.year)
            return

        self._db.upsert_video_metadata(
            track.id,
            metadata_provider="tmdb",
            tmdb_id=result.tmdb_id,
            imdb_id=result.imdb_id or "",
            plot=result.plot,
            genre=result.genre,
            poster_path=result.poster_path or "",
            backdrop_path=result.backdrop_path or "",
        )
        # Write scraped movie title back to tracks table.
        if result.title:
            self._db.update_track_info(
                track.id, title=result.title, album=result.title,
            )
        if result.year:
            self._db.update_track_info(track.id, year=result.year)

        log.debug("Enriched movie: %s → TMDb %d", track.title, result.tmdb_id)

    def _tmdb_enrich_episode(
        self,
        client: "TMDbClient",
        track: Track,
        series_cache: dict[str, int | None],
        series_details_cache: dict[str, object],
    ) -> None:
        series_title = track.artist
        season = track.disc_number or 1
        episode = track.track_number or 0

        # Check persistent binding before searching (covers watchdog/single-track events).
        if series_title not in series_cache and track.show_id:
            show = self._db.get_show(track.show_id)
            if show and show.get("scraper") == "tmdb" and show.get("scraper_id"):
                series_cache[series_title] = show["scraper_id"]
                try:
                    details = client.get_series_details(show["scraper_id"])
                    if details:
                        series_details_cache[series_title] = details
                except Exception:
                    pass
                log.debug("Inline binding hit: %s → tmdb %d", series_title, show["scraper_id"])

        # Look up series via search if not yet cached.
        if series_title not in series_cache:
            series_id = client.search_series(series_title, track.year)
            series_cache[series_title] = series_id
            if series_id is not None:
                details = client.get_series_details(series_id)
                if details:
                    series_details_cache[series_title] = details
                # Bind scraper_id to the show for future runs.
                if track.show_id:
                    self._db.update_show(
                        track.show_id,
                        scraper="tmdb", scraper_id=series_id,
                    )

        series_tmdb_id = series_cache.get(series_title)
        if series_tmdb_id is None:
            self._db.upsert_video_metadata(
                track.id, tmdb_id=0, metadata_provider="tmdb:not_found",
            )
            log.debug("No TMDb match for series: %s", series_title)
            return

        ep_result = None
        if episode > 0:
            ep_result = client.get_episode_details(
                series_tmdb_id, season, episode,
            )

        details = series_details_cache.get(series_title)

        self._db.upsert_video_metadata(
            track.id,
            metadata_provider="tmdb",
            tmdb_id=ep_result.tmdb_id if ep_result else series_tmdb_id,
            plot=ep_result.plot if ep_result else "",
            genre=details.genre if details else "",
            poster_path=(
                ep_result.still_path
                if ep_result and ep_result.still_path
                else (details.poster_path if details else "")
            ),
            backdrop_path=details.backdrop_path if details else "",
        )

        # Write scraped episode title + canonical series name to tracks table.
        updates: dict[str, str | int | None] = {}
        if ep_result and ep_result.title:
            updates["title"] = ep_result.title
        if details and details.title:
            updates["artist"] = details.title
            updates["album_artist"] = details.title
        if details and details.year:
            updates["year"] = details.year
        if updates:
            self._db.update_track_info(track.id, **updates)

        # Update show-level metadata.
        if track.show_id and details:
            show_updates: dict[str, str | int | None] = {}
            if details.title:
                show_updates["title"] = details.title
            if details.year:
                show_updates["year"] = details.year
            if details.genre:
                show_updates["genre"] = details.genre
            if details.poster_path:
                show_updates["poster_path"] = details.poster_path
            if hasattr(details, "backdrop_path") and details.backdrop_path:
                show_updates["backdrop_path"] = details.backdrop_path
            if hasattr(details, "plot") and details.plot:
                show_updates["plot"] = details.plot
            if show_updates:
                self._db.update_show(track.show_id, **show_updates)

        log.debug(
            "Enriched episode (TMDb): %s S%02dE%02d",
            series_title, season, episode,
        )


class MetadataService(QObject):
    """Manages background metadata enrichment across providers."""

    enrichment_started = Signal()
    enrichment_progress = Signal(int, int)  # (done, total)
    enrichment_finished = Signal()
    enrichment_error = Signal(str)

    def __init__(self, db: LibraryDB) -> None:
        super().__init__()
        self._db = db
        self._thread: QThread | None = None
        self._worker: _EnrichWorker | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start_enrichment(self, jobs: list[EnrichmentJob]) -> None:
        """Start background enrichment for the given jobs (one per tab)."""
        if self.is_running:
            return

        if not jobs:
            return

        self._thread = QThread()
        self._worker = _EnrichWorker(self._db, jobs)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.enrichment_progress.emit)
        self._worker.error.connect(self.enrichment_error.emit)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)

        self.enrichment_started.emit()
        self._thread.start()
        log.info("Metadata enrichment started (%d jobs)", len(jobs))

    def start_show_enrichment(self, show_id: int, provider: str, tmdb_api_key: str = "") -> None:
        """Enrich a single show's episodes in the background."""
        if self.is_running:
            return

        show_job = ShowEnrichmentJob(
            show_id=show_id, provider=provider, tmdb_api_key=tmdb_api_key,
        )
        self._thread = QThread()
        self._worker = _EnrichWorker(self._db, jobs=[], show_jobs=[show_job])
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.enrichment_progress.emit)
        self._worker.error.connect(self.enrichment_error.emit)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)

        self.enrichment_started.emit()
        self._thread.start()
        show = self._db.get_show(show_id)
        log.info("Per-show enrichment started: %s", show["title"] if show else show_id)

    def cancel(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _on_finished(self) -> None:
        self.enrichment_finished.emit()
        log.info("Metadata enrichment finished")
