"""Dialog for managing named library tabs and their source assignments.

Tabs organize sources for display. Source configuration (scraper, scan mode,
media type) is handled in the Sources dialog — this dialog only assigns
existing sources to tabs.
"""

from __future__ import annotations

import logging
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
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

from quaver.core.config import load_config, save_config
from quaver.core.library import FOLDER_TYPES, LibraryDB
from quaver.ui.dialogs.sources import TYPE_LABELS

log = logging.getLogger(__name__)


class LibraryTabsDialog(QDialog):
    """Configure named library tabs — assign sources for display grouping."""

    rescan_requested = Signal()

    def __init__(self, db: LibraryDB, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Configure Library Tabs")
        self.setMinimumSize(600, 400)

        self._config = load_config()
        self._build_ui()
        self._refresh_tab_list()

    def _build_ui(self) -> None:
        main = QHBoxLayout(self)

        # Left: tab list.
        left = QVBoxLayout()
        left.addWidget(QLabel("Library Tabs"))
        self._tab_list = QListWidget()
        self._tab_list.currentRowChanged.connect(self._on_tab_selected)
        left.addWidget(self._tab_list)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Add Tab")
        btn_add.clicked.connect(self._add_tab)
        btn_remove = QPushButton("Remove Tab")
        btn_remove.clicked.connect(self._remove_tab)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_remove)
        left.addLayout(btn_row)
        main.addLayout(left)

        # Right: tab details.
        right = QVBoxLayout()
        right.addWidget(QLabel("Tab Name"))
        self._name_edit = QLineEdit()
        self._name_edit.textChanged.connect(self._on_name_changed)
        right.addWidget(self._name_edit)

        # Type selector.
        right.addWidget(QLabel("Content Type"))
        self._type_combo = QComboBox()
        for ft in FOLDER_TYPES:
            self._type_combo.addItem(TYPE_LABELS.get(ft, ft), ft)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        right.addWidget(self._type_combo)

        # Source assignment list.
        right.addWidget(QLabel("Assigned Sources"))
        self._source_list = QListWidget()
        right.addWidget(self._source_list)

        source_btns = QHBoxLayout()
        btn_assign = QPushButton("Assign Source…")
        btn_assign.clicked.connect(self._assign_source)
        btn_unassign = QPushButton("Unassign")
        btn_unassign.clicked.connect(self._unassign_source)
        source_btns.addWidget(btn_assign)
        source_btns.addWidget(btn_unassign)
        right.addLayout(source_btns)

        # ── Files-tab specific options ───────────────────────────────
        # Visible only when the selected tab's type == "files".  The
        # checkbox controls whether the file grid shows extensions
        # (``my movie.mp4``) or just stems (``my movie``).
        self._hide_ext_cb = QCheckBox("Hide file extensions in display")
        self._hide_ext_cb.toggled.connect(self._on_hide_ext_toggled)
        right.addWidget(self._hide_ext_cb)
        self._hide_ext_cb.setVisible(False)

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

        main.addLayout(right)

        # Disable right panel until a tab is selected.
        self._set_detail_enabled(False)

    def _set_detail_enabled(self, enabled: bool) -> None:
        self._name_edit.setEnabled(enabled)
        self._type_combo.setEnabled(enabled)
        self._source_list.setEnabled(enabled)
        self._hide_ext_cb.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Tab list
    # ------------------------------------------------------------------

    def _refresh_tab_list(self) -> None:
        self._tab_list.clear()
        for tab in self._config.library_tabs:
            label = TYPE_LABELS.get(tab.type, "Music")
            self._tab_list.addItem(f"{tab.name}  [{label}]")
        if self._config.library_tabs:
            self._tab_list.setCurrentRow(0)

    def _on_tab_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._config.library_tabs):
            self._set_detail_enabled(False)
            self._name_edit.clear()
            self._source_list.clear()
            return

        self._set_detail_enabled(True)
        tab = self._config.library_tabs[row]

        self._name_edit.blockSignals(True)
        self._name_edit.setText(tab.name)
        self._name_edit.blockSignals(False)

        self._type_combo.blockSignals(True)
        idx = self._type_combo.findData(tab.type)
        self._type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._type_combo.blockSignals(False)

        # Files-tab specific row visibility.
        is_files = tab.type == "files"
        self._hide_ext_cb.setVisible(is_files)
        self._hide_ext_cb.blockSignals(True)
        self._hide_ext_cb.setChecked(tab.hide_extensions)
        self._hide_ext_cb.blockSignals(False)

        self._refresh_source_list(tab)

    def _refresh_source_list(self, tab) -> None:
        """Show sources assigned to this tab (display path/URI + tags)."""
        self._source_list.clear()
        src_by_path = {s.path: s for s in self._config.sources}
        for path in tab.source_paths:
            src = src_by_path.get(path)
            if src:
                display = src.uri or src.path
                tags: list[str] = []
                if src.scraper != "none":
                    tags.append(src.scraper.upper())
                if src.scan_mode != "multi":
                    tags.append(
                        "single show" if src.scan_mode == "single" else "deep scan"
                    )
                if tags:
                    display += f"  [{', '.join(tags)}]"
            else:
                display = f"{path}  [missing]"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self._source_list.addItem(item)

    def _on_name_changed(self, text: str) -> None:
        row = self._tab_list.currentRow()
        if 0 <= row < len(self._config.library_tabs):
            self._config.library_tabs[row].name = text
            label = TYPE_LABELS.get(self._config.library_tabs[row].type, "Music")
            self._tab_list.item(row).setText(f"{text}  [{label}]")

    def _on_type_changed(self, combo_idx: int) -> None:
        row = self._tab_list.currentRow()
        if row < 0 or row >= len(self._config.library_tabs):
            return
        new_type = self._type_combo.itemData(combo_idx)
        tab = self._config.library_tabs[row]

        if new_type != tab.type and tab.source_paths:
            reply = QMessageBox.question(
                self, "Change Content Type",
                "Changing the content type will remove all assigned sources.\n\n"
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._type_combo.blockSignals(True)
                self._type_combo.setCurrentIndex(
                    self._type_combo.findData(tab.type)
                )
                self._type_combo.blockSignals(False)
                return
            tab.source_paths.clear()
            self._source_list.clear()

        tab.type = new_type
        label = TYPE_LABELS.get(new_type, "Music")
        self._tab_list.item(row).setText(f"{tab.name}  [{label}]")
        # Show / hide the Files-only "hide extensions" checkbox to
        # match the new type.
        self._hide_ext_cb.setVisible(new_type == "files")

    def _on_hide_ext_toggled(self, checked: bool) -> None:
        row = self._tab_list.currentRow()
        if 0 <= row < len(self._config.library_tabs):
            self._config.library_tabs[row].hide_extensions = checked

    def _add_tab(self) -> None:
        from quaver.core.config import LibraryTabConfig
        tab = LibraryTabConfig(name="New Tab")
        self._config.library_tabs.append(tab)
        label = TYPE_LABELS.get(tab.type, "Music")
        self._tab_list.addItem(f"{tab.name}  [{label}]")
        self._tab_list.setCurrentRow(len(self._config.library_tabs) - 1)

    def _remove_tab(self) -> None:
        row = self._tab_list.currentRow()
        if 0 <= row < len(self._config.library_tabs):
            self._config.library_tabs.pop(row)
            self._refresh_tab_list()

    # ------------------------------------------------------------------
    # Source assignment
    # ------------------------------------------------------------------

    def _assign_source(self) -> None:
        """Pick from available sources whose media_type matches the tab's type."""
        row = self._tab_list.currentRow()
        if row < 0 or row >= len(self._config.library_tabs):
            return
        tab = self._config.library_tabs[row]

        # Find sources matching this tab's type that aren't already assigned.
        assigned = set(tab.source_paths)
        available = [
            s for s in self._config.sources
            if s.media_type == tab.type and s.path not in assigned
        ]

        if not available:
            QMessageBox.information(
                self, "No Sources Available",
                f"No unassigned {TYPE_LABELS.get(tab.type, tab.type)} sources available.\n\n"
                "Add new sources via Library \u203a Add Source\u2026",
            )
            return

        dlg = _SourcePickerDialog(available, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        for path in dlg.selected_paths():
            if path not in tab.source_paths:
                tab.source_paths.append(path)

        self._refresh_source_list(tab)

    def _unassign_source(self) -> None:
        """Remove the selected source from this tab (does NOT delete the source)."""
        row = self._tab_list.currentRow()
        src_row = self._source_list.currentRow()
        if row < 0 or src_row < 0:
            return
        tab = self._config.library_tabs[row]
        if src_row < len(tab.source_paths):
            tab.source_paths.pop(src_row)
            self._refresh_source_list(tab)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        save_config(self._config)
        self._db.sync_sources(self._config.sources)
        self.accept()


# ======================================================================
# Source picker — select from available sources to assign to a tab
# ======================================================================

class _SourcePickerDialog(QDialog):
    """Pick sources to assign to a tab."""

    def __init__(
        self, sources: list, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Assign Sources")
        self.setMinimumWidth(450)
        self._sources = sources
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select sources to assign to this tab:"))

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        for src in self._sources:
            display = src.uri or src.path
            tags: list[str] = []
            if src.scraper != "none":
                tags.append(src.scraper.upper())
            if tags:
                display += f"  [{', '.join(tags)}]"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, src.path)
            self._list.addItem(item)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_ok = QPushButton("Assign")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def selected_paths(self) -> list[str]:
        paths = []
        for item in self._list.selectedItems():
            path = item.data(Qt.ItemDataRole.UserRole)
            if path:
                paths.append(path)
        return paths
