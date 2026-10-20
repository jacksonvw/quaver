"""TMDb API client — movie/series metadata lookup.

No PySide6 imports — must be usable from background threads.
Uses httpx (already a project dependency) for HTTP requests.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from quaver.core.paths import image_dir

log = logging.getLogger(__name__)

_BASE_URL = "https://api.themoviedb.org/3"
_IMAGE_BASE = "https://image.tmdb.org/t/p"

# TMDb allows ~40 req/sec.  We stay well under that.
_MIN_REQUEST_INTERVAL = 0.05  # 20 req/sec max

# Permanent image storage (posters, stills).
_CACHE_DIR = image_dir() / "tmdb"


@dataclass
class TMDbSearchResult:
    """Lightweight search result for match picker dialog (no image download)."""

    tmdb_id: int
    title: str
    year: int | None
    overview: str
    poster_url: str | None  # full remote URL — not downloaded
    media_type: str  # "tv" or "movie"


@dataclass
class MovieResult:
    """Metadata returned from a TMDb movie lookup."""

    tmdb_id: int
    title: str
    year: int | None
    plot: str
    genre: str  # comma-separated
    imdb_id: str | None
    poster_path: str | None  # local path after download
    backdrop_path: str | None  # local path after download


@dataclass
class SeriesResult:
    """Metadata returned from a TMDb series lookup."""

    tmdb_id: int
    title: str
    year: int | None
    plot: str
    genre: str
    poster_path: str | None
    backdrop_path: str | None


@dataclass
class EpisodeResult:
    """Metadata for a single episode from TMDb."""

    tmdb_id: int  # episode tmdb_id
    series_tmdb_id: int
    season: int
    episode: int
    title: str
    plot: str
    still_path: str | None  # local path after download


class TMDbClient:
    """Synchronous TMDb API client with rate limiting and image caching."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.Client(timeout=15.0)
        self._last_request: float = 0.0

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """Rate-limited GET request to TMDb API."""
        if not self._api_key:
            return None

        # Rate limiting.
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        url = f"{_BASE_URL}{path}"
        all_params = {"api_key": self._api_key}
        if params:
            all_params.update(params)

        try:
            resp = self._client.get(url, params=all_params)
            self._last_request = time.monotonic()
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                log.error("TMDb API key is invalid")
                return None
            if resp.status_code == 429:
                # Rate limited — wait and retry once.
                retry_after = int(resp.headers.get("Retry-After", "2"))
                log.warning("TMDb rate limited, waiting %ds", retry_after)
                time.sleep(retry_after)
                resp = self._client.get(url, params=all_params)
                self._last_request = time.monotonic()
                if resp.status_code == 200:
                    return resp.json()
            log.warning("TMDb API %s returned %d", path, resp.status_code)
            return None
        except httpx.HTTPError as e:
            log.warning("TMDb request failed: %s", e)
            return None

    def _download_image(
        self, tmdb_path: str, size: str = "w500",
    ) -> str | None:
        """Download a TMDb image to permanent storage. Returns local path."""
        if not tmdb_path:
            return None

        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # tmdb_path looks like "/abc123.jpg" — use as filename.
        filename = tmdb_path.lstrip("/")
        local_path = _CACHE_DIR / f"{size}_{filename}"
        if local_path.exists():
            return str(local_path)

        url = f"{_IMAGE_BASE}/{size}{tmdb_path}"
        try:
            resp = self._client.get(url)
            if resp.status_code == 200:
                local_path.write_bytes(resp.content)
                return str(local_path)
            log.warning("TMDb image download failed (%d): %s", resp.status_code, url)
        except httpx.HTTPError as e:
            log.warning("TMDb image download failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Movie API
    # ------------------------------------------------------------------

    def search_series_all(
        self, title: str, year: int | None = None,
    ) -> list[TMDbSearchResult]:
        """Search for TV series and return all candidates for match picker."""
        params: dict = {"query": title}
        if year:
            params["first_air_date_year"] = str(year)
        data = self._get("/search/tv", params)
        if not data or not data.get("results"):
            return []

        results: list[TMDbSearchResult] = []
        for item in data["results"]:
            first_air = item.get("first_air_date", "")
            yr = int(first_air[:4]) if len(first_air) >= 4 else None
            poster = item.get("poster_path")
            results.append(TMDbSearchResult(
                tmdb_id=item.get("id", 0),
                title=item.get("name", ""),
                year=yr,
                overview=item.get("overview", ""),
                poster_url=f"{_IMAGE_BASE}/w185{poster}" if poster else None,
                media_type="tv",
            ))
        return results

    def search_movie_all(
        self, title: str, year: int | None = None,
    ) -> list[TMDbSearchResult]:
        """Search for movies and return all candidates for match picker."""
        params: dict = {"query": title}
        if year:
            params["year"] = str(year)
        data = self._get("/search/movie", params)
        if not data or not data.get("results"):
            return []

        results: list[TMDbSearchResult] = []
        for item in data["results"]:
            release = item.get("release_date", "")
            yr = int(release[:4]) if len(release) >= 4 else None
            poster = item.get("poster_path")
            results.append(TMDbSearchResult(
                tmdb_id=item.get("id", 0),
                title=item.get("title", ""),
                year=yr,
                overview=item.get("overview", ""),
                poster_url=f"{_IMAGE_BASE}/w185{poster}" if poster else None,
                media_type="movie",
            ))
        return results

    def search_movie(self, title: str, year: int | None = None) -> int | None:
        """Search for a movie by title and optional year. Returns tmdb_id."""
        params: dict = {"query": title}
        if year:
            params["year"] = str(year)
        data = self._get("/search/movie", params)
        if not data or not data.get("results"):
            # Retry without year — sometimes the year in the filename is wrong.
            if year:
                data = self._get("/search/movie", {"query": title})
            if not data or not data.get("results"):
                return None
        return data["results"][0]["id"]

    def get_movie_details(self, tmdb_id: int) -> MovieResult | None:
        """Fetch full movie details by TMDb ID."""
        data = self._get(f"/movie/{tmdb_id}")
        if not data:
            return None

        # Extract year from release_date.
        release_date = data.get("release_date", "")
        year = int(release_date[:4]) if len(release_date) >= 4 else None

        # Genres as comma-separated string.
        genres = ", ".join(g["name"] for g in data.get("genres", []))

        # Download poster and backdrop.
        poster_local = self._download_image(data.get("poster_path"), "w500")
        backdrop_local = self._download_image(data.get("backdrop_path"), "w1280")

        # Get IMDb ID from external IDs.
        imdb_id = data.get("imdb_id")

        return MovieResult(
            tmdb_id=tmdb_id,
            title=data.get("title", ""),
            year=year,
            plot=data.get("overview", ""),
            genre=genres,
            imdb_id=imdb_id,
            poster_path=poster_local,
            backdrop_path=backdrop_local,
        )

    def lookup_movie(
        self, title: str, year: int | None = None,
    ) -> MovieResult | None:
        """Search + fetch details in one call. Returns None if not found."""
        tmdb_id = self.search_movie(title, year)
        if tmdb_id is None:
            return None
        return self.get_movie_details(tmdb_id)

    # ------------------------------------------------------------------
    # Series API
    # ------------------------------------------------------------------

    def search_series(self, title: str, year: int | None = None) -> int | None:
        """Search for a TV series by title. Returns tmdb_id."""
        params: dict = {"query": title}
        if year:
            params["first_air_date_year"] = str(year)
        data = self._get("/search/tv", params)
        if not data or not data.get("results"):
            if year:
                data = self._get("/search/tv", {"query": title})
            if not data or not data.get("results"):
                return None
        return data["results"][0]["id"]

    def get_series_details(self, tmdb_id: int) -> SeriesResult | None:
        """Fetch series-level details."""
        data = self._get(f"/tv/{tmdb_id}")
        if not data:
            return None

        first_air = data.get("first_air_date", "")
        year = int(first_air[:4]) if len(first_air) >= 4 else None
        genres = ", ".join(g["name"] for g in data.get("genres", []))

        poster_local = self._download_image(data.get("poster_path"), "w500")
        backdrop_local = self._download_image(data.get("backdrop_path"), "w1280")

        return SeriesResult(
            tmdb_id=tmdb_id,
            title=data.get("name", ""),
            year=year,
            plot=data.get("overview", ""),
            genre=genres,
            poster_path=poster_local,
            backdrop_path=backdrop_local,
        )

    def get_episode_details(
        self, series_tmdb_id: int, season: int, episode: int,
    ) -> EpisodeResult | None:
        """Fetch episode-level details."""
        data = self._get(f"/tv/{series_tmdb_id}/season/{season}/episode/{episode}")
        if not data:
            return None

        still_local = self._download_image(data.get("still_path"), "w500")

        return EpisodeResult(
            tmdb_id=data.get("id", 0),
            series_tmdb_id=series_tmdb_id,
            season=season,
            episode=episode,
            title=data.get("name", ""),
            plot=data.get("overview", ""),
            still_path=still_local,
        )

    def get_season_episodes(
        self, series_tmdb_id: int, season: int,
    ) -> list[EpisodeResult]:
        """Fetch all episodes for a season in one request."""
        data = self._get(f"/tv/{series_tmdb_id}/season/{season}")
        if not data or "episodes" not in data:
            return []

        results: list[EpisodeResult] = []
        for ep in data["episodes"]:
            still_local = self._download_image(ep.get("still_path"), "w300")
            results.append(EpisodeResult(
                tmdb_id=ep.get("id", 0),
                series_tmdb_id=series_tmdb_id,
                season=season,
                episode=ep.get("episode_number", 0),
                title=ep.get("name", ""),
                plot=ep.get("overview", ""),
                still_path=still_local,
            ))
        return results
