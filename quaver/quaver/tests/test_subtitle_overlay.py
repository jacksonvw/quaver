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


class _Engine:
    """Just what SubtitleOverlay polls."""

    def __init__(self) -> None:
        self.serial = 0
        self.text = None

    def get_subtitle_flags(self) -> bool:
        return True

    def get_subtitle_serial(self) -> int:
        return self.serial

    def pull_ass_chunks(self) -> list:
        return []

    def get_ass_header(self):
        return None

    def get_position_ms(self) -> int:
        return 8000

    def get_subtitle_delay(self) -> int:
        return 0

    def get_active_external(self):
        return None

    def pull_subtitle_text(self):
        text, self.text = self.text, None
        return text


def test_switching_from_ass_to_srt_shows_the_srt(qapp) -> None:
    overlay = SubtitleOverlay()
    engine = _Engine()
    overlay.start(engine, None)
    overlay._libass_track_ready = True  # an ASS track was rendering

    engine.serial += 1  # the menu chose the SRT track
    engine.text = ("Italic via SRT tags", 2000)
    overlay._poll()
    assert not overlay._libass_track_ready
    assert overlay._current_text == "Italic via SRT tags"
    overlay.stop()


class _Libass:
    """Records the frame libass is given; renders one bitmap."""

    available = True

    def __init__(self) -> None:
        self.frame = None

    def set_frame_size(self, w: int, h: int) -> None:
        self.frame = (w, h)

    def render_frame(self, now_ms: int) -> list:
        return ["bitmap"]


def test_ass_is_laid_out_on_the_picture_not_the_page(qapp) -> None:
    """Letterboxed video: libass gets the picture's size, and its bitmaps
    are drawn from the picture's corner."""
    from PySide6.QtCore import QPoint, QRect

    page = QWidget()
    page.resize(1064, 670)

    class _Surface(QWidget):
        def video_rect(self):
            return QRect(0, 36, 1064, 598)

    surface = _Surface(page)
    surface.setGeometry(page.rect())
    overlay = SubtitleOverlay(page)
    overlay.start(_Engine(), surface)
    overlay._libass = _Libass()
    overlay._libass_track_ready = True
    overlay._poll()
    assert overlay._libass.frame == (1064, 598)
    assert overlay._ass_origin == QPoint(0, 36)
    overlay.stop()
