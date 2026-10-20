"""PlayerService queue manipulation — verifies the gapless next URI tracks
the queue across mutations.  Regression test for the "ghost queue memory"
bug where reordering or inserting tracks left the engine playing the
previously-scheduled URI.

PlayerService is constructed via __new__ + manual seeding to avoid spinning
up a real GStreamer pipeline.  Only queue/index/shuffle state is exercised.
"""

from PySide6.QtCore import QObject

from quaver.core.audio import PlaybackState
from quaver.core.replaygain import RGMode
from quaver.core.track import Track
from quaver.services.player_service import PlayerService, RepeatMode


def _track(i: int) -> Track:
    return Track(
        id=i,
        title=f"t{i}",
        artist="a",
        album="al",
        album_artist="",
        year=0,
        genre="",
        track_number=0,
        disc_number=0,
        bitrate=0,
        format="",
        replay_gain_track=None,
        replay_gain_album=None,
        filepath=f"/m/t{i}.mp3",
        duration_ms=1000,
        play_count=0,
        last_played=None,
        date_added=0,
        rating=0,
        fingerprint=None,
    )


class _FakeEngine:
    def __init__(self) -> None:
        self.next_uri: str | None = None
        self.stopped = False
        self.state = PlaybackState.STOPPED

    def play_file(self, path: str) -> None: ...
    def set_next_uri(self, path) -> None: self.next_uri = path
    def stop(self) -> None: self.stopped = True; self.state = PlaybackState.STOPPED
    def get_position_ms(self) -> int: return 0
    def get_duration_ms(self) -> int: return 0
    def shutdown(self) -> None: ...


class _FakeDB:
    def update_track_duration(self, *args, **kwargs) -> None: ...


def _make_player(queue: list[Track], current_index: int = 0) -> PlayerService:
    ps = PlayerService.__new__(PlayerService)
    QObject.__init__(ps, None)
    ps._db = _FakeDB()
    ps._engine = _FakeEngine()
    ps._queue = list(queue)
    ps._queue_index = current_index
    ps._shuffle = False
    ps._shuffle_order = []
    ps._repeat = RepeatMode.OFF
    ps._rg_mode = RGMode.OFF
    ps._queue_gapless_next()  # prime initial gapless URI
    return ps


def test_initial_next_uri() -> None:
    a, b = _track(1), _track(2)
    ps = _make_player([a, b])
    assert ps._engine.next_uri == b.filepath


def test_initial_next_uri_none_when_only_one_track() -> None:
    a = _track(1)
    ps = _make_player([a])
    assert ps._engine.next_uri is None


def test_move_in_queue_refreshes_next_uri() -> None:
    a, b, c, d = _track(1), _track(2), _track(3), _track(4)
    ps = _make_player([a, b, c, d])
    ps.move_in_queue(3, 1)  # → [a, d, b, c]
    assert ps._queue[1].id == d.id
    assert ps._engine.next_uri == d.filepath


def test_move_batch_in_queue_refreshes_next_uri() -> None:
    a, b, c, d = _track(1), _track(2), _track(3), _track(4)
    ps = _make_player([a, b, c, d])
    # Move tracks at rows [2, 3] (c, d) to position 1 → [a, c, d, b]
    ps.move_batch_in_queue([2, 3], 1)
    assert ps._engine.next_uri == ps._queue[1].filepath


def test_enqueue_next_refreshes_next_uri() -> None:
    a, b, c = _track(1), _track(2), _track(3)
    ps = _make_player([a, b, c])
    x = _track(99)
    ps.enqueue_next(x)
    assert ps._engine.next_uri == x.filepath


def test_insert_tracks_before_current_refreshes_next_uri() -> None:
    # Insert a track at the current position — current shifts forward, so
    # the same track is still playing but next changes.
    a, c = _track(1), _track(3)
    ps = _make_player([a, c])  # next = c
    b = _track(2)
    ps.insert_tracks([b], position=1)  # → [a, b, c]; current_index=0 still on a
    assert ps._engine.next_uri == b.filepath


