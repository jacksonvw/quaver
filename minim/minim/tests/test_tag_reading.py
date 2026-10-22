"""Tag reading for tracks opened by path.

Tracks are built from paths without touching the files, so a folder of
thousands queues instantly.  :class:`TagReader` fills their tags in on a
worker thread; the track about to play is read on the spot, before
anything displays it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import minim  # noqa: F401  (sys.path bootstrap for media_core)

pytest.importorskip("PySide6")

from mutagen.flac import FLAC
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

import media_core.services.tag_reader as tag_reader_module
from media_core.core.replaygain import RGMode
from media_core.core.tagger import apply_tags, read_tags
from media_core.services.player_service import PlayerService, RepeatMode
from media_core.services.tag_reader import TagReader
from minim.tracks import track_from_path, tracks_from_paths


def _write_flac(path: Path, seconds: float = 2.5, **tags: str) -> Path:
    """Write a header-only FLAC (no audio frames) with Vorbis comments.

    mutagen reads tags and duration from the metadata blocks alone, so
    this stands in for a real file at no cost.
    """
    rate, channels, bits = 44100, 2, 16
    packed = ((rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36)
              | int(rate * seconds))
    streaminfo = ((4096).to_bytes(2, "big") * 2 + bytes(6)
                  + packed.to_bytes(8, "big") + bytes(16))
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big")
                     + streaminfo)
    if tags:
        f = FLAC(path)
        for key, value in tags.items():
            f[key] = value
        f.save()
    return path


def _album(folder: Path, count: int) -> list[Path]:
    return [
        _write_flac(folder / f"{i:02d} Track {i}.flac", title=f"Song {i}",
                    artist="Made Up", album="Demo", tracknumber=str(i))
        for i in range(1, count + 1)
    ]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _settle(seconds: float = 0.3) -> None:
    _wait_until(lambda: False, timeout=seconds)


# ── apply_tags ──────────────────────────────────────────────────────


def test_apply_tags_fills_descriptive_fields(tmp_path: Path) -> None:
    f = _write_flac(tmp_path / "01 sea breeze.flac", seconds=165.0,
                    title="Sea Breeze", artist="Made Up", album="Summer",
                    date="1987", tracknumber="1/7", genre="City Pop")
    track = track_from_path(f)

    assert apply_tags(track, read_tags(f))

    assert (track.title, track.artist, track.album) == ("Sea Breeze", "Made Up", "Summer")
    assert track.album_artist == "Made Up"
    assert (track.year, track.track_number, track.genre) == (1987, 1, "City Pop")
    assert track.duration_ms == 165_000
    # Identity is untouched.
    assert (track.id, track.filepath, track.format, track.media_type) == (
        -1, str(f), "flac", "audio")


def test_apply_tags_keeps_filename_title_for_untagged_file(tmp_path: Path) -> None:
    track = track_from_path(_write_flac(tmp_path / "03 Sunburn.flac"))
    apply_tags(track, read_tags(track.filepath))
    assert track.title == "03 Sunburn"
    assert track.artist == ""
    assert track.duration_ms == 2500


def test_apply_tags_leaves_unreadable_file_alone(tmp_path: Path) -> None:
    f = tmp_path / "broken.mp3"
    f.write_bytes(b"not audio")
    track = track_from_path(f)
    assert not apply_tags(track, read_tags(f))
    assert track.title == "broken"


# ── TagReader ───────────────────────────────────────────────────────


def test_only_the_playing_track_is_read_on_the_ui_thread(
    app, tmp_path: Path, monkeypatch,
) -> None:
    """A big folder must not block: queue() reads nothing on the calling
    thread, and ensure() reads just the one track about to play."""
    ui_thread_reads: list[str] = []

    def spy(path):
        if threading.current_thread() is threading.main_thread():
            ui_thread_reads.append(str(path))
        return read_tags(path)

    monkeypatch.setattr(tag_reader_module, "read_tags", spy)
    _album(tmp_path, 20)
    tracks = tracks_from_paths([tmp_path])
    reader = TagReader()

    reader.queue(tracks)
    assert ui_thread_reads == []
    assert all(t.artist == "" for t in tracks)  # results apply on the UI thread

    reader.ensure(tracks[0])
    assert ui_thread_reads == [tracks[0].filepath]
    assert tracks[0].title == "Song 1"

    reader.shutdown()


def test_background_read_fills_every_track_and_announces_in_batches(
    app, tmp_path: Path,
) -> None:
    _album(tmp_path, 30)
    tracks = tracks_from_paths([tmp_path])
    reader = TagReader()
    announcements: list[int] = []
    reader.tracks_updated.connect(lambda: announcements.append(1))

    reader.queue(tracks)

    assert _wait_until(lambda: all(t.artist == "Made Up" for t in tracks))
    assert [t.title for t in tracks] == [f"Song {i}" for i in range(1, 31)]
    assert _wait_until(lambda: announcements)
    assert len(announcements) < len(tracks)
    reader.shutdown()


def test_ensure_ignores_tracks_never_queued(app, tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def spy(path):
        calls.append(str(path))
        return read_tags(path)

    monkeypatch.setattr(tag_reader_module, "read_tags", spy)
    track = track_from_path(_write_flac(tmp_path / "a.flac", title="A"))
    reader = TagReader()

    reader.ensure(track)
    reader.ensure(None)

    assert calls == []
    assert track.title == "a"


def test_cancel_forgets_pending_tracks(app, tmp_path: Path) -> None:
    _album(tmp_path, 10)
    tracks = tracks_from_paths([tmp_path])
    reader = TagReader()

    reader.queue(tracks)
    reader.cancel()
    _settle()
    reader.ensure(tracks[0])

    assert all(t.artist == "" for t in tracks)
    reader.shutdown()


def test_network_files_wait_until_they_play(app, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tag_reader_module, "is_network_path", lambda p: True)
    track = track_from_path(_write_flac(tmp_path / "remote.flac", title="Remote"))
    reader = TagReader()

    reader.queue([track])
    _settle()
    assert track.title == "remote"

    reader.ensure(track)
    assert track.title == "Remote"


# ── With PlayerService ──────────────────────────────────────────────


class _FakeEngine:
    def play_file(self, path: str) -> None: ...
    def set_next_uri(self, path) -> None: ...


def _player() -> PlayerService:
    ps = PlayerService.__new__(PlayerService)
    QObject.__init__(ps, None)
    ps._db = None
    ps._engine = _FakeEngine()
    ps._queue = []
    ps._queue_index = -1
    ps._shuffle = False
    ps._shuffle_order = []
    ps._repeat = RepeatMode.OFF
    ps._rg_mode = RGMode.OFF
    return ps


def test_track_changed_listeners_see_tags(app, tmp_path: Path) -> None:
    """Connected first, ensure() tags the track before Now Playing,
    the transport and MPRIS read it."""
    _album(tmp_path, 3)
    tracks = tracks_from_paths([tmp_path])
    player = _player()
    reader = TagReader()
    player.track_changed.connect(reader.ensure)
    seen: list[tuple[str, str, str]] = []
    player.track_changed.connect(lambda t: seen.append((t.title, t.artist, t.album)))

    reader.queue(tracks)
    player.play_tracks(tracks, start_index=1)

    assert seen == [("Song 2", "Made Up", "Demo")]
    reader.shutdown()
