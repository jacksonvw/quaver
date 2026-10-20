"""Transport controls widget — always visible at the bottom of the window.

Layout (MusicBee-style, flush full-width rows):
  Row 1 (status):   Artist — Title                                  [status]  pos/ dur
  Row 2 (controls): [|< >|| ■ >|] [🔈 vol] [━━━━seek━━━━] [spectrum] [Shuf Rep DJ EQ FX Q]
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QProxyStyle,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from media_core.core.audio import PlaybackState
from media_core.services.player_service import RepeatMode
from media_core.ui.widgets.icons import (
    icon_fullscreen,
    icon_next,
    icon_pause,
    icon_play,
    icon_prev,
    icon_queue_panel,
    icon_repeat,
    icon_repeat_off,
    icon_repeat_one,
    icon_shuffle,
    icon_stop,
    icon_volume,
    icon_volume_mute,
    set_icon_button,
)


_ICON_SIZE = 18


def _ms_to_mmss(ms: int) -> str:
    total_secs = ms // 1000
    m, s = divmod(total_secs, 60)
    return f"{m}:{s:02d}"


class _JumpToClickStyle(QProxyStyle):
    """Style override that makes QSlider jump to the clicked position.

    By default Qt page-steps on click (moves one "page" toward the cursor).
    This tells Qt to use absolute positioning for left-click instead,
    which also preserves native drag behavior — no mouse event hacks needed.
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return Qt.MouseButton.LeftButton.value
        return super().styleHint(hint, option, widget, returnData)


def _make_slider(orientation: Qt.Orientation) -> QSlider:
    """Create a QSlider with jump-to-click behavior."""
    slider = QSlider(orientation)
    # No base style: QProxyStyle(app_style) would take OWNERSHIP of the
    # application-wide style and delete it when this slider is destroyed.
    # A bare proxy delegates to the current app style without owning it.
    style = _JumpToClickStyle()
    style.setParent(slider)  # prevent GC
    slider.setStyle(style)
    return slider


