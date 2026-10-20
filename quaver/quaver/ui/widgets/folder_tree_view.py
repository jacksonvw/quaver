"""Folder-sidebar + thumbnail-grid view for ``files`` library tabs.

The library tab presents:
  - **Left**: a lazy filesystem folder tree (directories only, no files).
    Selecting a folder shows its files in the right panel; double-clicking
    plays everything under that folder recursively.
  - **Right**: a thumbnail grid of the playable files in the selected
    folder.  Audio files use their embedded album art; video files use
    the freedesktop thumbnail cache (``~/.cache/thumbnails/large/<md5>.png``)
    when present, falling back to a stock icon otherwise.  Generation
    of missing thumbnails is intentionally left to the desktop's own
    thumbnailer (Dolphin/Nautilus/Tumbler) — Quaver doesn't try to
    invoke ffmpeg/ffmpegthumbnailer at this point.

Every track produced is transient (``id == -1``) — the Files tab has
no DB rows.  See ``project_source_config`` memory for the broader
"files" media-type story.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QListView,
    QMenu,
    QSplitter,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from media_core.core.freedesktop_thumbs import load_thumbnail as _load_freedesktop_thumb
from media_core.core.media_exts import (
    AUDIO_EXTS_DOTTED,
    VIDEO_EXTS,
    VIDEO_EXTS_DOTTED,
    is_playable,
)
from media_core.core.track import Track
from media_core.ui.widgets import embedded_art_cache as _audio_art


_THUMB_SIZE = 128
_GRID_CELL_W = _THUMB_SIZE + 24
_GRID_CELL_H = _THUMB_SIZE + 44

_ROLE_PATH = Qt.ItemDataRole.UserRole + 1
_ROLE_IS_DIR = Qt.ItemDataRole.UserRole + 2
_ROLE_LOADED = Qt.ItemDataRole.UserRole + 3


# ── Helpers ──────────────────────────────────────────────────────────


def _track_from_path(path: Path) -> Track:
    """Build a transient :class:`Track` from a filesystem path.

    Mirrors :func:`minim.tracks.track_from_path` — Quaver doesn't
    import Minim, so the same minimal logic lives here.
    """
    ext = path.suffix.lstrip(".").lower()
    media_type = "movie" if ext in VIDEO_EXTS else "audio"
    return Track(
        id=-1,
        filepath=str(path),
        title=path.stem or path.name,
        artist="",
        album="",
        album_artist="",
        year=None,
        genre="",
        track_number=None,
        disc_number=None,
        duration_ms=0,
        bitrate=0,
        format=ext,
        replay_gain_track=None,
        replay_gain_album=None,
        play_count=0,
        last_played=None,
        date_added="",
        rating=0,
        fingerprint=None,
        media_type=media_type,
    )


def _list_subdirs(path: Path) -> list[Path]:
    """Visible subdirectories (no hidden, no files) in deterministic order."""
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for p in entries:
        try:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                out.append(p)
        except OSError:
            continue
    out.sort(key=lambda p: p.name.lower())
    return out


def _list_media_files(path: Path) -> list[Path]:
    """Visible playable media files (no hidden, no order applied)."""
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for p in entries:
        try:
            if p.name.startswith("."):
                continue
            if p.is_file() and is_playable(p):
                out.append(p)
        except OSError:
            continue
    return out


# Sort modes — keys map to user-facing combo labels and are persisted
# unchanged so a future TOML round-trip is straightforward.  Each entry
# also includes the secondary key callback used inside the model.
_SORT_MODES: list[tuple[str, str]] = [
    ("name", "Name"),
    ("size", "Size"),
    ("date", "Date Modified"),
    # ``length`` would require GStreamer/ffprobe duration probing per
    # file.  Stubbed for a follow-up — exposing it now would force
    # synchronous probes on every folder click which hammers the
    # filesystem and the NAS.
]


def _sort_files(files: list[Path], mode: str) -> list[Path]:
    """Order *files* according to *mode* — folders never appear here."""
    def _safe_stat_key(p: Path, attr: str) -> tuple[int, str]:
        # On stat() failure, sink the file to the bottom of the list
        # rather than raising.
        try:
            stat = p.stat()
            return (getattr(stat, attr), p.name.lower())
        except OSError:
            return (-1, p.name.lower())

    if mode == "size":
        return sorted(files, key=lambda p: _safe_stat_key(p, "st_size"))
    if mode == "date":
        # Newest first feels right for a "what did I just drop in here"
        # workflow.
        return sorted(
            files,
            key=lambda p: _safe_stat_key(p, "st_mtime"),
            reverse=True,
        )
    # Default: name (case-insensitive).
    return sorted(files, key=lambda p: p.name.lower())


def _walk_playable(path: Path, max_files: int = 5000) -> list[Path]:
    """Every playable file under *path*, capped to keep huge roots sane."""
    out: list[Path] = []
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                p = Path(root) / name
                if is_playable(p):
                    out.append(p)
                    if len(out) >= max_files:
                        return out
    except OSError:
        pass
    return out


# ── File grid model ──────────────────────────────────────────────────


class _FileGridModel(QAbstractListModel):
    """File grid backing model — name + thumbnail per file.

    Synchronous freedesktop-cache lookup is cheap enough to do inline;
    embedded audio art arrives asynchronously via the shared signal in
    :mod:`media_core.ui.widgets.embedded_art_cache`.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._files: list[Path] = []
        self._icons: dict[int, QIcon] = {}
        self._hide_extensions: bool = False
        # Stock fallbacks used when no thumbnail can be produced.
        style = parent.style() if parent else None
        if style is not None:
            self._video_fallback = style.standardIcon(
                QStyle.StandardPixmap.SP_FileIcon
            )
            self._audio_fallback = style.standardIcon(
                QStyle.StandardPixmap.SP_FileIcon
            )
        else:
            self._video_fallback = QIcon()
            self._audio_fallback = QIcon()
        # Subscribe to async embedded-art arrivals.
        _audio_art.on_art_ready().connect(self._on_audio_art_ready)

    def set_files(self, files: list[Path]) -> None:
        self.beginResetModel()
        self._files = list(files)
        self._icons.clear()
        # Synchronous prefetch from the freedesktop thumbnail cache —
        # one PNG load per file, fast enough not to block the UI for
        # typical folder sizes.
        for i, p in enumerate(self._files):
            pm = _load_freedesktop_thumb(p)
            if pm is not None:
                self._icons[i] = QIcon(pm)
                continue
            # Audio fallback: embedded art (async).
            if p.suffix.lower() in AUDIO_EXTS_DOTTED:
                _audio_art.get_thumbnail(str(p), _THUMB_SIZE)
        self.endResetModel()

    def set_hide_extensions(self, hide: bool) -> None:
        if hide == self._hide_extensions:
            return
        self._hide_extensions = hide
        if self._files:
            top = self.index(0)
            bottom = self.index(len(self._files) - 1)
            self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.DisplayRole])

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._files)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._files):
            return None
        path = self._files[row]
        if role == Qt.ItemDataRole.DisplayRole:
            return path.stem if self._hide_extensions else path.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(path)
        if role == Qt.ItemDataRole.DecorationRole:
            icon = self._icons.get(row)
            if icon is not None:
                return icon
            ext = path.suffix.lower()
            if ext in VIDEO_EXTS_DOTTED:
                return self._video_fallback
            return self._audio_fallback
        if role == _ROLE_PATH:
            return str(path)
        return None

    def file_at(self, row: int) -> Path | None:
        if 0 <= row < len(self._files):
            return self._files[row]
        return None

    def files(self) -> list[Path]:
        return list(self._files)

    def _on_audio_art_ready(self, filepath: str, size: int, pixmap) -> None:
        if size != _THUMB_SIZE or pixmap is None or pixmap.isNull():
            return
        for i, p in enumerate(self._files):
            if str(p) == filepath:
                self._icons[i] = QIcon(pixmap)
                idx = self.index(i)
                self.dataChanged.emit(
                    idx, idx, [Qt.ItemDataRole.DecorationRole]
                )
                break


