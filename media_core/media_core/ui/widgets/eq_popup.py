"""EQ popup with 10-band sliders and presets."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# 10-band center frequencies (Hz) for GStreamer equalizer-10bands.
_BANDS = [29, 59, 119, 237, 474, 947, 1889, 3770, 7523, 15011]

_PRESETS: dict[str, list[float]] = {
    "Flat": [0] * 10,
    "Bass Boost": [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "Treble Boost": [0, 0, 0, 0, 0, 0, 2, 4, 5, 6],
    "Bass + Treble": [5, 4, 2, 0, -2, -2, 0, 2, 4, 5],
    "Vocal": [-2, -1, 0, 2, 4, 4, 2, 0, -1, -2],
    "Rock": [4, 3, 1, 0, -1, 0, 2, 3, 4, 4],
}


class EqPopup(QDialog):
    """10-band equalizer popup dialog."""

    bands_changed = Signal(list)  # list of 10 float gains in dB

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Equalizer")
        self.setMinimumSize(400, 250)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.Tool
        )

        self._sliders: list[QSlider] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Presets.
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        self._preset_combo = QComboBox()
        for name in _PRESETS:
            self._preset_combo.addItem(name)
        self._preset_combo.currentTextChanged.connect(self._apply_preset)
        preset_row.addWidget(self._preset_combo, stretch=1)
        layout.addLayout(preset_row)

        # Sliders.
        sliders_row = QHBoxLayout()
        sliders_row.setSpacing(4)

        for i, freq in enumerate(_BANDS):
            band = QVBoxLayout()
            band.setSpacing(2)

            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(-12, 12)  # dB range
            slider.setValue(0)
            slider.setFixedWidth(30)
            slider.setMinimumHeight(120)
            slider.valueChanged.connect(self._on_slider_changed)
            self._sliders.append(slider)

            # Frequency label.
            if freq >= 1000:
                label = f"{freq // 1000}k"
            else:
                label = str(freq)

            freq_label = QLabel(label)
            freq_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            freq_label.setStyleSheet("font-size: 9px;")

            band.addWidget(slider, alignment=Qt.AlignmentFlag.AlignCenter)
            band.addWidget(freq_label)
            sliders_row.addLayout(band)

        layout.addLayout(sliders_row)

    def _on_slider_changed(self) -> None:
        gains = [s.value() for s in self._sliders]
        self.bands_changed.emit(gains)

    def _apply_preset(self, name: str) -> None:
        gains = _PRESETS.get(name, [0] * 10)
        for slider, gain in zip(self._sliders, gains):
            slider.blockSignals(True)
            slider.setValue(int(gain))
            slider.blockSignals(False)
        self.bands_changed.emit(gains)

    def set_gains(self, gains: list[float]) -> None:
        """Set slider positions from external state."""
        for slider, gain in zip(self._sliders, gains):
            slider.blockSignals(True)
            slider.setValue(int(gain))
            slider.blockSignals(False)
