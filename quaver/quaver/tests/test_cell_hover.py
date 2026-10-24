"""A hovered row is lit as a unit, not the cell under the pointer.

Regression: two fills landed on just the hovered cell of a table.  The
shared delegate left State_MouseOver on each cell's style option, so
Breeze painted its item hover there; and the transparent-mode stylesheet's
``::item:hover`` rule reaches the cell through the row panel the view
draws before the delegate runs.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QStandardItemModel
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QVBoxLayout, QWidget

from quaver.ui.main_window import MainWindow
from quaver.ui.panels.podcast_panel import _EpisodeTableView
from quaver.ui.widgets.delegates import SimpleItemDelegate


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _ItemHoverStyle(QProxyStyle):
    """Paints item hover the way Breeze does, loudly."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if (element == QStyle.PrimitiveElement.PE_PanelItemViewItem
                and option.state & QStyle.StateFlag.State_MouseOver):
            painter.fillRect(option.rect, QColor(255, 0, 0))
            return
        super().drawPrimitive(element, option, painter, widget)


def _hovered_row_colors(qapp, sheet: str = "", style: QStyle | None = None) -> tuple:
    """Hover cell (2, 0) of an episode table; return row 2's cell colours
    and row 5's colour."""
    window = QWidget()
    window.setStyleSheet(sheet)
    model = QStandardItemModel(10, 3)
    table = _EpisodeTableView()
    if style is not None:
        table.setStyle(style)
    table.setModel(model)
    table.setItemDelegate(SimpleItemDelegate(table))
    table.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)  # as Breeze does
    QVBoxLayout(window).addWidget(table)
    window.resize(500, 400)
    window.show()
    try:
        QTest.qWaitForWindowExposed(window)
        # Qt drops a move to where the pointer already is; start elsewhere.
        QTest.mouseMove(table.viewport(), table.visualRect(model.index(7, 1)).center())
        QTest.mouseMove(table.viewport(), table.visualRect(model.index(2, 0)).center())
        qapp.processEvents()
        assert table._hovered_row == 2

        image = table.viewport().grab().toImage()

        def color(row: int, col: int) -> int:
            return image.pixelColor(table.visualRect(model.index(row, col)).center()).rgba()

        return {color(2, col) for col in range(3)}, color(5, 0)
    finally:
        window.close()


def test_style_item_hover_does_not_light_one_cell(qapp) -> None:
    style = _ItemHoverStyle()
    row, _ = _hovered_row_colors(qapp, style=style)
    assert len(row) == 1


def test_transparent_stylesheet_does_not_light_one_cell(qapp) -> None:
    sheet = MainWindow._build_transparent_stylesheet(None)
    row, unhovered = _hovered_row_colors(qapp, sheet=sheet)
    assert len(row) == 1
    assert row != {unhovered}  # the row's own hover fill still shows
