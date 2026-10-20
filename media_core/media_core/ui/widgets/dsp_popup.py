"""DSP effects popup — compressor and playback speed controls."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)


_COMPRESSOR_PRESETS: dict[str, tuple[float, float]] = {
    "Off": (1.0, 1.0),
    "Light": (2.0, 0.6),
    "Medium": (4.0, 0.5),
    "Heavy": (8.0, 0.35),
    "Broadcast": (12.0, 0.25),
}

_SPEED_PRESETS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


class DspPopup(QDialog):
    """DSP effects dialog: compressor + playback speed."""

    compressor_changed = Signal(bool, float, float)  # enabled, ratio, threshold
    speed_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DSP Effects")
        self.setMinimumWidth(320)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # -- Compressor section -------------------------------------------
        comp_group = QGroupBox("Dynamic Range Compressor")
        comp_layout = QVBoxLayout(comp_group)

        self._comp_enable = QCheckBox("Enable Compressor")
        self._comp_enable.toggled.connect(self._on_compressor_changed)
        comp_layout.addWidget(self._comp_enable)

        # Preset selector.
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        self._comp_preset = QComboBox()
        for name in _COMPRESSOR_PRESETS:
            self._comp_preset.addItem(name)
        self._comp_preset.setCurrentText("Off")
        self._comp_preset.currentTextChanged.connect(self._on_comp_preset)
        preset_row.addWidget(self._comp_preset, stretch=1)
        comp_layout.addLayout(preset_row)

        # Ratio slider.
        ratio_row = QHBoxLayout()
        ratio_row.addWidget(QLabel("Ratio:"))
        self._ratio_slider = QSlider(Qt.Orientation.Horizontal)
        self._ratio_slider.setRange(10, 200)  # 1.0x – 20.0x
        self._ratio_slider.setValue(40)
        self._ratio_slider.valueChanged.connect(self._on_compressor_changed)
        self._ratio_label = QLabel("4.0:1")
        ratio_row.addWidget(self._ratio_slider, stretch=1)
        ratio_row.addWidget(self._ratio_label)
        comp_layout.addLayout(ratio_row)

        # Threshold slider.
        thresh_row = QHBoxLayout()
        thresh_row.addWidget(QLabel("Threshold:"))
        self._thresh_slider = QSlider(Qt.Orientation.Horizontal)
        self._thresh_slider.setRange(5, 100)  # 0.05 – 1.0
        self._thresh_slider.setValue(50)
        self._thresh_slider.valueChanged.connect(self._on_compressor_changed)
        self._thresh_label = QLabel("50%")
        thresh_row.addWidget(self._thresh_slider, stretch=1)
        thresh_row.addWidget(self._thresh_label)
        comp_layout.addLayout(thresh_row)

        layout.addWidget(comp_group)

        # -- Speed section ------------------------------------------------
        speed_group = QGroupBox("Playback Speed")
        speed_layout = QVBoxLayout(speed_group)

        speed_row = QHBoxLayout()
        self._speed_slider = QSlider(Qt.Orientation.Horizontal)
        self._speed_slider.setRange(50, 200)  # 0.5x – 2.0x
        self._speed_slider.setValue(100)
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        self._speed_label = QLabel("1.0x")
        speed_row.addWidget(self._speed_slider, stretch=1)
        speed_row.addWidget(self._speed_label)
        speed_layout.addLayout(speed_row)

        # Speed preset buttons.
        preset_speed_row = QHBoxLayout()
        for rate in _SPEED_PRESETS:
            from PySide6.QtWidgets import QPushButton
            btn = QPushButton(f"{rate}x")
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _, r=rate: self._set_speed(r))
            preset_speed_row.addWidget(btn)
        speed_layout.addLayout(preset_speed_row)

        layout.addWidget(speed_group)

    def set_compressor_state(
        self, enabled: bool, ratio: float, threshold: float
    ) -> None:
        self._comp_enable.setChecked(enabled)
        self._ratio_slider.setValue(int(ratio * 10))
        self._thresh_slider.setValue(int(threshold * 100))

    def set_speed(self, rate: float) -> None:
        self._speed_slider.setValue(int(rate * 100))

    def _on_comp_preset(self, name: str) -> None:
        ratio, threshold = _COMPRESSOR_PRESETS.get(name, (1.0, 1.0))
        enabled = name != "Off"
        self._comp_enable.setChecked(enabled)
        self._ratio_slider.setValue(int(ratio * 10))
        self._thresh_slider.setValue(int(threshold * 100))

    def _on_compressor_changed(self) -> None:
        enabled = self._comp_enable.isChecked()
        ratio = self._ratio_slider.value() / 10.0
        threshold = self._thresh_slider.value() / 100.0
        self._ratio_label.setText(f"{ratio:.1f}:1")
        self._thresh_label.setText(f"{int(threshold * 100)}%")
        self.compressor_changed.emit(enabled, ratio, threshold)

    def _on_speed_changed(self) -> None:
        rate = self._speed_slider.value() / 100.0
        self._speed_label.setText(f"{rate:.2f}x")
        self.speed_changed.emit(rate)

    def _set_speed(self, rate: float) -> None:
        self._speed_slider.setValue(int(rate * 100))
