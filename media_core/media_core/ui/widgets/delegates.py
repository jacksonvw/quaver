"""Shared delegate and view utilities for consistent selection/hover behavior.

All interactive list and table views in Quaver should use these building
blocks to get uniform full-row selection highlighting, hover feedback,
and no per-cell focus rectangles.

Usage:
    # Delegate — extend ConsistentItemDelegate instead of QStyledItemDelegate.
    class MyDelegate(ConsistentItemDelegate):
        def paint(self, painter, option, index):
            self.paint_item_background(painter, option, index)
            # ... custom painting ...

    # View — mix in HoverTrackingMixin for row-level hover tracking.
    class MyView(HoverTrackingMixin, QListView):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._init_hover_tracking()
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)


# -- Consistent colors used across the app -----------------------------

SELECTION_BG = QColor(255, 255, 255, 25)
HOVER_BG = QColor(255, 255, 255, 12)


# -- Base delegate -----------------------------------------------------


class ConsistentItemDelegate(QStyledItemDelegate):
    """Base delegate that strips system selection/focus painting.

    Subclasses should call ``paint_item_background()`` at the start of
    their ``paint()`` method, then do their custom rendering on top.
    For simple views that just need the fix without custom painting,
    override ``paint()`` to call ``paint_item_background()`` then
    ``super().paint()`` with the cleaned-up option.
    """

    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Strip State_Selected and State_HasFocus so Qt never draws its
        own blue/coral highlight or dotted focus rectangle."""
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_Selected
        option.state &= ~QStyle.StateFlag.State_HasFocus

    def paint_item_background(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        """Paint full-row selection or hover background.

        Call this at the very start of ``paint()`` before any custom
        rendering.  It checks the view's selection model and the
        ``_hovered_row`` attribute (set by ``HoverTrackingMixin``).
        """
        view = option.widget
        sel_model = view.selectionModel() if view else None
        is_selected = sel_model is not None and sel_model.isRowSelected(
            index.row(), index.parent()
        )
        is_hovered = getattr(view, "_hovered_row", -1) == index.row()

        if is_selected:
            painter.fillRect(option.rect, SELECTION_BG)
        elif is_hovered:
            painter.fillRect(option.rect, HOVER_BG)


# -- Simple delegate for views that only need the selection fix --------


class SimpleItemDelegate(ConsistentItemDelegate):
    """Delegate that fixes selection/hover but keeps default text painting.

    Drop this onto any QTableView or QListView that doesn't need custom
    cell rendering — it just fixes the selection/hover background and
    lets Qt paint the text normally.
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        self.paint_item_background(painter, option, index)
        super().paint(painter, option, index)


# -- Hover tracking mixin for views -----------------------------------


class HoverTrackingMixin:
    """Mixin for QAbstractItemView subclasses to track the hovered row.

    Usage::

        class MyView(HoverTrackingMixin, QListView):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._init_hover_tracking()

    The mixin sets ``_hovered_row`` (int, -1 when nothing is hovered)
    which delegates read via ``getattr(view, '_hovered_row', -1)``.
    """

    _hovered_row: int = -1

    def _init_hover_tracking(self) -> None:
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event) -> None:
        idx = self.indexAt(event.pos())
        new_row = idx.row() if idx.isValid() else -1
        if new_row != self._hovered_row:
            old = self._hovered_row
            self._hovered_row = new_row
            self._repaint_row(old)
            self._repaint_row(new_row)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        old = self._hovered_row
        self._hovered_row = -1
        self._repaint_row(old)
        super().leaveEvent(event)

    def _repaint_row(self, row: int) -> None:
        """Schedule a repaint for just the given row."""
        if row < 0 or not self.model():
            return
        rect = self.visualRect(self.model().index(row, 0))
        # Extend to full viewport width so the entire row repaints.
        rect.setLeft(0)
        rect.setRight(self.viewport().width())
        self.viewport().update(rect)


# -- Drop indicator painting mixin ------------------------------------


class DropIndicatorMixin:
    """Mixin for views that need a visible drop-target line during drag.

    Tracks the drop position in ``dragMoveEvent`` and paints a white
    line in ``paintEvent``.  Works for both QTableView and QListView.

    Usage::

        class MyView(DropIndicatorMixin, HoverTrackingMixin, QTableView):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._init_hover_tracking()
    """

    _drop_row: int = -1
    _drop_above: bool = True  # True = line above row, False = line below

    def dragMoveEvent(self, event) -> None:
        idx = self.indexAt(event.position().toPoint())
        if idx.isValid():
            rect = self.visualRect(idx)
            mid = rect.top() + rect.height() // 2
            above = event.position().y() < mid
            new_row = idx.row()
        else:
            new_row = self.model().rowCount() if self.model() else 0
            above = False

        if new_row != self._drop_row or above != self._drop_above:
            self._drop_row = new_row
            self._drop_above = above
            self.viewport().update()

        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._drop_row = -1
        self.viewport().update()
        super().dragLeaveEvent(event)

    def _paint_drop_indicator(self, painter: QPainter) -> None:
        """Call this at the end of paintEvent, after super().paintEvent()."""
        if self._drop_row < 0 or not self.model():
            return
        row_count = self.model().rowCount()
        if self._drop_row >= row_count:
            # Below the last row.
            if row_count > 0:
                rect = self.visualRect(self.model().index(row_count - 1, 0))
                y = rect.bottom()
            else:
                return
        else:
            rect = self.visualRect(self.model().index(self._drop_row, 0))
            y = rect.top() if self._drop_above else rect.bottom()

        pen = QPen(QColor(255, 255, 255, 200), 2)
        painter.setPen(pen)
        painter.drawLine(0, y, self.viewport().width(), y)

    def _reset_drop_indicator(self) -> None:
        self._drop_row = -1
        self.viewport().update()
