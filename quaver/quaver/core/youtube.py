"""YouTube integration — channel/video data, yt-dlp wrappers.

No PySide6 imports — must be testable without a display.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
import yt_dlp

from quaver.core.library import LibraryDB
from quaver.core.paths import data_dir, image_dir

log = logging.getLogger(__name__)

# Default download / storage locations.
_YOUTUBE_DIR = data_dir() / "youtube"
_THUMB_CACHE_DIR = image_dir() / "youtube_thumbs"

# yt-dlp "quiet" base options shared by all calls.
_YDL_BASE: dict = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
}


# ------------------------------------------------------------------
# Dataclasses
# ------------------------------------------------------------------


@dataclass
class Channel:
    id: int
    channel_id: str
    title: str
    thumbnail_url: str | None
    is_subscription: bool
    added_at: str


@dataclass
class Video:
    id: int
    channel_id: int  # FK → youtube_channels.id (DB row id)
    video_id: str  # YouTube 11-char video ID
    title: str
    duration_ms: int
    thumbnail_url: str | None
    published_at: str | None
    view_count: int
    description: str
    watched: bool
    local_path: str | None
    downloaded: bool


# ------------------------------------------------------------------
# YouTubeDB — database operations (follows PodcastDB pattern)
# ------------------------------------------------------------------


class YouTubeDB:
    """YouTube-specific database operations using the shared LibraryDB connection."""

    def __init__(self, db: LibraryDB) -> None:
        self._db = db

    def _conn(self):
        return self._db._get_conn()

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def add_channel(
        self,
        channel_id: str,
        title: str,
        thumbnail_url: str | None = None,
        is_subscription: bool = False,
    ) -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO youtube_channels "
            "(channel_id, title, thumbnail_url, is_subscription) VALUES (?, ?, ?, ?)",
            (channel_id, title, thumbnail_url, int(is_subscription)),
        )
        conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM youtube_channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["id"] if row else 0

    def remove_channel(self, channel_db_id: int) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM youtube_channels WHERE id = ?", (channel_db_id,))
        conn.commit()

    def get_all_channels(self) -> list[Channel]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM youtube_channels ORDER BY title"
        ).fetchall()
        return [
            Channel(
                id=r["id"],
                channel_id=r["channel_id"],
                title=r["title"],
                thumbnail_url=r["thumbnail_url"],
                is_subscription=bool(r["is_subscription"]),
                added_at=r["added_at"],
            )
            for r in rows
        ]

    def get_channel(self, channel_db_id: int) -> Channel | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM youtube_channels WHERE id = ?", (channel_db_id,)
        ).fetchone()
        if not row:
            return None
        return Channel(
            id=row["id"],
            channel_id=row["channel_id"],
            title=row["title"],
            thumbnail_url=row["thumbnail_url"],
            is_subscription=bool(row["is_subscription"]),
            added_at=row["added_at"],
        )

    def get_channel_by_yt_id(self, yt_channel_id: str) -> Channel | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM youtube_channels WHERE channel_id = ?", (yt_channel_id,)
        ).fetchone()
        if not row:
            return None
        return Channel(
            id=row["id"],
            channel_id=row["channel_id"],
            title=row["title"],
            thumbnail_url=row["thumbnail_url"],
            is_subscription=bool(row["is_subscription"]),
            added_at=row["added_at"],
        )

    def update_channel_thumbnail(
        self, channel_db_id: int, thumbnail_url: str | None
    ) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE youtube_channels SET thumbnail_url = ? WHERE id = ?",
            (thumbnail_url, channel_db_id),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Videos
    # ------------------------------------------------------------------

    def upsert_videos_batch(self, channel_db_id: int, videos: list[dict]) -> None:
        """Insert videos in a single transaction (fast for large batches).

        Each dict: video_id, title, duration_ms, thumbnail_url, published_at,
                   view_count, description.
        """
        conn = self._conn()
        for v in videos:
            conn.execute(
                """INSERT INTO youtube_videos
                   (channel_id, video_id, title, duration_ms, thumbnail_url,
                    published_at, view_count, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(channel_id, video_id) DO UPDATE SET
                       title=excluded.title,
                       duration_ms=excluded.duration_ms,
                       thumbnail_url=excluded.thumbnail_url,
                       published_at=COALESCE(NULLIF(excluded.published_at, ''), youtube_videos.published_at),
                       view_count=excluded.view_count,
                       description=excluded.description""",
                (
                    channel_db_id,
                    v["video_id"],
                    v["title"],
                    v.get("duration_ms", 0),
                    v.get("thumbnail_url"),
                    v.get("published_at"),
                    v.get("view_count", 0),
                    v.get("description", ""),
                ),
            )
        conn.commit()

    def get_videos(
        self, channel_db_id: int, limit: int = 0, offset: int = 0
    ) -> list[Video]:
        """Get videos for a channel, optionally paginated."""
        conn = self._conn()
        if limit > 0:
            rows = conn.execute(
                "SELECT * FROM youtube_videos WHERE channel_id = ? "
                "ORDER BY published_at DESC LIMIT ? OFFSET ?",
                (channel_db_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM youtube_videos WHERE channel_id = ? "
                "ORDER BY published_at DESC",
                (channel_db_id,),
            ).fetchall()
        return [_row_to_video(r) for r in rows]

    def get_video(self, video_db_id: int) -> Video | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM youtube_videos WHERE id = ?", (video_db_id,)
        ).fetchone()
        return _row_to_video(row) if row else None

    def get_video_by_yt_id(self, yt_video_id: str) -> Video | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM youtube_videos WHERE video_id = ?", (yt_video_id,)
        ).fetchone()
        return _row_to_video(row) if row else None

    def get_video_count(self, channel_db_id: int) -> int:
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM youtube_videos WHERE channel_id = ?",
            (channel_db_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def set_watched(self, video_db_id: int, watched: bool = True) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE youtube_videos SET watched = ? WHERE id = ?",
            (int(watched), video_db_id),
        )
        conn.commit()

    def set_downloaded(self, video_db_id: int, local_path: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE youtube_videos SET downloaded = 1, local_path = ? WHERE id = ?",
            (local_path, video_db_id),
        )
        conn.commit()

    def clear_download(self, video_db_id: int) -> None:
        conn = self._conn()
        row = conn.execute(
            "SELECT local_path FROM youtube_videos WHERE id = ?", (video_db_id,)
        ).fetchone()
        if row and row["local_path"]:
            Path(row["local_path"]).unlink(missing_ok=True)
        conn.execute(
            "UPDATE youtube_videos SET downloaded = 0, local_path = NULL WHERE id = ?",
            (video_db_id,),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Playlist integration (mixed playlists)
    # ------------------------------------------------------------------

    def add_to_playlist(
        self, playlist_id: int, video_db_id: int, position: int
    ) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO playlist_youtube_items "
            "(playlist_id, video_id, position) VALUES (?, ?, ?)",
            (playlist_id, video_db_id, position),
        )
        conn.commit()

    def get_playlist_video_ids(self, playlist_id: int) -> list[tuple[int, int]]:
        """Return [(video_db_id, position), ...] for a playlist."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT video_id, position FROM playlist_youtube_items "
            "WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()
        return [(r["video_id"], r["position"]) for r in rows]

    def remove_from_playlist(self, playlist_id: int, position: int) -> None:
        conn = self._conn()
        conn.execute(
            "DELETE FROM playlist_youtube_items "
            "WHERE playlist_id = ? AND position = ?",
            (playlist_id, position),
        )
        conn.execute(
            "UPDATE playlist_youtube_items SET position = position - 1 "
            "WHERE playlist_id = ? AND position > ?",
            (playlist_id, position),
        )
        conn.commit()

    def get_next_playlist_position(self, playlist_id: int) -> int:
        """Return the next available position across both track and video items."""
        conn = self._conn()
        r1 = conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS mp FROM playlist_tracks "
            "WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        r2 = conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS mp FROM playlist_youtube_items "
            "WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        return max(r1["mp"], r2["mp"]) + 1


def _row_to_video(row) -> Video:
    return Video(
        id=row["id"],
        channel_id=row["channel_id"],
        video_id=row["video_id"],
        title=row["title"],
        duration_ms=row["duration_ms"],
        thumbnail_url=row["thumbnail_url"],
        published_at=row["published_at"],
        view_count=row["view_count"],
        description=row["description"],
        watched=bool(row["watched"]),
        local_path=row["local_path"],
        downloaded=bool(row["downloaded"]),
    )


# ------------------------------------------------------------------
# yt-dlp wrapper functions
# ------------------------------------------------------------------


def _ydl_opts(
    cookies_browser: str = "",
    extra: dict | None = None,
) -> dict:
    """Build yt-dlp options dict with optional cookie auth."""
    opts = dict(_YDL_BASE)
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if extra:
        opts.update(extra)
    return opts


def fetch_channel_info(
    channel_url: str, cookies_browser: str = ""
) -> dict:
    """Fetch channel metadata via yt-dlp.

    Returns dict with keys: channel_id, title, thumbnail_url.
    Accepts URLs like:
      - https://www.youtube.com/@ChannelName
      - https://www.youtube.com/channel/UCxxxxxxx
      - https://www.youtube.com/c/ChannelName
    """
    opts = _ydl_opts(cookies_browser, {"extract_flat": True, "playlistend": 1})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    if not info:
        raise ValueError(f"Could not fetch channel info for {channel_url}")

    return {
        "channel_id": info.get("channel_id") or info.get("id", ""),
        "title": info.get("channel") or info.get("uploader") or info.get("title", ""),
        "thumbnail_url": _best_thumbnail(info),
    }


def _fetch_rss_dates(channel_id: str) -> dict[str, str]:
    """Fetch publish dates from a channel's RSS feed.

    Returns {video_id: "YYYY-MM-DD"} for the 15 most recent videos.
    Fast (single HTTP request, no yt-dlp).

    YouTube's RSS endpoint is flaky (intermittent 404s), so retry once.
    """
    import feedparser

    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    for attempt in range(2):
        try:
            resp = httpx.get(url, timeout=10, follow_redirects=True)
            if resp.status_code == 200 and "<feed" in resp.text[:500]:
                break
        except Exception:
            pass
        if attempt == 0:
            time.sleep(0.5)
    else:
        return {}

    feed = feedparser.parse(resp.text)
    dates: dict[str, str] = {}
    for entry in feed.entries:
        vid = entry.get("yt_videoid", "")
        published = entry.get("published", "")
        if vid and published:
            # ISO format: "2026-03-14T15:00:55+00:00" → "2026-03-14"
            dates[vid] = published[:10]
    return dates


def fetch_channel_videos(
    channel_url: str,
    limit: int = 50,
    cookies_browser: str = "",
    channel_id: str = "",
) -> list[dict]:
    """Fetch recent videos from a channel.

    Uses yt-dlp flat extraction for the video list (titles, durations, views)
    and the channel's RSS feed for publish dates (flat extraction doesn't
    provide dates).

    Returns list of dicts: video_id, title, duration_ms, thumbnail_url,
    published_at, view_count, description.
    """
    # Fetch dates from RSS (fast, separate from yt-dlp).
    rss_dates: dict[str, str] = {}
    if channel_id:
        rss_dates = _fetch_rss_dates(channel_id)

    opts = _ydl_opts(cookies_browser, {
        "extract_flat": True,
        "playlistend": limit,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    if not info:
        return []

    # If we didn't have a channel_id upfront, try to get it from the response.
    if not rss_dates:
        found_cid = info.get("channel_id") or ""
        if found_cid:
            rss_dates = _fetch_rss_dates(found_cid)

    entries = info.get("entries") or []
    videos: list[dict] = []
    for entry in entries:
        if not entry:
            continue
        vid = entry.get("id") or entry.get("url", "")
        if not vid or len(vid) != 11:
            continue
        duration = entry.get("duration") or 0

        # Date: prefer RSS (reliable), fall back to yt-dlp fields (rare).
        published = rss_dates.get(vid, "")
        if not published:
            published = _normalize_yt_date(entry.get("upload_date")) or ""

        videos.append({
            "video_id": vid,
            "title": entry.get("title", ""),
            "duration_ms": int(duration * 1000) if duration else 0,
            "thumbnail_url": _best_thumbnail(entry),
            "published_at": published,
            "view_count": entry.get("view_count") or 0,
            "description": entry.get("description") or "",
        })

    return videos


def resolve_stream_url(
    video_id: str,
    quality: str = "best",
    cookies_browser: str = "",
) -> tuple[str, str | None]:
    """Resolve a video ID to playable stream URLs.

    Returns (audio_url, video_url).
    audio_url is always provided; video_url may be None for audio-only quality.

    Single extraction — inspects the formats list directly to find the best
    audio-only stream URL (for GStreamer) and optionally a video URL (for mpv).

    Cookies are NOT passed on the first attempt — authenticated sessions can
    cause "format not available" errors on public videos.  If the first
    attempt fails with an age-restriction error, we retry with cookies.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        if "Sign in to confirm your age" in str(e) and cookies_browser:
            log.info("Age-restricted video %s — retrying with cookies", video_id)
            opts["cookiesfrombrowser"] = (cookies_browser,)
            # Cookies change the available format list — yt-dlp's default
            # selector can fail.  ignore_no_formats_error lets processing
            # finish (URLs get signed) while we pick formats ourselves.
            opts["ignore_no_formats_error"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        else:
            raise

    if not info:
        raise RuntimeError(f"Could not extract info for {video_id}")

    formats = info.get("formats") or []

    # Find best audio-only stream (for GStreamer playback with EQ/spectrum).
    audio_url = ""
    best_audio_abr = 0
    for f in formats:
        # Audio-only formats have vcodec == "none".
        if f.get("vcodec", "none") == "none" and f.get("acodec", "none") != "none":
            abr = f.get("abr") or f.get("tbr") or 0
            if abr >= best_audio_abr and f.get("url"):
                best_audio_abr = abr
                audio_url = f["url"]

    # Fallback: if no separate audio stream found, use the combined stream URL.
    if not audio_url:
        audio_url = info.get("url", "")
    if not audio_url and formats:
        # Last resort: pick the last format (usually best quality combined).
        audio_url = formats[-1].get("url", "")

    # Find combined video+audio stream URL (for GStreamer video mode).
    # GStreamer can play this directly — audio goes through the filter
    # chain (EQ/spectrum), video goes through ximagesink.
    video_url: str | None = None
    if quality != "audio_only":
        # Prefer 'best' combined format (typically 360p H.264+AAC).
        # These have both video and audio in a single stream with a direct URL.
        best_combined_tbr = 0
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec != "none" and acodec != "none" and f.get("url"):
                tbr = f.get("tbr") or 0
                if tbr >= best_combined_tbr:
                    best_combined_tbr = tbr
                    video_url = f["url"]

    if not audio_url and not video_url:
        raise RuntimeError(
            "No playable formats available. This video may be age-restricted "
            "and require a JavaScript runtime (Deno + EJS). "
            "See https://github.com/yt-dlp/yt-dlp/wiki/EJS"
        )

    return audio_url, video_url


def search_youtube(
    query: str,
    limit: int = 25,
    cookies_browser: str = "",
) -> list[dict]:
    """Search YouTube for videos and channels.

    Returns list of dicts with keys:
      type ("video" or "channel"), id, title, thumbnail_url,
      duration_ms (videos only), view_count (videos only),
      channel_title (videos only), subscriber_count (channels only).
    """
    opts = _ydl_opts(cookies_browser, {
        "extract_flat": True,
        "playlistend": limit,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    if not info:
        return []

    results: list[dict] = []
    for entry in info.get("entries") or []:
        if not entry:
            continue

        ie_key = entry.get("ie_key", "").lower()
        entry_type = entry.get("_type", "")

        if ie_key == "youtubetab" or entry_type == "playlist":
            # Channel result
            results.append({
                "type": "channel",
                "id": entry.get("channel_id") or entry.get("id", ""),
                "title": entry.get("title", ""),
                "thumbnail_url": _best_thumbnail(entry),
                "subscriber_count": entry.get("subscriber_count") or 0,
            })
        else:
            # Video result
            duration = entry.get("duration") or 0
            results.append({
                "type": "video",
                "id": entry.get("id", ""),
                "title": entry.get("title", ""),
                "thumbnail_url": _best_thumbnail(entry),
                "duration_ms": int(duration * 1000) if duration else 0,
                "view_count": entry.get("view_count") or 0,
                "channel_title": (
                    entry.get("channel") or entry.get("uploader") or ""
                ),
            })

    return results


def fetch_subscriptions(cookies_browser: str) -> list[dict]:
    """Fetch the authenticated user's YouTube subscriptions.

    Returns list of dicts: channel_id, title, thumbnail_url.
    Requires *cookies_browser* to be set (e.g. "firefox").
    """
    if not cookies_browser:
        return []

    opts = _ydl_opts(cookies_browser, {
        "extract_flat": True,
        "playlistend": 500,  # cap to avoid very long fetches
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            "https://www.youtube.com/feed/channels", download=False
        )

    if not info:
        return []

    subs: list[dict] = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        subs.append({
            "channel_id": entry.get("channel_id") or entry.get("id", ""),
            "title": (
                entry.get("channel") or entry.get("uploader")
                or entry.get("title", "")
            ),
            "thumbnail_url": _best_thumbnail(entry),
        })

    return subs


def download_video(
    video_id: str,
    dest_dir: Path | None = None,
    quality: str = "best",
    cookies_browser: str = "",
    on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Download a YouTube video to disk. Returns the local file path.

    Cookies are skipped on the first attempt (authenticated sessions can
    cause "format not available" errors).  If the attempt fails with an
    age-restriction error, we retry with cookies.
    """
    dest_dir = dest_dir or _YOUTUBE_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    quality_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
        "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
        "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
        "audio_only": "bestaudio[ext=m4a]/bestaudio/best",
    }
    fmt = quality_map.get(quality, quality_map["best"])

    output_template = str(dest_dir / "%(id)s.%(ext)s")

    def _progress_hook(d: dict) -> None:
        if on_progress and d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            on_progress(downloaded, total)

    url = f"https://www.youtube.com/watch?v={video_id}"
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "outtmpl": output_template,
        "progress_hooks": [_progress_hook],
        "merge_output_format": "mkv",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        if "Sign in to confirm your age" not in str(e) or not cookies_browser:
            raise
        log.info("Age-restricted video %s — retrying with cookies", video_id)
        # Cookies change the available format list — drop container
        # constraints and let yt-dlp pick what's available.
        cookie_opts = {
            **opts,
            "cookiesfrombrowser": (cookies_browser,),
            "format": "bv*+ba/b",
        }
        del cookie_opts["merge_output_format"]
        try:
            with yt_dlp.YoutubeDL(cookie_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError:
            raise RuntimeError(
                f"Age-restricted video — cookies from {cookies_browser} "
                "did not provide usable formats. YouTube may require a "
                "JavaScript runtime (Deno + EJS) for this content. "
                "See https://github.com/yt-dlp/yt-dlp/wiki/EJS"
            ) from None

    if not info:
        raise RuntimeError(f"Failed to download video {video_id}")

    # Find the downloaded file.
    filename = ydl.prepare_filename(info)
    # yt-dlp may change extension after merge.
    for ext in (".mkv", ".webm", ".mp4", ".m4a", ".opus", ".mp3"):
        candidate = Path(filename).with_suffix(ext)
        if candidate.exists():
            return str(candidate)

    return filename


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _best_thumbnail(info: dict) -> str | None:
    """Extract the best thumbnail URL from a yt-dlp info dict."""
    thumbs = info.get("thumbnails")
    if thumbs:
        # Prefer highest resolution.
        best = max(
            thumbs,
            key=lambda t: (t.get("height") or 0) * (t.get("width") or 0),
        )
        return best.get("url")
    return info.get("thumbnail")


def _normalize_yt_date(upload_date: str | None) -> str | None:
    """Convert yt-dlp upload_date (YYYYMMDD) to ISO format (YYYY-MM-DD)."""
    if not upload_date or len(upload_date) != 8:
        return upload_date
    try:
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
    except (IndexError, ValueError):
        return upload_date
