"""Video filename parser — extracts metadata from movie/series file paths.

Kodi-style folder interpretation: the show folder name is the canonical
series title, season folders determine season numbers, and episode patterns
are matched in priority order (S01E01 > 1x01 > compact 101).

No PySide6 imports — must be testable without a display.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from quaver.core.tagger import TrackTags

log = logging.getLogger(__name__)

# Quality/release tags stripped from filenames.
_NOISE_PATTERN = re.compile(
    r"\b(?:720p|1080p|1080i|2160p|4k|uhd|hdr|hdr10|dolby vision|dv|"
    r"bluray|blu-ray|bdrip|brrip|webrip|web-dl|webdl|hdtv|dvdrip|dvd|"
    r"ntsc|pal|x264|x265|h[\. ]?264|h[\. ]?265|hevc|avc|xvid|divx|"
    r"aac|dts|atmos|truehd|flac|ac3|dd\+?5[\. ]?1|ddp5[\. ]?1|"
    r"remux|proper|repack|extended|unrated|directors[\. ]?cut|"
    r"amzn|nf|hulu|dsnp|aptv|web)\b",
    re.IGNORECASE,
)

# Release group at end: -GroupName or [GroupName]
_RELEASE_GROUP = re.compile(r"[-\[]\w+[\]]*$")

# Episode patterns in priority order (Kodi accuracy ranking).
# 1. S01E01 (also s01e01, S1E1, multi-episode S01E01E02).
_SXXEXX = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
# 2. 1x01 format.
_NxNN = re.compile(r"(?<![a-zA-Z\d])(\d{1,2})x(\d{2,3})(?!\d)")
# 3. Compact 3-digit: 101 = s1e01 (only used after show name prefix is stripped).
_COMPACT3 = re.compile(r"(?<!\d)(\d)(\d{2})(?!\d)")
# 4. EP1 / EP01 format (common in anime releases).
_EPNN = re.compile(r"(?<![a-zA-Z])EP\.?(\d{1,3})(?!\d)", re.IGNORECASE)

# Filename identifier: {tmdb-12345} or {tvmaze-12345} in folder names.
_IDENTIFIER = re.compile(r"\{(tmdb|tvmaze)-(\d+)\}", re.IGNORECASE)

# Year in parentheses or after dots/spaces: (2024) or .2024. or " 2024 ".
_YEAR_PAREN = re.compile(r"\((\d{4})\)")
_YEAR_SEP = re.compile(r"[\.\s_](\d{4})(?:[\.\s_]|$)")

# Season folder pattern: "Season 01", "Season 1", "S01", "S1".
_SEASON_FOLDER = re.compile(r"^(?:Season\s*|S)(\d{1,2})$", re.IGNORECASE)
# Specials folder.
_SPECIALS_FOLDER = re.compile(r"^(?:Specials?|Season\s*0)$", re.IGNORECASE)
# Trailing season suffix in combined folder name: "Show Name Season 2", "Show S03".
_SEASON_SUFFIX = re.compile(
    r"[\s._-]+(?:Season\s*|S)(\d{1,2})\s*$", re.IGNORECASE,
)

# Video file suffixes (lowercase, with leading dot).
_VIDEO_SUFFIXES = frozenset({
    ".mp4", ".mkv", ".avi", ".webm", ".m4v", ".mov", ".wmv", ".ts", ".flv",
})

# Folders that contain extras/featurettes (excluded from scanning by default).
EXTRAS_FOLDERS = frozenset({
    "featurettes", "extras", "behind the scenes", "deleted scenes",
    "interviews", "bonus", "bonus features", "special features",
    "menu art", "trailers", "sample", "subs", "subtitles",
})

# Anime extras by filename stem (after stripping show name prefix via dots).
# ED = ending, OP = opening, NCED/NCOP = creditless variants, PV = preview.
_ANIME_EXTRAS = re.compile(
    r"^(?:NC)?(?:ED|OP)\d*$|^PV\d*$|^Trailer\d*$", re.IGNORECASE,
)


def _clean_title(raw: str) -> str:
    """Clean dots/underscores, strip noise tags, normalize whitespace."""
    s = raw.replace(".", " ").replace("_", " ")
    s = _NOISE_PATTERN.sub("", s)
    s = _RELEASE_GROUP.sub("", s)  # strip -GroupName suffix
    s = _YEAR_PAREN.sub("", s)  # strip (2015) from episode titles
    s = re.sub(r"\([^)]*\)", "", s)  # strip all parenthetical content (quality tags, release groups)
    s = re.sub(r"\[\s*\]", "", s)  # strip empty brackets
    s = re.sub(r"^\s*-\s*", "", s)  # leading dash
    s = re.sub(r"\s*-\s*$", "", s)  # trailing dash
    return re.sub(r"\s+", " ", s).strip()


def _extract_year(name: str) -> tuple[str, int | None]:
    """Extract year from a name string. Returns (name_without_year, year)."""
    m = _YEAR_PAREN.search(name)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2099:
            return name[: m.start()].strip(), year
    m = _YEAR_SEP.search(name)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2099:
            return name[: m.start()].strip(), year
    return name, None


def parse_movie_path(filepath: str) -> TrackTags:
    """Parse a movie filepath into TrackTags with mapped fields.

    Mapping: title=movie title, album=movie title (single-track album),
    year=release year, format=container extension.
    Each movie is its own album so AlbumView shows them as individual entries.

    Uses minimal_clean (Kodi-style) — the folder/filename IS the scraper
    search key.  Checks both the filename and parent folder for title/year.
    """
    p = Path(filepath)
    ext = p.suffix.lstrip(".").upper()

    # Try to get title + year from the filename first.
    title, year = minimal_clean(p.stem)

    # Fall back to parent folder for year (and possibly a better title).
    if year is None:
        folder_title, folder_year = minimal_clean(p.parent.name)
        if folder_year is not None:
            year = folder_year
            if folder_title:
                title = folder_title

    return TrackTags(
        filepath=str(p),
        title=title,
        artist="",
        album=title,
        album_artist="",
        year=year,
        genre="",
        track_number=None,
        disc_number=None,
        duration_ms=0,
        bitrate=0,
        format=ext,
        media_type="movie",
    )


def is_extras_path(filepath: str) -> bool:
    """Check whether a filepath is inside an extras/featurettes folder."""
    parts = Path(filepath).parts
    return any(p.lower() in EXTRAS_FOLDERS for p in parts)


def _extract_episode(stem: str) -> tuple[int, int, str, str]:
    """Try episode patterns in Kodi priority order.

    Returns (season, episode, ep_title_raw, series_from_filename).
    """
    # 1. S01E01 (highest accuracy).
    m = _SXXEXX.search(stem)
    if m:
        return (
            int(m.group(1)),
            int(m.group(2)),
            stem[m.end():],
            stem[: m.start()],
        )

    # 2. 1x01 format.
    m = _NxNN.search(stem)
    if m:
        return (
            int(m.group(1)),
            int(m.group(2)),
            stem[m.end():],
            stem[: m.start()],
        )

    # 3. Compact 3-digit (101 = s1e01). Only try after stripping show name
    #    prefix via dots/underscores to avoid false positives on titles with
    #    numbers.  Look for the pattern after the last dot-separated word that
    #    isn't a number.
    cleaned = stem.replace(".", " ").replace("_", " ")
    m = _COMPACT3.search(cleaned)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        if 1 <= s <= 30 and 1 <= e <= 99:  # sanity bounds
            return s, e, cleaned[m.end():], cleaned[: m.start()]

    # 4. EP1 / EP01 format (common in anime).
    m = _EPNN.search(stem)
    if m:
        return 1, int(m.group(1)), stem[m.end():], stem[: m.start()]

    return 1, 0, "", ""


def parse_series_path(filepath: str) -> TrackTags:
    """Parse a series episode filepath into TrackTags with mapped fields.

    Kodi-style: the show folder is the canonical series title source.
    Season comes from the season folder when present, otherwise from the
    filename pattern.

    Mapping: title=episode title, artist=series title, album="Season N",
    album_artist=series title, track_number=episode#, disc_number=season#.
    """
    p = Path(filepath)
    stem = p.stem
    ext = p.suffix.lstrip(".").upper()

    # --- Determine series title and season from folder hierarchy ---
    parent = p.parent
    specials_match = _SPECIALS_FOLDER.match(parent.name)
    season_match = _SEASON_FOLDER.match(parent.name)

    if specials_match:
        # Specials/Season 0 folder → season 0, show = grandparent.
        folder_season = 0
        series_title = parent.parent.name
    elif season_match:
        # Standard season folder → show = grandparent.
        folder_season = int(season_match.group(1))
        series_title = parent.parent.name
    else:
        # No exact season folder — clean noise tags first, then check
        # for trailing "Season N" suffix (e.g., "Adventure Time Season 2",
        # "The Haunting of Hill House (2018) Season 1 S01 Extended (...)").
        cleaned_parent = _clean_title(parent.name)
        remaining = cleaned_parent
        folder_season = None
        while True:
            suffix_match = _SEASON_SUFFIX.search(remaining)
            if not suffix_match:
                break
            folder_season = int(suffix_match.group(1))
            remaining = remaining[: suffix_match.start()]
        if folder_season is not None:
            series_title = remaining
        else:
            # No season folder at all → show = parent, season from filename.
            series_title = cleaned_parent

    # Extract year from series title (clean_title already applied for
    # the else branch; apply it for the specials/season folder branches).
    if specials_match or season_match:
        series_title = _clean_title(series_title)
    series_title_clean, year = _extract_year(series_title)
    if series_title_clean:
        series_title = series_title_clean

    # --- Extract episode number from filename ---
    season_from_file, episode, ep_title_raw, _ = _extract_episode(stem)

    # Season: folder takes precedence over filename.
    season = folder_season if folder_season is not None else season_from_file

    # --- Build episode title ---
    ep_title = _clean_title(ep_title_raw)
    # Strip release group suffix.
    ep_title = _RELEASE_GROUP.sub("", ep_title).strip()
    ep_title = ep_title.strip("- ")
    if not ep_title or len(ep_title) < 3:
        ep_title = f"Episode {episode}" if episode else stem

    album = "Specials" if season == 0 else f"Season {season}"

    return TrackTags(
        filepath=str(p),
        title=ep_title,
        artist=series_title,
        album=album,
        album_artist=series_title,
        year=year,
        genre="",
        track_number=episode or None,
        disc_number=season,
        duration_ms=0,
        bitrate=0,
        format=ext,
        media_type="series_episode",
    )


def extract_identifier(name: str) -> tuple[str, int] | None:
    """Parse a ``{tmdb-12345}`` or ``{tvmaze-12345}`` identifier from a name.

    Returns ``(scraper, scraper_id)`` or ``None`` if no identifier found.
    The scraper string is normalized to lowercase (``"tmdb"`` or ``"tvmaze"``).
    """
    m = _IDENTIFIER.search(name)
    if m:
        return m.group(1).lower(), int(m.group(2))
    return None


def minimal_clean(folder_name: str) -> tuple[str, int | None]:
    """Kodi-style minimal cleaning of a show/movie folder name.

    Only normalizes separators, strips identifier tags, and extracts a year.
    Does NOT strip noise tags, release groups, or other parenthetical content —
    the folder name IS the scraper search key.

    Returns (cleaned_name, year_or_None).
    """
    s = folder_name.replace(".", " ").replace("_", " ")
    # Strip {tmdb-NNN} / {tvmaze-NNN} identifier from display title.
    s = _IDENTIFIER.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    name, year = _extract_year(s)
    return name.strip() or s, year


def is_show_folder(path: Path) -> bool:
    """Determine whether *path* looks like a show's root folder.

    A show folder either contains video files directly, or contains season
    sub-folders (``Season N``, ``SN``, ``Specials``).  Extras folders are
    excluded.
    """
    if path.name.lower() in EXTRAS_FOLDERS:
        return False
    try:
        for child in path.iterdir():
            if child.is_file() and child.suffix.lower() in _VIDEO_SUFFIXES:
                return True
            if child.is_dir():
                cname = child.name
                if _SEASON_FOLDER.match(cname) or _SPECIALS_FOLDER.match(cname):
                    return True
                # Check for video files one level deeper (non-standard layout).
                if cname.lower() not in EXTRAS_FOLDERS:
                    try:
                        for grandchild in child.iterdir():
                            if grandchild.is_file() and grandchild.suffix.lower() in _VIDEO_SUFFIXES:
                                return True
                    except OSError:
                        pass
    except OSError:
        pass
    return False


def _is_anime_extra(stem: str) -> bool:
    """Check if a filename stem is an anime extra (OP, ED, NCED, NCOP, PV).

    Strips a show-name prefix (dot/underscore-separated) before checking,
    so ``FLCL.ED`` → ``ED`` → True.
    """
    # Take the last dot/underscore-separated segment as the candidate.
    parts = re.split(r"[._]", stem)
    candidate = parts[-1] if parts else stem
    return bool(_ANIME_EXTRAS.match(candidate))


def discover_episodes(
    show_folder: Path,
) -> list[tuple[Path, int, int]]:
    """Walk a show folder and extract (filepath, season, episode) for each video.

    Season is determined by folder structure first (``Season N`` / ``SN``),
    then by the filename episode pattern (``S01E01``).  Episodes inside
    extras folders and anime extras (OP/ED/PV) are skipped.
    """
    results: list[tuple[Path, int, int]] = []
    for child in sorted(show_folder.iterdir()):
        if child.name.lower() in EXTRAS_FOLDERS:
            continue
        if child.is_file() and child.suffix.lower() in _VIDEO_SUFFIXES:
            # Video directly in show folder — season from filename.
            season_from_file, episode, _, _ = _extract_episode(child.stem)
            if episode == 0 and _is_anime_extra(child.stem):
                continue
            results.append((child, season_from_file, episode))
        elif child.is_dir():
            folder_season: int | None = None
            sm = _SEASON_FOLDER.match(child.name)
            sp = _SPECIALS_FOLDER.match(child.name)
            if sm:
                folder_season = int(sm.group(1))
            elif sp:
                folder_season = 0
            # Walk into sub-folder (season folder or non-standard layout).
            try:
                for video_file in sorted(child.iterdir()):
                    if video_file.name.lower() in EXTRAS_FOLDERS:
                        continue
                    if video_file.is_file() and video_file.suffix.lower() in _VIDEO_SUFFIXES:
                        season_from_file, episode, _, _ = _extract_episode(video_file.stem)
                        if episode == 0 and _is_anime_extra(video_file.stem):
                            continue
                        season = folder_season if folder_season is not None else season_from_file
                        results.append((video_file, season, episode))
                    elif video_file.is_dir():
                        # Two levels deep (show/subfolder/subfolder/file).
                        if video_file.name.lower() in EXTRAS_FOLDERS:
                            continue
                        try:
                            for deep_file in sorted(video_file.iterdir()):
                                if deep_file.is_file() and deep_file.suffix.lower() in _VIDEO_SUFFIXES:
                                    s, e, _, _ = _extract_episode(deep_file.stem)
                                    if e == 0 and _is_anime_extra(deep_file.stem):
                                        continue
                                    results.append((deep_file, folder_season if folder_season is not None else s, e))
                        except OSError:
                            pass
            except OSError:
                pass
    return results


def extract_episode_title(filepath: str) -> str:
    """Extract an episode title from a filename, cleaning noise tags.

    Uses _clean_title (full noise stripping) since episode titles benefit
    from removing quality/release tags.  Falls back to "Episode N".
    """
    stem = Path(filepath).stem
    _, episode, ep_title_raw, _ = _extract_episode(stem)
    ep_title = _clean_title(ep_title_raw)
    ep_title = _RELEASE_GROUP.sub("", ep_title).strip()
    ep_title = ep_title.strip("- ")
    if not ep_title or len(ep_title) < 3:
        ep_title = f"Episode {episode}" if episode else stem
    return ep_title


def build_episode_tags(
    series_title: str,
    season: int,
    episode: int,
    ep_title: str,
    filepath: str,
    year: int | None = None,
) -> TrackTags:
    """Build a TrackTags for a series episode using pre-extracted metadata."""
    p = Path(filepath)
    album = "Specials" if season == 0 else f"Season {season}"
    return TrackTags(
        filepath=str(p),
        title=ep_title,
        artist=series_title,
        album=album,
        album_artist=series_title,
        year=year,
        genre="",
        track_number=episode or None,
        disc_number=season,
        duration_ms=0,
        bitrate=0,
        format=p.suffix.lstrip(".").upper(),
        media_type="series_episode",
    )


def discover_duration(filepath: str, timeout_sec: float = 5.0) -> int:
    """Get video duration in milliseconds using GStreamer Discoverer.

    Returns 0 if discovery fails or times out.
    """
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import Gst, GstPbutils

        if not Gst.is_initialized():
            Gst.init(None)

        discoverer = GstPbutils.Discoverer.new(int(timeout_sec * Gst.SECOND))
        uri = Gst.filename_to_uri(filepath)
        info = discoverer.discover_uri(uri)
        duration_ns = info.get_duration()
        return int(duration_ns / Gst.MSECOND) if duration_ns > 0 else 0
    except Exception as e:
        log.debug("Duration discovery failed for %s: %s", filepath, e)
        return 0
