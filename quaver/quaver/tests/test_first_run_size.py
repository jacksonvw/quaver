"""A first run opens at a share of the screen, not at the layout's hint."""

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from media_core.ui.window import size_for_first_run


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_size_is_a_share_of_the_screen(qapp) -> None:
    area = QGuiApplication.primaryScreen().availableGeometry()
    w = QWidget()
    size_for_first_run(w, 0.5, 0.25)
    assert (w.width(), w.height()) == (int(area.width() * 0.5), int(area.height() * 0.25))


def test_size_never_goes_below_the_minimum(qapp) -> None:
    w = QWidget()
    w.setMinimumSize(5000, 4000)
    size_for_first_run(w, 0.1, 0.1)
    assert (w.width(), w.height()) == (5000, 4000)
