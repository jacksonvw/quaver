"""AcoustID fingerprinting + MusicBrainz metadata lookup.

No PySide6 imports — must be testable without a display.

Rate limiting: MusicBrainz API requires max 1 request/second and a
descriptive User-Agent. AcoustID has its own rate limits but pyacoustid
handles that via its built-in throttle.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Callable

import acoustid
import musicbrainzngs

# MusicBrainz API setup.
musicbrainzngs.set_useragent(
    "Quaver", "0.1.0", "https://github.com/jacksonvw/quaver"
)

# AcoustID API key — this is a free-tier application key.
# Users of the app should register their own at https://acoustid.org/
_ACOUSTID_API_KEY = "GBmPMfBu5k"

# Rate limiting for MusicBrainz (1 req/sec).
_mb_lock = threading.Lock()
_mb_last_call: float = 0.0


def _rate_limit() -> None:
    """Ensure at least 1 second between MusicBrainz API calls."""
    global _mb_last_call
    with _mb_lock:
        now = time.monotonic()
        elapsed = now - _mb_last_call
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _mb_last_call = time.monotonic()


@dataclass
class MBMatch:
    """A single MusicBrainz match result for a track."""
    score: float  # 0.0–1.0 confidence
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: int | None = None
    track_number: int | None = None
    disc_number: int | None = None
    genre: str = ""
    musicbrainz_recording_id: str = ""
    musicbrainz_release_id: str = ""


@dataclass
class LookupResult:
    """Result of looking up a single file."""
    filepath: str
    fingerprint: str = ""
    duration: int = 0  # seconds
    matches: list[MBMatch] = field(default_factory=list)
    error: str = ""


def fingerprint_file(filepath: str) -> tuple[int, str] | None:
    """Compute AcoustID fingerprint for an audio file.

    Returns (duration_seconds, fingerprint_string) or None on failure.
    Requires `fpcalc` (from chromaprint) to be installed.
    """
    try:
        duration, fingerprint = acoustid.fingerprint_file(filepath)
        return int(duration), fingerprint
    except acoustid.FingerprintGenerationError:
        return None
    except Exception:
        return None


def lookup_acoustid(
    duration: int, fingerprint: str
) -> list[dict]:
    """Query AcoustID with a fingerprint. Returns raw recording matches."""
    try:
        results = acoustid.lookup(
            _ACOUSTID_API_KEY, fingerprint, duration,
            meta=["recordings", "releasegroups"],
        )
    except (acoustid.WebServiceError, Exception):
        return []

    recordings: list[dict] = []
    for result in results.get("results", []):
        score = result.get("score", 0)
        for rec in result.get("recordings", []):
            rec["_score"] = score
            recordings.append(rec)

    return recordings


def _extract_artist_credit(credit_list: list) -> str:
    """Extract a display artist name from a musicbrainzngs artist-credit list.

    musicbrainzngs returns artist-credit as a list of alternating dicts and
    join-phrase strings, e.g.:
        [{"artist": {"name": "Foo"}}, " & ", {"artist": {"name": "Bar"}}]
    """
    parts: list[str] = []
    for entry in credit_list:
        if isinstance(entry, str):
            parts.append(entry)
        elif isinstance(entry, dict):
            artist = entry.get("artist", {})
            if isinstance(artist, dict):
                parts.append(artist.get("name", ""))
            elif isinstance(artist, str):
                parts.append(artist)
            else:
                parts.append(entry.get("name", ""))
    return "".join(parts)


def lookup_recording(recording_id: str) -> dict | None:
    """Fetch full recording details from MusicBrainz by recording ID."""
    _rate_limit()
    try:
        result = musicbrainzngs.get_recording_by_id(
            recording_id,
            includes=["artists", "releases", "tags"],
        )
        return result.get("recording")
    except (musicbrainzngs.WebServiceError, Exception):
        return None


def lookup_release(release_id: str) -> dict | None:
    """Fetch release details from MusicBrainz."""
    _rate_limit()
    try:
        result = musicbrainzngs.get_release_by_id(
            release_id,
            includes=["artists", "recordings", "release-groups", "tags", "media"],
        )
        return result.get("release")
    except (musicbrainzngs.WebServiceError, Exception):
        return None


def lookup_file(filepath: str) -> LookupResult:
    """Full pipeline: fingerprint → AcoustID → MusicBrainz details.

    Returns a LookupResult with the best matches, fully resolved.
    Never raises — all errors are captured in result.error.
    """
    result = LookupResult(filepath=filepath)

    # Step 1: Fingerprint.
    fp = fingerprint_file(filepath)
    if fp is None:
        result.error = "Could not fingerprint file (is fpcalc/chromaprint installed?)"
        return result

    duration, fingerprint = fp
    result.duration = duration
    result.fingerprint = fingerprint

    # Step 2: AcoustID lookup.
    recordings = lookup_acoustid(duration, fingerprint)
    if not recordings:
        result.error = "No AcoustID matches found"
        return result

    # Step 3: Resolve the top recordings via MusicBrainz.
    seen_recording_ids: set[str] = set()
    for rec in recordings[:5]:  # Limit to top 5 candidates.
        rec_id = rec.get("id", "")
        if not rec_id or rec_id in seen_recording_ids:
            continue
        seen_recording_ids.add(rec_id)

        score = rec.get("_score", 0)

        try:
            mb_rec = lookup_recording(rec_id)
        except Exception:
            continue
        if mb_rec is None:
            continue

        # Extract artist.
        artist_credit = mb_rec.get("artist-credit", [])
        artist_name = _extract_artist_credit(artist_credit)

        # Extract tags/genres.
        tags = mb_rec.get("tag-list", [])
        genre = ""
        if tags:
            try:
                tags_sorted = sorted(
                    tags,
                    key=lambda t: int(t.get("count", 0)),
                    reverse=True,
                )
                genre = tags_sorted[0].get("name", "")
            except (ValueError, KeyError, IndexError):
                pass

        # Get release info (first few releases).
        releases = mb_rec.get("release-list", [])
        for release in releases[:3]:
            try:
                release_id = release.get("id", "")
                release_detail = lookup_release(release_id) if release_id else None

                album = release.get("title", "")
                album_artist = ""
                year = None
                track_number = None
                disc_number = None

                if release_detail:
                    # Album artist.
                    rc = release_detail.get("artist-credit", [])
                    if rc:
                        album_artist = _extract_artist_credit(rc)

                    # Year from release date.
                    date = release_detail.get("date", "")
                    if date and len(date) >= 4:
                        try:
                            year = int(date[:4])
                        except ValueError:
                            pass

                    # Track/disc number — find our recording in the medium list.
                    for medium in release_detail.get("medium-list", []):
                        try:
                            disc_num = int(medium.get("position", 1))
                        except (ValueError, TypeError):
                            disc_num = 1
                        for track in medium.get("track-list", []):
                            track_rec = track.get("recording", {})
                            if isinstance(track_rec, dict) and track_rec.get("id") == rec_id:
                                try:
                                    track_number = int(track.get("position", 0)) or None
                                except (ValueError, TypeError):
                                    track_number = None
                                disc_number = disc_num
                                break
                        if track_number is not None:
                            break

                match = MBMatch(
                    score=score,
                    title=mb_rec.get("title", ""),
                    artist=artist_name,
                    album=album,
                    album_artist=album_artist or artist_name,
                    year=year,
                    track_number=track_number,
                    disc_number=disc_number,
                    genre=genre,
                    musicbrainz_recording_id=rec_id,
                    musicbrainz_release_id=release_id,
                )
                result.matches.append(match)
            except Exception:
                continue

        # If no releases, still add the recording-level match.
        if not releases:
            match = MBMatch(
                score=score,
                title=mb_rec.get("title", ""),
                artist=artist_name,
                genre=genre,
                musicbrainz_recording_id=rec_id,
            )
            result.matches.append(match)

    # Sort by score descending.
    result.matches.sort(key=lambda m: m.score, reverse=True)

    if not result.matches:
        result.error = "Could not resolve MusicBrainz metadata"

    return result


def lookup_files(
    filepaths: list[str],
    on_progress: Callable[[int, int], None] | None = None,
) -> list[LookupResult]:
    """Look up multiple files sequentially (respecting rate limits).

    Calls *on_progress(done, total)* after each file.
    Never raises — errors are captured per-file in LookupResult.error.
    """
    results: list[LookupResult] = []
    total = len(filepaths)
    for i, fp in enumerate(filepaths):
        try:
            results.append(lookup_file(fp))
        except Exception as e:
            results.append(LookupResult(filepath=fp, error=str(e)))
        if on_progress:
            on_progress(i + 1, total)
    return results
