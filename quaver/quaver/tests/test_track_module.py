"""Track + MIME constants live in core/track.py, importable without library.py.

Phase 1 prep — Track must be loadable without dragging in the SQLite
library stack so that ``media_core`` (and Minim) can import it cleanly.
"""

from __future__ import annotations


def test_track_module_no_sqlite_dependency() -> None:
    """Importing quaver.core.track must not import sqlite3 transitively.

    This isn't airtight — sqlite3 is a stdlib module that any other
    test imports may already have loaded — but it does verify track.py
    has no direct sqlite3 import statement.
    """
    import quaver.core.track as t
    # The module's source must not import sqlite3.
    import inspect
    src = inspect.getsource(t)
    assert "sqlite3" not in src
    assert "LibraryDB" not in src


def test_track_dataclass_fields() -> None:
    from quaver.core.track import Track
    fields = Track.__dataclass_fields__
    # Spot-check the load-bearing fields used across the suite.
    for name in [
        "id", "filepath", "title", "artist", "album", "album_artist",
        "duration_ms", "media_type", "show_id",
    ]:
        assert name in fields, f"Track missing field: {name}"


def test_mime_constants_present() -> None:
    from quaver.core.track import (
        EPISODE_IDS_MIME,
        PLAYLIST_IDS_MIME,
        TRACK_IDS_MIME,
        VIDEO_IDS_MIME,
    )
    assert TRACK_IDS_MIME == "application/x-quaver-track-ids"
    assert EPISODE_IDS_MIME == "application/x-quaver-episode-ids"
    assert PLAYLIST_IDS_MIME == "application/x-quaver-playlist-ids"
    assert VIDEO_IDS_MIME == "application/x-quaver-video-ids"


def test_library_reexports_match_track_module() -> None:
    """library.py re-exports must point at the same objects as track.py."""
    from quaver.core import library, track
    assert library.Track is track.Track
    assert library.TRACK_IDS_MIME is track.TRACK_IDS_MIME
    assert library.EPISODE_IDS_MIME is track.EPISODE_IDS_MIME
    assert library.PLAYLIST_IDS_MIME is track.PLAYLIST_IDS_MIME
    assert library.VIDEO_IDS_MIME is track.VIDEO_IDS_MIME


def test_track_constructible_with_id_minus_one() -> None:
    """Transient tracks (podcast episodes, ad-hoc opens, Minim's queue)
    must be constructible with id=-1 — required for non-DB usage.
    """
    from quaver.core.track import Track
    t = Track(
        id=-1,
        filepath="/tmp/song.mp3",
        title="Test", artist="A", album="B", album_artist="A",
        year=None, genre="", track_number=None, disc_number=None,
        duration_ms=0, bitrate=0, format="mp3",
        replay_gain_track=None, replay_gain_album=None,
        play_count=0, last_played=None, date_added="",
        rating=0, fingerprint=None,
    )
    assert t.id == -1
    assert t.media_type == "audio"  # default
