"""Per-podcast settings dialog."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from quaver.core.podcast import Podcast


_REFRESH_OPTIONS = [
    ("Every 6 hours", 6.0),
    ("Every 12 hours", 12.0),
    ("Every 24 hours", 24.0),
    ("Every 2 days", 48.0),
    ("Every week", 168.0),
    ("Manual only", 0.0),
]


class PodcastSettingsDialog(QDialog):
    """Configure per-podcast settings: auto-download, retention, refresh rate."""

    def __init__(
        self, podcast: Podcast, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._podcast = podcast
        self.setWindowTitle(f"Settings — {podcast.title}")
        self.setMinimumWidth(350)

        self.auto_download: bool = podcast.auto_download
        self.auto_delete_played: bool = podcast.auto_delete_played
        self.retain_episodes: int = podcast.retain_episodes
        self.refresh_interval_hours: float = podcast.refresh_interval_hours

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        # Auto-download.
        self._cb_auto_dl = QCheckBox("Automatically download new episodes")
        self._cb_auto_dl.setChecked(self._podcast.auto_download)
        form.addRow(self._cb_auto_dl)

        # Auto-delete played.
        self._cb_auto_del = QCheckBox("Delete episodes after playing")
        self._cb_auto_del.setChecked(self._podcast.auto_delete_played)
        form.addRow(self._cb_auto_del)

        # Retain episodes.
        self._retain_spin = QSpinBox()
        self._retain_spin.setRange(1, 100)
        self._retain_spin.setValue(self._podcast.retain_episodes)
        self._retain_spin.setSuffix(" episodes")
        form.addRow("Keep latest:", self._retain_spin)

        # Refresh interval.
        self._refresh_combo = QComboBox()
        current_idx = 2  # default to 24h
        for i, (label, hours) in enumerate(_REFRESH_OPTIONS):
            self._refresh_combo.addItem(label, hours)
            if abs(hours - self._podcast.refresh_interval_hours) < 0.1:
                current_idx = i
        self._refresh_combo.setCurrentIndex(current_idx)
        form.addRow("Check for updates:", self._refresh_combo)

        layout.addLayout(form)

        # Buttons.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        self.auto_download = self._cb_auto_dl.isChecked()
        self.auto_delete_played = self._cb_auto_del.isChecked()
        self.retain_episodes = self._retain_spin.value()
        self.refresh_interval_hours = self._refresh_combo.currentData()
        self.accept()
