"""Quaver-flavored album art cache — adds scraper poster_path fallback.

Generic embedded/folder art lookup is delegated to
:mod:`quaver.ui.widgets.embedded_art_cache`.  This module layers
scraper poster support (TVMaze, TMDb URL or local poster files) on
top — when a track has a ``poster_path`` that's preferred over the
embedded art (episode stills are more useful than a generic show cover).

Minim does not import this module — it goes straight to the embedded
cache.  ``on_art_ready`` and ``clear_cache`` are re-exported so existing
Quaver imports (`from quaver.ui.widgets.art_cache import on_art_ready`)
keep working.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QRunnable
from PySide6.QtGui import QPixmap

from quaver.core.embedded_art import extract_art
from quaver.core.paths import image_dir
from quaver.ui.widgets.embedded_art_cache import (
    _cache,
    _get_pool,
    _loading,
    _is_network_path,
    _result,
    _scale_to_pixmap,
    clear_cache,
    on_art_ready,
)

__all__ = ["clear_cache", "get_thumbnail", "get_thumbnail_sync", "on_art_ready"]


def _read_poster_file(poster_path: str) -> bytes | None:
    """Read a poster image — local file or remote URL (downloaded to cache)."""
    if not poster_path:
        return None
    # Remote URL — download to local cache on first access.
    if poster_path.startswith("http://") or poster_path.startswith("https://"):
        return _download_poster_to_cache(poster_path)
    try:
        p = Path(poster_path)
        if p.is_file():
            return p.read_bytes()
    except OSError:
        pass
    return None


def _download_poster_to_cache(url: str) -> bytes | None:
    """Download a remote image, cache locally, return bytes."""
    cache = image_dir() / "tvmaze"
    cache.mkdir(parents=True, exist_ok=True)
    # Stable filename from URL hash.
    name = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest() + ".jpg"
    local = cache / name
    if local.is_file():
        return local.read_bytes()
    try:
        import httpx
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code == 200:
            local.write_bytes(resp.content)
            return resp.content
    except Exception:
        pass
    return None


class _PosterLoadTask(QRunnable):
    """Background art load that prefers a scraper poster over embedded art."""

    def __init__(self, filepath: str, size: int, poster_path: str) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._filepath = filepath
        self._size = size
        self._poster_path = poster_path

    def run(self) -> None:
        key = (self._filepath, self._size)
        if key in _cache:
            pm = _cache[key]
        else:
            # Prefer scraper-provided poster (episode still) over embedded art,
            # which is typically just the generic show poster for video files.
            art = None
            if self._poster_path:
                art = _read_poster_file(self._poster_path)
            if not art and not _is_network_path(self._filepath):
                # Only extract embedded art from local files.  Network-mounted
                # files (KIO FUSE / GVFS) are too expensive to open eagerly —
                # hundreds of stat+read calls would hammer the NAS at startup.
                art = extract_art(self._filepath)
            pm = _scale_to_pixmap(art, self._size)
            _cache[key] = pm
        _loading.discard(key)
        _result.ready.emit(self._filepath, self._size, pm)


def get_thumbnail(
    filepath: str, size: int, poster_path: str = "",
) -> QPixmap | None:
    """Return cached thumbnail immediately, or None if not yet loaded.

    If not cached, queues a background load. Connect to ``on_art_ready()``
    to be notified when it finishes.

    *poster_path* is an optional fallback image file (e.g. a cached poster
    from TVMaze/TMDb) used when the media file has no embedded art.  When
    omitted, behavior matches the generic embedded cache.
    """
    if not poster_path:
        from quaver.ui.widgets.embedded_art_cache import get_thumbnail as _generic
        return _generic(filepath, size)

    key = (filepath, size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    # If previously cached as None (no art found) but a poster_path is now
    # available, invalidate and re-load — the poster may have been downloaded
    # by enrichment since the last attempt.
    if key in _cache:
        del _cache[key]
    # Queue async load with poster fallback.
    if key not in _loading:
        _loading.add(key)
        _get_pool().start(_PosterLoadTask(filepath, size, poster_path))
    return None


def get_thumbnail_sync(
    filepath: str, size: int, poster_path: str = "",
) -> QPixmap | None:
    """Load and return a thumbnail synchronously (blocking)."""
    if not poster_path:
        from quaver.ui.widgets.embedded_art_cache import get_thumbnail_sync as _generic
        return _generic(filepath, size)

    key = (filepath, size)
    if key in _cache:
        return _cache[key]
    art = _read_poster_file(poster_path)
    if not art:
        art = extract_art(filepath)
    pm = _scale_to_pixmap(art, size)
    _cache[key] = pm
    return pm