def test_remove_next_track_refreshes_next_uri() -> None:
    a, b, c, d = _track(1), _track(2), _track(3), _track(4)
    ps = _make_player([a, b, c, d])
    ps.remove_from_queue(1)  # remove b → [a, c, d]
    assert ps._engine.next_uri == c.filepath


def test_enqueue_when_only_track_promotes_next_uri() -> None:
    a = _track(1)
    ps = _make_player([a])
    assert ps._engine.next_uri is None
    b = _track(2)
    ps.enqueue(b)
    assert ps._engine.next_uri == b.filepath


def test_clear_queue_clears_next_uri() -> None:
    a, b = _track(1), _track(2)
    ps = _make_player([a, b])
    ps.clear_queue()
    assert ps._engine.next_uri is None


def test_repeat_one_makes_current_the_next_uri() -> None:
    a, b = _track(1), _track(2)
    ps = _make_player([a, b])
    ps.set_repeat(RepeatMode.ONE)
    assert ps._engine.next_uri == a.filepath


def test_repeat_all_loops_back_from_last_track() -> None:
    a, b = _track(1), _track(2)
    ps = _make_player([a, b], current_index=1)  # on b, last track
    assert ps._engine.next_uri is None
    ps.set_repeat(RepeatMode.ALL)
    assert ps._engine.next_uri == a.filepath


# ── PlayerService library-less mode (Minim) ──────────────────────────


def _make_player_no_db(queue: list[Track], current_index: int = 0) -> PlayerService:
    """Same as _make_player but with db=None — the Minim/library-less path."""
    ps = PlayerService.__new__(PlayerService)
    QObject.__init__(ps, None)
    ps._db = None
    ps._engine = _FakeEngine()
    ps._queue = list(queue)
    ps._queue_index = current_index
    ps._shuffle = False
    ps._shuffle_order = []
    ps._repeat = RepeatMode.OFF
    ps._rg_mode = RGMode.OFF
    ps._queue_gapless_next()
    return ps


def test_player_service_works_without_db() -> None:
    """Queue mutations must work when db is None (Minim case)."""
    a, b, c, d = _track(1), _track(2), _track(3), _track(4)
    ps = _make_player_no_db([a, b, c, d])
    assert ps._db is None
    # Initial state — gapless next URI is the second track.
    assert ps._engine.next_uri == b.filepath
    # Reorder must still refresh the gapless URI without touching the DB.
    ps.move_in_queue(3, 1)  # → [a, d, b, c]
    assert ps._queue[1].id == d.id
    assert ps._engine.next_uri == d.filepath


def test_emit_position_safe_when_db_none() -> None:
    """``_emit_position`` must not crash when db is None (Minim case).

    The lazy duration backfill is now gated on ``self._db is not None``
    in addition to the pre-existing ``track.id > 0`` guard.  With a
    transient track (id=-1) — which is the only track shape Minim ever
    sees — backfill is correctly skipped and no AttributeError leaks.
    """
    t = Track(
        id=-1, filepath="/m/t.mp3", title="x", artist="", album="",
        album_artist="", year=0, genre="", track_number=0, disc_number=0,
        duration_ms=0, bitrate=0, format="",
        replay_gain_track=None, replay_gain_album=None,
        play_count=0, last_played=None, date_added=0, rating=0, fingerprint=None,
    )
    ps = _make_player_no_db([t])
    ps._engine.get_duration_ms = lambda: 12345
    ps._engine.get_position_ms = lambda: 100
    # Should not raise — both gates skip the backfill path.
    ps._emit_position()
    # Track stays at 0 because backfill never ran; that's fine — Minim
    # gets duration from GStreamer's get_duration_ms() each tick.
    assert ps._queue[0].duration_ms == 0
