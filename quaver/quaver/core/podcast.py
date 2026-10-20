"""Podcast management — RSS parsing, episode tracking, downloads.

No PySide6 imports — must be testable without a display.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import feedparser
import httpx

from quaver.core.library import LibraryDB
from quaver.core.paths import data_dir, image_dir

# Default download location.
_PODCAST_DIR = data_dir() / "podcasts"


_ART_CACHE_DIR = image_dir() / "podcast_art"


@dataclass
class Podcast:
    id: int
    title: str
    feed_url: str
    auto_download: bool
    auto_delete_played: bool
    retain_episodes: int
    refresh_interval_hours: float
    last_fetched: str | None
    artwork_url: str | None = None


@dataclass
class Episode:
    id: int
    podcast_id: int
    title: str
    url: str
    local_path: str | None
    duration_ms: int
    played: bool
    played_position_ms: int
    published_at: str | None
    downloaded: bool
    description: str = ""


class PodcastDB:
    """Podcast-specific database operations using the shared LibraryDB connection."""

    def __init__(self, db: LibraryDB) -> None:
        self._db = db

    def _conn(self):
        return self._db._get_conn()

    # ------------------------------------------------------------------
    # Podcasts
    # ------------------------------------------------------------------

    def add_podcast(self, title: str, feed_url: str, artwork_url: str | None = None) -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO podcasts (title, feed_url, artwork_url) VALUES (?, ?, ?)",
            (title, feed_url, artwork_url),
        )
        conn.commit()
        return cur.lastrowid or 0

    def update_artwork_url(self, podcast_id: int, artwork_url: str | None) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE podcasts SET artwork_url = ? WHERE id = ?",
            (artwork_url, podcast_id),
        )
        conn.commit()

    def remove_podcast(self, podcast_id: int) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM podcasts WHERE id = ?", (podcast_id,))
        conn.commit()

    def get_all_podcasts(self) -> list[Podcast]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM podcasts ORDER BY title").fetchall()
        return [Podcast(**dict(r)) for r in rows]

    def get_podcast(self, podcast_id: int) -> Podcast | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM podcasts WHERE id = ?", (podcast_id,)
        ).fetchone()
        return Podcast(**dict(row)) if row else None

    def update_podcast_settings(
        self,
        podcast_id: int,
        auto_download: bool,
        auto_delete_played: bool,
        retain_episodes: int = 2,
        refresh_interval_hours: float = 24.0,
    ) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE podcasts SET auto_download = ?, auto_delete_played = ?, "
            "retain_episodes = ?, refresh_interval_hours = ? WHERE id = ?",
            (
                int(auto_download), int(auto_delete_played),
                retain_episodes, refresh_interval_hours, podcast_id,
            ),
        )
        conn.commit()

    def update_last_fetched(self, podcast_id: int) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE podcasts SET last_fetched = datetime('now') WHERE id = ?",
            (podcast_id,),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    def upsert_episode(
        self, podcast_id: int, title: str, url: str,
        duration_ms: int = 0, published_at: str | None = None,
        description: str = "",
    ) -> int:
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO podcast_episodes
               (podcast_id, title, url, duration_ms, published_at, description)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (podcast_id, title, url, duration_ms, published_at, description),
        )
        conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM podcast_episodes WHERE podcast_id = ? AND url = ?",
            (podcast_id, url),
        ).fetchone()
        return row["id"] if row else 0

    def upsert_episodes_batch(self, podcast_id: int, episodes: list[dict]) -> None:
        """Insert episodes in a single transaction (much faster for large feeds)."""
        conn = self._conn()
        for ep in episodes:
            conn.execute(
                """INSERT INTO podcast_episodes
                   (podcast_id, title, url, duration_ms, published_at, description)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (podcast_id, ep["title"], ep["url"], ep.get("duration_ms", 0),
                 ep.get("published_at"), ep.get("description", "")),
            )
        conn.commit()

    def get_episodes(self, podcast_id: int, limit: int = 0, offset: int = 0) -> list[Episode]:
        """Get episodes for a podcast, optionally paginated.

        If *limit* is 0, returns all episodes (legacy behavior for retention etc.).
        """
        conn = self._conn()
        if limit > 0:
            rows = conn.execute(
                "SELECT * FROM podcast_episodes WHERE podcast_id = ? "
                "ORDER BY published_at DESC LIMIT ? OFFSET ?",
                (podcast_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM podcast_episodes WHERE podcast_id = ? "
                "ORDER BY published_at DESC",
                (podcast_id,),
            ).fetchall()
        return [Episode(**dict(r)) for r in rows]

    def get_episode_count(self, podcast_id: int) -> int:
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM podcast_episodes WHERE podcast_id = ?",
            (podcast_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_episode(self, episode_id: int) -> Episode | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM podcast_episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        return Episode(**dict(row)) if row else None

    def set_episode_downloaded(
        self, episode_id: int, local_path: str
    ) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE podcast_episodes SET downloaded = 1, local_path = ? WHERE id = ?",
            (local_path, episode_id),
        )
        conn.commit()

    def set_episode_played(self, episode_id: int, played: bool) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE podcast_episodes SET played = ? WHERE id = ?",
            (int(played), episode_id),
        )
        conn.commit()

    def save_playback_position(self, episode_id: int, position_ms: int) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE podcast_episodes SET played_position_ms = ? WHERE id = ?",
            (position_ms, episode_id),
        )
        conn.commit()

    def delete_episode_file(self, episode_id: int) -> None:
        """Remove the downloaded file and clear local_path (single query)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT local_path FROM podcast_episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row and row["local_path"]:
            Path(row["local_path"]).unlink(missing_ok=True)
        conn.execute(
            "UPDATE podcast_episodes SET downloaded = 0, local_path = NULL WHERE id = ?",
            (episode_id,),
        )
        conn.commit()


# ------------------------------------------------------------------
# RSS feed parsing
# ------------------------------------------------------------------


def parse_feed(feed_url: str) -> tuple[str, list[dict], str | None]:
    """Parse an RSS feed. Returns (podcast_title, [episode_dicts], artwork_url).

    Each episode dict has keys: title, url, duration_ms, published_at.
    """
    feed = feedparser.parse(feed_url)
    title = feed.feed.get("title", feed_url)

    # Extract podcast artwork URL.
    artwork_url: str | None = None
    itunes_img = feed.feed.get("itunes_image")
    if itunes_img:
        artwork_url = itunes_img.get("href") if isinstance(itunes_img, dict) else str(itunes_img)
    if not artwork_url:
        img = feed.feed.get("image")
        if img:
            artwork_url = img.get("href") if isinstance(img, dict) else None

    episodes: list[dict] = []
    for entry in feed.entries:
        ep_title = entry.get("title", "Untitled")
        url = ""
        duration_ms = 0

        # Find the audio enclosure.
        for enc in entry.get("enclosures", []):
            if enc.get("type", "").startswith("audio/") or enc.get("href", "").endswith(
                (".mp3", ".m4a", ".ogg", ".opus")
            ):
                url = enc.get("href", "")
                break

        # Fallback: links with audio type.
        if not url:
            for link in entry.get("links", []):
                if link.get("type", "").startswith("audio/"):
                    url = link.get("href", "")
                    break

        if not url:
            continue

        # Duration — itunes:duration tag (can be HH:MM:SS, MM:SS, or seconds).
        raw_dur = entry.get("itunes_duration", "")
        if raw_dur:
            duration_ms = _parse_duration(raw_dur)

        # Published date — normalize to ISO format for correct sorting.
        published = _normalize_date(entry.get("published", ""))

        # Description — prefer itunes:summary, fall back to summary/description.
        description = (
            entry.get("itunes_summary", "")
            or entry.get("summary", "")
            or entry.get("description", "")
        )

        episodes.append({
            "title": ep_title,
            "url": url,
            "duration_ms": duration_ms,
            "published_at": published,
            "description": description,
        })

    return title, episodes, artwork_url


def _normalize_date(raw: str) -> str:
    """Convert an RSS date string to ISO format for correct SQLite sorting."""
    if not raw:
        return ""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(raw)
        return dt.isoformat()
    except (ValueError, TypeError):
        return raw


def _parse_duration(raw: str) -> int:
    """Parse iTunes duration to milliseconds. Handles HH:MM:SS, MM:SS, or seconds."""
    raw = raw.strip()
    if not raw:
        return 0

    parts = raw.split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return (h * 3600 + m * 60 + s) * 1000
        elif len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return (m * 60 + s) * 1000
        else:
            return int(float(parts[0])) * 1000
    except (ValueError, IndexError):
        return 0


def download_episode(
    url: str,
    dest_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Download an episode to disk. Returns the local file path.

    *on_progress(bytes_downloaded, total_bytes)* is called after each chunk.
    *total_bytes* is 0 if the server doesn't send Content-Length.

    Uses streaming download with httpx.
    """
    dest_dir = dest_dir or _PODCAST_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Derive filename from URL.
    from urllib.parse import urlparse, unquote
    parsed = urlparse(url)
    filename = unquote(Path(parsed.path).name) or f"episode_{int(time.time())}.mp3"

    # Sanitize filename.
    filename = "".join(c for c in filename if c.isalnum() or c in ".-_ ")
    dest = dest_dir / filename

    # Don't re-download if already present.
    if dest.exists():
        return str(dest)

    # Use separate connect/read timeouts — podcast files can be large.
    timeout = httpx.Timeout(connect=15, read=300, write=300, pool=15)
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total)

    return str(dest)
