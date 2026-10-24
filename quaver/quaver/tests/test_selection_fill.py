"""Selected rows get one fill, the shared delegate's, in every view.

Regression: in transparent mode the stylesheet's ``::item:selected`` rule
filled table rows under the delegate's own selection, and the album view's
stylesheet stacked two more fills on its tracks, so selection had three
strengths: album view, tables, then lists and trees.
"""

import pytest
from PySide6.QtGui import QColor, QPalette, QStandardItemModel
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidgetItem, QTreeWidgetItem, QVBoxLayout, QWidget

from media_core.core.track import Track
from quaver.ui.main_window import MainWindow
from quaver.ui.panels.podcast_panel import _EpisodeTableView, _FeedListWidget
from quaver.ui.widgets.album_view import AlbumView
from quaver.ui.widgets.delegates import SimpleItemDelegate
from quaver.ui.widgets.folder_tree_view import _FolderTree

SELECTION_ALPHA = 25 / 255  # delegates.SELECTION_BG
ACCENT = QColor(246, 116, 0)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dark_palette(qapp):
    """White fills only show on a dark base.  Set on the application, since
    the album view's stylesheet stops it inheriting a parent's palette."""
    old = qapp.palette()
    palette = QPalette(old)
    palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 36))
    palette.setColor(QPalette.ColorRole.Highlight, ACCENT)
    qapp.setPalette(palette)
    yield
    qapp.setPalette(old)


def _track(i: int) -> Track:
    return Track(
        id=i, filepath=f"/music/{i}.opus", title="", artist="A", album="Drive",
        album_artist="A", year=None, genre="", track_number=i, disc_number=1,
        duration_ms=190_000, bitrate=0, format="", replay_gain_track=None,
        replay_gain_album=None, play_count=0, last_played=None, date_added="",
        rating=0, fingerprint=None,
    )


def _album():
    view = AlbumView()
    view.load_tracks([_track(i) for i in range(1, 7)])
    table = view._table
    # Rows: year, album header, tracks 1-6.  Sample between title and duration.
    return view, table, table.model().index(3, 1), table.model().index(5, 1), 400


def _episodes():
    model = QStandardItemModel(8, 2)
    view = _EpisodeTableView()
    view.setModel(model)
    view.setItemDelegate(SimpleItemDelegate(view))
    return view, view, model.index(1, 0), model.index(3, 0), None


def _feeds():
    view = _FeedListWidget()
    view.setItemDelegate(SimpleItemDelegate(view))
    for _ in range(8):
        view.addItem(QListWidgetItem(""))
    return view, view, view.model().index(1, 0), view.model().index(3, 0), None


def _files():
    view = _FolderTree()
    for _ in range(8):
        view.addTopLevelItem(QTreeWidgetItem([""]))
    return view, view, view.model().index(1, 0), view.model().index(3, 0), None


def _show(widget, sheet: str) -> QWidget:
    window = QWidget()
    window.setStyleSheet(sheet)
    QVBoxLayout(window).addWidget(widget)
    window.resize(700, 400)
    window.show()
    QTest.qWaitForWindowExposed(window)
    return window


def _overlay_alpha(selected: QColor, unselected: QColor) -> float:
    """Alpha of the white laid over *unselected* to give *selected*."""
    if selected.alpha() < 255 or unselected.alpha() < 255:
        return (selected.alpha() - unselected.alpha()) / (255 - unselected.alpha())
    return (selected.red() - unselected.red()) / (255 - unselected.red())


@pytest.mark.parametrize("transparent", [False, True], ids=["opaque", "transparent"])
@pytest.mark.parametrize("build", [_album, _episodes, _feeds, _files])
def test_selected_row_has_only_the_delegate_fill(qapp, dark_palette, build, transparent) -> None:
    widget, view, selected, other, x = build()
    sheet = MainWindow._build_transparent_stylesheet(None) if transparent else ""
    window = _show(widget, sheet)
    try:
        selection = view.selectionModel()
        selection.select(selected, selection.SelectionFlag.ClearAndSelect
                         | selection.SelectionFlag.Rows)
        qapp.processEvents()
        image = view.viewport().grab().toImage()
        rect, other_rect = view.visualRect(selected), view.visualRect(other)
        x = rect.right() - 6 if x is None else x
        alpha = _overlay_alpha(image.pixelColor(x, rect.center().y()),
                               image.pixelColor(x, other_rect.center().y()))
        assert alpha == pytest.approx(SELECTION_ALPHA, abs=0.01)
    finally:
        window.close()


def test_album_now_playing_bar_uses_the_accent(qapp, dark_palette) -> None:
    widget, view, playing, _, _ = _album()
    widget.set_playing_track("/music/2.opus")  # row 3
    window = _show(widget, "")
    try:
        qapp.processEvents()
        image = view.viewport().grab().toImage()
        rect = view.visualRect(playing)
        assert image.pixelColor(rect.left() + 1, rect.center().y()) == ACCENT
    finally:
        window.close()
