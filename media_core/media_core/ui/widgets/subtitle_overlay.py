"""Subtitle overlay — renders subtitles over the video surface.

Dual-path rendering:
  ASS/SSA → libass renders at display resolution (correct positioning,
            fonts, colors, drawings — same engine as VLC/Kodi)
  SRT/VTT → QPainter renders clean text at the bottom of the panel

Both paths render at the OVERLAY's resolution (display size), not at the
source video resolution.  Text stays crisp regardless of video quality.

External subtitle files, which the engine reads itself, take the same two
paths, drawn from the playback position less the subtitle delay.  The
overlay also shows VLC-style on-screen messages, top right.

Mouse-transparent — clicks pass through to the VideoSurface beneath.
"""

from __future__ import annotations

import html
import logging
import re

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer
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
        self._ass_origin = QPoint()  # the picture's corner, where bitmaps start
        self._sub_serial = 0  # the engine's, as of the last poll

        # --- QPainter text (SRT/VTT path) ---
        self._current_text: str = ""

        # --- External subtitle file (drawn from the playback position) ---
        self._external = None  # the engine's SubtitleFile being drawn

        # --- On-screen messages ("Subtitle track: …"), VLC-style ---
        self._osd_text: str = ""
        self._osd_timer = QTimer(self)
        self._osd_timer.setSingleShot(True)
        self._osd_timer.timeout.connect(self._on_hide_osd)

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

        # Cover the parent at every size it takes.  libass renders into this
        # widget's size, and a video page in a stacked layout only gets its
        # real size once it is the current page.
        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(parent.rect())

    def eventFilter(self, obj, event) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        return super().eventFilter(obj, event)

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
        self._external = None
        self._sub_serial = engine.get_subtitle_serial()
        self._timer.start()

    def stop(self) -> None:
        """Stop polling and clear display."""
        self._timer.stop()
        self._hide_timer.stop()
        self._osd_timer.stop()
        self._engine = None
        self._video_surface = None
        self._current_text = ""
        self._osd_text = ""
        self._ass_bitmaps = []
        self._libass_track_ready = False
        self._external = None
        self.update()

    def show_message(self, text: str, duration_ms: int = 2000) -> None:
        """Show *text* at the top right for a moment, as VLC shows a track
        change or a new subtitle delay."""
        self._osd_text = text
        self._osd_timer.start(duration_ms)
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

        # A track was chosen: drop the old one.  Its libass events would
        # otherwise keep rendering and skip the text path for a new SRT.
        serial = self._engine.get_subtitle_serial()
        if serial != self._sub_serial:
            self._sub_serial = serial
            self._libass_track_ready = False
            self._ass_bitmaps = []
            self._current_text = ""
            self._external = None
            self._hide_timer.stop()
            self.update()

        # Immediate off: if subtitles are disabled, clear and stop.
        if not self._engine.get_subtitle_flags():
            if self._current_text or self._ass_bitmaps:
                self._current_text = ""
                self._ass_bitmaps = []
                self._hide_timer.stop()
                self.update()
            return

        delay_ms = self._engine.get_subtitle_delay()
        external = self._engine.get_active_external()
        if external is not None:
            self._poll_external(external, delay_ms)
            return
        if self._external is not None:
            # Back from a file: libass still holds the file's track.
            self._external = None
            self._libass_track_ready = False
            self._ass_bitmaps = []
            self._current_text = ""
            self.update()

        # --- ASS path: drain raw chunks → feed to libass → render ---
        ass_chunks = self._engine.pull_ass_chunks()
        if ass_chunks:
            self._feed_ass_chunks(ass_chunks)

        if self._libass_track_ready and self._libass.available:
            self._render_libass(self._engine.get_position_ms() - delay_ms)
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

    def _poll_external(self, sub, delay_ms: int) -> None:
        """Draw an external file at the playback position, less the delay:
        an ASS/SSA script through libass, anything else as text."""
        if sub is not self._external:
            self._external = sub
            self._hide_timer.stop()
            self._current_text = ""
            self._ass_bitmaps = []
            self._libass_track_ready = sub.is_ass and self._libass.load_script(sub.script)
            self.update()
        pos_ms = self._engine.get_position_ms() - delay_ms
        if sub.is_ass:
            if self._libass_track_ready:
                self._render_libass(pos_ms)
            return
        text = _strip_markup(sub.text_at(pos_ms))
        if text != self._current_text:
            self._current_text = text
            self.update()

    def _render_libass(self, pos_ms: int) -> None:
        # Lay ASS out on the picture, not the page around it, so a
        # letterboxed video keeps its positions and font sizes.
        area = self._picture_rect()
        if area.width() > 0 and area.height() > 0:
            self._libass.set_frame_size(area.width(), area.height())
            bitmaps = self._libass.render_frame(pos_ms)
            if bitmaps != self._ass_bitmaps or area.topLeft() != self._ass_origin:
                self._ass_bitmaps = bitmaps
                self._ass_origin = area.topLeft()
                self.update()

    def _picture_rect(self) -> QRect:
        """Where the video picture is drawn, in this widget's coordinates."""
        surface = self._video_surface
        if surface is None:
            return self.rect()
        # The surface is a sibling in the same video page.
        return surface.video_rect().translated(surface.pos() - self.pos())

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

    def _on_hide_osd(self) -> None:
        self._osd_text = ""
        self.update()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        # ASS path: paint libass bitmaps.  Text path: paint QPainter text.
        if self._ass_bitmaps:
            self._paint_ass_bitmaps()
        elif self._current_text:
            self._paint_text()
        if self._osd_text:
            self._paint_osd()

    def _paint_osd(self) -> None:
        """The on-screen message, white with a dark outline, top right."""
        area = QRectF(self.rect())
        if area.isEmpty():
            return
        font = QFont()
        font.setPixelSize(max(14, int(area.height() / 24)))
        font.setBold(True)
        fm = QFontMetricsF(font)
        margin = area.height() * 0.04
        text = fm.elidedText(self._osd_text, Qt.TextElideMode.ElideMiddle,
                             area.width() - 2 * margin)
        x = area.right() - margin - fm.horizontalAdvance(text)
        y = area.top() + margin + fm.ascent()

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addText(x, y, font, text)
        p.setPen(QPen(QColor(0, 0, 0, 220), max(2.0, font.pixelSize() / 10.0)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 240)))
        p.drawPath(path)
        p.end()

    def _paint_ass_bitmaps(self) -> None:
        """Composite libass-rendered RGBA bitmaps onto the overlay."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        for bm in self._ass_bitmaps:
            if bm.w <= 0 or bm.h <= 0:
                continue
            img = QImage(bm.rgba, bm.w, bm.h, bm.w * 4,
                         QImage.Format.Format_RGBA8888)
            p.drawImage(self._ass_origin.x() + bm.x, self._ass_origin.y() + bm.y, img)

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
