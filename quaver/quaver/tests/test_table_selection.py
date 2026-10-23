"""Tables leave selection painting to the delegate."""

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from quaver.ui.panels.playlists_panel import _PlaylistTableView
from quaver.ui.panels.podcast_panel import _EpisodeTableView
from quaver.ui.panels.youtube_panel import _VideoTableView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("cls", [_EpisodeTableView, _VideoTableView, _PlaylistTableView])
def test_item_options_carry_no_accent_fill(qapp, cls) -> None:
    view = cls()
    option = QStyleOptionViewItem()
    view.initViewItemOption(option)
    assert option.palette.color(QPalette.ColorRole.Highlight).alpha() == 0
    # The header, scrollbars and focus frame keep the accent.
    assert view.palette().color(QPalette.ColorRole.Highlight).alpha() == 255
