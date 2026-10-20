"""Subtitle overlay — renders subtitles over the video surface.

Dual-path rendering:
  ASS/SSA → libass renders at display resolution (correct positioning,
            fonts, colors, drawings — same engine as VLC/Kodi)
  SRT/VTT → QPainter renders clean text at the bottom of the panel

Both paths render at the OVERLAY's resolution (display size), not at the
source video resolution.  Text stays crisp regardless of video quality.

Mouse-transparent — clicks pass through to the VideoSurface beneath.
"""

from __future__ import annotations

import html
import logging
import re

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QWidget

from media_core.core.libass_renderer import LibassRenderer, SubtitleBitmap

log = logging.getLogger(__name__)

# Regex patterns for stripping text/x-raw subtitle markup.
_ASS_OVERRIDE = re.compile(r"\{\\[^}]*\}")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_PANGO_TAG = re.compile(r"</?(?:b|i|u|s|span|sub|sup|big|small|tt)[^>]*>")


def _strip_markup(text: str) -> str:
    """Iteratively strip ASS overrides, HTML/Pango tags, and decode entities."""
    prev = None
    while text != prev:
        prev = text
        text = _ASS_OVERRIDE.sub("", text)
        text = _HTML_TAG.sub("", text)
        text = _PANGO_TAG.sub("", text)
        text = html.unescape(text)
    return text.strip()


