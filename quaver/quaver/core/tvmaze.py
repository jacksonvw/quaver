"""TVMaze API client — series/episode metadata lookup.

No API key required.  No PySide6 imports — must be usable from background threads.
Uses httpx for HTTP requests.

Rate limit: 20 calls per 10 seconds (we enforce ~2 calls/sec).
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from quaver.core.paths import cache_dir, image_dir

log = logging.getLogger(__name__)

_BASE_URL = "https://api.tvmaze.com"

# 20 calls / 10 seconds ≈ 0.5s between requests.
_MIN_REQUEST_INTERVAL = 0.5

# Permanent image storage (posters, stills).
_IMAGE_DIR = image_dir() / "tvmaze"
# Ephemeral JSON caches (episode lists with TTL).
_CACHE_DIR = cache_dir() / "tvmaze"


def _strip_html(text: str | None) -> str:
    """Strip HTML tags from TVMaze summary fields."""
    if not text:
        return ""
    # Remove tags, then decode entities.
    clean = re.sub(r"<[^>]+>", "", text)
    return html.unescape(clean).strip()


# ------------------------------------------------------------------
# Result dataclasses — field names match tmdb.py where applicable
# so the enrichment service can treat providers uniformly.
# ------------------------------------------------------------------


@dataclass
class TVMazeSearchResult:
    """Lightweight search result for match picker dialog (no image download)."""

    tvmaze_id: int
    title: str
    year: int | None
    network: str
    status: str
    poster_url: str | None  # remote URL — not downloaded
    summary: str


@dataclass
class TVMazeSeriesResult:
    """Metadata returned from a TVMaze series lookup."""

    tvmaze_id: int
    title: str
    year: int | None
    plot: str
    genre: str  # comma-separated
    poster_path: str | None  # local path after download
    backdrop_path: str | None  # local path after download (None — TVMaze has no backdrops)


@dataclass
class TVMazeEpisodeResult:
    """Metadata for a single episode from TVMaze."""

    tvmaze_id: int  # episode tvmaze_id
    series_tvmaze_id: int
    season: int
    episode: int
    title: str
    plot: str
    still_path: str | None  # local path after download


class TVMazeClient:
    """Synchronous TVMaze API client with rate limiting and image caching."""

    def __init__(self) -> None:
        self._client = httpx.Client(timeout=15.0)
        self._last_request: float = 0.0

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        """Rate-limited GET request to TVMaze API."""
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        url = f"{_BASE_URL}{path}"
        try:
            resp = self._client.get(url, params=params)
            self._last_request = time.monotonic()
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                # Rate limited — wait and retry once.
                retry_after = int(resp.headers.get("Retry-After", "5"))
                log.warning("TVMaze rate limited, waiting %ds", retry_after)
                time.sleep(retry_after)
                resp = self._client.get(url, params=params)
                self._last_request = time.monotonic()
                if resp.status_code == 200:
                    return resp.json()
            if resp.status_code == 404:
                return None  # Not found — normal for missing episodes.
            log.warning("TVMaze API %s returned %d", path, resp.status_code)
            return None
        except httpx.HTTPError as e:
            log.warning("TVMaze request failed: %s", e)
            return None

    def _download_image(self, url: str | None) -> str | None:
        """Download an image to permanent storage.  Returns local path."""
        if not url:
            return None

        _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        # Use last path segment as filename (e.g., "12345.jpg").
        filename = url.rsplit("/", 1)[-1]
        local_path = _IMAGE_DIR / filename
        if local_path.exists():
            return str(local_path)

        try:
            resp = self._client.get(url)
            if resp.status_code == 200:
                local_path.write_bytes(resp.content)
                return str(local_path)
            log.warning("TVMaze image download failed (%d): %s", resp.status_code, url)
        except httpx.HTTPError as e:
            log.warning("TVMaze image download failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Series API
    # ------------------------------------------------------------------

    def search_series_all(
        self, title: str, year: int | None = None,
    ) -> list[TVMazeSearchResult]:
        """Search for TV series and return all candidates for match picker."""
        data = self._get("/search/shows", {"q": title})
        if not data or not isinstance(data, list):
            return []

        results: list[TVMazeSearchResult] = []
        for item in data:
            show = item.get("show", {})
            premiered = show.get("premiered") or ""
            yr = int(premiered[:4]) if len(premiered) >= 4 else None
            network = show.get("network") or show.get("webChannel") or {}
            image = show.get("image") or {}
            results.append(TVMazeSearchResult(
                tvmaze_id=show.get("id", 0),
                title=show.get("name", ""),
                year=yr,
                network=network.get("name", ""),
                status=show.get("status", ""),
                poster_url=image.get("medium") or image.get("original"),
                summary=_strip_html(show.get("summary")),
            ))
        return results

    def search_series(self, title: str, year: int | None = None) -> int | None:
        """Search for a TV series by title.  Returns tvmaze_id or None.

        Applies a minimum score threshold to reject false matches (e.g.
        "M23" → "Chelmsford 123").  When *year* is provided, also tries
        searching ``"title year"`` to handle shows where the year is part
        of the name (e.g. "Sealab 2021").
        """
        _MIN_SCORE = 0.5

        best_id, best_score = self._pick_best(
            self._get("/search/shows", {"q": title}), year,
        )

        # If a year was extracted, the number might actually be part of
        # the show title (e.g. "Sealab 2021").  Try the combined query
        # and keep whichever scores higher.
        if year:
            alt_id, alt_score = self._pick_best(
                self._get("/search/shows", {"q": f"{title} {year}"}), None,
            )
            if alt_score > best_score:
                best_id, best_score = alt_id, alt_score

        if best_score < _MIN_SCORE:
            return None
        return best_id

    @staticmethod
    def _pick_best(
        data: dict | list | None, year: int | None,
    ) -> tuple[int | None, float]:
        """Return (tvmaze_id, score) of the best match in *data*."""
        if not data or not isinstance(data, list):
            return None, 0.0

        # If year is given, prefer a result whose premiered year matches.
        if year:
            for item in data:
                show = item.get("show", {})
                premiered = show.get("premiered") or ""
                if premiered.startswith(str(year)):
                    return show["id"], item.get("score", 0.0)

        top = data[0]
        return top["show"]["id"], top.get("score", 0.0)

    def get_series_details(self, tvmaze_id: int) -> TVMazeSeriesResult | None:
        """Fetch series-level details by TVMaze ID."""
        data = self._get(f"/shows/{tvmaze_id}")
        if not data or not isinstance(data, dict):
            return None

        premiered = data.get("premiered") or ""
        year = int(premiered[:4]) if len(premiered) >= 4 else None
        genres = ", ".join(data.get("genres", []))

        # Download poster image (original quality).
        image = data.get("image") or {}
        poster_url = image.get("original") or image.get("medium")
        poster_local = self._download_image(poster_url)

        return TVMazeSeriesResult(
            tvmaze_id=tvmaze_id,
            title=data.get("name", ""),
            year=year,
            plot=_strip_html(data.get("summary")),
            genre=genres,
            poster_path=poster_local,
            backdrop_path=None,  # TVMaze doesn't serve backdrop images.
        )

    def get_episode_details(
        self, series_tvmaze_id: int, season: int, episode: int,
    ) -> TVMazeEpisodeResult | None:
        """Fetch episode-level details by series ID + season + episode number."""
        data = self._get(
            f"/shows/{series_tvmaze_id}/episodebynumber",
            {"season": season, "number": episode},
        )
        if not data or not isinstance(data, dict):
            return None

        image = data.get("image") or {}
        still_url = image.get("original") or image.get("medium")
        still_local = self._download_image(still_url)

        return TVMazeEpisodeResult(
            tvmaze_id=data.get("id", 0),
            series_tvmaze_id=series_tvmaze_id,
            season=season,
            episode=episode,
            title=data.get("name", ""),
            plot=_strip_html(data.get("summary")),
            still_path=still_local,
        )

    def get_all_episodes(
        self, series_tvmaze_id: int,
    ) -> list[TVMazeEpisodeResult]:
        """Fetch all episodes for a series in one API call.

        Uses a JSON cache (7-day TTL) to avoid redundant API hits.
        Returns the full episode list — the scraper's source of truth.
        """
        cached = self._get_cached_episodes(series_tvmaze_id)
        if cached is not None:
            return cached

        data = self._get(f"/shows/{series_tvmaze_id}/episodes")
        if not data or not isinstance(data, list):
            return []

        # Cache the raw API response.
        self._cache_episodes(series_tvmaze_id, data)

        return self._parse_episode_list(series_tvmaze_id, data)

    def _parse_episode_list(
        self, series_tvmaze_id: int, data: list[dict],
    ) -> list[TVMazeEpisodeResult]:
        results: list[TVMazeEpisodeResult] = []
        for ep in data:
            season = ep.get("season", 0)
            number = ep.get("number")
            if number is None:
                continue  # Skip unnumbered specials.
            # Download still image eagerly (like show posters).
            image = ep.get("image") or {}
            still_url = image.get("original") or image.get("medium")
            still_local = self._download_image(still_url)
            results.append(TVMazeEpisodeResult(
                tvmaze_id=ep.get("id", 0),
                series_tvmaze_id=series_tvmaze_id,
                season=season,
                episode=number,
                title=ep.get("name", ""),
                plot=_strip_html(ep.get("summary")),
                still_path=still_local,
            ))
        return results

    # ------------------------------------------------------------------
    # Episode list JSON cache
    # ------------------------------------------------------------------

    _EPISODE_CACHE_TTL = 7 * 24 * 3600  # 7 days in seconds.

    def _episode_cache_path(self, tvmaze_id: int) -> Path:
        return _CACHE_DIR / f"episodes_{tvmaze_id}.json"

    def _get_cached_episodes(
        self, tvmaze_id: int,
    ) -> list[TVMazeEpisodeResult] | None:
        path = self._episode_cache_path(tvmaze_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            ts = raw.get("_cached_at", 0)
            if time.time() - ts > self._EPISODE_CACHE_TTL:
                return None  # Stale.
            return self._parse_episode_list(tvmaze_id, raw["episodes"])
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def _cache_episodes(self, tvmaze_id: int, data: list[dict]) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = self._episode_cache_path(tvmaze_id)
        try:
            path.write_text(json.dumps({
                "_cached_at": time.time(),
                "episodes": data,
            }))
        except OSError:
            pass

    def lookup_series(
        self, title: str, year: int | None = None,
    ) -> TVMazeSeriesResult | None:
        """Search + fetch details in one call.  Returns None if not found."""
        tvmaze_id = self.search_series(title, year)
        if tvmaze_id is None:
            return None
        return self.get_series_details(tvmaze_id)
