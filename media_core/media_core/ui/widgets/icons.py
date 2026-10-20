"""Vector transport icons — clean line-art rendered via QPainterPath.

Each function returns a QIcon that scales to any size and respects the
supplied color (defaulting to the current palette foreground).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication


def _palette_fg() -> QColor:
    """Return the current theme foreground color."""
    return QApplication.palette().color(QApplication.palette().currentColorGroup(),
                                        QApplication.palette().ColorRole.WindowText)


def _render(painter_fn, size: int = 64, color: QColor | None = None) -> QIcon:
    """Render a painter function into a QIcon at the given size."""
    color = color or _palette_fg()
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter_fn(p, QRectF(0, 0, size, size), color)
    p.end()
    return QIcon(pix)


# -- Transport controls -----------------------------------------------

def icon_play(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        # Triangle pointing right, with slight left padding.
        x0 = r.x() + r.width() * 0.28
        x1 = r.x() + r.width() * 0.78
        ym = r.y() + r.height() * 0.5
        yt = r.y() + r.height() * 0.18
        yb = r.y() + r.height() * 0.82
        path.moveTo(x0, yt)
        path.lineTo(x1, ym)
        path.lineTo(x0, yb)
        path.closeSubpath()
        p.drawPath(path)
    return _render(draw, size, color)


def icon_pause(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        w = r.width()
        h = r.height()
        bar_w = w * 0.18
        gap = w * 0.12
        x1 = r.x() + w * 0.5 - gap / 2 - bar_w
        x2 = r.x() + w * 0.5 + gap / 2
        yt = r.y() + h * 0.2
        yb = r.y() + h * 0.8
        p.drawRoundedRect(QRectF(x1, yt, bar_w, yb - yt), 2, 2)
        p.drawRoundedRect(QRectF(x2, yt, bar_w, yb - yt), 2, 2)
    return _render(draw, size, color)


def icon_stop(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        inset = r.width() * 0.25
        p.drawRoundedRect(r.adjusted(inset, inset, -inset, -inset), 2, 2)
    return _render(draw, size, color)


def icon_prev(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        w, h = r.width(), r.height()
        # Vertical bar on the left.
        bar_w = w * 0.1
        bx = r.x() + w * 0.18
        yt = r.y() + h * 0.22
        yb = r.y() + h * 0.78
        p.drawRect(QRectF(bx, yt, bar_w, yb - yt))
        # Triangle pointing left.
        path = QPainterPath()
        tx0 = bx + bar_w + w * 0.02
        tx1 = r.x() + w * 0.78
        ym = r.y() + h * 0.5
        path.moveTo(tx1, yt)
        path.lineTo(tx0, ym)
        path.lineTo(tx1, yb)
        path.closeSubpath()
        p.drawPath(path)
    return _render(draw, size, color)


def icon_next(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        w, h = r.width(), r.height()
        # Triangle pointing right.
        yt = r.y() + h * 0.22
        yb = r.y() + h * 0.78
        ym = r.y() + h * 0.5
        tx0 = r.x() + w * 0.22
        tx1 = r.x() + w * 0.72
        path = QPainterPath()
        path.moveTo(tx0, yt)
        path.lineTo(tx1, ym)
        path.lineTo(tx0, yb)
        path.closeSubpath()
        p.drawPath(path)
        # Vertical bar on the right.
        bar_w = w * 0.1
        bx = tx1 + w * 0.02
        p.drawRect(QRectF(bx, yt, bar_w, yb - yt))
    return _render(draw, size, color)


# -- Mode toggles ------------------------------------------------------

def icon_shuffle(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        pen = QPen(c, r.width() * 0.06, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        w, h = r.width(), r.height()
        # Two crossing paths.
        x0 = r.x() + w * 0.18
        x1 = r.x() + w * 0.72
        y_top = r.y() + h * 0.32
        y_bot = r.y() + h * 0.68
        xm = r.x() + w * 0.45
        # Path 1: top-left to bottom-right.
        path1 = QPainterPath()
        path1.moveTo(x0, y_top)
        path1.cubicTo(xm, y_top, xm, y_bot, x1, y_bot)
        p.drawPath(path1)
        # Path 2: bottom-left to top-right.
        path2 = QPainterPath()
        path2.moveTo(x0, y_bot)
        path2.cubicTo(xm, y_bot, xm, y_top, x1, y_top)
        p.drawPath(path2)
        # Arrowheads on the right.
        arr = w * 0.1
        p.setBrush(c)
        p.setPen(Qt.PenStyle.NoPen)
        for y in (y_top, y_bot):
            arrow = QPainterPath()
            arrow.moveTo(x1 + arr, y)
            arrow.lineTo(x1 - arr * 0.3, y - arr * 0.6)
            arrow.lineTo(x1 - arr * 0.3, y + arr * 0.6)
            arrow.closeSubpath()
            p.drawPath(arrow)
    return _render(draw, size, color)


def icon_repeat(size: int = 64, color: QColor | None = None) -> QIcon:
    """Repeat (all) — two arrows forming a loop."""
    def draw(p: QPainter, r: QRectF, c: QColor):
        pen = QPen(c, r.width() * 0.06, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        w, h = r.width(), r.height()
        # Rounded rectangle loop.
        x0 = r.x() + w * 0.2
        x1 = r.x() + w * 0.8
        yt = r.y() + h * 0.3
        yb = r.y() + h * 0.7
        rad = w * 0.12
        # Top line (left to right).
        p.drawLine(QPointF(x0 + rad, yt), QPointF(x1 - rad, yt))
        # Right arc (top to bottom).
        p.drawArc(QRectF(x1 - 2 * rad, yt, 2 * rad, 2 * rad), 0, 90 * 16)
        # Right vertical.
        p.drawLine(QPointF(x1, yt + rad), QPointF(x1, yb - rad))
        # Bottom-right arc.
        p.drawArc(QRectF(x1 - 2 * rad, yb - 2 * rad, 2 * rad, 2 * rad), 270 * 16, 90 * 16)
        # Bottom line (right to left).
        p.drawLine(QPointF(x1 - rad, yb), QPointF(x0 + rad, yb))
        # Left arc (bottom to top).
        p.drawArc(QRectF(x0, yb - 2 * rad, 2 * rad, 2 * rad), 180 * 16, 90 * 16)
        # Left vertical.
        p.drawLine(QPointF(x0, yb - rad), QPointF(x0, yt + rad))
        # Top-left arc.
        p.drawArc(QRectF(x0, yt, 2 * rad, 2 * rad), 90 * 16, 90 * 16)
        # Arrow on top-right pointing right.
        arr = w * 0.09
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        arrow = QPainterPath()
        ax = x1 - rad * 0.5
        arrow.moveTo(ax + arr, yt)
        arrow.lineTo(ax - arr * 0.4, yt - arr * 0.7)
        arrow.lineTo(ax - arr * 0.4, yt + arr * 0.7)
        arrow.closeSubpath()
        p.drawPath(arrow)
    return _render(draw, size, color)


def icon_repeat_one(size: int = 64, color: QColor | None = None) -> QIcon:
    """Repeat one — loop with a '1' in the center."""
    def draw(p: QPainter, r: QRectF, c: QColor):
        # Draw the repeat loop first.
        pen = QPen(c, r.width() * 0.06, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        w, h = r.width(), r.height()
        x0 = r.x() + w * 0.2
        x1 = r.x() + w * 0.8
        yt = r.y() + h * 0.3
        yb = r.y() + h * 0.7
        rad = w * 0.12
        p.drawLine(QPointF(x0 + rad, yt), QPointF(x1 - rad, yt))
        p.drawArc(QRectF(x1 - 2 * rad, yt, 2 * rad, 2 * rad), 0, 90 * 16)
        p.drawLine(QPointF(x1, yt + rad), QPointF(x1, yb - rad))
        p.drawArc(QRectF(x1 - 2 * rad, yb - 2 * rad, 2 * rad, 2 * rad), 270 * 16, 90 * 16)
        p.drawLine(QPointF(x1 - rad, yb), QPointF(x0 + rad, yb))
        p.drawArc(QRectF(x0, yb - 2 * rad, 2 * rad, 2 * rad), 180 * 16, 90 * 16)
        p.drawLine(QPointF(x0, yb - rad), QPointF(x0, yt + rad))
        p.drawArc(QRectF(x0, yt, 2 * rad, 2 * rad), 90 * 16, 90 * 16)
        # Arrow.
        arr = w * 0.09
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        arrow = QPainterPath()
        ax = x1 - rad * 0.5
        arrow.moveTo(ax + arr, yt)
        arrow.lineTo(ax - arr * 0.4, yt - arr * 0.7)
        arrow.lineTo(ax - arr * 0.4, yt + arr * 0.7)
        arrow.closeSubpath()
        p.drawPath(arrow)
        # "1" in the center.
        from PySide6.QtGui import QFont
        font = QFont()
        font.setPixelSize(int(h * 0.28))
        font.setBold(True)
        p.setFont(font)
        p.setPen(c)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, "1")
    return _render(draw, size, color)


def icon_repeat_off(size: int = 64, color: QColor | None = None) -> QIcon:
    """Repeat off — dimmed loop (rendered with reduced opacity)."""
    dimmed = QColor(color or _palette_fg())
    dimmed.setAlphaF(0.35)
    return icon_repeat(size, dimmed)


# -- Volume ------------------------------------------------------------

def icon_volume(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        pen = QPen(c, r.width() * 0.05, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        w, h = r.width(), r.height()
        # Speaker cone.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        cone = QPainterPath()
        # Body rectangle.
        bx = r.x() + w * 0.18
        by_t = r.y() + h * 0.38
        by_b = r.y() + h * 0.62
        bw = w * 0.14
        cone.addRect(QRectF(bx, by_t, bw, by_b - by_t))
        # Flare.
        cone.moveTo(bx + bw, by_t)
        cone.lineTo(r.x() + w * 0.48, r.y() + h * 0.22)
        cone.lineTo(r.x() + w * 0.48, r.y() + h * 0.78)
        cone.lineTo(bx + bw, by_b)
        cone.closeSubpath()
        p.drawPath(cone)
        # Sound waves (arcs).
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx = r.x() + w * 0.52
        cy = r.y() + h * 0.5
        for i, radius in enumerate([w * 0.12, w * 0.2]):
            arc_rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
            p.drawArc(arc_rect, -40 * 16, 80 * 16)
    return _render(draw, size, color)


def icon_volume_mute(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        w, h = r.width(), r.height()
        # Speaker cone (same as above but no waves).
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        cone = QPainterPath()
        bx = r.x() + w * 0.18
        by_t = r.y() + h * 0.38
        by_b = r.y() + h * 0.62
        bw = w * 0.14
        cone.addRect(QRectF(bx, by_t, bw, by_b - by_t))
        cone.moveTo(bx + bw, by_t)
        cone.lineTo(r.x() + w * 0.48, r.y() + h * 0.22)
        cone.lineTo(r.x() + w * 0.48, r.y() + h * 0.78)
        cone.lineTo(bx + bw, by_b)
        cone.closeSubpath()
        p.drawPath(cone)
        # X mark.
        pen = QPen(c, w * 0.06, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        cx = r.x() + w * 0.68
        cy = r.y() + h * 0.5
        d = w * 0.12
        p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        p.drawLine(QPointF(cx - d, cy + d), QPointF(cx + d, cy - d))
    return _render(draw, size, color)


# -- Menu --------------------------------------------------------------

def icon_hamburger(size: int = 64, color: QColor | None = None) -> QIcon:
    def draw(p: QPainter, r: QRectF, c: QColor):
        pen = QPen(c, r.width() * 0.07, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        w, h = r.width(), r.height()
        x0 = r.x() + w * 0.22
        x1 = r.x() + w * 0.78
        for frac in (0.3, 0.5, 0.7):
            y = r.y() + h * frac
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
    return _render(draw, size, color)


def icon_queue_panel(size: int = 64, color: QColor | None = None) -> QIcon:
    """Sidebar/panel icon — vertical divider with three horizontal lines."""
    def draw(p: QPainter, r: QRectF, c: QColor):
        pen = QPen(c, r.width() * 0.06, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        w, h = r.width(), r.height()
        # Vertical divider on the left.
        vx = r.x() + w * 0.35
        p.drawLine(QPointF(vx, r.y() + h * 0.2), QPointF(vx, r.y() + h * 0.8))
        # Three horizontal lines (representing list items) on the right.
        x0 = r.x() + w * 0.45
        x1 = r.x() + w * 0.82
        for frac in (0.32, 0.5, 0.68):
            y = r.y() + h * frac
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
    return _render(draw, size, color)


def icon_fullscreen(size: int = 64, color: QColor | None = None) -> QIcon:
    """Fullscreen icon — four outward-pointing corner arrows."""
    def draw(p: QPainter, r: QRectF, c: QColor):
        pen = QPen(c, r.width() * 0.07, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        w, h = r.width(), r.height()
        # Corner bracket size as fraction of widget.
        d = 0.22
        # Top-left corner.
        x0, y0 = r.x() + w * 0.2, r.y() + h * 0.2
        p.drawLine(QPointF(x0, y0 + h * d), QPointF(x0, y0))
        p.drawLine(QPointF(x0, y0), QPointF(x0 + w * d, y0))
        # Top-right corner.
        x1, y1 = r.x() + w * 0.8, r.y() + h * 0.2
        p.drawLine(QPointF(x1 - w * d, y1), QPointF(x1, y1))
        p.drawLine(QPointF(x1, y1), QPointF(x1, y1 + h * d))
        # Bottom-left corner.
        x2, y2 = r.x() + w * 0.2, r.y() + h * 0.8
        p.drawLine(QPointF(x2, y2 - h * d), QPointF(x2, y2))
        p.drawLine(QPointF(x2, y2), QPointF(x2 + w * d, y2))
        # Bottom-right corner.
        x3, y3 = r.x() + w * 0.8, r.y() + h * 0.8
        p.drawLine(QPointF(x3 - w * d, y3), QPointF(x3, y3))
        p.drawLine(QPointF(x3, y3), QPointF(x3, y3 - h * d))
    return _render(draw, size, color)


# -- App icon ----------------------------------------------------------

def icon_app(color: QColor | None = None) -> QIcon:
    """Quaver app icon — Q with equalizer bars as the tail.

    Rendered at multiple sizes so taskbar, title bar, and alt-tab all
    look crisp. Uses the palette foreground color for theme matching.
    """
    def draw(p: QPainter, r: QRectF, c: QColor):
        w, h = r.width(), r.height()
        cx, cy = r.x() + w * 0.42, r.y() + h * 0.44
        radius = w * 0.32

        # -- Q circle (open at bottom-right for the tail) --
        pen = QPen(c, w * 0.07, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        from PySide6.QtCore import QRectF as RF
        arc_rect = RF(cx - radius, cy - radius, radius * 2, radius * 2)
        # Draw arc from ~315° around to ~45° (leaving a gap at bottom-right).
        # Qt arcs: 0° = 3 o'clock, counter-clockwise, in 1/16th degree units.
        p.drawArc(arc_rect, 20 * 16, 300 * 16)

        # -- Equalizer bars as the Q's tail --
        # 4 bars descending from the gap, positioned at bottom-right.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)

        bar_w = w * 0.065
        bar_gap = w * 0.03
        bar_x_start = cx + radius * 0.35
        bar_bottom = cy + radius * 0.95

        # Bar heights as fractions — ascending pattern like a spectrum.
        bar_heights = [0.22, 0.42, 0.55, 0.35]

        for i, bh in enumerate(bar_heights):
            bx = bar_x_start + i * (bar_w + bar_gap)
            bar_h = h * bh
            by = bar_bottom - bar_h
            p.drawRoundedRect(RF(bx, by, bar_w, bar_h), bar_w * 0.3, bar_w * 0.3)

    color = color or _palette_fg()
    icon = QIcon()
    # Render at multiple sizes for different display contexts.
    for sz in (16, 24, 32, 48, 64, 128, 256):
        pix = QPixmap(sz, sz)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw(p, QRectF(0, 0, sz, sz), color)
        p.end()
        icon.addPixmap(pix)
    return icon


# -- Utility: icon button helper ---------------------------------------

def set_icon_button(button, icon: QIcon, size: int = 18) -> None:
    """Apply a QIcon to a QPushButton, replacing any text."""
    from PySide6.QtCore import QSize
    button.setText("")
    button.setIcon(icon)
    button.setIconSize(QSize(size, size))
