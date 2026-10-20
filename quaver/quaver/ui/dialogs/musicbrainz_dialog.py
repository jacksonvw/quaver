"""MusicBrainz lookup dialog — fingerprint, review, and apply tags.

Runs the lookup in a background thread, then shows a side-by-side
comparison so the user can review before writing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from quaver.core.library import LibraryDB, Track
from quaver.core.musicbrainz import LookupResult, MBMatch, lookup_files
from quaver.core.tagger import read_tags, write_tags


# Fields we compare and can write.
_FIELDS = [
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("album_artist", "Album Artist"),
    ("year", "Year"),
    ("genre", "Genre"),
    ("track_number", "Track #"),
    ("disc_number", "Disc #"),
]


class _LookupWorker(QThread):
    """Runs MusicBrainz lookups in a background thread."""

    progress = Signal(int, int)  # done, total
    finished = Signal(list)  # list[LookupResult]

    def __init__(self, filepaths: list[str], parent=None):
        super().__init__(parent)
        self._filepaths = filepaths

    error = Signal(str)

    def run(self):
        try:
            results = lookup_files(
                self._filepaths,
                on_progress=lambda done, total: self.progress.emit(done, total),
            )
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class MusicBrainzDialog(QDialog):
    """Fingerprint tracks, review MusicBrainz matches, and apply selected tags."""

    def __init__(
        self,
        tracks: list[Track],
        db: LibraryDB,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._db = db
        self._results: list[LookupResult] = []
        self._checkboxes: list[list[QCheckBox]] = []  # per-track, per-field
        self._match_combos: list[QComboBox] = []

        self.setWindowTitle("MusicBrainz Lookup")
        self.setMinimumSize(700, 500)

        self._build_ui()
        self._start_lookup()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Stacked: page 0 = progress, page 1 = review.
        self._pages = QStackedWidget()
        layout.addWidget(self._pages, stretch=1)

        # Page 0: progress.
        progress_page = QWidget()
        p_layout = QVBoxLayout(progress_page)
        p_layout.addStretch()
        self._progress_label = QLabel("Fingerprinting and looking up tracks…")
        self._progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        p_layout.addWidget(self._progress_label)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, len(self._tracks))
        p_layout.addWidget(self._progress_bar)
        p_layout.addStretch()
        self._pages.addWidget(progress_page)

        # Page 1: review (built after results arrive).
        self._review_page = QWidget()
        self._review_layout = QVBoxLayout(self._review_page)
        self._pages.addWidget(self._review_page)

        # Buttons.
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(False)
        self._buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self._apply
        )
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _start_lookup(self) -> None:
        filepaths = [t.filepath for t in self._tracks]
        self._worker = _LookupWorker(filepaths, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_results)
        self._worker.error.connect(self._on_worker_error)
        self._worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        self._progress_bar.setValue(done)
        self._progress_label.setText(
            f"Looking up track {done}/{total}…"
        )

    def _on_worker_error(self, msg: str) -> None:
        self._progress_label.setText(f"Lookup failed: {msg}")
        self._buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(False)

    def _on_results(self, results: list[LookupResult]) -> None:
        self._results = results
        self._build_review_page()
        self._pages.setCurrentIndex(1)
        self._buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(True)

    def _build_review_page(self) -> None:
        layout = self._review_layout

        # Summary.
        matched = sum(1 for r in self._results if r.matches)
        failed = sum(1 for r in self._results if r.error)
        summary = QLabel(
            f"{matched} matched, {failed} failed out of {len(self._results)} tracks."
        )
        summary.setStyleSheet("font-weight: bold; margin-bottom: 8px;")
        layout.addWidget(summary)

        # One section per track.
        for i, (track, result) in enumerate(zip(self._tracks, self._results)):
            if not result.matches:
                err_label = QLabel(
                    f"<b>{track.title or track.filepath}</b> — {result.error}"
                )
                err_label.setStyleSheet("color: #c44;")
                layout.addWidget(err_label)
                self._checkboxes.append([])
                self._match_combos.append(QComboBox())
                continue

            # Track header + match selector.
            header = QHBoxLayout()
            header.addWidget(QLabel(f"<b>{track.title or track.filepath}</b>"))
            header.addStretch()

            combo = QComboBox()
            for j, m in enumerate(result.matches):
                score_pct = int(m.score * 100)
                label = f"{m.artist} — {m.title} ({m.album}) [{score_pct}%]"
                combo.addItem(label, j)
            header.addWidget(QLabel("Match:"))
            header.addWidget(combo)
            self._match_combos.append(combo)
            layout.addLayout(header)

            # Comparison table.
            table = QTableWidget(len(_FIELDS), 4)
            table.setHorizontalHeaderLabels(["Field", "Current", "MusicBrainz", "Apply"])
            table.verticalHeader().setVisible(False)
            h = table.horizontalHeader()
            h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

            track_cbs: list[QCheckBox] = []
            match = result.matches[0]

            for row, (field, label) in enumerate(_FIELDS):
                current_val = str(getattr(track, field, "") or "")
                mb_val = str(getattr(match, field, "") or "")

                table.setItem(row, 0, QTableWidgetItem(label))
                table.setItem(row, 1, QTableWidgetItem(current_val))
                table.setItem(row, 2, QTableWidgetItem(mb_val))

                cb = QCheckBox()
                # Auto-check if the value differs and MB has something.
                cb.setChecked(bool(mb_val) and mb_val != current_val)
                track_cbs.append(cb)
                table.setCellWidget(row, 3, cb)

            table.setMaximumHeight(30 + len(_FIELDS) * 30)
            self._checkboxes.append(track_cbs)

            # Update table when match selection changes.
            combo.currentIndexChanged.connect(
                lambda idx, t=track, tbl=table, cbs=track_cbs, res=result:
                    self._update_match(t, tbl, cbs, res, idx)
            )

            layout.addWidget(table)

        layout.addStretch()

    def _update_match(
        self,
        track: Track,
        table: QTableWidget,
        checkboxes: list[QCheckBox],
        result: LookupResult,
        match_idx: int,
    ) -> None:
        """Update the comparison table when the user picks a different match."""
        if match_idx < 0 or match_idx >= len(result.matches):
            return
        match = result.matches[match_idx]

        for row, (field, _label) in enumerate(_FIELDS):
            current_val = str(getattr(track, field, "") or "")
            mb_val = str(getattr(match, field, "") or "")
            table.setItem(row, 2, QTableWidgetItem(mb_val))
            checkboxes[row].setChecked(bool(mb_val) and mb_val != current_val)

    def _apply(self) -> None:
        """Write checked fields to files and update the database."""
        written = 0
        errors: list[str] = []

        for i, (track, result) in enumerate(zip(self._tracks, self._results)):
            if not result.matches or not self._checkboxes[i]:
                continue

            # Which match is selected?
            combo = self._match_combos[i]
            match_idx = combo.currentData()
            if match_idx is None or match_idx >= len(result.matches):
                continue
            match = result.matches[match_idx]

            # Collect checked fields.
            changes: dict[str, str] = {}
            for row, (field, _label) in enumerate(_FIELDS):
                if self._checkboxes[i][row].isChecked():
                    val = str(getattr(match, field, "") or "")
                    if val:
                        changes[field] = val

            if not changes:
                continue

            ok = write_tags(track.filepath, changes)
            if ok:
                written += 1
                new_tags = read_tags(track.filepath)
                if new_tags:
                    self._db.upsert_track(new_tags)
            else:
                errors.append(track.filepath)

        msg = f"Updated tags for {written} track(s)."
        if errors:
            msg += f"\nFailed on {len(errors)} file(s)."
        QMessageBox.information(self, "MusicBrainz", msg)
        self.accept()
