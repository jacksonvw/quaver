"""SubtitleOverlay: placement and track switches."""

import pytest
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget

from media_core.ui.widgets.subtitle_overlay import SubtitleOverlay


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_overlay_covers_a_stacked_page_once_it_shows(qapp) -> None:
    """libass renders into the overlay's size, so a stale size draws
    subtitles off-centre.  A stacked layout only lays out its current page,
    so the video page's size can't be read before it shows."""
    stack = QStackedWidget()
    stack.addWidget(QWidget())
    page = QWidget()
    overlay = SubtitleOverlay(page)
    stack.addWidget(page)
    stack.resize(1000, 600)
    stack.show()
    qapp.processEvents()

    stack.setCurrentIndex(1)
    qapp.processEvents()
    assert page.size() == stack.size()
    assert overlay.geometry() == page.rect()

    stack.resize(1200, 700)
    qapp.processEvents()
    assert overlay.geometry() == page.rect()
    stack.close()

