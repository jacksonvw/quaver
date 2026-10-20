"""Fullscreen Now Playing — thin shell that reparents NowPlayingPanel.

The NowPlayingPanel itself handles all rendering (background, art, video,
context menu, wheel volume, spectrum).  This shell only adds:
- Floating draggable transport container
- Queue side panel
- Auto-hide timer + cursor hiding
- Keyboard shortcuts (Space, Left/Right, Esc)
- Double-click to exit

Screen saver inhibit is handled globally by MainWindow (covers both
fullscreen and windowed video playback).

No duplicate rendering.  No duplicate config.  No duplicate context menu.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, Qt, QPoint, QTimer, Signal
from PySide6.QtGui import (
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QColor,
)
from PySide6.QtWidgets import (
    QAbstractSlider,
    QApplication,
    QVBoxLayout,
    QWidget,
)

_log = logging.getLogger(__name__)


_HIDE_DELAY_MS = 3000
_CONTAINER_MARGIN_BOTTOM = 40
_CONTAINER_PADDING = 8


# ------------------------------------------------------------------
# Floating draggable container for transport
# ------------------------------------------------------------------

class _FloatingContainer(QWidget):
    """Thin draggable wrapper with rounded corners and translucent bg.

    Holds the reparented TransportWidget.  Positioned near the bottom-center
    of the fullscreen overlay.  VLC-style: user can drag it anywhere.

    Drag mechanics:
    - Event filter on non-interactive children so clicks on dead areas drag.
    - Sliders and buttons excluded — they handle their own events.
    - 5px threshold prevents accidental micro-drags.
    - ``user_dragged`` prevents auto-repositioning after manual drag.
    """

    _DRAG_THRESHOLD = 5

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._press_global: QPoint | None = None
        self._pos_at_press: QPoint | None = None
        self._dragging = False
        self.user_dragged = False

        self._layout = QVBoxLayout(self)
        p = _CONTAINER_PADDING
        self._layout.setContentsMargins(p, p, p, p)
        self._layout.setSpacing(0)

    def add_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)
        self._install_drag_filter(widget)

    def _install_drag_filter(self, widget: QWidget) -> None:
        from PySide6.QtWidgets import QAbstractButton
        if not isinstance(widget, (QAbstractSlider, QAbstractButton)):
            widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            if not isinstance(child, (QAbstractSlider, QAbstractButton)):
                child.installEventFilter(self)

    # -- Shared drag helpers (used by both eventFilter and direct events) --

    def _begin_drag(self, global_pos: QPoint) -> None:
        self._press_global = global_pos
        self._pos_at_press = self.pos()
        self._dragging = False

    def _update_drag(self, global_pos: QPoint) -> bool:
        """Returns True if drag threshold was crossed and position updated."""
        if self._press_global is None:
            return False
        if not self._dragging:
            dist = (global_pos - self._press_global).manhattanLength()
            if dist < self._DRAG_THRESHOLD:
                return False
            self._dragging = True
        delta = global_pos - self._press_global
        self.move(self._pos_at_press + delta)
        self.user_dragged = True
        return True

    def _end_drag(self) -> None:
        self._press_global = None
        self._pos_at_press = None
        self._dragging = False

    # -- Event filter for child widgets -----------

    def eventFilter(self, obj, event) -> bool:
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._begin_drag(obj.mapToGlobal(event.position().toPoint()))
        elif etype == QEvent.Type.MouseMove:
            if (self._press_global is not None
                    and event.buttons() & Qt.MouseButton.LeftButton):
                gp = obj.mapToGlobal(event.position().toPoint())
                if not self._update_drag(gp):
                    return False
        elif etype == QEvent.Type.MouseButtonRelease:
            self._end_drag()
        return False

    # -- Direct mouse events (container padding area) --

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().toRectF(), 12, 12)
        p.fillPath(path, QColor(0, 0, 0, 180))
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._begin_drag(event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (self._press_global is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self._update_drag(event.globalPosition().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._end_drag()
        super().mouseReleaseEvent(event)


# ------------------------------------------------------------------
# Floating side panel for queue
# ------------------------------------------------------------------

class _QueuePanel(QWidget):
    """Right-side panel for the reparented QueueWidget in fullscreen."""

    _PANEL_WIDTH = 320

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedWidth(self._PANEL_WIDTH)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

    def add_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().toRectF(), 8, 8)
        p.fillPath(path, QColor(0, 0, 0, 160))
        p.end()


# ------------------------------------------------------------------
# Main fullscreen shell
# ------------------------------------------------------------------

class FullscreenOverlay(QWidget):
    """Fullscreen shell — reparents NowPlayingPanel as the background.

    The panel handles all rendering, context menus, and wheel volume.
    This shell only provides floating transport, queue panel, auto-hide,
    cursor hiding, and keyboard shortcuts.
    """

    closed = Signal()

    def __init__(self, nowplaying_panel: QWidget,
                 transport_widget: QWidget,
                 queue_widget: QWidget | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nowplaying_panel = nowplaying_panel
        self._transport_widget = transport_widget
        self._queue_widget = queue_widget
        self._activated = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMouseTracking(True)

        # Cursor/transport auto-hide timer.
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._hide_controls)
        self._last_mouse_pos = None

        # Floating container for transport.
        self._container = _FloatingContainer(self)
        self._container.setVisible(False)

        # Queue panel.
        self._queue_panel: _QueuePanel | None = None
        self._queue_visible = False
        if self._queue_widget is not None:
            self._queue_panel = _QueuePanel(self)
            self._queue_panel.setVisible(False)

        # Show fullscreen — reparenting deferred to next tick so the window
        # manager composites this on screen before transport leaves main window.
        screen = (parent.screen() if parent else None) or QApplication.primaryScreen()
        self.setScreen(screen)
        self.showFullScreen()
        QTimer.singleShot(0, self._activate)

    def _activate(self) -> None:
        """Reparent panel + transport + queue after overlay is visible."""
        if self._activated:
            return
        self._activated = True

        # Reparent NowPlayingPanel as the background filling this window.
        self._nowplaying_panel.setParent(self)
        self._nowplaying_panel.setGeometry(self.rect())
        self._nowplaying_panel.show()
        self._nowplaying_panel.lower()  # Behind floating containers.

        # Reparent transport into the floating container.
        self._transport_widget.setParent(self._container)
        self._container.add_widget(self._transport_widget)
        self._transport_widget.show()
        self._transport_widget._btn_fullscreen.setVisible(False)

        # Reparent queue into the side panel.
        if self._queue_panel is not None and self._queue_widget is not None:
            self._queue_widget.setParent(self._queue_panel)
            self._queue_panel.add_widget(self._queue_widget)
            self._queue_widget.show()
            self._queue_visible = self._transport_widget._btn_queue.isChecked()
            self._queue_panel.setVisible(self._queue_visible)
            self._transport_widget.queue_toggled.connect(self._toggle_queue)

        # Intercept the panel's fullscreen_requested to exit fullscreen.
        self._nowplaying_panel.fullscreen_requested.connect(self._exit_fullscreen)

        # Enable mouse tracking on the panel and all descendants so hover
        # events propagate up and we can detect mouse movement for auto-hide.
        self._nowplaying_panel.setMouseTracking(True)
        for child in self._nowplaying_panel.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

        # Start with controls visible, then auto-hide.
        self._show_controls()
        self._hide_timer.start()

    # ------------------------------------------------------------------
    # Queue toggle
    # ------------------------------------------------------------------

    def _toggle_queue(self) -> None:
        if self._queue_panel is None:
            return
        self._queue_visible = not self._queue_visible
        self._queue_panel.setVisible(self._queue_visible)
        self._position_overlays()

    # ------------------------------------------------------------------
    # Transport show/hide
    # ------------------------------------------------------------------

    def _show_controls(self) -> None:
        if self._container.isVisible():
            return  # Already visible — skip redundant layout work.
        self._container.setVisible(True)
        if self._queue_panel is not None and self._queue_visible:
            self._queue_panel.setVisible(True)
        self._position_overlays()
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _hide_controls(self) -> None:
        self._container.setVisible(False)
        if self._queue_panel is not None:
            self._queue_panel.setVisible(False)
        self.setCursor(Qt.CursorShape.BlankCursor)

    def _position_overlays(self, force: bool = False) -> None:
        w = self.width()
        h = self.height()

        side_margin = 40
        qp_width = 0
        if self._queue_panel is not None and self._queue_panel.isVisible():
            qp_width = self._queue_panel.width() + side_margin
        cw = min(w - 2 * side_margin - qp_width, 1200)
        self._container.setFixedWidth(max(cw, 600))
        self._container.adjustSize()
        ch = self._container.sizeHint().height()
        self._container.setFixedHeight(ch)

        if not self._container.user_dragged or force:
            cx = (w - cw - qp_width) // 2
            cy = h - ch - _CONTAINER_MARGIN_BOTTOM
            self._container.move(cx, cy)

        if self._queue_panel is not None and self._queue_panel.isVisible():
            qw = self._queue_panel.width()
            margin = 20
            self._queue_panel.setGeometry(
                w - qw - margin, margin,
                qw, h - 2 * margin,
            )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._container.user_dragged = False
        self._position_overlays(force=True)
        # Keep the panel filling the window.
        if self._activated:
            self._nowplaying_panel.setGeometry(self.rect())

    def _on_mouse_activity(self, global_pos: QPoint) -> None:
        """Show controls and restart auto-hide timer on mouse movement."""
        if self._last_mouse_pos == global_pos:
            return
        self._last_mouse_pos = global_pos

        self._show_controls()

        # Don't auto-hide while cursor is over the transport or queue.
        local_pos = self.mapFromGlobal(global_pos)
        in_container = self._container.geometry().contains(local_pos)
        in_queue = False
        if self._queue_panel is not None and self._queue_panel.isVisible():
            in_queue = self._queue_panel.geometry().contains(local_pos)
        if not in_container and not in_queue:
            self._hide_timer.start()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.MouseMove:
            self._on_mouse_activity(obj.mapToGlobal(event.position().toPoint()))
        return False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._on_mouse_activity(event.globalPosition().toPoint())

        super().mouseMoveEvent(event)

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._exit_fullscreen()
        elif event.key() == Qt.Key.Key_Space:
            self._transport_widget.play_toggled.emit()
        elif event.key() == Qt.Key.Key_Left:
            self._transport_widget.prev_clicked.emit()
        elif event.key() == Qt.Key.Key_Right:
            self._transport_widget.next_clicked.emit()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._exit_fullscreen()
        else:
            super().mouseDoubleClickEvent(event)

    def _exit_fullscreen(self) -> None:
        self._hide_timer.stop()
        # Remove event filters from panel children.
        for child in self._nowplaying_panel.findChildren(QWidget):
            child.removeEventFilter(self)
        # Disconnect panel's fullscreen_requested so it doesn't re-trigger.
        try:
            self._nowplaying_panel.fullscreen_requested.disconnect(self._exit_fullscreen)
        except (RuntimeError, TypeError):
            pass
        # Disconnect queue toggle.
        if self._queue_panel is not None:
            try:
                self._transport_widget.queue_toggled.disconnect(self._toggle_queue)
            except (RuntimeError, TypeError):
                pass
        self.closed.emit()
        self.hide()
