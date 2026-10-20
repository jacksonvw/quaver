"""Verify argv / drop-target paths expand and convert to transient tracks."""

from pathlib import Path

import pytest

import minim  # noqa: F401  (sys.path bootstrap for media_core)

from minim.tracks import (
    AUDIO_EXTS,
    PLAYABLE_EXTS,
    VIDEO_EXTS,
    expand_paths,
    is_playable,
    track_from_path,
    tracks_from_paths,
)


def test_track_from_path_is_transient(tmp_path: Path) -> None:
    f = tmp_path / "song.mp3"
    f.write_bytes(b"")
    t = track_from_path(f)
    # Library-less mode: every track is transient.
    assert t.id == -1
    assert t.filepath == str(f)
    assert t.title == "song"
    assert t.format == "mp3"
    assert t.media_type == "audio"
    assert t.duration_ms == 0
    assert t.artist == ""


def test_track_from_path_video_media_type(tmp_path: Path) -> None:
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"")
    t = track_from_path(f)
    assert t.media_type == "movie"
    assert t.format == "mkv"


def test_track_from_path_handles_no_extension(tmp_path: Path) -> None:
    f = tmp_path / "noext"
    f.write_bytes(b"")
    t = track_from_path(f)
    # Format empty, falls back to "audio" classification.
    assert t.format == ""
    assert t.media_type == "audio"


def test_is_playable() -> None:
    assert is_playable(Path("a.mp3"))
    assert is_playable(Path("a.MP3"))
    assert is_playable(Path("a.mkv"))
    assert not is_playable(Path("a.txt"))
    assert not is_playable(Path("README"))


def test_audio_video_disjoint() -> None:
    # No extension lives in both buckets.
    assert AUDIO_EXTS.isdisjoint(VIDEO_EXTS)
    assert PLAYABLE_EXTS == AUDIO_EXTS | VIDEO_EXTS


def test_expand_paths_skips_non_playable(tmp_path: Path) -> None:
    good = tmp_path / "a.mp3"
    bad = tmp_path / "a.txt"
    missing = tmp_path / "no_such_file.mp3"
    good.write_bytes(b"")
    bad.write_bytes(b"")
    out = expand_paths([good, bad, missing])
    assert out == [good]


def test_expand_paths_walks_directory(tmp_path: Path) -> None:
    (tmp_path / "b.mp3").write_bytes(b"")
    (tmp_path / "a.mp3").write_bytes(b"")
    (tmp_path / "c.txt").write_bytes(b"")
    out = expand_paths([tmp_path])
    # Sorted by name so folder drops have a predictable play order.
    assert [p.name for p in out] == ["a.mp3", "b.mp3"]


def test_expand_paths_preserves_argv_order(tmp_path: Path) -> None:
    a = tmp_path / "first.mp3"
    b = tmp_path / "second.flac"
    a.write_bytes(b"")
    b.write_bytes(b"")
    # Files in argv order — preserved as given.
    out = expand_paths([b, a])
    assert out == [b, a]


def test_tracks_from_paths_end_to_end(tmp_path: Path) -> None:
    a = tmp_path / "song.mp3"
    b = tmp_path / "video.mkv"
    a.write_bytes(b"")
    b.write_bytes(b"")
    tracks = tracks_from_paths([a, b])
    assert [t.title for t in tracks] == ["song", "video"]
    assert [t.media_type for t in tracks] == ["audio", "movie"]
    assert all(t.id == -1 for t in tracks)


def test_tracks_from_paths_empty_for_empty_dir(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tracks_from_paths([empty]) == []
