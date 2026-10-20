"""Spectrum bar visualizer — compact QPainter widget.

Renders frequency magnitude data as vertical bars with smooth falloff.
Used in the transport bar (small) and Now Playing panel (large).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication, QWidget


class SpectrumWidget(QWidget):
    """Frequency spectrum bar display.

    Call update_magnitudes() with a list of 0.0–1.0 values to animate.
    """

    def __init__(
        self,
        bar_count: int = 32,
        bar_gap: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._bar_count = bar_count
        self._bar_gap = bar_gap
        self._magnitudes: list[float] = [0.0] * bar_count
        self._display: list[float] = [0.0] * bar_count  # smoothed
        self._peaks: list[float] = [0.0] * bar_count
        self._peak_hold: list[int] = [0] * bar_count  # frames to hold

        # Smooth falloff timer — runs at ~60 fps when active.
        self._decay_timer = QTimer(self)
        self._decay_timer.setInterval(16)
        self._decay_timer.timeout.connect(self._decay_tick)
        self._active = False

    def update_magnitudes(self, magnitudes: list[float]) -> None:
        """Feed new spectrum data (called from signal, typically 20 Hz)."""
        n = self._bar_count
        src = magnitudes or []
        num_src = len(src)

        if num_src >= n:
            # Logarithmic grouping: more raw bands per bar at high frequencies,
            # fewer at low frequencies. This spreads the visual energy evenly
            # across the display instead of piling everything on the left.
            import math
            result = []
            for i in range(n):
                # Map display bar index to source range using log scale.
                lo = int(num_src * (math.exp(i / n * math.log(num_src + 1)) - 1) / num_src)
                hi = int(num_src * (math.exp((i + 1) / n * math.log(num_src + 1)) - 1) / num_src)
                lo = max(0, min(lo, num_src - 1))
                hi = max(lo + 1, min(hi, num_src))
                chunk = src[lo:hi]
                result.append(max(chunk) if chunk else 0.0)
            self._magnitudes = result
        else:
            self._magnitudes = (src + [0.0] * n)[:n]

        # Update peaks.
        for i in range(n):
            val = self._magnitudes[i]
            if val >= self._peaks[i]:
                self._peaks[i] = val
                self._peak_hold[i] = 20  # hold ~320 ms at 60 fps
            if val > self._display[i]:
                self._display[i] = val

        if not self._active:
            self._active = True
            self._decay_timer.start()

    def clear(self) -> None:
        """Reset to silence."""
        n = self._bar_count
        self._magnitudes = [0.0] * n
        self._display = [0.0] * n
        self._peaks = [0.0] * n
        self._peak_hold = [0] * n
        self.update()

    def _decay_tick(self) -> None:
        """Smooth falloff for bars and peaks."""
        any_active = False
        for i in range(self._bar_count):
            target = self._magnitudes[i]
            # Bars: fast attack, smooth decay.
            if self._display[i] > target:
                self._display[i] = max(target, self._display[i] - 0.04)
            else:
                self._display[i] = target
            # Peaks: hold then fall.
            if self._peak_hold[i] > 0:
                self._peak_hold[i] -= 1
            else:
                self._peaks[i] = max(0.0, self._peaks[i] - 0.02)
            if self._display[i] > 0.001 or self._peaks[i] > 0.001:
                any_active = True

        if not any_active:
            self._active = False
            self._decay_timer.stop()

        self.update()

    def paintEvent(self, event) -> None:
        if self.width() < 2 or self.height() < 2:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        n = self._bar_count
        bar_w = max(1, (w - (n - 1) * self._bar_gap) / n)

        # Bar gradient: accent color at top, dimmed at bottom.
        palette = QApplication.palette()
        accent = palette.color(palette.ColorRole.Highlight)
        base = QColor(accent)
        base.setAlphaF(0.3)

        for i in range(n):
            x = i * (bar_w + self._bar_gap)
            bar_h = self._display[i] * h
            if bar_h < 1:
                continue

            y = h - bar_h
            grad = QLinearGradient(x, y, x, h)
            grad.setColorAt(0.0, accent)
            grad.setColorAt(1.0, base)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(x, y, bar_w, bar_h), 1, 1)

            # Peak indicator line.
            peak_y = h - self._peaks[i] * h
            if self._peaks[i] > 0.01:
                peak_color = QColor(accent)
                peak_color.setAlphaF(0.7)
                p.setPen(peak_color)
                p.drawLine(int(x), int(peak_y), int(x + bar_w), int(peak_y))

        p.end()
