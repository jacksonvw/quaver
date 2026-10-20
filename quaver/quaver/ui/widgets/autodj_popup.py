"""AutoDJ settings popup — source scope, genre filter, recency, lookahead."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class AutoDJPopup(QDialog):
    """AutoDJ configuration dialog.

    Emits signals immediately on change (no OK/Cancel). The caller is
    responsible for applying settings to AutoDJService and saving config.
    """

    source_tabs_changed = Signal(list)   # list[str]; empty = whole library
    genre_filter_changed = Signal(str)
    avoid_hours_changed = Signal(int)
    lookahead_changed = Signal(int)

    def __init__(
        self,
        tab_names: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto-DJ Settings")
        self.setMinimumWidth(280)
        self._tab_names = list(tab_names)
        self._tab_checkboxes: list[QCheckBox] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # -- Source section -----------------------------------------------
        source_group = QGroupBox("Source")
        source_layout = QVBoxLayout(source_group)

        self._radio_all = QRadioButton("All library")
        self._radio_selected = QRadioButton("Selected tabs:")
        self._radio_all.setChecked(True)

        self._source_group = QButtonGroup(self)
        self._source_group.addButton(self._radio_all)
        self._source_group.addButton(self._radio_selected)

        source_layout.addWidget(self._radio_all)
        source_layout.addWidget(self._radio_selected)

        # Indented checkbox area for individual tabs.
        self._tabs_container = QWidget()
        tabs_layout = QVBoxLayout(self._tabs_container)
        tabs_layout.setContentsMargins(20, 0, 0, 0)
        tabs_layout.setSpacing(2)

        for name in self._tab_names:
            cb = QCheckBox(name)
            cb.setEnabled(False)  # disabled until "Selected tabs" is chosen
            cb.toggled.connect(self._on_source_changed)
            self._tab_checkboxes.append(cb)
            tabs_layout.addWidget(cb)

        source_layout.addWidget(self._tabs_container)
        layout.addWidget(source_group)

        self._radio_all.toggled.connect(self._on_source_changed)

        # -- Filters section ----------------------------------------------
        filter_group = QGroupBox("Filters")
        filter_layout = QVBoxLayout(filter_group)

        genre_row = QHBoxLayout()
        genre_row.addWidget(QLabel("Genre:"))
        self._genre_edit = QLineEdit()
        self._genre_edit.setPlaceholderText("e.g. jazz  (substring match)")
        self._genre_edit.textChanged.connect(self.genre_filter_changed.emit)
        genre_row.addWidget(self._genre_edit, stretch=1)
        filter_layout.addLayout(genre_row)

        avoid_row = QHBoxLayout()
        avoid_row.addWidget(QLabel("Avoid tracks played within:"))
        self._avoid_spin = QSpinBox()
        self._avoid_spin.setRange(0, 168)
        self._avoid_spin.setSuffix(" h")
        self._avoid_spin.setToolTip("Set to 0 to disable recency filter")
        self._avoid_spin.valueChanged.connect(self.avoid_hours_changed.emit)
        avoid_row.addWidget(self._avoid_spin)
        avoid_row.addStretch()
        filter_layout.addLayout(avoid_row)

        layout.addWidget(filter_group)

        # -- Queue top-up section -----------------------------------------
        topup_group = QGroupBox("Queue top-up")
        topup_layout = QVBoxLayout(topup_group)

        lookahead_row = QHBoxLayout()
        lookahead_row.addWidget(QLabel("Keep ahead:"))
        self._lookahead_spin = QSpinBox()
        self._lookahead_spin.setRange(1, 20)
        self._lookahead_spin.setSuffix(" tracks")
        self._lookahead_spin.valueChanged.connect(self.lookahead_changed.emit)
        lookahead_row.addWidget(self._lookahead_spin)
        lookahead_row.addStretch()
        topup_layout.addLayout(lookahead_row)

        hint = QLabel("Auto-DJ adds tracks when fewer than this many remain ahead.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        topup_layout.addWidget(hint)

        layout.addWidget(topup_group)

    def set_state(
        self,
        avoid_hours: int,
        genre_filter: str,
        lookahead: int,
        selected_tab_names: list[str],
    ) -> None:
        """Load persisted config values without emitting signals."""
        # Block all signals while setting initial values.
        for w in (self._avoid_spin, self._lookahead_spin, self._genre_edit):
            w.blockSignals(True)
        self._radio_all.blockSignals(True)
        self._radio_selected.blockSignals(True)
        for cb in self._tab_checkboxes:
            cb.blockSignals(True)

        self._avoid_spin.setValue(avoid_hours)
        self._genre_edit.setText(genre_filter)
        self._lookahead_spin.setValue(lookahead)

        if not selected_tab_names:
            self._radio_all.setChecked(True)
            for cb in self._tab_checkboxes:
                cb.setEnabled(False)
                cb.setChecked(False)
        else:
            self._radio_selected.setChecked(True)
            selected_set = set(selected_tab_names)
            for cb in self._tab_checkboxes:
                cb.setEnabled(True)
                cb.setChecked(cb.text() in selected_set)

        # Unblock.
        for w in (self._avoid_spin, self._lookahead_spin, self._genre_edit):
            w.blockSignals(False)
        self._radio_all.blockSignals(False)
        self._radio_selected.blockSignals(False)
        for cb in self._tab_checkboxes:
            cb.blockSignals(False)

    def _on_source_changed(self) -> None:
        use_selected = self._radio_selected.isChecked()
        for cb in self._tab_checkboxes:
            cb.setEnabled(use_selected)

        if not use_selected:
            self.source_tabs_changed.emit([])
        else:
            selected = [cb.text() for cb in self._tab_checkboxes if cb.isChecked()]
            self.source_tabs_changed.emit(selected)
