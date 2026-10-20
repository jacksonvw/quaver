"""LibraryDB sanity checks."""

from quaver.core.library import LibraryDB


def test_db_opens_empty(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    db = LibraryDB(db_path)
    assert db_path.exists()
    assert db.get_all_tracks() == []


def test_get_sources_empty(tmp_path) -> None:
    db = LibraryDB(tmp_path / "test.db")
    assert db.get_sources() == []
