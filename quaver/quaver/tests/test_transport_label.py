"""The transport names the track without a stray dash for a missing artist."""

import pytest
from PySide6.QtWidgets import QApplication

from media_core.core.track import Track
from media_core.ui.widgets.transport import TransportWidget


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _track(title: str, artist: str) -> Track:
    return Track(
        id=-1, filepath="/videos/Subtitle Test.mkv", title=title, artist=artist,
        album="", album_artist="", year=None, genre="", track_number=None,
        disc_number=None, duration_ms=0, bitrate=0, format="", replay_gain_track=None,
        replay_gain_album=None, play_count=0, last_played=None, date_added="",
        rating=0, fingerprint=None,
    )


def test_show_track(qapp) -> None:
    t = TransportWidget()
    t.show_track(_track("Sea Breeze", "Ember"))
    assert t._track_label.text() == "Ember — Sea Breeze"
    t.show_track(_track("Subtitle Test", ""))
    assert t._track_label.text() == "Subtitle Test"
    t.show_track(_track("", ""))
    assert t._track_label.text() == "Subtitle Test.mkv"
    t.show_track(None)
    assert t._track_label.text() == "No file loaded"
