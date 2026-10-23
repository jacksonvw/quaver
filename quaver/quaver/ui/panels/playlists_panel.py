"""Playlists panel — saved playlist management.

Left: playlist list with create/rename/delete.
Right: playlist header + track table with reorder/remove/play.
"""

from __future__ import annotations

import json

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtCore import QMimeData
from quaver.core.library import PLAYLIST_IDS_MIME, TRACK_IDS_MIME, Track
from quaver.ui.widgets.delegates import (
    DropIndicatorMixin,
    HoverTrackingMixin,
    SimpleItemDelegate,
    TableSelectionMixin,
)


def _ms_to_hhmmss(ms: int) -> str:
    total_secs = ms // 1000
    h, remainder = divmod(total_secs, 3600)
    m, s = divmod(remainder, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _ms_to_mmss(ms: int) -> str:
    total_secs = ms // 1000
    m, s = divmod(total_secs, 60)
    return f"{m}:{s:02d}"


# ── Track table model ────────────────────────────────────────────────

_COL_NUM = 0
_COL_TITLE = 1
_COL_ARTIST = 2
_COL_ALBUM = 3
_COL_DURATION = 4
_N_COLS = 5

_ROLE_TRACK = Qt.ItemDataRole.UserRole + 1
_ROLE_POSITION = Qt.ItemDataRole.UserRole + 2


_INTERNAL_REORDER_MIME = "application/x-quaver-playlist-reorder"


class _PlaylistTrackModel(QAbstractTableModel):
    """Table model for tracks in a saved playlist."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks: list[Track] = []

    def load(self, tracks: list[Track]) -> None:
        self.beginResetModel()
        self._tracks = list(tracks)
        self.endResetModel()

    def track_at(self, row: int) -> Track | None:
        if 0 <= row < len(self._tracks):
            return self._tracks[row]
        return None

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    def move_row(self, from_row: int, to_row: int) -> None:
        """Move a track within the model (for drag reorder)."""
        if from_row == to_row or not (0 <= from_row < len(self._tracks)):
            return
        to_row = max(0, min(to_row, len(self._tracks) - 1))
        self.beginResetModel()
        track = self._tracks.pop(from_row)
        self._tracks.insert(to_row, track)
        self.endResetModel()

    def move_rows_batch(self, source_rows: list[int], dest_row: int) -> None:
        """Move multiple tracks to a destination as a contiguous block."""
        rows = sorted(source_rows)
        if not rows or any(r < 0 or r >= len(self._tracks) for r in rows):
            return
        self.beginResetModel()
        moved = [self._tracks[r] for r in rows]
        for r in reversed(rows):
            self._tracks.pop(r)
        adjusted = dest_row - sum(1 for r in rows if r < dest_row)
        adjusted = max(0, min(adjusted, len(self._tracks)))
        for i, track in enumerate(moved):
            self._tracks.insert(adjusted + i, track)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._tracks)

    def columnCount(self, parent=QModelIndex()) -> int:
        return _N_COLS

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ["#", "Title", "Artist", "Album", "Duration"][section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row >= len(self._tracks):
            return None
        t = self._tracks[row]
        col = index.column()

        if role == _ROLE_TRACK:
            return t
        if role == _ROLE_POSITION:
            return row

        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_NUM:
                return str(row + 1)
            if col == _COL_TITLE:
                return t.title or "Unknown"
            if col == _COL_ARTIST:
                return t.artist or ""
            if col == _COL_ALBUM:
                return t.album or ""
            if col == _COL_DURATION:
                return _ms_to_mmss(t.duration_ms) if t.duration_ms else ""

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (_COL_NUM, _COL_DURATION):
                return int(Qt.AlignmentFlag.AlignCenter)

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        return base | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def mimeTypes(self) -> list[str]:
        return [_INTERNAL_REORDER_MIME, TRACK_IDS_MIME]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        rows = sorted({idx.row() for idx in indexes if idx.isValid()})
        ids = [self._tracks[r].id for r in rows if r < len(self._tracks)]
        mime = QMimeData()
        mime.setData(TRACK_IDS_MIME, json.dumps(ids).encode())
        mime.setData(_INTERNAL_REORDER_MIME, json.dumps(rows).encode())
        return mime


# ── Playlist track table view (supports internal reorder + drag out) ─


class _PlaylistTableView(DropIndicatorMixin, TableSelectionMixin, HoverTrackingMixin, QTableView):
    """QTableView with consistent selection/hover and internal reorder."""

    reordered = Signal(int, int)        # from_row, to_row
    batch_reordered = Signal(list, int)  # source_rows, dest_row

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_hover_tracking()

    def startDrag(self, supportedActions) -> None:
        """Lightweight QDrag to avoid compositor blanking with translucent windows."""
        from PySide6.QtGui import QColor, QDrag, QPainter, QPixmap
        indexes = self.selectedIndexes()
        if not indexes:
            return
        mime = self.model().mimeData(indexes)
        if mime is None:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        rows = sorted({idx.row() for idx in indexes})
        hint = QPixmap(120, 24)
        hint.fill(QColor(255, 255, 255, 40))
        p = QPainter(hint)
        p.setPen(QColor(255, 255, 255, 180))
        p.drawText(hint.rect(), Qt.AlignmentFlag.AlignCenter, f"{len(rows)} track(s)")
        p.end()
        drag.setPixmap(hint)
        drag.exec(supportedActions)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_row >= 0:
            from PySide6.QtGui import QPainter
            p = QPainter(self.viewport())
            self._paint_drop_indicator(p)
            p.end()

    def dropEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(_INTERNAL_REORDER_MIME):
            rows = json.loads(bytes(mime.data(_INTERNAL_REORDER_MIME)).decode())
            if rows:
                drop_index = self.indexAt(event.position().toPoint())
                to_row = drop_index.row() if drop_index.isValid() else self.model().rowCount()
                if len(rows) == 1:
                    self.reordered.emit(rows[0], to_row)
                else:
                    self.batch_reordered.emit(rows, to_row)
                event.acceptProposedAction()
                self._reset_drop_indicator()
                return
        self._reset_drop_indicator()
        super().dropEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_INTERNAL_REORDER_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)


# ── Draggable playlist list ──────────────────────────────────────────


class _PlaylistListWidget(HoverTrackingMixin, QListWidget):
    """QListWidget that provides PLAYLIST_IDS_MIME for drag-to-queue."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_hover_tracking()
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(SimpleItemDelegate(self))

    def mimeTypes(self) -> list[str]:
        return [PLAYLIST_IDS_MIME]

    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:
        ids = []
        for item in items:
            pid = item.data(Qt.ItemDataRole.UserRole)
            if pid is not None:
                ids.append(pid)
        mime = QMimeData()
        mime.setData(PLAYLIST_IDS_MIME, json.dumps(ids).encode())
        return mime


# ── Panel ────────────────────────────────────────────────────────────


class PlaylistsPanel(QWidget):
    """Saved playlists management panel."""

    play_playlist = Signal(int, int)  # playlist_id, start_index
    add_to_queue = Signal(int)        # playlist_id — append all tracks to queue
    add_tracks_to_queue = Signal(list) # individual tracks to append to queue
    track_moved = Signal(int, int, int)      # playlist_id, from_pos, to_pos
    tracks_reordered = Signal(int, list)      # playlist_id, track_ids (new order)
    playlist_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._playlists: list[tuple[int, str, int]] = []  # (id, name, count)
        self._current_playlist_id: int | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: playlist list.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._header = QLabel("Playlists")
        self._header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._header.customContextMenuRequested.connect(self._header_context_menu)
        left_layout.addWidget(self._header)

        self._list = _PlaylistListWidget()
        self._list.currentRowChanged.connect(self._on_playlist_selected)
        self._list.doubleClicked.connect(self._on_playlist_double_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._list_context_menu)
        self._list.setDragEnabled(True)
        left_layout.addWidget(self._list)
        self._splitter.addWidget(left)

        # Right: playlist content.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Header.
        self._header_name = QLabel("")
        self._header_name.setStyleSheet("font-size: 16px; font-weight: bold;")
        self._header_stats = QLabel("")
        self._header_stats.setStyleSheet("font-size: 11px; color: #888;")
        right_layout.addWidget(self._header_name)
        right_layout.addWidget(self._header_stats)

        # Track table.
        self._model = _PlaylistTrackModel(self)
        self._table = _PlaylistTableView()
        self._table.setModel(self._model)
        self._table.setItemDelegate(SimpleItemDelegate(self._table))
        self._table.reordered.connect(self._on_track_reordered)
        self._table.batch_reordered.connect(self._on_track_batch_reordered)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setDragEnabled(True)
        self._table.setAcceptDrops(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self._table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._table.viewport().setAcceptDrops(True)

        h = self._table.horizontalHeader()
        h.setSectionResizeMode(_COL_NUM, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_COL_TITLE, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(_COL_ARTIST, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(_COL_ALBUM, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(_COL_DURATION, QHeaderView.ResizeMode.ResizeToContents)

        self._table.doubleClicked.connect(self._on_track_double_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._track_context_menu)

        right_layout.addWidget(self._table)
        self._splitter.addWidget(right)

        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 3)
        layout.addWidget(self._splitter)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_playlists(self, playlists: list[tuple[int, str, int]]) -> None:
        """Load the playlist list. Each entry is (id, name, track_count)."""
        self._playlists = playlists
        self._list.clear()
        for pid, name, count in playlists:
            item = QListWidgetItem(f"{name}  ({count})")
            item.setData(Qt.ItemDataRole.UserRole, pid)
            self._list.addItem(item)

    def load_playlist_tracks(self, tracks: list[Track]) -> None:
        """Load tracks for the currently selected playlist."""
        self._model.load(tracks)
        self._update_header(tracks)

    def _update_header(self, tracks: list[Track]) -> None:
        if self._current_playlist_id is None:
            self._header_name.setText("")
            self._header_stats.setText("")
            return

        # Find playlist name.
        name = ""
        for pid, pname, _ in self._playlists:
            if pid == self._current_playlist_id:
                name = pname
                break
        self._header_name.setText(name)

        n = len(tracks)
        albums = len({t.album for t in tracks if t.album})
        total_ms = sum(t.duration_ms or 0 for t in tracks)
        dur = _ms_to_hhmmss(total_ms)
        self._header_stats.setText(f"{n} tracks / {albums} albums, duration: {dur}")

    # ------------------------------------------------------------------
    # Playlist list actions
    # ------------------------------------------------------------------

    def _header_context_menu(self, pos) -> None:
        menu = QMenu(self)
        new_action = QAction("New Playlist", self)
        new_action.triggered.connect(self._on_new_playlist)
        menu.addAction(new_action)
        menu.exec(self._header.mapToGlobal(pos))

    def _on_new_playlist(self) -> None:
        name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:")
        if ok and name.strip():
            self._new_playlist_name = name.strip()
            self.playlist_changed.emit()

    @property
    def pending_new_playlist(self) -> str | None:
        name = getattr(self, "_new_playlist_name", None)
        self._new_playlist_name = None
        return name

    def _on_playlist_selected(self, row: int) -> None:
        if 0 <= row < len(self._playlists):
            self._current_playlist_id = self._playlists[row][0]
        else:
            self._current_playlist_id = None
            self._model.load([])
            self._update_header([])

    def _on_playlist_double_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        if 0 <= row < len(self._playlists):
            pid = self._playlists[row][0]
            self.play_playlist.emit(pid, 0)

    @property
    def current_playlist_id(self) -> int | None:
        return self._current_playlist_id

    def _list_context_menu(self, pos) -> None:
        index = self._list.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        if row >= len(self._playlists):
            return
        pid, name, _ = self._playlists[row]

        menu = QMenu(self)

        play_action = QAction("Play", self)
        play_action.triggered.connect(lambda: self.play_playlist.emit(pid, 0))
        menu.addAction(play_action)

        queue_action = QAction("Add to Queue", self)
        queue_action.triggered.connect(lambda: self.add_to_queue.emit(pid))
        menu.addAction(queue_action)

        menu.addSeparator()

        rename_action = QAction("Rename", self)
        rename_action.triggered.connect(lambda: self._on_rename_playlist(pid, name))
        menu.addAction(rename_action)

        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self._on_delete_playlist(pid, name))
        menu.addAction(delete_action)

        menu.exec(self._list.viewport().mapToGlobal(pos))

    def _on_rename_playlist(self, playlist_id: int, current_name: str) -> None:
        name, ok = QInputDialog.getText(
            self, "Rename Playlist", "New name:", text=current_name
        )
        if ok and name.strip():
            self._rename_request = (playlist_id, name.strip())
            self.playlist_changed.emit()

    @property
    def pending_rename(self) -> tuple[int, str] | None:
        req = getattr(self, "_rename_request", None)
        self._rename_request = None
        return req

    def _on_delete_playlist(self, playlist_id: int, name: str) -> None:
        reply = QMessageBox.question(
            self, "Delete Playlist",
            f"Delete playlist '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._delete_request = playlist_id
            self.playlist_changed.emit()

    @property
    def pending_delete(self) -> int | None:
        req = getattr(self, "_delete_request", None)
        self._delete_request = None
        return req

    # ------------------------------------------------------------------
    # Track table actions
    # ------------------------------------------------------------------

    def _on_track_double_clicked(self, index: QModelIndex) -> None:
        if self._current_playlist_id is not None and index.isValid():
            self.play_playlist.emit(self._current_playlist_id, index.row())

    def _track_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid() or self._current_playlist_id is None:
            return
        row = index.row()

        menu = QMenu(self)

        play_action = QAction("Play from Here", self)
        play_action.triggered.connect(
            lambda: self.play_playlist.emit(self._current_playlist_id, row)
        )
        menu.addAction(play_action)

        queue_action = QAction("Add to Queue", self)
        queue_action.triggered.connect(lambda: self._emit_selected_to_queue())
        menu.addAction(queue_action)

        menu.addSeparator()

        remove_action = QAction("Remove from Playlist", self)
        remove_action.triggered.connect(lambda: self._request_remove(row))
        menu.addAction(remove_action)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _request_remove(self, position: int) -> None:
        self._remove_request = position
        self.playlist_changed.emit()

    @property
    def pending_remove(self) -> int | None:
        req = getattr(self, "_remove_request", None)
        self._remove_request = None
        return req

    def _on_track_reordered(self, from_row: int, to_row: int) -> None:
        """Handle drag-reorder within the playlist track table."""
        if self._current_playlist_id is None:
            return
        if from_row == to_row:
            return
        # Adjust target if dragging downward.
        actual_to = to_row if to_row < from_row else max(0, to_row - 1)
        self._model.move_row(from_row, actual_to)
        self.track_moved.emit(self._current_playlist_id, from_row, actual_to)

    def _on_track_batch_reordered(self, source_rows: list[int], dest_row: int) -> None:
        """Handle multi-track drag-reorder within the playlist track table."""
        if self._current_playlist_id is None:
            return
        self._model.move_rows_batch(source_rows, dest_row)
        # Persist the full new order to the database.
        track_ids = [t.id for t in self._model.tracks]
        self.tracks_reordered.emit(self._current_playlist_id, track_ids)

    def _emit_selected_to_queue(self) -> None:
        """Emit selected tracks from the track table to the queue."""
        rows = sorted({idx.row() for idx in self._table.selectionModel().selectedRows()})
        tracks = [self._model.track_at(r) for r in rows]
        tracks = [t for t in tracks if t is not None]
        if tracks:
            self.add_tracks_to_queue.emit(tracks)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            rows = sorted(
                {idx.row() for idx in self._table.selectionModel().selectedRows()},
                reverse=True,
            )
            for row in rows:
                self._remove_request = row
                self.playlist_changed.emit()
        else:
            super().keyPressEvent(event)
