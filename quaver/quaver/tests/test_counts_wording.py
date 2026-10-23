"""Counts read "1 track", "1 album", "1 min", not "1 tracks"."""

import pytest
from PySide6.QtWidgets import QApplication

from media_core.core.track import Track
from quaver.ui.panels.playlists_panel import PlaylistsPanel
from quaver.ui.widgets.album_view import _format_album_duration


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _track(i: int, album: str) -> Track:
    return Track(
        id=i, filepath=f"/music/{i}.opus", title=f"T{i}", artist="A", album=album,
        album_artist="A", year=None, genre="", track_number=i, disc_number=1,
        duration_ms=190_000, bitrate=0, format="", replay_gain_track=None,
        replay_gain_album=None, play_count=0, last_played=None, date_added="",
        rating=0, fingerprint=None,
    )


def test_playlist_header_counts(qapp) -> None:
    panel = PlaylistsPanel()
    panel.load_playlists([(1, "Mix", 1)])
    panel._current_playlist_id = 1
    panel.load_playlist_tracks([_track(1, "Drive")])
    assert panel._header_stats.text().startswith("1 track / 1 album,")
    panel.load_playlist_tracks([_track(1, "Drive"), _track(2, "Sun")])
    assert panel._header_stats.text().startswith("2 tracks / 2 albums,")


def test_album_duration_minutes() -> None:
    assert _format_album_duration(60_000) == "1 min"
    assert _format_album_duration(22 * 60_000) == "22 mins"
    assert _format_album_duration(61 * 60_000) == "1 hr 1 min"
    assert _format_album_duration(78 * 60_000) == "1 hr 18 mins"
