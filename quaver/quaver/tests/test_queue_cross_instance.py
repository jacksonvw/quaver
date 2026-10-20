"""Cross-instance queue drag-and-drop — drops between separate
QueueWidget instances (different Minim windows, different processes).

Coverage:
  - Track serialise/deserialise round-trip survives every dataclass field.
  - The queue model exports BOTH the internal-reorder MIME and the
    full-track MIME on every drag.
  - The public QueueWidget forwards inner-view ``cross_instance_drop``
    as ``tracks_dropped`` and translates ``cross_instance_move_remove``
    into per-row ``remove_requested`` emissions in descending order.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QApplication

from media_core.core.track import QUEUE_TRACKS_MIME, Track
from media_core.ui.widgets.queue_widget import (
    _INTERNAL_MIME,
    QueueWidget,
    _deserialize_tracks,
    _serialize_tracks,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _track(i: int, **overrides) -> Track:
    base = dict(
        id=i, filepath=f"/tmp/{i}.mp3", title=f"Title {i}", artist="A",
        album="Alb", album_artist="A", year=2020, genre="g",
        track_number=i, disc_number=1, duration_ms=1000 * i, bitrate=320,
        format="mp3", replay_gain_track=-1.5, replay_gain_album=None,
        play_count=0, last_played=None, date_added="2026-01-01", rating=0,
        fingerprint=None, media_type="audio", show_id=None,
    )
    base.update(overrides)
    return Track(**base)


def test_serialize_round_trip_preserves_all_fields() -> None:
    """Every Track field survives JSON round-trip — the helper must
    track new dataclass fields automatically (it uses ``asdict``).
    """
    src = [_track(1), _track(2, media_type="movie", show_id=42)]
    blob = _serialize_tracks(src)
    out = _deserialize_tracks(blob)
    assert out == src


def test_deserialize_drops_unknown_fields() -> None:
    """A newer sender's extra fields don't break an older receiver."""
    blob = _serialize_tracks([_track(1)])
    # Pretend the sender added a "future_field": shape it as JSON and re-encode.
    import json
    payload = json.loads(blob.decode())
    payload[0]["future_field"] = "ignored"
    blob = json.dumps(payload).encode()
    out = _deserialize_tracks(blob)
    assert len(out) == 1
    assert out[0].id == 1


def test_model_mime_data_includes_both_mime_types(qapp) -> None:
    """A drag from row N exports the internal-reorder index list AND
    the full-track payload — same-window drops use the first, cross-
    window/process drops use the second.
    """
    w = QueueWidget()
    w.load_queue([_track(1), _track(2), _track(3)], current_index=0)

    indexes = [w._model.index(0), w._model.index(2)]
    mime = w._model.mimeData(indexes)

    assert mime.hasFormat(_INTERNAL_MIME)
    assert mime.hasFormat(QUEUE_TRACKS_MIME)

    tracks = _deserialize_tracks(bytes(mime.data(QUEUE_TRACKS_MIME)))
    assert [t.id for t in tracks] == [1, 3]


def test_tracks_dropped_signal_forwards_cross_instance_drop(qapp) -> None:
    """The inner view's ``cross_instance_drop`` surfaces on the public
    widget as ``tracks_dropped`` (so hosts only depend on the public
    surface).
    """
    w = QueueWidget()
    received: list[tuple[list[Track], int]] = []
    w.tracks_dropped.connect(lambda ts, pos: received.append((list(ts), pos)))

    payload = [_track(7), _track(8)]
    w._view.cross_instance_drop.emit(payload, 1)

    assert len(received) == 1
    tracks, pos = received[0]
    assert pos == 1
    assert [t.id for t in tracks] == [7, 8]


def test_cross_instance_move_remove_emits_descending_remove_requests(qapp) -> None:
    """After a successful Move out of this queue, the source widget
    must emit ``remove_requested`` for each row in DESCENDING order so
    indices stay valid as the host pops items one by one.
    """
    w = QueueWidget()
    received: list[int] = []
    w.remove_requested.connect(received.append)

    w._view.cross_instance_move_remove.emit([0, 2, 5])

    assert received == [5, 2, 0]
