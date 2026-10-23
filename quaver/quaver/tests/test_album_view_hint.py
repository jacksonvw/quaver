"""An empty album view shows a hint only while one is set."""

import pytest
from PySide6.QtWidgets import QApplication

from quaver.ui.widgets.album_view import AlbumView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_empty_hint_is_painted_only_when_set(qapp) -> None:
    view = AlbumView()
    view.resize(600, 400)
    view.load_tracks([])
    blank = view.grab().toImage()

    view.set_empty_hint("Your library is empty.")
    assert view.grab().toImage() != blank

    view.set_empty_hint("")
    assert view.grab().toImage() == blank
