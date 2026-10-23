"""Video surface — renders GStreamer frames via QPainter.

Polls the AudioEngine's appsink for decoded RGBA frames and paints
them into a QWidget.  Transparent background — the parent panel's
gradient/blur shows through for any pillarboxing/letterboxing areas.

No X11 window embedding, no ximagesink, no separate windows.
Works on both X11 and Wayland.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QWidget, QSizePolicy

log = logging.getLogger(__name__)


class VideoSurface(QWidget):
    """QWidget that renders video frames pulled from GStreamer's appsink.

    The video is scaled to fill the widget's full width, with height
    determined by the video's aspect ratio.  Vertically centered.
    Background is transparent — the parent's backdrop shows through.
    """

    fullscreen_requested = Signal()

    def contextMenuEvent(self, event) -> None:
        if self._context_menu_callback:
            self._context_menu_callback(event.globalPos())
            event.accept()
        else:
            super().contextMenuEvent(event)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_frame: QImage | None = None
        self._engine = None
        self._context_menu_callback = None

        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60 fps polling
        self._timer.timeout.connect(self._pull_frame)

        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        # Transparent — parent's gradient/blur background shows through.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def start_rendering(self, engine) -> None:
        """Start pulling video frames from the engine's appsink."""
        self._engine = engine
        self._timer.start()

    def stop_rendering(self) -> None:
        """Stop pulling frames and clear the display."""
        self._timer.stop()
        self._engine = None
        self._current_frame = None
        self.update()

    def _pull_frame(self) -> None:
        if not self._engine:
            return
        result = self._engine.pull_video_frame()
        if result:
            data, w, h = result
            img = QImage(data, w, h, w * 4, QImage.Format.Format_RGBA8888)
            self._current_frame = img.copy()  # Deep copy — data is transient
            self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.fullscreen_requested.emit()
        else:
            super().mouseDoubleClickEvent(event)

    def video_rect(self) -> QRect:
        """Return the rectangle where the video frame is drawn.

        Accounts for aspect-ratio scaling and centering (letterbox/pillarbox).
        Returns the full widget rect if no frame is available.
        """
        if not self._current_frame:
            return self.rect()
        # The size QImage.scaled() gives, without scaling the frame.
        size = self._current_frame.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - size.width()) // 2
        y = (self.height() - size.height()) // 2
        return QRect(x, y, size.width(), size.height())

    def paintEvent(self, event) -> None:
        if not self._current_frame:
            return
        p = QPainter(self)
        rect = self.video_rect()
        scaled = self._current_frame.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        p.drawImage(rect.topLeft(), scaled)
        p.end()
