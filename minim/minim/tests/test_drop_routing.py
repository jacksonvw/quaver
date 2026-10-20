"""Drop routing — drops on the panel play instantly, drops on the queue
append.  See :meth:`MainWindow.dropEvent` and
:meth:`MainWindow.play_paths_now`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import minim  # noqa: F401  (sys.path bootstrap)

pytest.importorskip("PySide6")

from media_core.core.track import Track
from minim.ui.main_window import MainWindow


def _stub_track(path: str) -> Track:
    return Track(
        id=-1, filepath=path, title=path, artist="", album="", album_artist="",
        year=None, genre="", track_number=None, disc_number=None,
        duration_ms=0, bitrate=0, format="mp3",
        replay_gain_track=None, replay_gain_album=None,
        play_count=0, last_played=None, date_added="", rating=0,
        fingerprint=None, media_type="audio",
    )


class _StubPlayer:
    def __init__(self, queue: list[Track] | None = None) -> None:
        self.queue = list(queue or [])
        self.calls: list[tuple] = []

    def play_tracks(self, tracks: list[Track], start_index: int = 0) -> None:
        self.calls.append(("play_tracks", list(tracks), start_index))
        self.queue = list(tracks)

    def enqueue(self, track: Track) -> None:
        self.calls.append(("enqueue", track))
        self.queue.append(track)

    def insert_tracks(self, tracks: list[Track], position: int) -> None:
        self.calls.append(("insert_tracks", list(tracks), position))
        for i, t in enumerate(tracks):
            self.queue.insert(position + i, t)


class _StubTransport:
    def __init__(self) -> None:
        self.status: str | None = None

    def set_status_message(self, msg: str) -> None:
        self.status = msg

    def clear_status_message(self) -> None:
        self.status = None


def _make_stub(queue: list[Track] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        _player=_StubPlayer(queue=queue),
        _transport=_StubTransport(),
    )


def test_play_paths_now_on_empty_queue_replaces_and_starts_at_zero(tmp_path) -> None:
    """Empty queue → identical to play_paths: replace + start at 0."""
    f = tmp_path / "a.mp3"
    f.write_bytes(b"")
    stub = _make_stub(queue=[])

    MainWindow.play_paths_now(stub, [str(f)])

    assert len(stub._player.calls) == 1
    name, tracks, idx = stub._player.calls[0]
    assert name == "play_tracks"
    assert idx == 0
    assert len(tracks) == 1
    assert tracks[0].filepath == str(f)


def test_play_paths_now_appends_and_jumps_to_first_appended(tmp_path) -> None:
    """Non-empty queue → preserve existing tracks, jump to first new."""
    existing = [_stub_track("/old/a.mp3"), _stub_track("/old/b.mp3")]
    stub = _make_stub(queue=existing)

    f1 = tmp_path / "new1.mp3"
    f1.write_bytes(b"")
    f2 = tmp_path / "new2.mp3"
    f2.write_bytes(b"")

    MainWindow.play_paths_now(stub, [str(f1), str(f2)])

    assert len(stub._player.calls) == 1
    name, tracks, idx = stub._player.calls[0]
    assert name == "play_tracks"
    # Combined queue: 2 existing + 2 new, jump to position 2 (first new).
    assert len(tracks) == 4
    assert idx == 2
    assert [t.filepath for t in tracks[:2]] == ["/old/a.mp3", "/old/b.mp3"]
    assert tracks[2].filepath == str(f1)
    assert tracks[3].filepath == str(f2)


def test_play_paths_now_with_no_playable_files_sets_status() -> None:
    """Dropping a non-playable path leaves the queue alone and surfaces
    a transport status message.
    """
    stub = _make_stub(queue=[])

    MainWindow.play_paths_now(stub, ["/does/not/exist.xyz"])

    assert stub._player.calls == []
    assert stub._transport.status == "No playable files in selection"


def test_tracks_dropped_into_idle_queue_replaces_and_plays() -> None:
    """An empty Minim receiving cross-window tracks turns into a fresh
    playlist starting at the first dropped track — same UX as a file
    drop on the panel.
    """
    stub = _make_stub(queue=[])
    incoming = [_stub_track("/a.mp3"), _stub_track("/b.mp3")]

    MainWindow._on_tracks_dropped(stub, incoming, 0)

    assert len(stub._player.calls) == 1
    name, tracks, idx = stub._player.calls[0]
    assert name == "play_tracks"
    assert idx == 0
    assert [t.filepath for t in tracks] == ["/a.mp3", "/b.mp3"]


def test_tracks_dropped_into_active_queue_inserts_at_position() -> None:
    """A non-empty Minim splices incoming tracks at the drop row
    without disturbing playback.
    """
    existing = [_stub_track("/x.mp3"), _stub_track("/y.mp3")]
    stub = _make_stub(queue=existing)
    incoming = [_stub_track("/new.mp3")]

    MainWindow._on_tracks_dropped(stub, incoming, 1)

    assert len(stub._player.calls) == 1
    name, tracks, pos = stub._player.calls[0]
    assert name == "insert_tracks"
    assert pos == 1
    assert [t.filepath for t in tracks] == ["/new.mp3"]


def test_tracks_dropped_with_empty_list_is_noop() -> None:
    """Defensive: empty list shouldn't reach the player."""
    stub = _make_stub(queue=[_stub_track("/x.mp3")])
    MainWindow._on_tracks_dropped(stub, [], 0)
    assert stub._player.calls == []