class SubtitleOverlay(QWidget):
    """Transparent overlay that renders subtitles over the video area."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine = None
        self._video_surface = None

        # --- libass renderer (ASS/SSA path) ---
        self._libass = LibassRenderer()
        self._libass_track_ready = False
        self._ass_bitmaps: list[SubtitleBitmap] = []

        # --- QPainter text (SRT/VTT path) ---
        self._current_text: str = ""

        # Configuration.
        self._font_size: int = 24
        self._outline: bool = True
        self._bg_bar: bool = False

        # Polling timer — 100ms (10 fps, plenty for subtitles).
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

        # Hide timer — clears text subtitles after their duration expires.
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hide_text)

        # Transparent, mouse-transparent.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, engine, video_surface) -> None:
        """Begin polling for subtitle buffers."""
        self._engine = engine
        self._video_surface = video_surface
        self._current_text = ""
        self._ass_bitmaps = []
        self._libass_track_ready = False
        self._timer.start()

    def stop(self) -> None:
        """Stop polling and clear display."""
        self._timer.stop()
        self._hide_timer.stop()
        self._engine = None
        self._video_surface = None
        self._current_text = ""
        self._ass_bitmaps = []
        self._libass_track_ready = False
        self.update()

    def clear(self) -> None:
        """Clear all displayed subtitles immediately."""
        self._hide_timer.stop()
        self._current_text = ""
        self._ass_bitmaps = []
        self.update()

    def set_font_size(self, pt: int) -> None:
        self._font_size = max(12, min(48, pt))
        self.update()

    def set_outline(self, enabled: bool) -> None:
        self._outline = enabled
        self.update()

    def set_bg_bar(self, enabled: bool) -> None:
        self._bg_bar = enabled
        self.update()

    def pause(self) -> None:
        self._timer.stop()

    def resume(self) -> None:
        if self._engine:
            self._timer.start()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        if not self._engine:
            return

        # Immediate off: if subtitles are disabled, clear and stop.
        if not self._engine.get_subtitle_flags():
            if self._current_text or self._ass_bitmaps:
                self._current_text = ""
                self._ass_bitmaps = []
                self._hide_timer.stop()
                self.update()
            return

        # --- ASS path: drain raw chunks → feed to libass → render ---
        ass_chunks = self._engine.pull_ass_chunks()
        if ass_chunks:
            self._feed_ass_chunks(ass_chunks)

        if self._libass_track_ready and self._libass.available:
            w, h = self.width(), self.height()
            if w > 0 and h > 0:
                self._libass.set_frame_size(w, h)
                pos_ms = self._engine.get_position_ms()
                bitmaps = self._libass.render_frame(pos_ms)
                if bitmaps != self._ass_bitmaps:
                    self._ass_bitmaps = bitmaps
                    self.update()
            # ASS path is active — skip text path.
            return

        # --- Text path: SRT/VTT/pango-markup ---
        result = self._engine.pull_subtitle_text()
        if result:
            text, duration_ms = result
            text = _strip_markup(text)
            if text:
                self._current_text = text
                self._hide_timer.stop()
                self._hide_timer.start(max(duration_ms, 500))
                self.update()

    def _feed_ass_chunks(self, chunks: list[tuple[bytes, int, int]]) -> None:
        """Initialize libass track if needed, then feed dialogue chunks."""
        if not self._libass.available:
            return

        if not self._libass_track_ready:
            header = self._engine.get_ass_header()
            self._libass.new_track(header)
            self._libass_track_ready = True
            log.info("libass track created (header: %s)",
                     f"{len(header)} bytes" if header else "none")

        for data, pts_ms, dur_ms in chunks:
            self._libass.process_chunk(data, pts_ms, dur_ms)

    def _on_hide_text(self) -> None:
        self._current_text = ""
        self.update()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        # ASS path: paint libass bitmaps.
        if self._ass_bitmaps:
            self._paint_ass_bitmaps()
            return

        # Text path: paint QPainter text.
        if self._current_text:
            self._paint_text()

    def _paint_ass_bitmaps(self) -> None:
        """Composite libass-rendered RGBA bitmaps onto the overlay."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        for bm in self._ass_bitmaps:
            if bm.w <= 0 or bm.h <= 0:
                continue
            img = QImage(bm.rgba, bm.w, bm.h, bm.w * 4,
                         QImage.Format.Format_RGBA8888)
            p.drawImage(bm.x, bm.y, img)

        p.end()

    def _paint_text(self) -> None:
        """Render plain-text subtitles at the bottom of the panel."""
        panel = QRectF(self.rect())
        if panel.isEmpty():
            return

        # Scale font relative to panel height for proportional appearance.
        scale = panel.height() / 500.0
        font_pt = max(10, self._font_size * scale)
        font = QFont()
        font.setPointSizeF(font_pt)
        font.setBold(True)

        fm = QFontMetricsF(font)

        # Text area: bottom of panel, with horizontal margins.
        margin_x = panel.width() * 0.05
        margin_bottom = panel.height() * 0.03
        text_h = panel.height() * 0.18
        text_area = QRectF(
            panel.left() + margin_x,
            panel.bottom() - text_h - margin_bottom,
            panel.width() - 2 * margin_x,
            text_h,
        )

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Word-wrap the text.
        lines = []
        for raw_line in self._current_text.split("\n"):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            words = raw_line.split()
            current = ""
            for word in words:
                test = f"{current} {word}".strip()
                if fm.horizontalAdvance(test) > text_area.width():
                    if current:
                        lines.append(current)
                    current = word
                else:
                    current = test
            if current:
                lines.append(current)

        if not lines:
            p.end()
            return

        line_height = fm.height() * 1.15
        total_height = line_height * len(lines)
        y_start = text_area.bottom() - total_height

        # Optional background bar.
        if self._bg_bar:
            bg_rect = QRectF(
                text_area.left() - margin_x * 0.5,
                y_start - fm.descent(),
                text_area.width() + margin_x,
                total_height + fm.descent() * 2,
            )
            p.fillRect(bg_rect, QColor(0, 0, 0, 140))

        # Draw each line, centered horizontally.
        for i, line in enumerate(lines):
            line_y = y_start + i * line_height + fm.ascent()
            line_w = fm.horizontalAdvance(line)
            line_x = text_area.left() + (text_area.width() - line_w) / 2

            if self._outline:
                path = QPainterPath()
                path.addText(line_x, line_y, font, line)
                outline_w = max(2.0, font_pt / 12.0)
                p.setPen(QPen(QColor(0, 0, 0, 220), outline_w))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(255, 255, 255, 240)))
                p.drawPath(path)
            else:
                p.setFont(font)
                p.setPen(QColor(0, 0, 0, 180))
                p.drawText(int(line_x + 2), int(line_y + 2), line)
                p.setPen(QColor(255, 255, 255, 240))
                p.drawText(int(line_x), int(line_y), line)

        p.end()
