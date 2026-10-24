"""A row's hover highlight goes when the pointer leaves the rows.

Regression: views cleared hover in their own leaveEvent, but the scroll
bars and headers are children of the view, so moving onto them sends a
Leave only to the viewport.  The row stayed lit until the pointer came
back over the rows.
"""

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from media_core.core.track import Track
from quaver.ui.panels.podcast_panel import _EpisodeTableView
from quaver.ui.widgets.album_view import AlbumView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _track(i: int) -> Track:
    return Track(
        id=i, filepath=f"/music/{i}.opus", title=f"T{i}", artist="A", album="Drive",
        album_artist="A", year=None, genre="", track_number=i, disc_number=1,
        duration_ms=190_000, bitrate=0, format="", replay_gain_track=None,
        replay_gain_album=None, play_count=0, last_played=None, date_added="",
        rating=0, fingerprint=None,
    )


def _album_table():
    view = AlbumView()
    view.load_tracks([_track(i) for i in range(1, 41)])
    return view, view._table


def _episode_table():
    model = QStandardItemModel(60, 2)
    for row in range(60):
        model.setItem(row, 0, QStandardItem(f"Episode {row}"))
    view = _EpisodeTableView()
    view.setModel(model)
    return view, view


def _hover_row(qapp, window, table) -> None:
    window.resize(600, 400)
    window.show()
    QTest.qWaitForWindowExposed(window)
    QTest.mouseMove(table.viewport(), table.visualRect(table.model().index(5, 1)).center())
    qapp.processEvents()
    assert table._hovered_row == 5


def _move_onto(qapp, widget) -> None:
    QTest.mouseMove(widget, widget.rect().center())
    qapp.processEvents()
    assert widget.underMouse()


@pytest.mark.parametrize("build", [_album_table, _episode_table])
def test_moving_onto_the_scroll_bar_clears_hover(qapp, build) -> None:
    window, table = build()
    try:
        _hover_row(qapp, window, table)
        _move_onto(qapp, table.verticalScrollBar())
        assert table._hovered_row == -1
    finally:
        window.close()


def test_moving_onto_the_header_clears_hover(qapp) -> None:
    window, table = _episode_table()
    try:
        _hover_row(qapp, window, table)
        _move_onto(qapp, table.horizontalHeader())
        assert table._hovered_row == -1
    finally:
        window.close()


@pytest.mark.parametrize("build", [_album_table, _episode_table])
def test_leaving_the_window_clears_hover(qapp, build) -> None:
    window, table = build()
    try:
        _hover_row(qapp, window, table)
        QTest.mouseMove(window, QPoint(-50, -50))
        qapp.processEvents()
        assert table._hovered_row == -1
    finally:
        window.close()