class _AutoDJButton(QPushButton):
    """DJ toggle button that emits a separate signal on right-click."""

    right_clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class TransportWidget(QWidget):
    """Playback transport controls with vector icons."""

    # Signals for the main window to wire to PlayerService.
    play_toggled = Signal()
    stop_clicked = Signal()
    prev_clicked = Signal()
    next_clicked = Signal()
    seek_requested = Signal(int)  # position_ms
    volume_changed = Signal(float)  # 0.0–1.0
    shuffle_toggled = Signal()
    repeat_cycled = Signal()
    eq_clicked = Signal()
    dsp_clicked = Signal()
    autodj_toggled = Signal()
    autodj_settings_requested = Signal()
    fullscreen_clicked = Signal()
    queue_toggled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._seeking = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(1)

        # -- Row 1: all controls (full-width, no orphaned elements) --
        controls = QHBoxLayout()
        controls.setSpacing(4)

        # Transport buttons (left).
        self._btn_prev = QPushButton()
        self._btn_prev.setFixedSize(32, 28)
        self._btn_prev.setToolTip("Previous")
        set_icon_button(self._btn_prev, icon_prev(), _ICON_SIZE)
        self._btn_prev.clicked.connect(self.prev_clicked.emit)

        self._btn_play = QPushButton()
        self._btn_play.setFixedSize(36, 28)
        self._btn_play.setToolTip("Play")
        set_icon_button(self._btn_play, icon_play(), _ICON_SIZE)
        self._btn_play.clicked.connect(self.play_toggled.emit)

        self._btn_stop = QPushButton()
        self._btn_stop.setFixedSize(32, 28)
        self._btn_stop.setToolTip("Stop")
        set_icon_button(self._btn_stop, icon_stop(), _ICON_SIZE)
        self._btn_stop.clicked.connect(self.stop_clicked.emit)

        self._btn_next = QPushButton()
        self._btn_next.setFixedSize(32, 28)
        self._btn_next.setToolTip("Next")
        set_icon_button(self._btn_next, icon_next(), _ICON_SIZE)
        self._btn_next.clicked.connect(self.next_clicked.emit)

        controls.addWidget(self._btn_prev)
        controls.addWidget(self._btn_play)
        controls.addWidget(self._btn_stop)
        controls.addWidget(self._btn_next)

        # Volume (next to transport buttons, MusicBee-style).
        self._vol_icon = QPushButton()
        self._vol_icon.setFixedSize(28, 28)
        self._vol_icon.setFlat(True)
        set_icon_button(self._vol_icon, icon_volume(), _ICON_SIZE)
        self._vol_icon.setToolTip("Mute")
        self._vol_icon.clicked.connect(self._toggle_mute)
        self._muted = False
        self._last_volume = 100

        self._vol_slider = _make_slider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(100)
        self._vol_slider.setFixedWidth(80)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)

        controls.addWidget(self._vol_icon)
        controls.addWidget(self._vol_slider)

        # Seek bar (center, stretches — no flanking time labels).
        self._seek_slider = _make_slider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.setMinimumWidth(120)
        self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)

        controls.addWidget(self._seek_slider, stretch=1)

        # Spectrum mini-visualizer.
        from media_core.ui.widgets.spectrum import SpectrumWidget
        self._spectrum = SpectrumWidget(bar_count=24, bar_gap=1)
        self._spectrum.setFixedHeight(28)
        self._spectrum.setFixedWidth(140)
        controls.addWidget(self._spectrum)

        # Playback mode buttons (right).
        self._btn_shuffle = QPushButton()
        self._btn_shuffle.setFixedSize(32, 28)
        self._btn_shuffle.setCheckable(True)
        self._btn_shuffle.setToolTip("Shuffle")
        set_icon_button(self._btn_shuffle, icon_shuffle(), _ICON_SIZE)
        self._btn_shuffle.clicked.connect(self.shuffle_toggled.emit)

        self._btn_repeat = QPushButton()
        self._btn_repeat.setFixedSize(32, 28)
        self._btn_repeat.setToolTip("Repeat")
        set_icon_button(self._btn_repeat, icon_repeat_off(), _ICON_SIZE)
        self._btn_repeat.clicked.connect(self.repeat_cycled.emit)

        self._btn_autodj = _AutoDJButton("DJ")
        self._btn_autodj.setFixedSize(32, 28)
        self._btn_autodj.setCheckable(True)
        self._btn_autodj.setToolTip("Auto-DJ (right-click for settings)")
        self._btn_autodj.clicked.connect(self.autodj_toggled.emit)
        self._btn_autodj.right_clicked.connect(self.autodj_settings_requested.emit)

        self._btn_eq = QPushButton("EQ")
        self._btn_eq.setFixedSize(32, 28)
        self._btn_eq.setToolTip("Equalizer")
        self._btn_eq.clicked.connect(self.eq_clicked.emit)

        self._btn_dsp = QPushButton("FX")
        self._btn_dsp.setFixedSize(32, 28)
        self._btn_dsp.setToolTip("DSP Effects")
        self._btn_dsp.clicked.connect(self.dsp_clicked.emit)

        self._btn_fullscreen = QPushButton()
        self._btn_fullscreen.setFixedSize(32, 28)
        self._btn_fullscreen.setToolTip("Fullscreen (F11)")
        set_icon_button(self._btn_fullscreen, icon_fullscreen(), _ICON_SIZE)
        self._btn_fullscreen.clicked.connect(self.fullscreen_clicked.emit)

        self._btn_queue = QPushButton()
        self._btn_queue.setFixedSize(32, 28)
        self._btn_queue.setCheckable(True)
        self._btn_queue.setChecked(True)
        self._btn_queue.setToolTip("Toggle Queue Panel")
        set_icon_button(self._btn_queue, icon_queue_panel(), _ICON_SIZE)
        self._btn_queue.clicked.connect(self.queue_toggled.emit)

        controls.addWidget(self._btn_shuffle)
        controls.addWidget(self._btn_repeat)
        controls.addWidget(self._btn_autodj)
        controls.addWidget(self._btn_eq)
        controls.addWidget(self._btn_dsp)
        controls.addWidget(self._btn_fullscreen)
        controls.addWidget(self._btn_queue)

        # -- Row 1: status bar (above controls — MusicBee style) --
        status = QHBoxLayout()
        status.setSpacing(8)
        status.setContentsMargins(2, 0, 2, 0)

        self._track_label = QLabel("No file loaded")
        self._track_label.setStyleSheet("font-size: 11px; color: #ccc;")

        # Progress area: label + bar, shown during scan/enrichment.
        self._progress_widget = QWidget()
        self._progress_widget.setFixedHeight(16)
        progress_layout = QHBoxLayout(self._progress_widget)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(4)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("font-size: 10px; color: #888;")
        self._status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedWidth(100)
        self._progress_bar.setFixedHeight(12)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setStyleSheet(
            "QProgressBar { background: #333; border: 1px solid #555;"
            " border-radius: 3px; }"
            "QProgressBar::chunk { background: #6a9; border-radius: 2px; }"
        )

        progress_layout.addWidget(self._status_label)
        progress_layout.addWidget(self._progress_bar)
        self._progress_widget.hide()

        self._time_label = QLabel("")
        self._time_label.setStyleSheet("font-size: 10px; color: #aaa;")
        self._time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        status.addWidget(self._track_label, stretch=1)
        status.addWidget(self._progress_widget)
        status.addWidget(self._time_label)

        layout.addLayout(status)

        # -- Row 2: all controls --
        layout.addLayout(controls)

    # ------------------------------------------------------------------
    # Update methods (called by main window)
    # ------------------------------------------------------------------

    @property
    def spectrum_widget(self) -> object:
        """The embedded spectrum visualizer widget."""
        return self._spectrum

    def set_track_label(self, text: str) -> None:
        self._track_label.setText(text)

    def show_progress(self, message: str, done: int, total: int) -> None:
        """Show progress bar with message and numeric progress."""
        self._status_label.setText(message)
        self._progress_bar.setRange(0, total)
        self._progress_bar.setValue(done)
        self._progress_bar.show()
        self._progress_widget.show()

    def show_status(self, message: str) -> None:
        """Show indeterminate status (pulsing bar, no numeric progress)."""
        self._status_label.setText(message)
        self._progress_bar.setRange(0, 0)  # Indeterminate.
        self._progress_bar.show()
        self._progress_widget.show()

    def hide_progress(self) -> None:
        """Hide the progress widget."""
        self._progress_widget.hide()
        self._status_label.setText("")

    def set_status_message(self, text: str) -> None:
        """Show a text-only status message (no progress bar)."""
        self._status_label.setText(text)
        if text:
            self._progress_bar.hide()
            self._progress_widget.show()
        else:
            self._progress_widget.hide()

    def clear_status_message(self) -> None:
        """Clear the status message area."""
        self.hide_progress()

    def set_position(self, pos_ms: int, dur_ms: int) -> None:
        if dur_ms > 0:
            self._seek_slider.setRange(0, dur_ms)
        if not self._seeking:
            self._seek_slider.setValue(pos_ms)
        # Update the time label in the status bar: "pos/ dur".
        if dur_ms > 0:
            self._time_label.setText(f"{_ms_to_mmss(pos_ms)}/ {_ms_to_mmss(dur_ms)}")
        else:
            self._time_label.setText("")

    def set_play_state(self, state: PlaybackState) -> None:
        if state == PlaybackState.PLAYING:
            set_icon_button(self._btn_play, icon_pause(), _ICON_SIZE)
            self._btn_play.setToolTip("Pause")
        else:
            set_icon_button(self._btn_play, icon_play(), _ICON_SIZE)
            self._btn_play.setToolTip("Play")
        if state == PlaybackState.STOPPED:
            self._seek_slider.setValue(0)
            self._seek_slider.setRange(0, 0)
            self._time_label.setText("")

    def set_shuffle_checked(self, checked: bool) -> None:
        self._btn_shuffle.setChecked(checked)

    def set_repeat_label(self, mode: RepeatMode) -> None:
        icon_map = {
            RepeatMode.OFF: icon_repeat_off,
            RepeatMode.ALL: icon_repeat,
            RepeatMode.ONE: icon_repeat_one,
        }
        factory = icon_map.get(mode, icon_repeat_off)
        set_icon_button(self._btn_repeat, factory(), _ICON_SIZE)

    def set_autodj_checked(self, checked: bool) -> None:
        self._btn_autodj.setChecked(checked)

    def set_queue_checked(self, checked: bool) -> None:
        self._btn_queue.setChecked(checked)

    def set_volume(self, level: float) -> None:
        self._vol_slider.setValue(int(level * 100))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self.seek_requested.emit(self._seek_slider.value())
        self._seeking = False

    def _on_volume_changed(self, value: int) -> None:
        self.volume_changed.emit(value / 100.0)
        if value == 0:
            set_icon_button(self._vol_icon, icon_volume_mute(), _ICON_SIZE)
        else:
            set_icon_button(self._vol_icon, icon_volume(), _ICON_SIZE)

    def _toggle_mute(self) -> None:
        if self._muted:
            self._vol_slider.setValue(self._last_volume)
            self._muted = False
        else:
            self._last_volume = self._vol_slider.value() or 100
            self._vol_slider.setValue(0)
            self._muted = True
