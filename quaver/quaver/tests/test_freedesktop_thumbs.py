"""freedesktop thumbnail-cache lookup + queue/grid integration."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import quaver  # noqa: F401  (sys.path bootstrap)

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from media_core.core.freedesktop_thumbs import load_thumbnail, thumbnail_path


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _md5_of_uri(p: Path) -> str:
    return hashlib.md5(p.absolute().as_uri().encode("utf-8")).hexdigest()


def _write_thumb(cache_root: Path, source: Path, size: str = "large") -> Path:
    """Plant a 64x64 PNG at the freedesktop-spec path for *source*."""
    target = cache_root / "thumbnails" / size / f"{_md5_of_uri(source)}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    img = QImage(64, 64, QImage.Format.Format_RGBA8888)
    img.fill(0xFF00FF00)
    img.save(str(target), "PNG")
    return target


def test_thumbnail_path_is_md5_of_uri(tmp_path: Path) -> None:
    """The path is ``XDG_CACHE_HOME/thumbnails/<size>/<md5(file_uri)>.png``."""
    f = tmp_path / "video.mkv"
    f.write_bytes(b"")
    expected_dir = "thumbnails/large"
    expected_name = f"{_md5_of_uri(f)}.png"
    p = thumbnail_path(f, size="large")
    assert p is not None
    assert str(p).endswith(f"{expected_dir}/{expected_name}")


def test_load_returns_none_when_no_thumb_exists(qapp, tmp_path: Path,
                                                 monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    f = tmp_path / "video.mkv"
    f.write_bytes(b"")
    assert load_thumbnail(f) is None


def test_load_picks_up_planted_thumb(qapp, tmp_path: Path, monkeypatch) -> None:
    """Once a thumbnail PNG exists at the expected path, load_thumbnail
    returns a non-null pixmap.
    """
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    f = tmp_path / "video.mkv"
    f.write_bytes(b"")

    _write_thumb(cache_root, f, size="large")
    pm = load_thumbnail(f)
    assert pm is not None and not pm.isNull()


def test_load_falls_back_from_large_to_normal(qapp, tmp_path: Path,
                                                monkeypatch) -> None:
    """If only the 128px thumbnail exists, load_thumbnail still picks
    it up (after preferring large/256px).
    """
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    f = tmp_path / "video.mp4"
    f.write_bytes(b"")
    _write_thumb(cache_root, f, size="normal")
    pm = load_thumbnail(f)
    assert pm is not None and not pm.isNull()


def test_queue_art_cache_uses_freedesktop_for_video(qapp, tmp_path: Path,
                                                      monkeypatch) -> None:
    """Regression: the queue / Files-grid art cache must use the
    freedesktop thumbnail cache for video files (which never have
    embedded album art).  Before this wiring, video tracks showed a
    blank thumbnail in the queue.
    """
    from media_core.ui.widgets import embedded_art_cache as cache
    cache.clear_cache()

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))

    f = tmp_path / "movie.mkv"
    f.write_bytes(b"")
    _write_thumb(cache_root, f, size="large")

    pm = cache.get_thumbnail_sync(str(f), 64)
    assert pm is not None and not pm.isNull()


def test_queue_art_cache_returns_none_for_video_without_thumb(
    qapp, tmp_path: Path, monkeypatch,
) -> None:
    """Video files that the desktop's thumbnailer hasn't touched yet
    return None — we don't generate thumbnails ourselves.
    """
    from media_core.ui.widgets import embedded_art_cache as cache
    cache.clear_cache()

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))

    f = tmp_path / "untouched.mkv"
    f.write_bytes(b"")  # no thumb planted

    pm = cache.get_thumbnail_sync(str(f), 64)
    assert pm is None
