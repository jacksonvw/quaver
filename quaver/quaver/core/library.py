"""Library database — SQLite schema, queries, and background scanner."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

from quaver.core.paths import data_dir
from quaver.core.tagger import read_tags, TrackTags
# Track + MIME constants live in core/track.py so they're importable
# without pulling in the SQLite stack.  Re-exported here for backwards
# compatibility with existing Quaver-side importers.
from quaver.core.track import (  # noqa: F401  (re-exports)
    EPISODE_IDS_MIME,
    PLAYLIST_IDS_MIME,
    TRACK_IDS_MIME,
    VIDEO_IDS_MIME,
    Track,
)

# Default database path.
_DEFAULT_DB = data_dir() / "quaver.db"

# Canonical extension sets live in media_core so Minim and the new
# Quaver "files" library tab share the same definition of what counts
# as a playable file.  Re-exported under the legacy names so existing
# Quaver call sites (``p.suffix.lower() in AUDIO_EXTENSIONS``) work
# unchanged.
from media_core.core.media_exts import (
    AUDIO_EXTS_DOTTED as AUDIO_EXTENSIONS,
    VIDEO_EXTS_DOTTED as VIDEO_EXTENSIONS,
)

# Watch folder type constants.  ``files`` is the live filesystem-tree
# tab type — no scanning, no metadata, no scrapers.
FOLDER_TYPES = ("music", "series", "movies", "files")


class LibraryDB:
    """Thread-safe SQLite wrapper for the music library."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema(self._get_conn())

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        # Rename watch_folders → sources (legacy migration, must run before CREATE).
        try:
            conn.execute("ALTER TABLE watch_folders RENAME TO sources")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Already renamed or never existed.

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tracks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath        TEXT UNIQUE NOT NULL,
                title           TEXT NOT NULL DEFAULT '',
                artist          TEXT NOT NULL DEFAULT '',
                album           TEXT NOT NULL DEFAULT '',
                album_artist    TEXT NOT NULL DEFAULT '',
                year            INTEGER,
                genre           TEXT NOT NULL DEFAULT '',
                track_number    INTEGER,
                disc_number     INTEGER,
                duration_ms     INTEGER NOT NULL DEFAULT 0,
                bitrate         INTEGER NOT NULL DEFAULT 0,
                format          TEXT NOT NULL DEFAULT '',
                replay_gain_track REAL,
                replay_gain_album REAL,
                play_count      INTEGER NOT NULL DEFAULT 0,
                last_played     TEXT,
                date_added      TEXT NOT NULL DEFAULT (datetime('now')),
                rating          INTEGER NOT NULL DEFAULT 0,
                fingerprint     TEXT,
                media_type      TEXT NOT NULL DEFAULT 'audio'
            );

            CREATE TABLE IF NOT EXISTS video_metadata (
                track_id            INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
                plot                TEXT NOT NULL DEFAULT '',
                tmdb_id             INTEGER,
                imdb_id             TEXT,
                poster_path         TEXT,
                backdrop_path       TEXT,
                watched             INTEGER NOT NULL DEFAULT 0,
                watched_position_ms INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS video_shows (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                folder_title    TEXT NOT NULL,
                year            INTEGER,
                plot            TEXT DEFAULT '',
                genre           TEXT DEFAULT '',
                poster_path     TEXT,
                backdrop_path   TEXT,
                scraper         TEXT,
                scraper_id      INTEGER,
                imdb_id         TEXT,
                status          TEXT DEFAULT '',
                date_added      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS albums (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL DEFAULT '',
                artist          TEXT NOT NULL DEFAULT '',
                year            INTEGER,
                cover_art_path  TEXT,
                musicbrainz_id  TEXT
            );

            CREATE TABLE IF NOT EXISTS artists (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL DEFAULT '',
                musicbrainz_id  TEXT
            );

            CREATE TABLE IF NOT EXISTS playlists (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                type            TEXT NOT NULL DEFAULT 'manual',
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS playlist_tracks (
                playlist_id     INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                track_id        INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                position        INTEGER NOT NULL,
                PRIMARY KEY (playlist_id, track_id, position)
            );

            CREATE TABLE IF NOT EXISTS autodj_rules (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id     INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
                source_type     TEXT NOT NULL,
                source_value    TEXT,
                filter_genre    TEXT,
                avoid_recent_hours INTEGER NOT NULL DEFAULT 4
            );

            CREATE TABLE IF NOT EXISTS sources (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                path            TEXT UNIQUE NOT NULL,
                recursive       INTEGER NOT NULL DEFAULT 1,
                last_scanned    TEXT,
                type            TEXT NOT NULL DEFAULT 'music'
            );

            CREATE TABLE IF NOT EXISTS podcasts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                feed_url        TEXT UNIQUE NOT NULL,
                auto_download   INTEGER NOT NULL DEFAULT 1,
                auto_delete_played INTEGER NOT NULL DEFAULT 0,
                retain_episodes INTEGER NOT NULL DEFAULT 2,
                refresh_interval_hours REAL NOT NULL DEFAULT 24.0,
                last_fetched    TEXT
            );

            CREATE TABLE IF NOT EXISTS podcast_episodes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                podcast_id      INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
                title           TEXT NOT NULL,
                url             TEXT NOT NULL,
                local_path      TEXT,
                duration_ms     INTEGER NOT NULL DEFAULT 0,
                played          INTEGER NOT NULL DEFAULT 0,
                played_position_ms INTEGER NOT NULL DEFAULT 0,
                published_at    TEXT,
                downloaded      INTEGER NOT NULL DEFAULT 0,
                UNIQUE(podcast_id, url)
            );

            CREATE TABLE IF NOT EXISTS youtube_channels (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id      TEXT UNIQUE NOT NULL,
                title           TEXT NOT NULL,
                thumbnail_url   TEXT,
                is_subscription INTEGER NOT NULL DEFAULT 0,
                added_at        TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS youtube_videos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id      INTEGER NOT NULL REFERENCES youtube_channels(id) ON DELETE CASCADE,
                video_id        TEXT NOT NULL,
                title           TEXT NOT NULL,
                duration_ms     INTEGER NOT NULL DEFAULT 0,
                thumbnail_url   TEXT,
                published_at    TEXT,
                view_count      INTEGER NOT NULL DEFAULT 0,
                description     TEXT NOT NULL DEFAULT '',
                watched         INTEGER NOT NULL DEFAULT 0,
                local_path      TEXT,
                downloaded      INTEGER NOT NULL DEFAULT 0,
                UNIQUE(channel_id, video_id)
            );

            CREATE TABLE IF NOT EXISTS playlist_youtube_items (
                playlist_id     INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                video_id        INTEGER NOT NULL REFERENCES youtube_videos(id) ON DELETE CASCADE,
                position        INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
            CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album);
            CREATE INDEX IF NOT EXISTS idx_tracks_album_artist ON tracks(album_artist);

            CREATE INDEX IF NOT EXISTS idx_yt_video_channel_date
                ON youtube_videos(channel_id, published_at DESC);
        """)
        # Migrations for columns added after initial schema.
        for col, default in [
            ("retain_episodes", "2"),
            ("refresh_interval_hours", "24.0"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE podcasts ADD COLUMN {col} "
                    f"{'INTEGER' if '.' not in default else 'REAL'} "
                    f"NOT NULL DEFAULT {default}"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists.

        # Add description column to podcast_episodes if missing.
        try:
            conn.execute(
                "ALTER TABLE podcast_episodes ADD COLUMN description TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass

        # Add artwork_url column to podcasts if missing.
        try:
            conn.execute("ALTER TABLE podcasts ADD COLUMN artwork_url TEXT")
        except sqlite3.OperationalError:
            pass

        # Normalize RSS dates in existing episodes to ISO format for correct sorting.
        rows = conn.execute(
            "SELECT id, published_at FROM podcast_episodes WHERE published_at != ''"
        ).fetchall()
        for row in rows:
            raw = row["published_at"]
            if raw and not raw.startswith("20"):  # Not already ISO format.
                from email.utils import parsedate_to_datetime
                try:
                    dt = parsedate_to_datetime(raw)
                    conn.execute(
                        "UPDATE podcast_episodes SET published_at = ? WHERE id = ?",
                        (dt.isoformat(), row["id"]),
                    )
                except (ValueError, TypeError):
                    pass
        conn.commit()

        # Migrate old podcasts that had auto_download=0 default to the new default of 1.
        conn.execute("UPDATE podcasts SET auto_download = 1 WHERE auto_download = 0")

        # Deduplicate podcast episodes and add unique index for existing DBs.
        # Prefer keeping the row that has downloaded=1 (preserves download state).
        conn.execute("""
            DELETE FROM podcast_episodes
            WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY podcast_id, url
                        ORDER BY downloaded DESC, id ASC
                    ) AS rn
                    FROM podcast_episodes
                ) WHERE rn = 1
            )
        """)
        try:
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ep_podcast_url
                    ON podcast_episodes(podcast_id, url)
            """)
        except sqlite3.OperationalError:
            pass  # Index already exists.

        # Performance index for paginated episode queries.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ep_podcast_date
                ON podcast_episodes(podcast_id, published_at DESC)
        """)

        # Add type column to sources if missing (music/series/movies).
        try:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN type TEXT NOT NULL DEFAULT 'music'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Add uri column to sources for network locations (smb://).
        try:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN uri TEXT"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Add scraper column to sources (per-source scraper config).
        try:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN scraper TEXT NOT NULL DEFAULT 'none'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Add scan_mode column to sources (multi/single/deep).
        try:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN scan_mode TEXT NOT NULL DEFAULT 'multi'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Add folder_path column to video_shows (canonical filesystem path).
        try:
            conn.execute(
                "ALTER TABLE video_shows ADD COLUMN folder_path TEXT"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Add media_type column to tracks if missing (audio/movie/series_episode).
        try:
            conn.execute(
                "ALTER TABLE tracks ADD COLUMN media_type TEXT NOT NULL DEFAULT 'audio'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Add genre column to video_metadata if missing.
        try:
            conn.execute(
                "ALTER TABLE video_metadata ADD COLUMN genre TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Add metadata_provider column to video_metadata (NULL = not enriched).
        try:
            conn.execute(
                "ALTER TABLE video_metadata ADD COLUMN metadata_provider TEXT"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Backfill: existing rows with a non-NULL tmdb_id were enriched by TMDb.
        conn.execute(
            "UPDATE video_metadata SET metadata_provider = 'tmdb' "
            "WHERE tmdb_id IS NOT NULL AND metadata_provider IS NULL"
        )

        # Add show_id FK to tracks (links episodes to video_shows).
        try:
            conn.execute(
                "ALTER TABLE tracks ADD COLUMN show_id INTEGER REFERENCES video_shows(id)"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

        # Migrate existing series episodes into video_shows.
        # Group by artist (series title) → one show per unique artist.
        existing_shows = conn.execute(
            "SELECT COUNT(*) FROM video_shows"
        ).fetchone()[0]
        orphan_episodes = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE media_type = 'series_episode' AND show_id IS NULL"
        ).fetchone()[0]
        if orphan_episodes > 0:
            # Get distinct series titles from episodes that have no show_id.
            series = conn.execute(
                "SELECT DISTINCT artist FROM tracks "
                "WHERE media_type = 'series_episode' AND show_id IS NULL AND artist != ''"
            ).fetchall()
            for row in series:
                folder_title = row[0]
                # Check if a show already exists for this folder title.
                existing = conn.execute(
                    "SELECT id FROM video_shows WHERE folder_title = ?",
                    (folder_title,),
                ).fetchone()
                if existing:
                    show_id = existing[0]
                else:
                    # Create new show from available data.
                    # Pull genre/poster/year from the first enriched episode if available.
                    meta = conn.execute(
                        "SELECT vm.genre, vm.poster_path, t.year "
                        "FROM tracks t LEFT JOIN video_metadata vm ON t.id = vm.track_id "
                        "WHERE t.artist = ? AND t.media_type = 'series_episode' "
                        "AND vm.genre != '' "
                        "LIMIT 1",
                        (folder_title,),
                    ).fetchone()
                    genre = meta[0] if meta else ""
                    poster = meta[1] if meta else ""
                    year = meta[2] if meta else None
                    cur = conn.execute(
                        "INSERT INTO video_shows (title, folder_title, year, genre, poster_path) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (folder_title, folder_title, year, genre, poster),
                    )
                    show_id = cur.lastrowid
                # Link all episodes with this artist to the show.
                conn.execute(
                    "UPDATE tracks SET show_id = ? "
                    "WHERE artist = ? AND media_type = 'series_episode' AND show_id IS NULL",
                    (show_id, folder_title),
                )
            log.info(
                "Migrated %d series into video_shows (%d episodes linked)",
                len(series), orphan_episodes,
            )

        # Backfill scraper on sources from tab config (one-time migration).
        needs_backfill = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE scraper != 'none'"
        ).fetchone()[0]
        if needs_backfill == 0:
            # Read config to populate source-level scraper settings.
            try:
                from quaver.core.config import load_config as _load_cfg
                cfg = _load_cfg()
                for src in cfg.sources:
                    if src.scraper == "none":
                        continue
                    conn.execute(
                        "UPDATE sources SET scraper = ? WHERE path = ? AND scraper = 'none'",
                        (src.scraper, src.path),
                    )
            except Exception:
                pass  # Config file may not exist yet.

        # Backfill folder_path on video_shows from episode filepaths.
        shows_without_path = conn.execute(
            "SELECT id FROM video_shows WHERE folder_path IS NULL"
        ).fetchall()
        if shows_without_path:
            sources = conn.execute("SELECT path FROM sources WHERE type = 'series'").fetchall()
            source_paths = [r[0] for r in sources]
            for (show_id,) in shows_without_path:
                # Find an episode filepath to derive the show folder.
                row = conn.execute(
                    "SELECT filepath FROM tracks WHERE show_id = ? LIMIT 1",
                    (show_id,),
                ).fetchone()
                if not row:
                    continue
                fp = row[0]
                # The show folder is the first directory under a source path.
                for src_path in source_paths:
                    if fp.startswith(src_path):
                        # Extract the show-level folder:
                        # source/ShowName/Season 1/file.mkv → source/ShowName
                        relative = fp[len(src_path):].lstrip("/")
                        parts = relative.split("/")
                        if parts:
                            show_folder = src_path.rstrip("/") + "/" + parts[0]
                            conn.execute(
                                "UPDATE video_shows SET folder_path = ? WHERE id = ?",
                                (show_folder, show_id),
                            )
                        break

        # Create scan_cache table for incremental scanning.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_cache (
                dir_path    TEXT PRIMARY KEY,
                source_id   INTEGER REFERENCES sources(id) ON DELETE CASCADE,
                content_hash TEXT NOT NULL,
                last_scanned TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        conn.commit()

    # ------------------------------------------------------------------
    # Scan cache (incremental scanning)
    # ------------------------------------------------------------------

    def get_scan_cache_hash(self, dir_path: str) -> str | None:
        """Return the cached content_hash for *dir_path*, or None."""
        row = self._get_conn().execute(
            "SELECT content_hash FROM scan_cache WHERE dir_path = ?",
            (dir_path,),
        ).fetchone()
        return row[0] if row else None

    def set_scan_cache(
        self, dir_path: str, source_id: int, content_hash: str,
    ) -> None:
        """Insert or update a scan cache entry."""
        self._get_conn().execute(
            "INSERT INTO scan_cache (dir_path, source_id, content_hash, last_scanned) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(dir_path) DO UPDATE SET "
            "content_hash = excluded.content_hash, last_scanned = excluded.last_scanned",
            (dir_path, source_id, content_hash),
        )
        self._get_conn().commit()

    def clear_scan_cache_for_source(self, source_id: int) -> None:
        """Remove all scan cache entries for a source (forces full rescan)."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM scan_cache WHERE source_id = ?", (source_id,),
        )
        conn.commit()

    def clear_scan_cache(self) -> None:
        """Remove all scan cache entries (forces full rescan everywhere)."""
        conn = self._get_conn()
        conn.execute("DELETE FROM scan_cache")
        conn.commit()

    # ------------------------------------------------------------------
    # Track queries
    # ------------------------------------------------------------------

    def upsert_track(self, tags: TrackTags) -> int:
        """Insert or update a track from scanned tags. Returns the track id.

        For video tracks that have already been enriched, title/artist/
        album_artist are preserved to avoid overwriting scraped metadata
        with filename-derived stubs.
        """
        conn = self._get_conn()
        cur = conn.execute(
            """
            INSERT INTO tracks (
                filepath, title, artist, album, album_artist, year, genre,
                track_number, disc_number, duration_ms, bitrate, format,
                replay_gain_track, replay_gain_album, media_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET
                title = CASE
                    WHEN excluded.media_type IN ('movie', 'series_episode')
                         AND EXISTS (
                             SELECT 1 FROM video_metadata
                             WHERE track_id = tracks.id
                               AND metadata_provider IS NOT NULL
                               AND metadata_provider NOT LIKE '%:not_found'
                         )
                    THEN tracks.title
                    ELSE excluded.title END,
                artist = CASE
                    WHEN excluded.media_type IN ('movie', 'series_episode')
                         AND EXISTS (
                             SELECT 1 FROM video_metadata
                             WHERE track_id = tracks.id
                               AND metadata_provider IS NOT NULL
                               AND metadata_provider NOT LIKE '%:not_found'
                         )
                    THEN tracks.artist
                    ELSE excluded.artist END,
                album=excluded.album,
                album_artist = CASE
                    WHEN excluded.media_type IN ('movie', 'series_episode')
                         AND EXISTS (
                             SELECT 1 FROM video_metadata
                             WHERE track_id = tracks.id
                               AND metadata_provider IS NOT NULL
                               AND metadata_provider NOT LIKE '%:not_found'
                         )
                    THEN tracks.album_artist
                    ELSE excluded.album_artist END,
                year=excluded.year, genre=excluded.genre,
                track_number=excluded.track_number, disc_number=excluded.disc_number,
                duration_ms=excluded.duration_ms, bitrate=excluded.bitrate,
                format=excluded.format,
                replay_gain_track=excluded.replay_gain_track,
                replay_gain_album=excluded.replay_gain_album,
                media_type=excluded.media_type
            """,
            (
                tags.filepath, tags.title, tags.artist, tags.album,
                tags.album_artist, tags.year, tags.genre,
                tags.track_number, tags.disc_number, tags.duration_ms,
                tags.bitrate, tags.format,
                tags.replay_gain_track, tags.replay_gain_album,
                tags.media_type,
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def remove_missing_tracks(self, existing_paths: set[str]) -> int:
        """Remove tracks whose files no longer exist. Returns count removed."""
        conn = self._get_conn()
        cur = conn.execute("SELECT id, filepath FROM tracks")
        to_delete = [row["id"] for row in cur if row["filepath"] not in existing_paths]
        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            conn.execute(f"DELETE FROM tracks WHERE id IN ({placeholders})", to_delete)
            conn.commit()
        return len(to_delete)

    def remove_missing_tracks_for_source(
        self, source_path: str, existing_paths: set[str],
    ) -> int:
        """Remove tracks under *source_path* that aren't in *existing_paths*.

        Scoped to one source so that unmounted/skipped sources don't lose data.
        """
        conn = self._get_conn()
        prefix = source_path.rstrip("/") + "/"
        cur = conn.execute(
            "SELECT id, filepath FROM tracks WHERE filepath LIKE ?",
            (prefix + "%",),
        )
        to_delete = [row["id"] for row in cur if row["filepath"] not in existing_paths]
        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            conn.execute(
                f"DELETE FROM tracks WHERE id IN ({placeholders})", to_delete,
            )
            conn.commit()
        return len(to_delete)

    def update_track_duration(self, track_id: int, duration_ms: int) -> None:
        """Set duration for a track (lazy backfill from playback)."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE tracks SET duration_ms = ? WHERE id = ?",
            (duration_ms, track_id),
        )
        conn.commit()

    def get_library_stats(self) -> tuple[int, int]:
        """Return (track_count, total_duration_ms)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration_ms), 0) FROM tracks"
        ).fetchone()
        return (row[0], row[1])

    def get_all_tracks(self) -> list[Track]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tracks ORDER BY album_artist, album, disc_number, track_number"
        ).fetchall()
        return [Track(**dict(r)) for r in rows]

    def get_track_by_id(self, track_id: int) -> Track | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return Track(**dict(row)) if row else None

    def get_track_by_path(self, filepath: str) -> Track | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM tracks WHERE filepath = ?", (filepath,)
        ).fetchone()
        return Track(**dict(row)) if row else None

    def get_tracks_by_folders(self, folder_paths: list[str]) -> list[Track]:
        """Return tracks whose filepath starts with any of the given folders.

        An empty list means no folders — returns nothing.  Callers that want
        all tracks should call ``get_all_tracks()`` explicitly.
        """
        if not folder_paths:
            return []
        conn = self._get_conn()
        clauses = " OR ".join("filepath LIKE ?" for _ in folder_paths)
        params = [p.rstrip("/") + "/%" for p in folder_paths]
        rows = conn.execute(
            f"SELECT * FROM tracks WHERE {clauses} "
            "ORDER BY album_artist, album, disc_number, track_number",
            params,
        ).fetchall()
        return [Track(**dict(r)) for r in rows]

    def get_tracks_by_album(self, album_artist: str, album: str) -> list[Track]:
        """Return all tracks for a given album, ordered by disc/track number."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tracks "
            "WHERE (album_artist = ? OR (album_artist = '' AND artist = ?)) "
            "AND album = ? "
            "ORDER BY disc_number, track_number",
            (album_artist, album_artist, album),
        ).fetchall()
        return [Track(**dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def add_source(
        self, path: str, recursive: bool = True, folder_type: str = "music",
        uri: str | None = None,
    ) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO sources (path, recursive, type, uri) "
            "VALUES (?, ?, ?, ?)",
            (path, int(recursive), folder_type, uri),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_source_type(self, source_id: int, folder_type: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE sources SET type = ? WHERE id = ?",
            (folder_type, source_id),
        )
        conn.commit()

    def remove_source(self, source_id: int) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()

    def get_sources(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM sources").fetchall()
        return [dict(r) for r in rows]

    def update_last_scanned(self, folder_id: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE sources SET last_scanned = datetime('now') WHERE id = ?",
            (folder_id,),
        )
        conn.commit()

    def update_source_scraper(self, source_id: int, scraper: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE sources SET scraper = ? WHERE id = ?",
            (scraper, source_id),
        )
        conn.commit()

    def get_scraper_for_path(self, filepath: str) -> str:
        """Return the scraper configured for the source that contains *filepath*."""
        conn = self._get_conn()
        # Find the source whose path is a prefix of filepath (longest match).
        rows = conn.execute(
            "SELECT path, scraper FROM sources ORDER BY LENGTH(path) DESC"
        ).fetchall()
        for row in rows:
            if filepath.startswith(row[0]):
                return row[1] or "none"
        return "none"

    def sync_sources(self, sources) -> None:
        """Reconcile sources table to match config (sources are authoritative).

        Each SourceConfig becomes one source row. Source rows not in the list
        are deleted (CASCADE cleans scan_cache) and their tracks are pruned.
        """
        conn = self._get_conn()

        # Collect desired source rows.
        desired: dict[str, dict] = {}  # path → {type, scraper, uri, scan_mode}
        for src in sources:
            desired[src.path] = {
                "type": src.media_type,
                "scraper": src.scraper,
                "uri": src.uri or None,
                "scan_mode": src.scan_mode,
            }

        # Find source paths being removed so we can prune their tracks.
        existing_rows = conn.execute("SELECT path FROM sources").fetchall()
        removed_paths = [
            row[0] for row in existing_rows if row[0] not in desired
        ]

        # Upsert desired sources.
        for path, attrs in desired.items():
            conn.execute(
                "INSERT INTO sources (path, recursive, type, scraper, uri, scan_mode) "
                "VALUES (?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "type = excluded.type, scraper = excluded.scraper, "
                "uri = excluded.uri, scan_mode = excluded.scan_mode",
                (path, attrs["type"], attrs["scraper"], attrs["uri"], attrs["scan_mode"]),
            )

        # Delete source rows not in config.
        if desired:
            placeholders = ",".join("?" * len(desired))
            conn.execute(
                f"DELETE FROM sources WHERE path NOT IN ({placeholders})",
                list(desired.keys()),
            )
        else:
            conn.execute("DELETE FROM sources")

        # Prune tracks belonging to removed sources.
        for rpath in removed_paths:
            prefix = rpath.rstrip("/") + "/"
            conn.execute(
                "DELETE FROM tracks WHERE filepath LIKE ?", (prefix + "%",),
            )

        conn.commit()

    # ------------------------------------------------------------------
    # Playlist queries
    # ------------------------------------------------------------------

    def create_playlist(self, name: str) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT INTO playlists (name, created_at) VALUES (?, datetime('now'))",
            (name,),
        )
        conn.commit()
        return cur.lastrowid or 0

    def delete_playlist(self, playlist_id: int) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        conn.commit()

    def rename_playlist(self, playlist_id: int, name: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE playlists SET name = ? WHERE id = ?", (name, playlist_id)
        )
        conn.commit()

    def get_all_playlists(self) -> list[tuple[int, str, int]]:
        """Return [(id, name, track_count), ...] ordered by name."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT p.id, p.name, COUNT(pt.track_id) AS track_count
            FROM playlists p
            LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
            GROUP BY p.id
            ORDER BY p.name
        """).fetchall()
        return [(r["id"], r["name"], r["track_count"]) for r in rows]

    def get_playlist_tracks(self, playlist_id: int) -> list[Track]:
        """Return tracks in a playlist ordered by position."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT t.* FROM tracks t
            JOIN playlist_tracks pt ON pt.track_id = t.id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position
        """, (playlist_id,)).fetchall()
        return [Track(**dict(r)) for r in rows]

    def add_tracks_to_playlist(self, playlist_id: int, track_ids: list[int]) -> None:
        """Append tracks to the end of a playlist."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
            "FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        start_pos = row["next_pos"]
        for i, tid in enumerate(track_ids):
            conn.execute(
                "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) "
                "VALUES (?, ?, ?)",
                (playlist_id, tid, start_pos + i),
            )
        conn.commit()

    def remove_track_from_playlist(self, playlist_id: int, position: int) -> None:
        """Remove a track at *position* and compact remaining positions."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ? AND position = ?",
            (playlist_id, position),
        )
        # Compact: shift positions down.
        conn.execute(
            "UPDATE playlist_tracks SET position = position - 1 "
            "WHERE playlist_id = ? AND position > ?",
            (playlist_id, position),
        )
        conn.commit()

    def move_playlist_track(self, playlist_id: int, from_pos: int, to_pos: int) -> None:
        """Move a track from one position to another within a playlist."""
        if from_pos == to_pos:
            return
        conn = self._get_conn()
        # Temporarily move to -1 to avoid unique constraint.
        conn.execute(
            "UPDATE playlist_tracks SET position = -1 "
            "WHERE playlist_id = ? AND position = ?",
            (playlist_id, from_pos),
        )
        if from_pos < to_pos:
            conn.execute(
                "UPDATE playlist_tracks SET position = position - 1 "
                "WHERE playlist_id = ? AND position > ? AND position <= ?",
                (playlist_id, from_pos, to_pos),
            )
        else:
            conn.execute(
                "UPDATE playlist_tracks SET position = position + 1 "
                "WHERE playlist_id = ? AND position >= ? AND position < ?",
                (playlist_id, to_pos, from_pos),
            )
        conn.execute(
            "UPDATE playlist_tracks SET position = ? "
            "WHERE playlist_id = ? AND position = -1",
            (to_pos, playlist_id),
        )
        conn.commit()

    def reorder_playlist(self, playlist_id: int, track_ids: list[int]) -> None:
        """Re-save all track positions for a playlist in the given order."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        )
        for pos, tid in enumerate(track_ids):
            conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) "
                "VALUES (?, ?, ?)",
                (playlist_id, tid, pos),
            )
        conn.commit()

    # ------------------------------------------------------------------
    # Video shows
    # ------------------------------------------------------------------

    def get_or_create_show(
        self, folder_title: str, *, folder_path: str | None = None,
    ) -> int:
        """Find or create a video_shows row. Returns show id.

        Lookup order:
        1. Exact ``folder_path`` match (most stable across parser changes)
        2. Exact ``folder_title`` match (backward compat)
        3. Create new
        """
        conn = self._get_conn()

        # 1. Match by folder_path (canonical filesystem location).
        if folder_path:
            row = conn.execute(
                "SELECT id FROM video_shows WHERE folder_path = ?",
                (folder_path,),
            ).fetchone()
            if row:
                return row[0]

        # 2. Match by folder_title (text-based fallback).
        row = conn.execute(
            "SELECT id FROM video_shows WHERE folder_title = ?",
            (folder_title,),
        ).fetchone()
        if row:
            show_id = row[0]
            # Backfill folder_path if it wasn't set.
            if folder_path:
                conn.execute(
                    "UPDATE video_shows SET folder_path = ? WHERE id = ? AND folder_path IS NULL",
                    (folder_path, show_id),
                )
                conn.commit()
            return show_id

        # 3. Create new show.
        cur = conn.execute(
            "INSERT INTO video_shows (title, folder_title, folder_path) VALUES (?, ?, ?)",
            (folder_title, folder_title, folder_path),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_show(self, show_id: int) -> dict | None:
        """Return full show metadata dict, or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM video_shows WHERE id = ?", (show_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_show_by_folder(self, folder_path: str) -> dict | None:
        """Return a show matching *folder_path*, or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM video_shows WHERE folder_path = ?",
            (folder_path,),
        ).fetchone()
        return dict(row) if row else None

    def get_all_shows(self) -> list[dict]:
        """Return all shows ordered by title."""
        conn = self._get_conn()
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM video_shows ORDER BY title"
            ).fetchall()
        ]

    def update_show(self, show_id: int, **kwargs: str | int | None) -> None:
        """Update show fields (title, year, genre, scraper, scraper_id, etc.)."""
        if not kwargs:
            return
        conn = self._get_conn()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        conn.execute(
            f"UPDATE video_shows SET {sets} WHERE id = ?",
            (*kwargs.values(), show_id),
        )
        conn.commit()

    def get_show_episode_ids(self, show_id: int) -> list[int]:
        """Return all track IDs for episodes of a show."""
        conn = self._get_conn()
        return [
            r[0] for r in conn.execute(
                "SELECT id FROM tracks WHERE show_id = ? ORDER BY disc_number, track_number",
                (show_id,),
            ).fetchall()
        ]

    def get_show_for_track(self, track_id: int) -> dict | None:
        """Return the show dict for a track, or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT vs.* FROM video_shows vs "
            "JOIN tracks t ON t.show_id = vs.id WHERE t.id = ?",
            (track_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_shows_with_counts(
        self, folders: list[str] | None = None,
    ) -> list[dict]:
        """Return all shows with episode_count and season_count.

        If *folders* is provided, only include shows that have episodes
        in those source folders.  Each dict has all video_shows columns
        plus ``episode_count`` and ``season_count``.
        """
        conn = self._get_conn()
        if folders:
            like_clauses = " OR ".join(
                "t.filepath LIKE ?" for _ in folders
            )
            params = [f.rstrip("/") + "/%" for f in folders]
            rows = conn.execute(
                "SELECT vs.*, "
                "  COUNT(t.id) AS episode_count, "
                "  COUNT(DISTINCT t.disc_number) AS season_count "
                "FROM video_shows vs "
                "JOIN tracks t ON t.show_id = vs.id "
                f"WHERE ({like_clauses}) "
                "GROUP BY vs.id ORDER BY vs.title COLLATE NOCASE",
                params,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT vs.*, "
                "  COUNT(t.id) AS episode_count, "
                "  COUNT(DISTINCT t.disc_number) AS season_count "
                "FROM video_shows vs "
                "JOIN tracks t ON t.show_id = vs.id "
                "GROUP BY vs.id ORDER BY vs.title COLLATE NOCASE",
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_show_watched(self, show_id: int, watched: bool = True) -> None:
        """Mark all episodes of a show as watched or unwatched."""
        conn = self._get_conn()
        episode_ids = self.get_show_episode_ids(show_id)
        if not episode_ids:
            return
        val = 1 if watched else 0
        for eid in episode_ids:
            conn.execute(
                "INSERT OR IGNORE INTO video_metadata (track_id) VALUES (?)",
                (eid,),
            )
        placeholders = ", ".join("?" for _ in episode_ids)
        conn.execute(
            f"UPDATE video_metadata SET watched = ? "
            f"WHERE track_id IN ({placeholders})",
            (val, *episode_ids),
        )
        conn.commit()

    def remove_show(self, show_id: int) -> list[int]:
        """Remove a show and all its episodes from the library.

        Deletes video_metadata, tracks, and the video_shows row.
        Returns the list of removed track IDs (for UI refresh).
        """
        conn = self._get_conn()
        episode_ids = self.get_show_episode_ids(show_id)
        if episode_ids:
            ph = ", ".join("?" for _ in episode_ids)
            conn.execute(
                f"DELETE FROM video_metadata WHERE track_id IN ({ph})",
                tuple(episode_ids),
            )
            conn.execute(
                f"DELETE FROM tracks WHERE id IN ({ph})",
                tuple(episode_ids),
            )
        conn.execute("DELETE FROM video_shows WHERE id = ?", (show_id,))
        conn.commit()
        return episode_ids

    def reset_stub_episode_titles(self) -> int:
        """Reset metadata_provider for episodes with stub 'Episode N' titles.

        These episodes were marked as enriched but the API returned no
        real episode title.  Resetting allows re-enrichment to retry.
        Returns the count of episodes reset.
        """
        conn = self._get_conn()
        cur = conn.execute(
            """
            UPDATE video_metadata SET metadata_provider = NULL
            WHERE track_id IN (
                SELECT t.id FROM tracks t
                WHERE t.media_type = 'series_episode'
                  AND t.title GLOB 'Episode [0-9]*'
            )
            AND metadata_provider IS NOT NULL
            AND metadata_provider NOT LIKE '%:not_found'
            """,
        )
        conn.commit()
        return cur.rowcount

    def prune_orphaned_shows(self) -> int:
        """Delete video_shows entries with zero linked episodes.

        Returns the count of shows removed.
        """
        conn = self._get_conn()
        cur = conn.execute(
            """
            DELETE FROM video_shows WHERE id NOT IN (
                SELECT DISTINCT show_id FROM tracks
                WHERE show_id IS NOT NULL
            )
            """,
        )
        conn.commit()
        return cur.rowcount

    def get_episodes_missing_poster(self) -> list[dict]:
        """Return tvmaze-enriched episodes with NULL or empty poster_path.

        Returns list of dicts with keys: track_id, show_id, disc_number
        (season), track_number (episode), scraper_id.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT t.id AS track_id, t.show_id, t.disc_number, t.track_number,
                   vs.scraper_id
            FROM tracks t
            JOIN video_metadata vm ON vm.track_id = t.id
            JOIN video_shows vs ON t.show_id = vs.id
            WHERE t.media_type = 'series_episode'
              AND vm.metadata_provider = 'tvmaze'
              AND (vm.poster_path IS NULL OR vm.poster_path = '')
              AND vs.scraper_id IS NOT NULL
            """,
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Video metadata
    # ------------------------------------------------------------------

    def upsert_video_metadata(
        self, track_id: int, **kwargs: str | int | None,
    ) -> None:
        """Insert or update video metadata for a track.

        Accepted kwargs: plot, tmdb_id, imdb_id, poster_path, backdrop_path,
        watched, watched_position_ms.
        """
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO video_metadata (track_id) VALUES (?)",
            (track_id,),
        )
        if kwargs:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            conn.execute(
                f"UPDATE video_metadata SET {sets} WHERE track_id = ?",
                (*kwargs.values(), track_id),
            )
        conn.commit()

    def update_track_info(self, track_id: int, **kwargs: str | int | None) -> None:
        """Update track-level fields (title, artist, album_artist, year).

        Used by enrichment to replace placeholder titles with scraped data.
        Only updates fields that are provided and non-empty.
        """
        updates = {k: v for k, v in kwargs.items() if v}
        if not updates:
            return
        conn = self._get_conn()
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE tracks SET {sets} WHERE id = ?",
            (*updates.values(), track_id),
        )
        conn.commit()

    def get_video_metadata(self, track_id: int) -> dict | None:
        """Return video metadata dict for a track, or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM video_metadata WHERE track_id = ?", (track_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_video_watched(self, track_id: int, watched: bool) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO video_metadata (track_id) VALUES (?)",
            (track_id,),
        )
        conn.execute(
            "UPDATE video_metadata SET watched = ? WHERE track_id = ?",
            (int(watched), track_id),
        )
        conn.commit()

    def save_video_position(self, track_id: int, position_ms: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO video_metadata (track_id) VALUES (?)",
            (track_id,),
        )
        conn.execute(
            "UPDATE video_metadata SET watched_position_ms = ? WHERE track_id = ?",
            (position_ms, track_id),
        )
        conn.commit()

    def clear_metadata_provider(self, track_ids: list[int]) -> None:
        """Reset metadata_provider to NULL so tracks are re-enriched on next run."""
        if not track_ids:
            return
        conn = self._get_conn()
        for i in range(0, len(track_ids), 500):
            batch = track_ids[i : i + 500]
            placeholders = ",".join("?" * len(batch))
            conn.execute(
                f"UPDATE video_metadata SET metadata_provider = NULL "
                f"WHERE track_id IN ({placeholders})",
                batch,
            )
        conn.commit()

    def get_video_states(
        self, track_ids: list[int],
    ) -> dict[int, tuple[bool, int]]:
        """Return {track_id: (watched, watched_position_ms)} for given IDs."""
        if not track_ids:
            return {}
        conn = self._get_conn()
        result: dict[int, tuple[bool, int]] = {}
        # Query in batches to avoid SQLite variable limit.
        for i in range(0, len(track_ids), 500):
            batch = track_ids[i : i + 500]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT track_id, watched, watched_position_ms "
                f"FROM video_metadata WHERE track_id IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                result[row[0]] = (bool(row[1]), row[2])
        return result

    def get_unenriched_video_tracks(
        self, folder_paths: list[str] | None = None,
        *, include_failed: bool = True,
    ) -> list[Track]:
        """Return video tracks that need enrichment.

        Always includes tracks with no metadata (never enriched).
        If *include_failed* is True (default), also includes tracks where a
        previous lookup failed (``metadata_provider`` ends with ``:not_found``).
        If *folder_paths* is given, only return tracks under those folders.
        """
        conn = self._get_conn()
        if include_failed:
            provider_cond = "(vm.metadata_provider IS NULL OR vm.metadata_provider LIKE '%:not_found')"
        else:
            provider_cond = "vm.metadata_provider IS NULL"
        conditions = [
            "t.media_type IN ('movie', 'series_episode')",
            provider_cond,
        ]
        params: list = []

        if folder_paths:
            clauses = " OR ".join("t.filepath LIKE ?" for _ in folder_paths)
            conditions.append(f"({clauses})")
            params.extend(p.rstrip("/") + "/%" for p in folder_paths)

        where = " AND ".join(conditions)
        rows = conn.execute(
            f"""
            SELECT t.* FROM tracks t
            LEFT JOIN video_metadata vm ON vm.track_id = t.id
            WHERE {where}
            ORDER BY t.media_type, t.artist, t.album, t.track_number
            """,
            params,
        ).fetchall()
        return [Track(**dict(r)) for r in rows]

    def get_poster_paths(
        self, folder_paths: list[str] | None = None,
    ) -> dict[str, str]:
        """Return {filepath: poster_path} for video tracks with a poster.

        If *folder_paths* is given, only return tracks under those folders.
        """
        conn = self._get_conn()
        conditions = ["vm.poster_path IS NOT NULL AND vm.poster_path != ''"]
        params: list = []
        if folder_paths:
            clauses = " OR ".join("t.filepath LIKE ?" for _ in folder_paths)
            conditions.append(f"({clauses})")
            params.extend(p.rstrip("/") + "/%" for p in folder_paths)
        where = " AND ".join(conditions)
        rows = conn.execute(
            f"""
            SELECT t.filepath, vm.poster_path
            FROM tracks t
            JOIN video_metadata vm ON vm.track_id = t.id
            WHERE {where}
            """,
            params,
        ).fetchall()
        return {r["filepath"]: r["poster_path"] for r in rows}

    def get_video_genres(
        self, folder_paths: list[str] | None = None,
    ) -> dict[int, str]:
        """Return {track_id: genre_string} for video tracks with a genre.

        If *folder_paths* is given, only return tracks under those folders.
        """
        conn = self._get_conn()
        conditions = ["vm.genre IS NOT NULL AND vm.genre != ''"]
        params: list = []
        if folder_paths:
            clauses = " OR ".join("t.filepath LIKE ?" for _ in folder_paths)
            conditions.append(f"({clauses})")
            params.extend(p.rstrip("/") + "/%" for p in folder_paths)
        where = " AND ".join(conditions)
        rows = conn.execute(
            f"""
            SELECT vm.track_id, vm.genre
            FROM video_metadata vm
            JOIN tracks t ON t.id = vm.track_id
            WHERE {where}
            """,
            params,
        ).fetchall()
        return {r["track_id"]: r["genre"] for r in rows}

    def get_video_tracks_by_type(self, media_type: str) -> list[Track]:
        """Return all video tracks of a given media_type."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tracks WHERE media_type = ? ORDER BY artist, album, track_number",
            (media_type,),
        ).fetchall()
        return [Track(**dict(r)) for r in rows]
