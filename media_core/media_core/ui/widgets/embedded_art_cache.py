"""Generic embedded album art cache — no library or scraper knowledge.

Loads embedded ID3/FLAC/MP4 art and sibling ``cover.jpg``-style files
via :mod:`quaver.core.embedded_art`.  Network paths (KIO FUSE / GVFS)
are skipped to avoid hammering the NAS at startup.

Quaver's scraper-aware cache (:mod:`quaver.ui.widgets.art_cache`) wraps
this module to add scraper-poster fallbacks.  Minim only ever touches
this generic cache.

Design note — both modules share the same in-process ``_cache`` dict
keyed by ``(filepath, size)``, so a poster-loaded thumbnail and an
embedded-loaded thumbnail for the same file don't both occupy memory.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QPixmap

from media_core.core.embedded_art import extract_art
from media_core.core.freedesktop_thumbs import load_thumbnail as _fdo_load
from media_core.core.media_exts import VIDEO_EXTS_DOTTED
from media_core.core.network import is_network_path as _is_network_path


# Global cache: (filepath, size) → scaled QPixmap | None.
_cache: dict[tuple[str, int], QPixmap | None] = {}

# Thread pool shared across all art loading.
_pool: QThreadPool | None = None


def _get_pool() -> QThreadPool:
    global _pool
    if _pool is None:
        _pool = QThreadPool()
        _pool.setMaxThreadCount(2)
    return _pool


class ArtResult(QObject):
    """Tiny QObject that delivers the loaded pixmap via a signal."""
    ready = Signal(str, int, object)  # filepath, size, QPixmap | None


# Single shared result emitter — widgets connect to this.
_result = ArtResult()


def on_art_ready() -> Signal:
    """Return the signal that fires when any art finishes loading.

    Signature: (filepath: str, size: int, pixmap: QPixmap | None)
    """
    return _result.ready


def _scale_to_pixmap(art: bytes | None, size: int) -> QPixmap | None:
    """Decode raw image bytes to a square QPixmap of the requested size."""
    if not art:
        return None
    pm = QPixmap()
    pm.loadFromData(art)
    return pm.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _is_video_path(filepath: str) -> bool:
    # Cheap suffix check — works equally well on local and KIO-FUSE
    # paths (we only ever care about the extension).
    dot = filepath.rfind(".")
    return dot >= 0 and filepath[dot:].lower() in VIDEO_EXTS_DOTTED


def _scale_pixmap(pm: QPixmap | None, size: int) -> QPixmap | None:
    if pm is None or pm.isNull():
        return None
    return pm.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _load_video_thumb(filepath: str, size: int) -> QPixmap | None:
    """Try the freedesktop thumbnail cache for video files.

    Mirrors what Dolphin/Nautilus draw next to a video file.  Returns
    ``None`` if no cached thumbnail exists yet — the desktop's own
    thumbnailer is responsible for generating them; we don't.
    """
    pm = _fdo_load(filepath)
    return _scale_pixmap(pm, size)


class _LoadTask(QRunnable):
    """Background embedded/folder art extraction + scaling."""

    def __init__(self, filepath: str, size: int) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._filepath = filepath
        self._size = size

    def run(self) -> None:
        key = (self._filepath, self._size)
        if key in _cache:
            pm = _cache[key]
        else:
            pm: QPixmap | None = None
            # Video → freedesktop thumbnail cache (Dolphin/Tumbler etc.
            # already generated one for files the user has browsed in
            # a file manager).  Cheap PNG load, no decode of the video.
            if _is_video_path(self._filepath):
                pm = _load_video_thumb(self._filepath, self._size)
            # Audio (or video without a freedesktop thumb) →
            # embedded/folder album art via mutagen.
            if pm is None:
                # Network-mounted files (KIO FUSE / GVFS) are too
                # expensive to open eagerly — hundreds of stat+read
                # calls would hammer the NAS at startup.  Skip art
                # extraction for them entirely.
                art = (
                    None if _is_network_path(self._filepath)
                    else extract_art(self._filepath)
                )
                pm = _scale_to_pixmap(art, self._size)
            _cache[key] = pm
        _result.ready.emit(self._filepath, self._size, pm)


def get_thumbnail(
    filepath: str, size: int, poster_path: str = "",
) -> QPixmap | None:
    """Return cached thumbnail immediately, or None if not yet loaded.

    If not cached, queues a background load. Connect to ``on_art_ready()``
    to be notified when it finishes.

    *poster_path* is accepted for signature parity with library-flavored
    caches (e.g. Quaver's ``art_cache``) but is ignored here — this
    module knows nothing about scrapers.  It only loads embedded/folder
    art.
    """
    del poster_path  # silently ignored — see docstring
    key = (filepath, size)
    if key in _cache:
        return _cache[key]
    _get_pool().start(_LoadTask(filepath, size))
    return None


def get_thumbnail_sync(
    filepath: str, size: int, poster_path: str = "",
) -> QPixmap | None:
    """Load and return a thumbnail synchronously (blocking).

    *poster_path* is accepted but ignored — see :func:`get_thumbnail`.
    """
    del poster_path
    key = (filepath, size)
    if key in _cache:
        return _cache[key]
    pm: QPixmap | None = None
    if _is_video_path(filepath):
        pm = _load_video_thumb(filepath, size)
    if pm is None:
        art = None if _is_network_path(filepath) else extract_art(filepath)
        pm = _scale_to_pixmap(art, size)
    _cache[key] = pm
    return pm


def clear_cache() -> None:
    """Drop all cached thumbnails."""
    _cache.clear()
