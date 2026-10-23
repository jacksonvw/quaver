"""Adding a podcast stores its episodes along with the feed."""

from quaver.core.library import LibraryDB
from quaver.core.podcast import PodcastDB
from quaver.services import podcast_service


def test_add_podcast_stores_episodes_and_defaults(tmp_path, monkeypatch) -> None:
    episodes = [
        {"title": "Two", "url": "http://example.invalid/2.opus", "duration_ms": 1000,
         "published_at": "2026-10-09T22:00:00+00:00", "description": ""},
        {"title": "One", "url": "http://example.invalid/1.opus", "duration_ms": 2000,
         "published_at": "2026-10-02T22:00:00+00:00", "description": ""},
    ]
    monkeypatch.setattr(podcast_service, "parse_feed",
                        lambda url: ("Midnight Radio", episodes, None))
    pdb = PodcastDB(LibraryDB(tmp_path / "test.db"))
    worker = podcast_service._AddPodcastWorker(
        pdb, "http://example.invalid/feed.xml", default_retain=5, default_refresh_hours=6.0,
    )
    errors = []
    worker.error.connect(errors.append)
    worker.run()

    assert errors == []
    [podcast] = pdb.get_all_podcasts()
    assert podcast.title == "Midnight Radio"
    assert (podcast.retain_episodes, podcast.refresh_interval_hours) == (5, 6.0)
    assert sorted(e.title for e in pdb.get_episodes(podcast.id)) == ["One", "Two"]
