"""Playing an episode saves its position for resume."""

import pytest
from PySide6.QtWidgets import QApplication

from quaver.core.library import LibraryDB
from quaver.services.podcast_service import PodcastService


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_tracking_saves_position_once_per_tick(qapp, tmp_path, monkeypatch) -> None:
    service = PodcastService(LibraryDB(tmp_path / "test.db"))
    saved = []
    monkeypatch.setattr(service._pdb, "save_playback_position",
                        lambda episode_id, pos: saved.append((episode_id, pos)))

    service.start_tracking_position(7, lambda: 1000)
    service.start_tracking_position(8, lambda: 2000)  # the next episode
    assert service._position_save_timer.isActive()
    service._position_save_timer.timeout.emit()
    assert saved == [(8, 2000)]

    service.stop_tracking_position()
    assert saved == [(8, 2000), (8, 2000)]
    assert not service._position_save_timer.isActive()
