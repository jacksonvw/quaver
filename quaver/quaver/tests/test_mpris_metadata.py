"""MPRIS metadata names local files and streams alike."""

from media_core.core.track import Track
from media_core.services.mpris2 import Mpris2Service


def _track(filepath: str) -> Track:
    return Track(
        id=-1, filepath=filepath, title="Signal Lost", artist="Podcast",
        album="", album_artist="", year=None, genre="Podcast",
        track_number=None, disc_number=None, duration_ms=235000, bitrate=0, format="",
        replay_gain_track=None, replay_gain_album=None,
        play_count=0, last_played=None, date_added="", rating=0, fingerprint=None,
    )


def test_streamed_track_url_is_kept() -> None:
    url = "https://example.invalid/episode-06.opus"
    meta = Mpris2Service._build_metadata(None, _track(url))
    assert meta["xesam:url"] == url


def test_local_track_url_is_a_file_uri() -> None:
    meta = Mpris2Service._build_metadata(None, _track("/music/Sea Breeze.opus"))
    assert meta["xesam:url"] == "file:///music/Sea%20Breeze.opus"
