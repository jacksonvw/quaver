"""Tag editor dialog — single and multi-track editing.

Only writes fields that the user actually changed.
In multi-track mode, fields with mixed values show a placeholder;
leaving them unchanged skips the write for that field.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from quaver.core.library import LibraryDB, Track
from quaver.core.tagger import read_tags, write_tags

# Fields exposed in the editor.
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

_MIXED = "<mixed>"


class TagEditorDialog(QDialog):
    """Edit tags for one or more tracks."""

    def __init__(
        self,
        tracks: list[Track],
        db: LibraryDB,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._db = db
        self._multi = len(tracks) > 1
        self._edits: dict[str, QLineEdit] = {}
        self._original: dict[str, str] = {}

        self.setWindowTitle(
            f"Edit Tags — {len(tracks)} tracks" if self._multi
            else f"Edit Tags — {tracks[0].title}"
        )
        self.setMinimumWidth(400)

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        if self._multi:
            note = QLabel(
                f"Editing {len(self._tracks)} tracks. "
                "Fields left unchanged will not be modified."
            )
            note.setWordWrap(True)
            note.setStyleSheet("color: #888; margin-bottom: 6px;")
            layout.addWidget(note)

        form = QFormLayout()

        for field, label in _FIELDS:
            edit = QLineEdit()
            value = self._get_field_value(field)
            self._original[field] = value

            if value == _MIXED:
                edit.setPlaceholderText("<mixed>")
            else:
                edit.setText(value)

            self._edits[field] = edit
            form.addRow(label, edit)

        layout.addLayout(form)

        # Info row (read-only).
        if not self._multi:
            t = self._tracks[0]
            info = QLabel(
                f"Format: {t.format}  |  "
                f"Bitrate: {t.bitrate // 1000}kbps  |  "
                f"Path: {t.filepath}"
            )
            info.setWordWrap(True)
            info.setStyleSheet("color: #666; font-size: 10px; margin-top: 8px;")
            layout.addWidget(info)

        # Buttons.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _get_field_value(self, field: str) -> str:
        """Get the value for a field across all tracks.

        Returns the common value, or _MIXED if they differ.
        """
        values = set()
        for t in self._tracks:
            val = self._track_field(t, field)
            values.add(val)
        if len(values) == 1:
            return values.pop()
        return _MIXED

    @staticmethod
    def _track_field(track: Track, field: str) -> str:
        """Get a string representation of a track field."""
        val = getattr(track, field, None)
        if val is None:
            return ""
        return str(val)

    def _save(self) -> None:
        """Write only changed fields to files and update the database."""
        # Determine which fields changed.
        changes: dict[str, str] = {}
        for field, _label in _FIELDS:
            edit = self._edits[field]
            current = edit.text()
            original = self._original[field]

            # Skip if unchanged (or if mixed and left blank/untouched).
            if original == _MIXED:
                if current == "" and edit.placeholderText() == "<mixed>":
                    continue  # User didn't touch it.
                if current == "":
                    continue
            elif current == original:
                continue

            changes[field] = current

        if not changes:
            self.accept()
            return

        # Write to files.
        errors: list[str] = []
        for track in self._tracks:
            ok = write_tags(track.filepath, changes)
            if not ok:
                errors.append(track.filepath)
            else:
                # Re-read tags and update the database.
                new_tags = read_tags(track.filepath)
                if new_tags:
                    self._db.upsert_track(new_tags)

        if errors:
            QMessageBox.warning(
                self,
                "Tag Write Errors",
                f"Failed to write tags to {len(errors)} file(s):\n"
                + "\n".join(errors[:5]),
            )

        self.accept()
