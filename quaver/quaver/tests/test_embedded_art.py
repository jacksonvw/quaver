"""Embedded art module + art_cache split.

Verifies that the Qt-free ``embedded_art`` module is importable on its
own (no PySide6 needed for the helpers it exports) and that the cache
modules expose the expected public surface for the ``poster_path`` /
no-``poster_path`` split.
"""

from __future__ import annotations

import pytest


def test_embedded_art_has_no_qt_imports() -> None:
    """The core embedded_art module must stay Qt-free so it can move to media_core."""
    import quaver.core.embedded_art as ea
    # Qt symbols must not leak in.
    qt_names = [n for n in dir(ea) if n.startswith("Q")]
    assert qt_names == [], f"Qt names leaked into embedded_art: {qt_names}"


def test_embedded_art_public_helpers() -> None:
    from quaver.core.embedded_art import (
        extract_art,
        extract_embedded_art,
        find_folder_art,
    )
    # Non-existent file → safe None on each.
    assert find_folder_art("/nonexistent/track.flac") is None
    assert extract_embedded_art("/nonexistent/track.flac") is None
    assert extract_art("/nonexistent/track.flac") is None
    assert extract_art("/nonexistent/track.flac", prefer_hires=True) is None


def test_embedded_art_finds_folder_cover(tmp_path) -> None:
    track = tmp_path / "song.flac"
    track.write_bytes(b"not a real flac")
    cover = tmp_path / "cover.jpg"
    payload = b"fake-jpeg-bytes"
    cover.write_bytes(payload)

    from quaver.core.embedded_art import find_folder_art, extract_art
    assert find_folder_art(str(track)) == payload
    # extract_art falls back to folder art when embedded extraction yields nothing.
    assert extract_art(str(track)) == payload


def test_art_cache_modules_share_state() -> None:
    """The Quaver wrapper and the generic cache must share the underlying dict."""
    from quaver.ui.widgets import embedded_art_cache as generic
    from quaver.ui.widgets import art_cache as quaver
    # Same `_cache` reference — split must not double-allocate cache memory.
    assert generic._cache is quaver._cache
    assert generic._result is quaver._result


def test_art_cache_public_surface() -> None:
    """The Quaver wrapper re-exports clear_cache / on_art_ready."""
    from quaver.ui.widgets.art_cache import (
        clear_cache,
        get_thumbnail,
        get_thumbnail_sync,
        on_art_ready,
    )
    # Accept the (filepath, size) call form as well as (filepath, size, poster_path).
    assert get_thumbnail("/nonexistent.mp3", 64) is None  # queues async, returns None
    # Sync path with no poster + missing file → cleanly returns None.
    assert get_thumbnail_sync("/nonexistent.mp3", 64) is None
    # And the re-exports exist.
    assert callable(clear_cache)
    assert callable(on_art_ready)


def test_embedded_art_cache_ignores_poster_path() -> None:
    """The generic cache accepts ``poster_path`` for signature parity with
    the Quaver wrapper, but ignores it — this module knows nothing about
    scrapers.  Lets the queue widget swap providers without per-call
    branching on whether poster_path is supported.
    """
    import inspect
    from quaver.ui.widgets.embedded_art_cache import (
        get_thumbnail,
        get_thumbnail_sync,
    )
    for fn in (get_thumbnail, get_thumbnail_sync):
        params = inspect.signature(fn).parameters
        assert "poster_path" in params, f"{fn.__name__} must accept poster_path"
        assert params["poster_path"].default == ""

    # And it really is a no-op — calling with a poster URL on a missing
    # file returns None just like calling without poster_path.
    from quaver.ui.widgets.embedded_art_cache import get_thumbnail_sync
    assert get_thumbnail_sync("/nonexistent.mp3", 64) is None
    assert get_thumbnail_sync("/nonexistent.mp3", 64, poster_path="x") is None


def test_thumbnail_requests_while_loading_start_one_load(monkeypatch) -> None:
    """Queue reloads ask for every row's art again; asks that arrive while a
    load is in flight must not start duplicates."""
    import media_core.ui.widgets.embedded_art_cache as cache

    started = []
    monkeypatch.setattr(cache, "_get_pool",
                        lambda: type("Pool", (), {"start": lambda self, t: started.append(t)})())
    key = ("/nowhere/song.flac", 48)
    cache._cache.pop(key, None)
    cache._loading.discard(key)

    assert cache.get_thumbnail(*key) is None
    assert cache.get_thumbnail(*key) is None
    assert len(started) == 1

    started[0].run()  # finishes: no art found, cached as None
    assert key not in cache._loading
    assert cache.get_thumbnail(*key) is None
    assert len(started) == 1
    cache._cache.pop(key, None)



def _tagged_flac(path, **tags):
    """A header-only FLAC that mutagen parses, with Vorbis comments."""
    from mutagen.flac import FLAC

    rate = 44100
    packed = (rate << 44) | (1 << 41) | (15 << 36) | rate
    info = (4096).to_bytes(2, "big") * 2 + bytes(6) + packed.to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    f = FLAC(path)
    for key, value in tags.items():
        f[key] = value
    f.save()
    return path


def test_tagged_flac_without_picture_falls_back_to_folder_art(tmp_path) -> None:
    """Vorbis comments iterate as (key, value) tuples; scanning them for ID3
    APIC keys raised, so these files got no art at all — not even cover.jpg."""
    from quaver.core.embedded_art import extract_art, extract_embedded_art

    track = _tagged_flac(tmp_path / "song.flac", title="Song", artist="Someone")
    (tmp_path / "cover.jpg").write_bytes(b"folder-art")

    assert extract_embedded_art(str(track)) is None
    assert extract_art(str(track)) == b"folder-art"


def test_vorbis_comment_picture_is_found(tmp_path) -> None:
    """Opus and Ogg Vorbis carry art as a base64 METADATA_BLOCK_PICTURE comment."""
    import base64

    from mutagen.flac import Picture

    from quaver.core.embedded_art import extract_embedded_art

    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/jpeg", b"embedded-art"
    track = _tagged_flac(tmp_path / "song.flac", title="Song",
                         metadata_block_picture=base64.b64encode(pic.write()).decode())

    assert extract_embedded_art(str(track)) == b"embedded-art"