# ── Public widget ────────────────────────────────────────────────────


class FolderTreeView(QWidget):
    """Files-tab view: folder sidebar (left) + thumbnail grid (right).

    Selecting a folder in the sidebar loads its non-recursive media
    files into the grid.  Double-click a folder to play everything
    underneath; double-click a thumbnail to play that file.
    """

    play_tracks_requested = Signal(list, int)   # (tracks, start_index)
    add_to_queue_requested = Signal(list)       # tracks to append
    open_in_minim_requested = Signal(list)      # filesystem paths (str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._roots: list[Path] = []
        self._sort_mode: str = "name"
        self._hide_extensions: bool = False
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Filter bar — substring-match search + sort combo for the
        # file grid.
        filter_bar = QWidget()
        filter_bar.setFixedHeight(32)
        fb = QHBoxLayout(filter_bar)
        fb.setContentsMargins(4, 2, 4, 2)
        fb.setSpacing(6)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filter files in folder…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._apply_filter)
        fb.addWidget(self._search_edit)

        fb.addStretch()

        self._sort_combo = QComboBox()
        for key, label in _SORT_MODES:
            self._sort_combo.addItem(f"Sort: {label}", key)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self._sort_combo.setMaximumWidth(180)
        fb.addWidget(self._sort_combo)
        layout.addWidget(filter_bar)

        # Splitter: folder tree | thumbnail grid.
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self._splitter, stretch=1)

        # ── Left: folder tree (dirs only, lazy) ──
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemSelectionChanged.connect(self._on_folder_selected)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self._tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._splitter.addWidget(self._tree)

        # ── Right: thumbnail grid ──
        self._file_model = _FileGridModel(self)
        self._file_grid = QListView()
        self._file_grid.setModel(self._file_model)
        self._file_grid.setViewMode(QListView.ViewMode.IconMode)
        self._file_grid.setIconSize(QSize(_THUMB_SIZE, _THUMB_SIZE))
        self._file_grid.setGridSize(QSize(_GRID_CELL_W, _GRID_CELL_H))
        self._file_grid.setMovement(QListView.Movement.Static)
        self._file_grid.setResizeMode(QListView.ResizeMode.Adjust)
        self._file_grid.setSpacing(8)
        self._file_grid.setUniformItemSizes(True)
        self._file_grid.setWordWrap(True)
        self._file_grid.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._file_grid.doubleClicked.connect(self._on_grid_double_clicked)
        self._file_grid.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._file_grid.customContextMenuRequested.connect(
            self._on_grid_context_menu
        )
        self._splitter.addWidget(self._file_grid)

        self._splitter.setStretchFactor(0, 1)  # tree narrower
        self._splitter.setStretchFactor(1, 4)  # grid wider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_source_paths(self, paths: list[str]) -> None:
        """Repopulate the tree with the given root folders.

        Existing expansion state is dropped.  After loading, the first
        available root is auto-selected so the grid isn't empty on
        first paint.
        """
        self._roots = [Path(p).expanduser() for p in paths]
        self._tree.clear()
        first_real_item: QTreeWidgetItem | None = None
        for root in self._roots:
            if not root.is_dir():
                stub = QTreeWidgetItem(
                    self._tree, [f"{root.name}  (unavailable)"]
                )
                stub.setData(0, _ROLE_PATH, str(root))
                stub.setData(0, _ROLE_IS_DIR, False)
                stub.setData(0, _ROLE_LOADED, True)
                stub.setDisabled(True)
                continue
            item = self._make_dir_item(root, label=root.name or str(root))
            self._tree.addTopLevelItem(item)
            if first_real_item is None:
                first_real_item = item
        if first_real_item is not None:
            self._tree.setCurrentItem(first_real_item)

    # ------------------------------------------------------------------
    # Tree (folders only, lazy-expanded)
    # ------------------------------------------------------------------

    def _make_dir_item(
        self, path: Path, label: str | None = None,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label or path.name])
        item.setData(0, _ROLE_PATH, str(path))
        item.setData(0, _ROLE_IS_DIR, True)
        item.setData(0, _ROLE_LOADED, False)
        item.setIcon(0, self.style().standardIcon(
            QStyle.StandardPixmap.SP_DirIcon
        ))
        item.addChild(QTreeWidgetItem(["…"]))  # sentinel for the arrow
        return item

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, _ROLE_LOADED):
            return
        path = Path(item.data(0, _ROLE_PATH))
        item.takeChildren()
        for sub in _list_subdirs(path):
            item.addChild(self._make_dir_item(sub))
        item.setData(0, _ROLE_LOADED, True)

    def _refresh_item(self, item: QTreeWidgetItem) -> None:
        if not item.data(0, _ROLE_IS_DIR):
            return
        item.takeChildren()
        item.setData(0, _ROLE_LOADED, False)
        item.addChild(QTreeWidgetItem(["…"]))
        if item.isExpanded():
            self._on_item_expanded(item)
        # Refresh the grid too if this folder is the active one.
        if self._tree.currentItem() is item:
            self._on_folder_selected()

    def _on_folder_selected(self) -> None:
        item = self._tree.currentItem()
        if item is None:
            self._file_model.set_files([])
            return
        path_raw = item.data(0, _ROLE_PATH)
        if not path_raw:
            self._file_model.set_files([])
            return
        path = Path(path_raw)
        files = _list_media_files(path) if path.is_dir() else []
        self._file_model.set_files(_sort_files(files, self._sort_mode))
        # Re-apply any active search filter to the freshly-loaded grid.
        self._apply_filter(self._search_edit.text())

    def _on_sort_changed(self) -> None:
        mode = self._sort_combo.currentData() or "name"
        if mode == self._sort_mode:
            return
        self._sort_mode = mode
        # Reload current folder with the new ordering.
        self._on_folder_selected()

    def set_hide_extensions(self, hide: bool) -> None:
        """Hide / show file extensions in the grid display.

        Per-tab setting — Files-tab config exposes it via the Library
        Tabs dialog.  The actual ``Path.stem`` vs ``Path.name`` swap
        happens in the model so existing thumbnails / paths are
        untouched; only the visible label changes.
        """
        self._hide_extensions = hide
        self._file_model.set_hide_extensions(hide)

    def _on_tree_double_clicked(
        self, item: QTreeWidgetItem, column: int,
    ) -> None:
        path = Path(item.data(0, _ROLE_PATH) or "")
        if not path or not item.data(0, _ROLE_IS_DIR):
            return
        tracks = [_track_from_path(p) for p in _walk_playable(path)]
        if tracks:
            self.play_tracks_requested.emit(tracks, 0)

    def _on_tree_context_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        path = Path(item.data(0, _ROLE_PATH) or "")
        if not path:
            return
        menu = QMenu(self)
        menu.addAction("Play All", lambda: self._emit_play_all(path))
        menu.addAction(
            "Add Folder to Queue", lambda: self._emit_enqueue_all(path),
        )
        menu.addSeparator()
        menu.addAction("Refresh", lambda: self._refresh_item(item))
        menu.addAction("Copy Path", lambda: self._copy_path(path))
        menu.addAction(
            "Open in File Manager",
            lambda: self._open_in_file_manager(path),
        )
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Grid (current folder's files)
    # ------------------------------------------------------------------

    def _on_grid_double_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        path = self._file_model.file_at(index.row())
        if path is None or not is_playable(path):
            return
        # Build the queue from every file in the grid, start at the
        # double-clicked one — matches album-view "double click track →
        # play album from this point" semantics.
        files = self._file_model.files()
        tracks = [_track_from_path(p) for p in files]
        self.play_tracks_requested.emit(tracks, index.row())

    def _on_grid_context_menu(self, pos) -> None:
        index = self._file_grid.indexAt(pos)
        if not index.isValid():
            return
        # Multi-selection support: act on every selected row.
        rows = sorted(
            {i.row() for i in self._file_grid.selectionModel().selectedIndexes()}
        )
        if not rows:
            rows = [index.row()]
        paths = [self._file_model.file_at(r) for r in rows]
        paths = [p for p in paths if p is not None]
        if not paths:
            return

        menu = QMenu(self)
        if len(paths) == 1:
            menu.addAction(
                "Play",
                lambda: self.play_tracks_requested.emit(
                    [_track_from_path(paths[0])], 0,
                ),
            )
            menu.addAction(
                "Add to Queue",
                lambda: self.add_to_queue_requested.emit(
                    [_track_from_path(paths[0])],
                ),
            )
        else:
            menu.addAction(
                f"Play {len(paths)} Tracks",
                lambda: self.play_tracks_requested.emit(
                    [_track_from_path(p) for p in paths], 0,
                ),
            )
            menu.addAction(
                f"Add {len(paths)} to Queue",
                lambda: self.add_to_queue_requested.emit(
                    [_track_from_path(p) for p in paths],
                ),
            )
        menu.addSeparator()
        menu.addAction(
            "Open in Minim",
            lambda: self.open_in_minim_requested.emit(
                [str(p) for p in paths],
            ),
        )
        menu.addSeparator()
        menu.addAction("Copy Path", lambda: self._copy_path(paths[0]))
        menu.addAction(
            "Open in File Manager",
            lambda: self._open_in_file_manager(paths[0]),
        )
        menu.exec(self._file_grid.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Shared menu actions
    # ------------------------------------------------------------------

    def _emit_play_all(self, folder: Path) -> None:
        tracks = [_track_from_path(p) for p in _walk_playable(folder)]
        if tracks:
            self.play_tracks_requested.emit(tracks, 0)

    def _emit_enqueue_all(self, folder: Path) -> None:
        tracks = [_track_from_path(p) for p in _walk_playable(folder)]
        if tracks:
            self.add_to_queue_requested.emit(tracks)

    @staticmethod
    def _copy_path(path: Path) -> None:
        QGuiApplication.clipboard().setText(str(path))

    @staticmethod
    def _open_in_file_manager(path: Path) -> None:
        target = path if path.is_dir() else path.parent
        try:
            subprocess.Popen(
                ["xdg-open", str(target)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Search filter (applies to the file grid)
    # ------------------------------------------------------------------

    def _apply_filter(self, text: str) -> None:
        text = (text or "").strip().lower()
        # Hide rows whose filename doesn't match.
        for row in range(self._file_model.rowCount()):
            path = self._file_model.file_at(row)
            visible = (text == "") or (
                path is not None and text in path.name.lower()
            )
            self._file_grid.setRowHidden(row, not visible)
