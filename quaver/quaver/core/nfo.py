"""Kodi-format NFO file parser.

Reads tvshow.nfo, episode .nfo, and movie .nfo XML files.  Uses stdlib
xml.etree.ElementTree — best-effort parsing, missing fields silently skipped.

No PySide6 imports — must be testable without a display.
"""

from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree

log = logging.getLogger(__name__)


def _parse_xml(path: Path) -> ElementTree.Element | None:
    """Parse an XML file, returning the root element or None on failure."""
    try:
        tree = ElementTree.parse(path)
        return tree.getroot()
    except (ElementTree.ParseError, OSError) as e:
        log.debug("Failed to parse NFO %s: %s", path, e)
        return None


def _text(el: ElementTree.Element, tag: str) -> str:
    """Get stripped text content of a child element, or empty string."""
    child = el.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _int(el: ElementTree.Element, tag: str) -> int | None:
    """Get integer content of a child element, or None."""
    text = _text(el, tag)
    if text:
        try:
            return int(text)
        except ValueError:
            pass
    return None


def _extract_uniqueids(root: ElementTree.Element) -> dict[str, int]:
    """Extract uniqueid elements → {type: id} mapping.

    Kodi format: ``<uniqueid type="tmdb">12345</uniqueid>``
    """
    result: dict[str, int] = {}
    for uid in root.findall("uniqueid"):
        uid_type = (uid.get("type") or "").lower().strip()
        if uid.text:
            try:
                result[uid_type] = int(uid.text.strip())
            except ValueError:
                # IMDB IDs are strings (tt1234567) — store as-is won't work
                # for int fields, but we capture them separately.
                if uid_type == "imdb":
                    result["imdb"] = uid.text.strip()  # type: ignore[assignment]
    return result


def read_show_nfo(path: Path) -> dict | None:
    """Parse a tvshow.nfo file in Kodi XML format.

    Args:
        path: Path to the tvshow.nfo file.

    Returns dict with keys (all optional):
        title, year, plot, genre, uniqueids (dict of type→id),
        scraper (str), scraper_id (int).
    Returns None if the file can't be parsed or isn't a <tvshow> root.
    """
    root = _parse_xml(path)
    if root is None:
        return None
    # Accept both <tvshow> root and other roots (some NFO generators vary).
    if root.tag != "tvshow":
        # Check if there's a <tvshow> child.
        child = root.find("tvshow")
        if child is not None:
            root = child
        else:
            log.debug("NFO %s has unexpected root <%s>", path, root.tag)
            return None

    result: dict = {}
    title = _text(root, "title")
    if title:
        result["title"] = title
    year = _int(root, "year")
    if year:
        result["year"] = year
    plot = _text(root, "plot")
    if plot:
        result["plot"] = plot

    # Genre: Kodi uses multiple <genre> elements.
    genres = [g.text.strip() for g in root.findall("genre") if g.text]
    if genres:
        result["genre"] = "; ".join(genres)

    # Extract scraper binding from uniqueids.
    uniqueids = _extract_uniqueids(root)
    if uniqueids:
        result["uniqueids"] = uniqueids
        # Prefer tmdb, then tvmaze, then tvdb for scraper binding.
        for scraper_type in ("tmdb", "tvmaze", "tvdb"):
            if scraper_type in uniqueids:
                result["scraper"] = scraper_type
                result["scraper_id"] = uniqueids[scraper_type]
                break
        if "imdb" in uniqueids:
            result["imdb_id"] = uniqueids["imdb"]

    return result if result else None


def read_episode_nfo(video_path: Path) -> dict | None:
    """Parse an episode NFO file alongside a video file.

    Looks for ``<video_stem>.nfo`` next to the video file.

    Returns dict with keys (all optional):
        title, season, episode, plot.
    Returns None if no NFO exists or can't be parsed.
    """
    nfo_path = video_path.with_suffix(".nfo")
    if not nfo_path.is_file():
        return None

    root = _parse_xml(nfo_path)
    if root is None:
        return None
    # Accept <episodedetails> or other roots.
    if root.tag != "episodedetails":
        child = root.find("episodedetails")
        if child is not None:
            root = child

    result: dict = {}
    title = _text(root, "title")
    if title:
        result["title"] = title
    season = _int(root, "season")
    if season is not None:
        result["season"] = season
    episode = _int(root, "episode")
    if episode is not None:
        result["episode"] = episode
    plot = _text(root, "plot")
    if plot:
        result["plot"] = plot

    return result if result else None


def read_movie_nfo(video_path: Path) -> dict | None:
    """Parse a movie NFO file alongside a video file.

    Looks for ``<video_stem>.nfo`` next to the video, or ``movie.nfo``
    in the same directory.

    Returns dict with keys (all optional):
        title, year, plot, genre, uniqueids, scraper, scraper_id, imdb_id.
    Returns None if no NFO exists or can't be parsed.
    """
    # Try <stem>.nfo first, then movie.nfo in same dir.
    nfo_path = video_path.with_suffix(".nfo")
    if not nfo_path.is_file():
        nfo_path = video_path.parent / "movie.nfo"
    if not nfo_path.is_file():
        return None

    root = _parse_xml(nfo_path)
    if root is None:
        return None
    if root.tag != "movie":
        child = root.find("movie")
        if child is not None:
            root = child

    result: dict = {}
    title = _text(root, "title")
    if title:
        result["title"] = title
    year = _int(root, "year")
    if year:
        result["year"] = year
    plot = _text(root, "plot")
    if plot:
        result["plot"] = plot

    genres = [g.text.strip() for g in root.findall("genre") if g.text]
    if genres:
        result["genre"] = "; ".join(genres)

    uniqueids = _extract_uniqueids(root)
    if uniqueids:
        result["uniqueids"] = uniqueids
        if "tmdb" in uniqueids:
            result["scraper"] = "tmdb"
            result["scraper_id"] = uniqueids["tmdb"]
        if "imdb" in uniqueids:
            result["imdb_id"] = uniqueids["imdb"]

    return result if result else None
