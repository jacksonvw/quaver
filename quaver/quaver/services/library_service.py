"""Library service — background scanning, watchdog file watching, DB bridge.

Runs I/O in threads; communicates to the UI via Qt signals.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from quaver.core.library import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, LibraryDB, Track
from quaver.core.network import ensure_mounted
from quaver.core.tagger import read_tags
from quaver.core.nfo import read_episode_nfo, read_movie_nfo, read_show_nfo
from quaver.core.video_parser import (
    build_episode_tags,
    discover_duration,
    discover_episodes,
    extract_episode_title,
    extract_identifier,
    is_extras_path,
    is_show_folder,
    minimal_clean,
    parse_movie_path,
    parse_series_path,
)

from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ── Discovery manifest (ephemeral, never persisted) ───────────────────


@dataclass
class _DiscoveredEpisode:
    """A video file discovered during the discovery pass."""
    path: Path
    season: int
    episode_num: int
    ep_title: str
    nfo_data: dict | None = None


@dataclass
class _DiscoveredShow:
    """A show folder discovered during the discovery pass."""
    folder: Path
    title: str
    year: int | None = None
    identifier: tuple[str, int] | None = None  # (scraper, scraper_id)
    nfo_data: dict | None = None
    episodes: list[_DiscoveredEpisode] = field(default_factory=list)
    pre_validated: bool = False
    existing_show_id: int | None = None
    scraper_id: int | None = None  # set by validation pass
    # Populated by validation when scraper is active:
    scraper_episodes: list | None = None  # list[TVMazeEpisodeResult]
    scraper_details: object | None = None  # TVMazeSeriesResult


@dataclass
class _DiscoveredMovie:
    """A movie file discovered during the discovery pass."""
    path: Path
    title: str
    year: int | None = None
    identifier: tuple[str, int] | None = None
    nfo_data: dict | None = None
    pre_validated: bool = False


def _compute_dir_hash(dir_path: Path) -> str:
    """Compute a fast content hash for a directory using os.scandir().

    Hashes: directory mtime, entry count, and sorted (name, size) pairs.
    A single syscall per directory — much cheaper than stat-ing every file,
    which is critical for SMB/NAS performance.
    """
    try:
        entries = []
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                    entries.append((entry.name, st.st_size, entry.is_dir()))
                except OSError:
                    entries.append((entry.name, 0, False))
        # Also hash season subdirectory contents (one level deeper).
        sub_entries = []
        for name, _size, is_dir in entries:
            if is_dir:
                sub_path = dir_path / name
                try:
                    with os.scandir(sub_path) as sit:
                        for se in sit:
                            try:
                                sst = se.stat(follow_symlinks=False)
                                sub_entries.append((name + "/" + se.name, sst.st_size))
                            except OSError:
                                sub_entries.append((name + "/" + se.name, 0))
                except OSError:
                    pass
        entries.sort()
        sub_entries.sort()
        h = hashlib.md5(usedforsecurity=False)
        try:
            h.update(str(int(dir_path.stat().st_mtime)).encode())
        except OSError:
            pass
        h.update(str(len(entries)).encode())
        for name, size, _is_dir in entries:
            h.update(f"{name}:{size}".encode())
        for name, size in sub_entries:
            h.update(f"{name}:{size}".encode())
        return h.hexdigest()
    except OSError:
        return ""


class _ScanWorker(QObject):
    """Scans library sources in a background QThread."""

    progress = Signal(int, int)  # (scanned, total)
    status = Signal(str)  # descriptive phase messages for UI
    finished = Signal()
    track_scanned = Signal()  # Emitted after each track is upserted.

    def __init__(
        self, db: LibraryDB, source_ids: list[int] | None = None,
        *, full_rescan: bool = False,
    ) -> None:
        super().__init__()
        self._db = db
        self._source_ids = source_ids  # None = scan all sources.
        self._full_rescan = full_rescan

    def run(self) -> None:
        all_sources = self._db.get_sources()
        if self._source_ids is not None:
            id_set = set(self._source_ids)
            sources = [s for s in all_sources if s["id"] in id_set]
        else:
            sources = all_sources

        # Clear scan cache for targeted sources when doing a full rescan.
        if self._full_rescan:
            for src in sources:
                self._db.clear_scan_cache_for_source(src["id"])

        # Route each source to the appropriate scanner.
        for src in sources:
            folder_path = Path(src["path"])
            if not folder_path.is_dir():
                log.warning(
                    "Source %r (%s) not accessible — skipping",
                    src["path"], src.get("uri", "local"),
                )
                continue
            folder_type = src.get("type", "music")
            if folder_type == "files":
                # Live filesystem tree — no DB rows to write, nothing
                # to scan.  The folder-tree view reads the filesystem
                # directly when rendered.
                continue
            if folder_type == "series":
                self._scan_series_source(src, folder_path)
            elif folder_type == "movies":
                self._scan_movie_source(src, folder_path)
            else:
                self._scan_music_source(src, folder_path)
            self._db.update_last_scanned(src["id"])

        self.finished.emit()

    # ── Music: file-by-file (unchanged) ───────────────────────

    def _scan_music_source(self, src: dict, folder_path: Path) -> None:
        pattern = "**/*" if src["recursive"] else "*"
        files: list[Path] = []
        self.status.emit(f"Discovering files in {folder_path.name}…")
        try:
            for p in folder_path.glob(pattern):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                    files.append(p)
        except OSError as e:
            log.warning("Error listing %r: %s", src["path"], e)
            return

        total = len(files)
        existing_paths: set[str] = set()
        for i, fp in enumerate(files, 1):
            if i % 50 == 0 or i == total:
                self.status.emit(f"Scanning music… {i:,} / {total:,}")
            try:
                tags = read_tags(fp)
                if tags:
                    self._db.upsert_track(tags)
                    existing_paths.add(str(fp))
            except Exception:
                log.warning("Failed to scan %s", fp, exc_info=True)
            self.track_scanned.emit()

        removed = self._db.remove_missing_tracks_for_source(
            src["path"], existing_paths,
        )
        if removed:
            log.info("Removed %d missing tracks from source %r", removed, src["path"])

    # ── Series: Kodi-style gated scanning (discover → validate → commit) ──

    def _scan_series_source(self, src: dict, folder_path: Path) -> None:
        """Orchestrate gated series scanning: discover, validate, commit."""
        self.status.emit(f"Discovering shows in {folder_path.name}…")
        manifest, cached_paths = self._discover_series_source(src, folder_path)
        if manifest:
            self.status.emit(
                f"Validating {len(manifest)} show{'s' if len(manifest) != 1 else ''} "
                f"against scraper…"
            )
        validated = self._validate_series_manifest(manifest, src)
        if validated:
            self.status.emit(f"Committing {len(validated)} validated show{'s' if len(validated) != 1 else ''}…")
        committed_paths = self._commit_series_manifest(validated, src)
        all_paths = cached_paths | committed_paths

        # Per-source cleanup.
        removed = self._db.remove_missing_tracks_for_source(
            src["path"], all_paths,
        )
        if removed:
            log.info("Removed %d missing tracks from source %r", removed, src["path"])

    def _discover_series_source(
        self, src: dict, folder_path: Path,
    ) -> tuple[list[_DiscoveredShow], set[str]]:
        """Discovery pass: walk show folders, build manifest. No DB writes."""
        manifest: list[_DiscoveredShow] = []
        cached_paths: set[str] = set()
        scan_mode = src.get("scan_mode", "multi")

        if scan_mode == "single":
            # Source folder IS a single show.
            show = self._discover_one_show(folder_path, cached_paths)
            if show:
                manifest.append(show)
        elif scan_mode == "deep":
            # Recursively find show folders at any depth.
            self._discover_deep(folder_path, manifest, cached_paths)
        else:
            # Default (multi): each direct child is a show.
            try:
                children = sorted(folder_path.iterdir())
            except OSError as e:
                log.warning("Error listing %r: %s", src["path"], e)
                return manifest, cached_paths
            for child in children:
                if not child.is_dir():
                    continue
                if not is_show_folder(child):
                    continue
                show = self._discover_one_show(child, cached_paths)
                if show:
                    manifest.append(show)

        return manifest, cached_paths

    def _discover_deep(
        self, root: Path,
        manifest: list[_DiscoveredShow], cached_paths: set[str],
    ) -> None:
        """Recursively find show folders at any depth under *root*."""
        try:
            children = sorted(root.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir():
                continue
            if is_show_folder(child):
                show = self._discover_one_show(child, cached_paths)
                if show:
                    manifest.append(show)
            else:
                # Not a show — recurse deeper (genre/network grouping).
                self._discover_deep(child, manifest, cached_paths)

    def _discover_one_show(
        self, show_folder: Path, cached_paths: set[str],
    ) -> _DiscoveredShow | None:
        """Discover a single show folder. Returns None if cache-hit."""
        show_folder_path = str(show_folder)

        # Incremental scanning: skip unchanged show folders that have tracks.
        # If the cache matches but no tracks exist, a previous scan failed to
        # commit (or a different source scanned this folder) — rescan.
        if not self._full_rescan:
            content_hash = _compute_dir_hash(show_folder)
            cached_hash = self._db.get_scan_cache_hash(show_folder_path)
            if cached_hash and cached_hash == content_hash:
                conn = self._db._get_conn()
                rows = conn.execute(
                    "SELECT filepath FROM tracks WHERE filepath LIKE ?",
                    (show_folder_path.rstrip("/") + "/%",),
                ).fetchall()
                if rows:
                    for row in rows:
                        cached_paths.add(row[0])
                    return None

        show_title, year = minimal_clean(show_folder.name)

        # Check pre-validation: identifier, NFO, or existing scraper_id.
        identifier = extract_identifier(show_folder.name)
        nfo_data = None
        nfo_path = show_folder / "tvshow.nfo"
        if nfo_path.is_file():
            nfo_data = read_show_nfo(nfo_path)

        # Check if show already exists with a scraper binding.
        existing_show_id = None
        pre_validated = False
        existing_show = self._db.get_show_by_folder(show_folder_path)
        if existing_show:
            existing_show_id = existing_show["id"]
            if existing_show.get("scraper_id"):
                pre_validated = True

        if identifier:
            pre_validated = True
        if nfo_data and (nfo_data.get("scraper") or nfo_data.get("scraper_id")):
            pre_validated = True

        # Discover episodes.
        episodes_raw = discover_episodes(show_folder)
        episodes: list[_DiscoveredEpisode] = []
        for ep_path, season, episode_num in episodes_raw:
            ep_title = extract_episode_title(str(ep_path))
            ep_nfo = read_episode_nfo(ep_path)
            if ep_nfo:
                if ep_nfo.get("title"):
                    ep_title = ep_nfo["title"]
                if ep_nfo.get("season") is not None:
                    season = ep_nfo["season"]
                if ep_nfo.get("episode") is not None:
                    episode_num = ep_nfo["episode"]
            episodes.append(_DiscoveredEpisode(
                path=ep_path, season=season, episode_num=episode_num,
                ep_title=ep_title, nfo_data=ep_nfo,
            ))
            self.track_scanned.emit()

        return _DiscoveredShow(
            folder=show_folder, title=show_title, year=year,
            identifier=identifier, nfo_data=nfo_data,
            episodes=episodes, pre_validated=pre_validated,
            existing_show_id=existing_show_id,
        )

    def _validate_series_manifest(
        self, manifest: list[_DiscoveredShow], src: dict,
    ) -> list[_DiscoveredShow]:
        """Validation pass: query scraper, fetch episode lists. Drop unmatched.

        For TVMaze: search for each show, then fetch the complete episode
        list.  The episode list is the scraper's source of truth — only
        local files that match a scraper episode will be committed.
        """
        scraper = src.get("scraper", "none")
        if scraper == "none":
            return manifest

        validated: list[_DiscoveredShow] = []

        total = len(manifest)
        if scraper == "tvmaze":
            from quaver.core.tvmaze import TVMazeClient
            client = TVMazeClient()
            try:
                for i, show in enumerate(manifest, 1):
                    self.status.emit(f"TVMaze: {show.title} ({i}/{total})")
                    self._validate_tvmaze_show(show, client, validated)
                    self.track_scanned.emit()
            finally:
                client.close()
        elif scraper == "tmdb":
            from quaver.core.config import load_config
            config = load_config()
            if not config.tmdb_api_key:
                return manifest
            from quaver.core.tmdb import TMDbClient
            client = TMDbClient(config.tmdb_api_key)
            for i, show in enumerate(manifest, 1):
                self.status.emit(f"TMDb: {show.title} ({i}/{total})")
                try:
                    result = client.search_series(show.title, show.year)
                    if result:
                        show.scraper_id = result
                        validated.append(show)
                except Exception:
                    log.warning("Show %r: TMDb error", show.title, exc_info=True)
                self.track_scanned.emit()

        skipped = len(manifest) - len(validated)
        if skipped:
            log.info(
                "Validation: %d/%d shows skipped in %r",
                skipped, len(manifest), src["path"],
            )
        return validated

    def _validate_tvmaze_show(
        self, show: _DiscoveredShow, client, validated: list[_DiscoveredShow],
    ) -> None:
        """Validate one show against TVMaze and fetch its episode list."""
        tvmaze_id = show.scraper_id

        # Pre-validated shows already have an ID — skip search.
        if show.pre_validated and show.existing_show_id:
            existing = self._db.get_show(show.existing_show_id)
            if existing and existing.get("scraper_id"):
                tvmaze_id = existing["scraper_id"]

        # Search TVMaze if we don't have an ID yet.
        if not tvmaze_id:
            if show.identifier:
                _, tvmaze_id = show.identifier
            else:
                try:
                    tvmaze_id = client.search_series(show.title, show.year)
                except Exception:
                    log.warning("Show %r: TVMaze search error", show.title, exc_info=True)
                    return

        if not tvmaze_id:
            log.info("Show %r: not found on TVMaze — skipping", show.title)
            return

        show.scraper_id = tvmaze_id

        # Fetch full episode list (cached after first call).
        try:
            show.scraper_episodes = client.get_all_episodes(tvmaze_id)
            show.scraper_details = client.get_series_details(tvmaze_id)
        except Exception:
            log.warning("Show %r: failed to fetch episodes", show.title, exc_info=True)

        validated.append(show)
        log.info(
            "Show %r: validated (tvmaze=%d, %d episodes)",
            show.title, tvmaze_id,
            len(show.scraper_episodes) if show.scraper_episodes else 0,
        )

    def _commit_series_manifest(
        self, validated: list[_DiscoveredShow], src: dict,
    ) -> set[str]:
        """Commit pass: match local files against scraper episode list.

        If the show has a scraper episode list, only files whose (season,
        episode) matches an entry in that list are committed.  Metadata
        comes from the scraper at commit time — no separate enrichment pass
        needed.  Shows without a scraper list (scraper=none or fetch
        failed) commit all episodes as before.
        """
        existing_paths: set[str] = set()
        source_id = src["id"]

        for show in validated:
            show_folder_path = str(show.folder)

            # Use scraper's canonical title if available.
            show_title = show.title
            if show.scraper_details and show.scraper_details.title:
                show_title = show.scraper_details.title

            show_id = self._db.get_or_create_show(
                show_title, folder_path=show_folder_path,
            )

            # Apply scraper binding + show-level metadata.
            self._apply_show_metadata(show, show_id, src)

            # Build scraper episode lookup for gated matching.
            ep_lookup: dict[tuple[int, int], object] | None = None
            if show.scraper_episodes:
                ep_lookup = {
                    (e.season, e.episode): e
                    for e in show.scraper_episodes
                }

            # Commit episodes — gated by scraper list when available.
            matched = 0
            skipped = 0
            for ep in show.episodes:
                scraper_ep = None
                if ep_lookup is not None:
                    scraper_ep = ep_lookup.get((ep.season, ep.episode_num))
                    if scraper_ep is None:
                        skipped += 1
                        log.debug(
                            "Skipping unmatched file: %s (S%02dE%02d not in scraper)",
                            ep.path.name, ep.season, ep.episode_num,
                        )
                        continue

                try:
                    # Use scraper title when available, else filename-derived.
                    ep_title = scraper_ep.title if scraper_ep else ep.ep_title
                    # NFO overrides scraper if present.
                    if ep.nfo_data and ep.nfo_data.get("title"):
                        ep_title = ep.nfo_data["title"]

                    tags = build_episode_tags(
                        show_title, ep.season, ep.episode_num, ep_title,
                        str(ep.path), year=show.year,
                    )
                    track_id = self._db.upsert_track(tags)
                    existing_paths.add(str(ep.path))

                    conn = self._db._get_conn()
                    conn.execute(
                        "UPDATE tracks SET show_id = ? WHERE id = ?",
                        (show_id, track_id),
                    )
                    conn.commit()

                    # Write metadata immediately — no separate enrichment.
                    if scraper_ep:
                        self._db.upsert_video_metadata(
                            track_id,
                            metadata_provider="tvmaze",
                            plot=scraper_ep.plot or "",
                            poster_path=scraper_ep.still_path or "",
                        )
                    elif ep.nfo_data and ep.nfo_data.get("plot"):
                        self._db.upsert_video_metadata(
                            track_id,
                            metadata_provider="nfo",
                            plot=ep.nfo_data["plot"],
                        )
                    matched += 1
                except Exception:
                    log.warning("Failed to scan %s", ep.path, exc_info=True)
                self.track_scanned.emit()

            if skipped:
                log.info(
                    "Show %r: %d matched, %d skipped (not in scraper)",
                    show_title, matched, skipped,
                )

            content_hash = _compute_dir_hash(show.folder)
            self._db.set_scan_cache(show_folder_path, source_id, content_hash)

        return existing_paths

    def _apply_show_metadata(
        self, show: _DiscoveredShow, show_id: int, src: dict,
    ) -> None:
        """Apply scraper/NFO metadata to a video_shows row."""
        db_show = self._db.get_show(show_id)

        # Bind scraper ID.
        if show.identifier:
            scraper_name, scraper_id = show.identifier
            if not db_show or db_show.get("scraper_id") != scraper_id:
                self._db.update_show(
                    show_id, scraper=scraper_name, scraper_id=scraper_id,
                )
        elif show.scraper_id and (not db_show or not db_show.get("scraper_id")):
            scraper_name = src.get("scraper", "tvmaze")
            self._db.update_show(
                show_id, scraper=scraper_name, scraper_id=show.scraper_id,
            )

        # Apply scraper details (title, year, genre, poster, plot).
        if show.scraper_details:
            sd = show.scraper_details
            updates: dict = {}
            if sd.title:
                updates["title"] = sd.title
            if sd.year:
                updates["year"] = sd.year
            if sd.genre:
                updates["genre"] = sd.genre
            if sd.plot:
                updates["plot"] = sd.plot
            if sd.poster_path:
                updates["poster_path"] = sd.poster_path
            if updates:
                self._db.update_show(show_id, **updates)
            return

        # Fallback: NFO metadata (no scraper details available).
        if show.nfo_data:
            nfo = show.nfo_data
            updates = {}
            if nfo.get("title") and db_show and db_show["title"] == db_show["folder_title"]:
                updates["title"] = nfo["title"]
            if nfo.get("year") and (not db_show or not db_show.get("year")):
                updates["year"] = nfo["year"]
            if nfo.get("plot") and (not db_show or not db_show.get("plot")):
                updates["plot"] = nfo["plot"]
            if nfo.get("genre") and (not db_show or not db_show.get("genre")):
                updates["genre"] = nfo["genre"]
            if nfo.get("scraper") and (not db_show or not db_show.get("scraper_id")):
                updates["scraper"] = nfo["scraper"]
                updates["scraper_id"] = nfo["scraper_id"]
            if nfo.get("imdb_id") and (not db_show or not db_show.get("imdb_id")):
                updates["imdb_id"] = nfo["imdb_id"]
            if updates:
                self._db.update_show(show_id, **updates)

        # Fallback: update title/year from discovery if not already set.
        if db_show:
            if db_show["title"] == db_show["folder_title"] and show.title != db_show["title"]:
                self._db.update_show(show_id, title=show.title, folder_title=show.title)
            if show.year and not db_show.get("year"):
                self._db.update_show(show_id, year=show.year)

    # ── Movies: gated scanning (discover → validate → commit) ─────────

    def _scan_movie_source(self, src: dict, folder_path: Path) -> None:
        source_id = src["id"]

        # Incremental: hash the entire source directory tree.
        if not self._full_rescan:
            source_hash = _compute_dir_hash(folder_path)
            cached_hash = self._db.get_scan_cache_hash(str(folder_path))
            if cached_hash and cached_hash == source_hash:
                log.info(
                    "Incremental scan: movie source %r unchanged — skipping",
                    src["path"],
                )
                return

        # Discovery pass.
        self.status.emit(f"Discovering movies in {folder_path.name}…")
        manifest = self._discover_movie_source(src, folder_path)
        # Validation pass.
        if manifest:
            self.status.emit(f"Validating {len(manifest)} movie{'s' if len(manifest) != 1 else ''}…")
        validated = self._validate_movie_manifest(manifest, src)
        # Commit pass.
        existing_paths: set[str] = set()
        for movie in validated:
            try:
                tags = parse_movie_path(str(movie.path))
                if tags:
                    track_id = self._db.upsert_track(tags)
                    existing_paths.add(str(movie.path))

                    # Apply NFO / identifier metadata.
                    vm_updates: dict = {}
                    if movie.nfo_data:
                        if movie.nfo_data.get("plot"):
                            vm_updates["plot"] = movie.nfo_data["plot"]
                        if movie.nfo_data.get("scraper_id"):
                            vm_updates["tmdb_id"] = movie.nfo_data["scraper_id"]
                        if movie.nfo_data.get("imdb_id"):
                            vm_updates["imdb_id"] = movie.nfo_data["imdb_id"]
                    if movie.identifier:
                        _scraper, scraper_id = movie.identifier
                        vm_updates["tmdb_id"] = scraper_id
                    if vm_updates:
                        vm_updates["metadata_provider"] = "nfo"
                        self._db.upsert_video_metadata(track_id, **vm_updates)
            except Exception:
                log.warning("Failed to scan %s", movie.path, exc_info=True)
            self.track_scanned.emit()

        removed = self._db.remove_missing_tracks_for_source(
            src["path"], existing_paths,
        )
        if removed:
            log.info("Removed %d missing tracks from source %r", removed, src["path"])

        # Update scan cache.
        new_hash = _compute_dir_hash(folder_path)
        self._db.set_scan_cache(str(folder_path), source_id, new_hash)

    def _discover_movie_source(
        self, src: dict, folder_path: Path,
    ) -> list[_DiscoveredMovie]:
        """Discovery pass: walk movie files, build manifest. No DB writes."""
        pattern = "**/*" if src["recursive"] else "*"
        manifest: list[_DiscoveredMovie] = []
        try:
            for p in folder_path.glob(pattern):
                if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                    if is_extras_path(str(p)):
                        continue
                    tags = parse_movie_path(str(p))
                    if not tags:
                        continue
                    ident = extract_identifier(p.parent.name)
                    nfo_data = read_movie_nfo(p)
                    pre_validated = bool(ident) or bool(
                        nfo_data and (nfo_data.get("scraper_id") or nfo_data.get("imdb_id"))
                    )
                    manifest.append(_DiscoveredMovie(
                        path=p, title=tags.title, year=tags.year,
                        identifier=ident, nfo_data=nfo_data,
                        pre_validated=pre_validated,
                    ))
                    self.track_scanned.emit()
        except OSError as e:
            log.warning("Error listing %r: %s", src["path"], e)
        return manifest

    def _validate_movie_manifest(
        self, manifest: list[_DiscoveredMovie], src: dict,
    ) -> list[_DiscoveredMovie]:
        """Validation pass: query TMDb for each movie. Drop unmatched."""
        scraper = src.get("scraper", "none")
        if scraper == "none":
            return manifest

        from quaver.core.config import load_config
        config = load_config()
        if scraper == "tmdb" and not config.tmdb_api_key:
            return manifest

        validated: list[_DiscoveredMovie] = []
        for movie in manifest:
            if movie.pre_validated:
                validated.append(movie)
                continue
            try:
                from quaver.core.tmdb import TMDbClient
                client = TMDbClient(config.tmdb_api_key)
                result = client.search_movie(movie.title, movie.year)
                if result:
                    validated.append(movie)
                else:
                    log.info(
                        "Movie %r (%s): not found on TMDb — skipping",
                        movie.title, movie.year,
                    )
            except Exception:
                log.warning(
                    "Movie %r: scraper error — skipping", movie.title,
                    exc_info=True,
                )

        skipped = len(manifest) - len(validated)
        if skipped:
            log.info(
                "Validation: %d/%d movies skipped (not found) in %r",
                skipped, len(manifest), src["path"],
            )
        return validated


class _WatchdogHandler(FileSystemEventHandler):
    """Handles filesystem events from watchdog and forwards to the service."""

    def __init__(
        self, callback: callable, folder_types: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._callback = callback
        # Map folder path → type ("music", "movies", "series").
        self._folder_types = folder_types or {}

    def _get_folder_type(self, path: str) -> str:
        """Determine the folder type for a given file path."""
        for folder_path, folder_type in self._folder_types.items():
            if path.startswith(folder_path):
                return folder_type
        return "music"

    def _get_extensions(self, path: str) -> frozenset[str]:
        folder_type = self._get_folder_type(path)
        if folder_type in ("movies", "series"):
            return VIDEO_EXTENSIONS
        return AUDIO_EXTENSIONS

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_handle(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_handle(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._callback(event.src_path, deleted=True)

    def _maybe_handle(self, path: str) -> None:
        extensions = self._get_extensions(path)
        if Path(path).suffix.lower() in extensions:
            folder_type = self._get_folder_type(path)
            self._callback(path, deleted=False, folder_type=folder_type)


class LibraryService(QObject):
    """Manages library scanning, live file watching, and DB access."""

    scan_started = Signal()
    scan_progress = Signal(int, int)
    scan_status = Signal(str)  # descriptive phase messages for the transport bar
    scan_finished = Signal()
    library_updated = Signal()  # Emitted when tracks change (scan or watch).

    def __init__(self, db: LibraryDB | None = None) -> None:
        super().__init__()
        self._db = db or LibraryDB()
        self._scan_thread: QThread | None = None
        self._scan_worker: _ScanWorker | None = None
        self._observer: Observer | None = None

    @property
    def db(self) -> LibraryDB:
        return self._db

    # ------------------------------------------------------------------
    # Network mounts
    # ------------------------------------------------------------------

    def mount_network_sources(self) -> None:
        """Mount all network sources (SMB shares) via KIO FUSE.

        Called at startup before scanning. Non-blocking for offline shares —
        skips any that fail to mount.
        """
        sources = self._db.get_sources()
        for src in sources:
            uri = src.get("uri")
            if not uri:
                continue
            ok, err = ensure_mounted(uri)
            if ok:
                log.info("Network source mounted: %s", uri)
            else:
                log.warning("Could not mount network source: %s (%s)", uri, err)

    # ------------------------------------------------------------------
    # Full scan
    # ------------------------------------------------------------------

    def start_scan(
        self, source_ids: list[int] | None = None, *, full_rescan: bool = False,
    ) -> None:
        """Kick off a background scan.

        Args:
            source_ids: Scan only these sources. ``None`` scans all sources.
            full_rescan: If True, ignore the scan cache and re-process
                every directory. Default is incremental.
        """
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return  # Already scanning.

        self._scan_thread = QThread()
        self._scan_worker = _ScanWorker(
            self._db, source_ids=source_ids, full_rescan=full_rescan,
        )
        self._scan_worker.moveToThread(self._scan_thread)

        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self.scan_progress.emit)
        self._scan_worker.status.connect(self.scan_status.emit)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.finished.connect(self._scan_thread.quit)

        self.scan_started.emit()
        self._scan_thread.start()

    def _on_scan_finished(self) -> None:
        # Housekeeping: reset stub titles for re-enrichment, prune orphans.
        n_reset = self._db.reset_stub_episode_titles()
        n_pruned = self._db.prune_orphaned_shows()
        if n_reset:
            log.info("Reset %d stub episode titles for re-enrichment", n_reset)
        if n_pruned:
            log.info("Pruned %d orphaned shows", n_pruned)
        # Backfill missing poster_paths from TVMaze episode cache.
        n_filled = self._backfill_poster_paths()
        if n_filled:
            log.info("Backfilled %d episode poster paths from cache", n_filled)
        # Convert any remaining HTTP poster URLs to local file paths.
        n_converted = self._convert_url_poster_paths()
        if n_converted:
            log.info("Downloaded %d episode stills to local cache", n_converted)
        self.scan_finished.emit()
        self.library_updated.emit()

    def _backfill_poster_paths(self) -> int:
        """Fill missing poster_path for tvmaze-enriched episodes from cache.

        Reads the on-disk TVMaze episode JSON cache — no API calls.
        """
        episodes = self._db.get_episodes_missing_poster()
        if not episodes:
            return 0

        from quaver.core.tvmaze import TVMazeClient
        client = TVMazeClient()
        try:
            # Group by show scraper_id.
            by_show: dict[int, list[dict]] = {}
            for ep in episodes:
                sid = ep["scraper_id"]
                by_show.setdefault(sid, []).append(ep)

            filled = 0
            for scraper_id, show_eps in by_show.items():
                cached = client._get_cached_episodes(scraper_id)
                if not cached:
                    continue
                ep_lookup = {
                    (e.season, e.episode): e for e in cached
                }
                for ep in show_eps:
                    se_key = (ep["disc_number"], ep["track_number"])
                    scraper_ep = ep_lookup.get(se_key)
                    if scraper_ep and scraper_ep.still_path:
                        self._db.upsert_video_metadata(
                            ep["track_id"],
                            poster_path=scraper_ep.still_path,
                        )
                        filled += 1
            return filled
        finally:
            client.close()

    def _convert_url_poster_paths(self) -> int:
        """Download HTTP poster_path URLs to local files, update DB.

        One-time migration for episodes stored with remote URLs before
        the eager-download fix in _parse_episode_list().
        """
        conn = self._db._get_conn()
        rows = conn.execute(
            "SELECT vm.track_id, vm.poster_path "
            "FROM video_metadata vm "
            "WHERE vm.poster_path LIKE 'https://%' OR vm.poster_path LIKE 'http://%'",
        ).fetchall()
        if not rows:
            return 0

        from quaver.core.tvmaze import TVMazeClient
        client = TVMazeClient()
        try:
            total = len(rows)
            converted = 0
            for i, row in enumerate(rows):
                if i % 50 == 0:
                    self.scan_status.emit(
                        f"Downloading episode stills\u2026 {i:,} / {total:,}"
                    )
                local_path = client._download_image(row["poster_path"])
                if local_path:
                    self._db.upsert_video_metadata(
                        row["track_id"], poster_path=local_path,
                    )
                    converted += 1
            return converted
        finally:
            client.close()

    # ------------------------------------------------------------------
    # Live file watching (watchdog)
    # ------------------------------------------------------------------

    def start_watching(self) -> None:
        """Start inotify-based file watching on all sources."""
        self.stop_watching()
        folders = self._db.get_sources()
        if not folders:
            return

        # Build folder path → type mapping for the handler.
        folder_types = {}
        for f in folders:
            path = f["path"].rstrip("/") + "/"
            folder_types[path] = f.get("type", "music")

        self._observer = Observer()
        handler = _WatchdogHandler(self._on_fs_event, folder_types)

        for f in folders:
            # Skip network sources — recursive inotify walks the entire
            # directory tree to register watches, hammering the NAS on
            # every startup.  Network-mounted libraries are scan-on-demand.
            if f.get("uri"):
                continue
            # Files-tab sources have no DB rows; an inotify event would
            # fall through every branch in _on_fs_event without writing
            # anything.  Skip the watch entirely.
            if f.get("type") == "files":
                continue
            folder_path = str(Path(f["path"]))
            try:
                self._observer.schedule(
                    handler, folder_path, recursive=bool(f["recursive"]),
                )
            except OSError:
                log.debug("Skipping inaccessible source: %s", folder_path)

        self._observer.daemon = True
        self._observer.start()

    def stop_watching(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer = None

    def _on_fs_event(
        self, path: str, deleted: bool, folder_type: str = "music",
    ) -> None:
        if deleted:
            track = self._db.get_track_by_path(path)
            if track:
                conn = self._db._get_conn()
                conn.execute("DELETE FROM tracks WHERE filepath = ?", (path,))
                conn.commit()
                self.library_updated.emit()
        else:
            # Skip extras/featurettes for video sources.
            if folder_type in ("movies", "series") and is_extras_path(path):
                return
            tags = None
            if folder_type == "movies":
                tags = parse_movie_path(path)
                tags.duration_ms = discover_duration(path)
            elif folder_type == "series":
                # Gate: only add if show is validated AND episode is in scraper list.
                show_folder = self._derive_show_folder(path)
                if not show_folder:
                    return
                existing_show = self._db.get_show_by_folder(show_folder)
                if not existing_show or not existing_show.get("scraper_id"):
                    return  # Unvalidated show — skip until next full scan.
                tags = self._build_series_tags_for_watchdog(path)
                if not tags:
                    return
                # Match against cached scraper episode list.
                scraper_ep = self._match_watchdog_episode(
                    existing_show["scraper_id"], tags,
                )
                if scraper_ep is None:
                    return  # Not in scraper list — skip.
                if scraper_ep and scraper_ep.title:
                    tags.title = scraper_ep.title
                tags.duration_ms = discover_duration(path)
            else:
                tags = read_tags(path)
            if tags:
                track_id = self._db.upsert_track(tags)
                # Link series episodes to their show + write metadata.
                if folder_type == "series" and tags.artist:
                    show_folder = self._derive_show_folder(path)
                    show_id = self._db.get_or_create_show(
                        tags.artist, folder_path=show_folder,
                    )
                    conn = self._db._get_conn()
                    conn.execute(
                        "UPDATE tracks SET show_id = ? WHERE id = ?",
                        (show_id, track_id),
                    )
                    conn.commit()
                    if scraper_ep:
                        self._db.upsert_video_metadata(
                            track_id,
                            metadata_provider="tvmaze",
                            plot=scraper_ep.plot or "",
                            poster_path=scraper_ep.still_path or "",
                        )
                self.library_updated.emit()

    def _build_series_tags_for_watchdog(self, path: str):
        """Build episode tags for a single file event (watchdog).

        Derives the show title from the show-level folder using minimal_clean,
        consistent with the bulk scanner.
        """
        from quaver.core.video_parser import _extract_episode, _SEASON_FOLDER, _SPECIALS_FOLDER

        p = Path(path)
        parent = p.parent

        # Walk up from the file to find the show folder.
        # Check if parent is a season folder → show is grandparent.
        if _SEASON_FOLDER.match(parent.name) or _SPECIALS_FOLDER.match(parent.name):
            show_folder = parent.parent
            season_match = _SEASON_FOLDER.match(parent.name)
            folder_season = int(season_match.group(1)) if season_match else 0
        else:
            show_folder = parent
            folder_season = None

        show_title, year = minimal_clean(show_folder.name)
        season_from_file, episode, ep_title_raw, _ = _extract_episode(p.stem)
        season = folder_season if folder_season is not None else season_from_file
        ep_title = extract_episode_title(path)

        return build_episode_tags(show_title, season, episode, ep_title, path, year=year)

    def _match_watchdog_episode(self, scraper_id: int, tags):
        """Check if an episode exists in the cached scraper episode list."""
        from quaver.core.tvmaze import TVMazeClient
        client = TVMazeClient()
        try:
            episodes = client.get_all_episodes(scraper_id)
        finally:
            client.close()
        season = tags.disc_number or 1
        episode = tags.track_number or 0
        if episode <= 0:
            return None
        for ep in episodes:
            if ep.season == season and ep.episode == episode:
                return ep
        return None  # Not in scraper list.

    def _derive_show_folder(self, path: str) -> str | None:
        """Derive the show folder path from a file path and known sources."""
        sources = self._db.get_sources()
        for src in sources:
            if src.get("type") != "series":
                continue
            src_path = src["path"]
            if not path.startswith(src_path):
                continue
            scan_mode = src.get("scan_mode", "multi")
            if scan_mode == "single":
                return src_path.rstrip("/")
            if scan_mode == "deep":
                # Walk up from the file to find the nearest show folder.
                p = Path(path).parent
                src_p = Path(src_path)
                while p != src_p and p != p.parent:
                    if is_show_folder(p):
                        return str(p)
                    p = p.parent
                return None
            # Default (multi): first path component after source.
            relative = path[len(src_path):].lstrip("/")
            parts = relative.split("/")
            if parts:
                return src_path.rstrip("/") + "/" + parts[0]
        return None

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_all_tracks(self) -> list[Track]:
        return self._db.get_all_tracks()

    def get_track_by_id(self, track_id: int) -> Track | None:
        return self._db.get_track_by_id(track_id)

    def shutdown(self) -> None:
        self.stop_watching()
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.quit()
            self._scan_thread.wait(3000)
