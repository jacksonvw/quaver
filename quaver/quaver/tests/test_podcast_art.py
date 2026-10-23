"""Queued podcast episodes find their show and its artwork."""

from quaver.core import podcast
from quaver.core.library import LibraryDB
from quaver.core.podcast import PodcastDB


def test_sources_map_to_their_podcast(tmp_path) -> None:
    pdb = PodcastDB(LibraryDB(tmp_path / "test.db"))
    pid = pdb.add_podcast("Midnight Radio", "http://example.invalid/feed.xml")
    pdb.upsert_episodes_batch(pid, [
        {"title": "One", "url": "http://example.invalid/1.opus"},
        {"title": "Two", "url": "http://example.invalid/2.opus"},
    ])
    two = next(e for e in pdb.get_episodes(pid) if e.title == "Two")
    pdb.set_episode_downloaded(two.id, "/podcasts/2.opus")

    found = pdb.podcast_ids_for_sources([
        "http://example.invalid/1.opus", "/podcasts/2.opus", "/music/song.opus",
    ])
    assert found == {"http://example.invalid/1.opus": pid, "/podcasts/2.opus": pid}


def test_artwork_path_finds_the_cached_image(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(podcast, "_ART_CACHE_DIR", tmp_path)
    (tmp_path / "3.png").write_bytes(b"png")
    assert podcast.artwork_path(3) == tmp_path / "3.png"
    assert podcast.artwork_path(4) is None
