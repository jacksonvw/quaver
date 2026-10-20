"""Minim preferences dialog — Appearance / Playback / Video.

Trimmed for the player shell:
  - No library, podcast, YouTube, or scraper sections.
  - Single :class:`PlayerConfig` round-trip via :mod:`minim.core.config`.
  - Same widget vocabulary as Quaver's dialog so users get a consistent
    look between the two apps; just shorter.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from media_core.core.config import PlayerConfig

from minim.core.config import load_config, save_config


_BG_MODES: list[tuple[str, str]] = [
    ("blur", "Blurred Album Art"),
    ("gradient_natural", "Gradient — Natural"),
    ("gradient_vivid", "Gradient — Vivid"),
    ("gradient_warm", "Gradient — Warm"),
    ("gradient_cool", "Gradient — Cool"),
    ("gradient_pastel", "Gradient — Pastel"),
    ("solid", "Solid Dark"),
    ("transparent", "Transparent"),
]

_RG_MODES: list[tuple[str, str]] = [
    ("off", "Off"),
    ("track", "Track"),
    ("album", "Album"),
]

_NP_VIDEO_MODES: list[tuple[str, str]] = [
    ("video", "Video Player"),
    ("thumbnail", "Thumbnail"),
]


class PreferencesDialog(QDialog):
    """Minim preferences with three tabs: Appearance, Playback, Video."""

    settings_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Minim Preferences")
        self.setMinimumSize(450, 340)

        self._config: PlayerConfig = load_config()
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_appearance_tab(), "Appearance")
        tabs.addTab(self._build_playback_tab(), "Playback")
        tabs.addTab(self._build_video_tab(), "Video")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self._on_apply
        )
        layout.addWidget(buttons)

    def _build_appearance_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # -- Now Playing -------------------------------------------------
        np_group = QGroupBox("Now Playing")
        np_form = QFormLayout(np_group)

        self._np_bg_combo = QComboBox()
        for _, label in _BG_MODES:
            self._np_bg_combo.addItem(label)
        idx = next(
            (i for i, (k, _) in enumerate(_BG_MODES)
             if k == self._config.nowplaying_bg_mode),
            0,
        )
        self._np_bg_combo.setCurrentIndex(idx)
        np_form.addRow("Background:", self._np_bg_combo)

        self._np_tint_slider = QSlider(Qt.Orientation.Horizontal)
        self._np_tint_slider.setRange(0, 255)
        self._np_tint_slider.setValue(self._config.nowplaying_tint_alpha)
        np_form.addRow("Overlay intensity:", self._np_tint_slider)

        self._np_blur_slider = QSlider(Qt.Orientation.Horizontal)
        self._np_blur_slider.setRange(1, 20)
        self._np_blur_slider.setValue(self._config.nowplaying_blur_strength)
        np_form.addRow("Blur strength:", self._np_blur_slider)

        self._np_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._np_opacity_slider.setRange(0, 100)
        self._np_opacity_slider.setValue(int(self._config.nowplaying_bg_opacity * 100))
        np_form.addRow("Background opacity:", self._np_opacity_slider)

        self._np_art_size_slider = QSlider(Qt.Orientation.Horizontal)
        self._np_art_size_slider.setRange(10, 90)
        self._np_art_size_slider.setValue(self._config.nowplaying_art_size)
        np_form.addRow("Art size (% of panel):", self._np_art_size_slider)

        self._np_show_labels_cb = QCheckBox("Show track labels")
        self._np_show_labels_cb.setChecked(self._config.nowplaying_show_labels)
        np_form.addRow(self._np_show_labels_cb)

        self._np_show_spectrum_cb = QCheckBox("Show spectrum visualizer")
        self._np_show_spectrum_cb.setChecked(self._config.nowplaying_show_spectrum)
        np_form.addRow(self._np_show_spectrum_cb)

        layout.addWidget(np_group)

        # -- Window ------------------------------------------------------
        win_group = QGroupBox("Window")
        win_form = QFormLayout(win_group)

        self._bg_color = QColor(*self._config.bg_color)
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(60, 24)
        self._update_color_btn()
        self._color_btn.clicked.connect(self._pick_bg_color)
        win_form.addRow("Background color:", self._color_btn)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(0, 100)
        self._opacity_slider.setValue(int(self._config.window_opacity * 100))
        self._opacity_label = QLabel(f"{self._opacity_slider.value()}%")
        self._opacity_slider.valueChanged.connect(
            lambda v: self._opacity_label.setText(f"{v}%")
        )
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(self._opacity_slider)
        opacity_row.addWidget(self._opacity_label)
        win_form.addRow("Transparency:", opacity_row)

        self._blur_behind_cb = QCheckBox("Enable blur behind window (KDE)")
        self._blur_behind_cb.setChecked(self._config.blur_behind)
        win_form.addRow(self._blur_behind_cb)

        self._tint_slider = QSlider(Qt.Orientation.Horizontal)
        self._tint_slider.setRange(0, 100)
        self._tint_slider.setValue(self._config.bg_tint_strength)
        self._tint_label = QLabel(f"{self._tint_slider.value()}%")
        self._tint_slider.valueChanged.connect(
            lambda v: self._tint_label.setText(f"{v}%")
        )
        tint_row = QHBoxLayout()
        tint_row.addWidget(self._tint_slider)
        tint_row.addWidget(self._tint_label)
        win_form.addRow("Tint strength:", tint_row)

        layout.addWidget(win_group)
        layout.addStretch()
        return widget

    def _build_playback_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        rg_group = QGroupBox("ReplayGain")
        rg_form = QFormLayout(rg_group)

        self._rg_combo = QComboBox()
        for _, label in _RG_MODES:
            self._rg_combo.addItem(label)
        idx = next(
            (i for i, (k, _) in enumerate(_RG_MODES)
             if k == self._config.replaygain_mode),
            0,
        )
        self._rg_combo.setCurrentIndex(idx)
        rg_form.addRow("Mode:", self._rg_combo)
        layout.addWidget(rg_group)

        queue_group = QGroupBox("Queue")
        queue_form = QFormLayout(queue_group)
        self._restore_queue_cb = QCheckBox("Restore queue on startup")
        self._restore_queue_cb.setChecked(self._config.restore_queue)
        queue_form.addRow(self._restore_queue_cb)
        layout.addWidget(queue_group)

        layout.addStretch()
        return widget

    def _build_video_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Now Playing video render mode.
        mode_group = QGroupBox("Video display")
        mode_form = QFormLayout(mode_group)

        self._np_video_combo = QComboBox()
        for _, label in _NP_VIDEO_MODES:
            self._np_video_combo.addItem(label)
        idx = next(
            (i for i, (k, _) in enumerate(_NP_VIDEO_MODES)
             if k == self._config.youtube_nowplaying_mode),
            0,
        )
        self._np_video_combo.setCurrentIndex(idx)
        mode_form.addRow("Now Playing mode:", self._np_video_combo)
        layout.addWidget(mode_group)

        # Subtitles.
        sub_group = QGroupBox("Subtitles")
        sub_form = QFormLayout(sub_group)

        self._sub_font_spin = QSpinBox()
        self._sub_font_spin.setRange(12, 48)
        self._sub_font_spin.setValue(self._config.subtitle_font_size)
        self._sub_font_spin.setSuffix(" pt")
        sub_form.addRow("Font size:", self._sub_font_spin)

        self._sub_outline_cb = QCheckBox("Text outline")
        self._sub_outline_cb.setChecked(self._config.subtitle_outline)
        sub_form.addRow(self._sub_outline_cb)

        self._sub_bg_cb = QCheckBox("Background bar")
        self._sub_bg_cb.setChecked(self._config.subtitle_bg)
        sub_form.addRow(self._sub_bg_cb)

        layout.addWidget(sub_group)
        layout.addStretch()
        return widget

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_color_btn(self) -> None:
        self._color_btn.setStyleSheet(
            f"background-color: {self._bg_color.name()}; border: 1px solid #666;"
        )

    def _pick_bg_color(self) -> None:
        color = QColorDialog.getColor(self._bg_color, self, "Background Color")
        if color.isValid():
            self._bg_color = color
            self._update_color_btn()

    def _apply_to_config(self) -> None:
        # Appearance.
        bg_key, _ = _BG_MODES[self._np_bg_combo.currentIndex()]
        self._config.nowplaying_bg_mode = bg_key
        self._config.nowplaying_tint_alpha = self._np_tint_slider.value()
        self._config.nowplaying_blur_strength = self._np_blur_slider.value()
        self._config.nowplaying_bg_opacity = self._np_opacity_slider.value() / 100.0
        self._config.nowplaying_art_size = self._np_art_size_slider.value()
        self._config.nowplaying_show_labels = self._np_show_labels_cb.isChecked()
        self._config.nowplaying_show_spectrum = self._np_show_spectrum_cb.isChecked()
        self._config.window_opacity = self._opacity_slider.value() / 100.0
        self._config.blur_behind = self._blur_behind_cb.isChecked()
        self._config.bg_color = [
            self._bg_color.red(),
            self._bg_color.green(),
            self._bg_color.blue(),
        ]
        self._config.bg_tint_strength = self._tint_slider.value()
        # Playback.
        rg_key, _ = _RG_MODES[self._rg_combo.currentIndex()]
        self._config.replaygain_mode = rg_key
        self._config.restore_queue = self._restore_queue_cb.isChecked()
        # Video.
        np_video_key, _ = _NP_VIDEO_MODES[self._np_video_combo.currentIndex()]
        self._config.youtube_nowplaying_mode = np_video_key
        self._config.subtitle_font_size = self._sub_font_spin.value()
        self._config.subtitle_outline = self._sub_outline_cb.isChecked()
        self._config.subtitle_bg = self._sub_bg_cb.isChecked()

    def _on_apply(self) -> None:
        self._apply_to_config()
        save_config(self._config)
        self.settings_changed.emit()

    def _on_ok(self) -> None:
        self._on_apply()
        self.accept()

    @property
    def config(self) -> PlayerConfig:
        return self._config
