"""Window helpers shared by Quaver and Minim."""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


def size_for_first_run(window: QWidget, width_share: float, height_share: float) -> None:
    """Size *window* to a share of its screen's available area.

    For a first run, when there is no saved geometry to restore.  Layout
    size hints suit a 1080p screen; on a 4K screen at 100 % scale they
    open very small.  Never smaller than the window's minimum size.
    """
    screen = window.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry()
    size = QSize(int(area.width() * width_share), int(area.height() * height_share))
    window.resize(size.expandedTo(window.minimumSize()))
