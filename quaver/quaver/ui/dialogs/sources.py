"""Standalone source management dialog — the primary place to add, edit,
and remove scan sources.  Each source is a folder (local or network) with
its own media type, scraper, and scan mode.

Menu > Library > Add Source… opens this dialog.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from quaver.core.config import (
    AppConfig,
    LibraryTabConfig,
    SourceConfig,
    load_config,
    save_config,
)
from quaver.core.library import LibraryDB

log = logging.getLogger(__name__)

# Shared label constants — also imported by library_tabs_dialog.
TYPE_LABELS = {
    "music": "Music",
    "series": "Series",
    "movies": "Movies",
    "files": "Files",
}
TYPE_OPTIONS = ("music", "series", "movies", "files")

SCRAPER_OPTIONS = ("none", "tvmaze", "tmdb")
SCRAPER_LABELS = {
    "none": "None",
    "tvmaze": "TVMaze (no key required)",
    "tmdb": "TMDb (requires API key)",
}

SCAN_MODE_LABELS = {
    "multi": "Each subfolder is a different show",
    "single": "This folder contains a single show",
    "deep": "Scan recursively for shows",
}


# XDG user-dirs surfaced first in the file-dialog sidebar.  These are
# the locations users actually keep media in (Music, Downloads, Videos)
# — Qt's non-native dialog doesn't include them by default, and the
# previous build_sidebar_urls() only listed mount points like /mnt and
# /media, leaving users no way to navigate to ~/Music or ~/Downloads.
_USER_DIR_LOCATIONS = (
    QStandardPaths.StandardLocation.HomeLocation,
    QStandardPaths.StandardLocation.MusicLocation,
    QStandardPaths.StandardLocation.MoviesLocation,
    QStandardPaths.StandardLocation.DownloadLocation,
    QStandardPaths.StandardLocation.DocumentsLocation,
    QStandardPaths.StandardLocation.PicturesLocation,
    QStandardPaths.StandardLocation.DesktopLocation,
)


def _user_dir_urls() -> list[QUrl]:
    """Pick up the user's XDG Music/Downloads/Videos/etc. directories.

    Returns absolute file:// URLs for every standard user dir that
    actually resolves to an existing directory.  De-duplicated in the
    order listed above, so Home appears first.
    """
    seen: set[str] = set()
    out: list[QUrl] = []
    for loc in _USER_DIR_LOCATIONS:
        for raw in QStandardPaths.standardLocations(loc):
            p = Path(raw)
            key = str(p)
            if key in seen or not p.is_dir():
                continue
            seen.add(key)
            out.append(QUrl.fromLocalFile(key))
    return out


def build_sidebar_urls() -> list[QUrl]:
    """Build sidebar URLs for file dialogs.

    Order: XDG user dirs (Home, Music, Videos, Downloads, …) first so
    users land on familiar locations, then drive/mount roots and their
    immediate children.  Both halves are de-duped against each other.
    """
    seen_paths: set[str] = set()
    sidebar: list[QUrl] = []

    def _add(url: QUrl) -> None:
        key = url.toLocalFile()
        if key and key not in seen_paths:
            seen_paths.add(key)
            sidebar.append(url)

    for url in _user_dir_urls():
        _add(url)

    if sys.platform == "linux":
        import os
        user = os.environ.get("USER", "")
        uid = str(os.getuid())
        local_roots = [
            "/mnt", "/media", "/run/media", f"/run/media/{user}",
            f"/run/user/{uid}/gvfs",
        ]
        for mount_root in local_roots:
            root = Path(mount_root)
            if not root.is_dir():
                continue
            _add(QUrl.fromLocalFile(str(root)))
            try:
                for child in root.iterdir():
                    if child.is_dir():
                        _add(QUrl.fromLocalFile(str(child)))
            except PermissionError:
                pass
    elif sys.platform == "win32":
        from PySide6.QtCore import QDir
        for fi in QDir.drives():
            _add(QUrl.fromLocalFile(fi.absoluteFilePath()))
    elif sys.platform == "darwin":
        volumes = Path("/Volumes")
        if volumes.is_dir():
            _add(QUrl.fromLocalFile(str(volumes)))
            try:
                for child in volumes.iterdir():
                    if child.is_dir():
                        _add(QUrl.fromLocalFile(str(child)))
            except PermissionError:
                pass
    return sidebar


def _default_dialog_start_dir() -> str:
    """Where the file picker should open before the user navigates.

    Returns the home directory if it exists; otherwise an empty string
    (Qt falls back to its own default).  Without this, the dialog can
    open in cwd or last-used path, leaving the user nowhere obvious.
    """
    home = QStandardPaths.standardLocations(
        QStandardPaths.StandardLocation.HomeLocation
    )
    return home[0] if home else ""


# ======================================================================
# Sources dialog
# ======================================================================

class SourcesDialog(QDialog):
    """Manage scan sources — add, edit, remove, configure."""

    sources_changed = Signal()

    def __init__(self, db: LibraryDB, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Manage Sources")
        self.setMinimumSize(700, 480)

        self._config = load_config()
        self._build_ui()
        self._refresh_list()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        main = QHBoxLayout(self)

        # Left: source list.
        left = QVBoxLayout()
        left.addWidget(QLabel("Sources"))
        self._source_list = QListWidget()
        self._source_list.currentRowChanged.connect(self._on_source_selected)
        left.addWidget(self._source_list)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Add Local…")
        btn_add.clicked.connect(self._add_local)
        btn_network = QPushButton("Add Network…")
        btn_network.clicked.connect(self._add_network)
        btn_remove = QPushButton("Remove")
        btn_remove.clicked.connect(self._remove_source)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_network)
        btn_row.addWidget(btn_remove)
        left.addLayout(btn_row)
        main.addLayout(left, stretch=1)

        # Right: source configuration (inline).
        right = QVBoxLayout()
        right.addWidget(QLabel("Source Settings"))

        form = QFormLayout()

        # Path (read-only display).
        self._path_label = QLabel("")
        self._path_label.setWordWrap(True)
        self._path_label.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 11px;")
        form.addRow("Path:", self._path_label)

        # Media type.
        self._type_combo = QComboBox()
        for t in TYPE_OPTIONS:
            self._type_combo.addItem(TYPE_LABELS[t], t)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Media Type:", self._type_combo)

        # Scraper.
        self._scraper_combo = QComboBox()
        for key in SCRAPER_OPTIONS:
            self._scraper_combo.addItem(SCRAPER_LABELS[key], key)
        self._scraper_combo.currentIndexChanged.connect(self._on_config_changed)
        form.addRow("Metadata Source:", self._scraper_combo)

        # Scraper note.
        self._scraper_note = QLabel("")
        self._scraper_note.setStyleSheet(
            "color: rgba(255,200,80,0.8); font-size: 11px;"
        )
        self._scraper_note.setWordWrap(True)
        self._scraper_note.setVisible(False)
        form.addRow("", self._scraper_note)

        # Scan mode.
        self._scan_mode_combo = QComboBox()
        for mode, label in SCAN_MODE_LABELS.items():
            self._scan_mode_combo.addItem(label, mode)
        self._scan_mode_combo.currentIndexChanged.connect(self._on_config_changed)
        self._scan_mode_label = QLabel("Folder Structure:")
        form.addRow(self._scan_mode_label, self._scan_mode_combo)

        right.addLayout(form)
        right.addStretch()

        # Save / Cancel.
        action_btns = QHBoxLayout()
        action_btns.addStretch()
        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self._save)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        action_btns.addWidget(btn_save)
        action_btns.addWidget(btn_cancel)
        right.addLayout(action_btns)

        main.addLayout(right, stretch=1)

        # Start with config panel disabled.
        self._set_config_enabled(False)

    def _set_config_enabled(self, enabled: bool) -> None:
        self._type_combo.setEnabled(enabled)
        self._scraper_combo.setEnabled(enabled)
        self._scan_mode_combo.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Source list
    # ------------------------------------------------------------------

    def _refresh_list(self) -> None:
        self._source_list.clear()
        for src in self._config.sources:
            display = src.uri or src.path
            tags: list[str] = [TYPE_LABELS.get(src.media_type, src.media_type)]
            if src.scraper != "none":
                tags.append(src.scraper.upper())
            if src.scan_mode != "multi":
                tags.append(
                    "single show" if src.scan_mode == "single" else "deep scan"
                )
            display += f"  [{', '.join(tags)}]"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, src.path)
            self._source_list.addItem(item)

    def _on_source_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._config.sources):
            self._set_config_enabled(False)
            self._path_label.clear()
            self._scraper_note.setVisible(False)
            return

        self._set_config_enabled(True)
        src = self._config.sources[row]

        self._path_label.setText(src.uri or src.path)

        self._type_combo.blockSignals(True)
        idx = self._type_combo.findData(src.media_type)
        self._type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._type_combo.blockSignals(False)

        self._scraper_combo.blockSignals(True)
        idx = self._scraper_combo.findData(src.scraper)
        self._scraper_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._scraper_combo.blockSignals(False)

        self._scan_mode_combo.blockSignals(True)
        idx = self._scan_mode_combo.findData(src.scan_mode)
        self._scan_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._scan_mode_combo.blockSignals(False)

        self._update_visibility()

    # ------------------------------------------------------------------
    # Config changes (inline)
    # ------------------------------------------------------------------

    def _on_type_changed(self) -> None:
        row = self._source_list.currentRow()
        if row < 0 or row >= len(self._config.sources):
            return
        src = self._config.sources[row]
        src.media_type = self._type_combo.currentData() or "music"
        # Reset scraper to sensible default when type changes.
        if src.media_type == "series":
            src.scraper = "tvmaze"
        elif src.media_type == "movies":
            src.scraper = "tmdb"
        else:
            # music + files — no scraper, no scan mode.
            src.scraper = "none"
        self._scraper_combo.blockSignals(True)
        idx = self._scraper_combo.findData(src.scraper)
        self._scraper_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._scraper_combo.blockSignals(False)
        self._update_visibility()
        self._refresh_list()
        self._source_list.setCurrentRow(row)

    def _on_config_changed(self) -> None:
        row = self._source_list.currentRow()
        if row < 0 or row >= len(self._config.sources):
            return
        src = self._config.sources[row]
        src.scraper = self._scraper_combo.currentData() or "none"
        src.scan_mode = self._scan_mode_combo.currentData() or "multi"
        self._update_visibility()
        self._refresh_list()
        self._source_list.setCurrentRow(row)

    def _update_visibility(self) -> None:
        """Show/hide scraper + scan mode based on media type, update note."""
        row = self._source_list.currentRow()
        media_type = "music"
        if 0 <= row < len(self._config.sources):
            media_type = self._config.sources[row].media_type
        # ``files`` sources are pure live filesystem trees \u2014 no scraper,
        # no scan mode, no scraper note.
        is_files = media_type == "files"
        self._scraper_combo.setVisible(not is_files)
        self._scan_mode_label.setVisible(media_type == "series")
        self._scan_mode_combo.setVisible(media_type == "series")

        if is_files:
            self._scraper_note.setVisible(False)
            return

        # Scraper note (non-files types).
        scraper = self._scraper_combo.currentData() or "none"
        if scraper == "none" and media_type in ("series", "movies"):
            self._scraper_note.setText(
                "\u26a0 No metadata source \u2014 episode titles, posters, "
                "and plot summaries will not be fetched."
            )
            self._scraper_note.setVisible(True)
        elif scraper == "tmdb" and not self._config.tmdb_api_key:
            self._scraper_note.setText(
                "\u26a0 TMDb requires an API key. Set it in Preferences \u203a Video."
            )
            self._scraper_note.setVisible(True)
        else:
            self._scraper_note.setVisible(False)

    # ------------------------------------------------------------------
    # Add / Remove
    # ------------------------------------------------------------------

    def _add_local(self) -> None:
        # Qt's non-native dialog is required for setSidebarUrls() to
        # actually take effect; the native KDE/GNOME dialog has its own
        # places sidebar that ignores Qt's hint.  We force non-native
        # so users see Music / Downloads / Videos as one-click entries
        # regardless of platform.
        dlg = QFileDialog(self, "Select Folder")
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.setFileMode(QFileDialog.FileMode.Directory)
        dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dlg.setSidebarUrls(build_sidebar_urls())
        start = _default_dialog_start_dir()
        if start:
            dlg.setDirectory(start)
        if dlg.exec() != QFileDialog.DialogCode.Accepted:
            return
        paths = dlg.selectedFiles()
        if not paths:
            return
        folder = paths[0]
        if self._path_exists(folder):
            return
        self._add_source(folder, uri="")

    def _add_network(self) -> None:
        dlg = NetworkLocationDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        uri = dlg.uri()

        from quaver.core.network import ensure_mounted, uri_to_local_path

        self.setCursor(Qt.CursorShape.WaitCursor)
        ok, err = ensure_mounted(uri)
        self.unsetCursor()
        if not ok:
            box = QMessageBox(
                QMessageBox.Icon.Warning, "Mount Failed", err, parent=self,
            )
            box.setMinimumWidth(450)
            box.exec()
            return

        local_path = uri_to_local_path(uri)
        if not local_path or not Path(local_path).is_dir():
            QMessageBox.warning(
                self, "Path Not Found",
                f"Mounted but local path not found:\n{local_path or '(unknown)'}",
            )
            return
        if self._path_exists(local_path):
            return
        self._add_source(local_path, uri=uri)

    def _add_source(self, path: str, uri: str) -> None:
        """Create a new source with defaults, add it, then ask for tab assignment."""
        src = SourceConfig(path=path, uri=uri)
        self._config.sources.append(src)
        self._refresh_list()
        # Select the new source so the user can configure it.
        self._source_list.setCurrentRow(len(self._config.sources) - 1)

    def _remove_source(self) -> None:
        row = self._source_list.currentRow()
        if row < 0 or row >= len(self._config.sources):
            return
        src = self._config.sources[row]
        # Remove from all tabs' source_paths.
        for tab in self._config.library_tabs:
            if src.path in tab.source_paths:
                tab.source_paths.remove(src.path)
        self._config.sources.pop(row)
        self._refresh_list()

    def _path_exists(self, path: str) -> bool:
        if any(s.path == path for s in self._config.sources):
            QMessageBox.information(
                self, "Already Added",
                "This folder is already configured as a source.",
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        # Ask for tab assignment for any sources not yet in a tab.  With no
        # tabs configured, the default Library tab shows every source, so
        # there is nothing to ask.
        unassigned = []
        all_assigned_paths = set()
        for tab in self._config.library_tabs:
            all_assigned_paths.update(tab.source_paths)
        for src in self._config.sources:
            if src.path not in all_assigned_paths:
                unassigned.append(src)

        if unassigned and self._config.library_tabs:
            dlg = _TabAssignmentDialog(
                unassigned, self._config.library_tabs, parent=self,
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                dlg.apply_assignments()

        save_config(self._config)
        self._db.sync_sources(self._config.sources)
        self.accept()


# ======================================================================
# Tab assignment picker — shown after adding sources
# ======================================================================

class _TabAssignmentDialog(QDialog):
    """Assign unassigned sources to library tabs."""

    def __init__(
        self,
        sources: list[SourceConfig],
        tabs: list[LibraryTabConfig],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Assign Sources to Tabs")
        self.setMinimumWidth(450)
        self._sources = sources
        self._tabs = tabs
        self._checkboxes: list[list[QCheckBox]] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "The following sources are not assigned to any tab.\n"
            "Select which tab(s) each source should appear in:"
        ))

        for src in self._sources:
            display = src.uri or src.path
            type_label = TYPE_LABELS.get(src.media_type, src.media_type)
            layout.addWidget(QLabel(f"\n{display}  [{type_label}]"))

            matching_tabs = [
                t for t in self._tabs if t.type == src.media_type
            ]
            src_checks: list[QCheckBox] = []
            if matching_tabs:
                for tab in matching_tabs:
                    cb = QCheckBox(tab.name)
                    cb.setProperty("tab_ref", tab)
                    cb.setProperty("src_path", src.path)
                    # Auto-check if there's only one matching tab.
                    if len(matching_tabs) == 1:
                        cb.setChecked(True)
                    layout.addWidget(cb)
                    src_checks.append(cb)
            else:
                layout.addWidget(QLabel(
                    f"  (No {type_label} tab exists — create one in Configure Library Tabs)"
                ))
            self._checkboxes.append(src_checks)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_ok = QPushButton("OK")
        btn_ok.clicked.connect(self.accept)
        btn_skip = QPushButton("Skip")
        btn_skip.clicked.connect(self.reject)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_skip)
        layout.addLayout(btn_row)

    def apply_assignments(self) -> None:
        """Add source paths to the checked tabs."""
        for src_checks in self._checkboxes:
            for cb in src_checks:
                if cb.isChecked():
                    tab = cb.property("tab_ref")
                    path = cb.property("src_path")
                    if path not in tab.source_paths:
                        tab.source_paths.append(path)


# ======================================================================
# Network location input dialog (moved from library_tabs_dialog.py)
# ======================================================================

class NetworkLocationDialog(QDialog):
    """Small input dialog for adding an SMB network share."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Network Location")
        self.setMinimumWidth(400)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._uri_edit = QLineEdit()
        self._uri_edit.setPlaceholderText("smb://server/share/path")
        form.addRow("URI:", self._uri_edit)
        layout.addLayout(form)

        hint = QLabel(
            "Enter an SMB share URI. The share will be mounted via KIO FUSE."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_ok = QPushButton("Mount && Add")
        btn_ok.clicked.connect(self._on_accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def _on_accept(self) -> None:
        uri = self._uri_edit.text().strip()
        if not uri.startswith("smb://"):
            QMessageBox.warning(
                self, "Invalid URI", "URI must start with smb://"
            )
            return
        self.accept()

    def uri(self) -> str:
        return self._uri_edit.text().strip()
