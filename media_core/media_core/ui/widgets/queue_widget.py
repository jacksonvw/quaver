"""Global queue widget — the single play order visible on every tab.

MusicBee-style: each entry shows album art thumbnail, title (bold),
artist (lighter), and duration. Supports drag-to-reorder, external
drops from library/podcast panels, and right-click context menu.

NOTE: This is the QUEUE (temporary play order), not saved Playlists.
Saved playlists are managed by playlists_panel.py.
"""

from __future__ import annotations

import json

from PySide6.QtCore import (
    QAbstractListModel,
    QMimeData,
    QModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QStyle,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from dataclasses import asdict, fields as dataclass_fields

from media_core.core.track import (
    EPISODE_IDS_MIME,
    PLAYLIST_IDS_MIME,
    QUEUE_TRACKS_MIME,
    TRACK_IDS_MIME,
    VIDEO_IDS_MIME,
    Track,
)
from media_core.ui.subtitles import add_subtitle_menu
from media_core.ui.widgets import embedded_art_cache as _default_art_provider
from media_core.ui.widgets.delegates import ConsistentItemDelegate, DropIndicatorMixin, HoverTrackingMixin

# The queue model uses module-level ``art_cache`` for both async signal
# subscription and synchronous lookups.  Quaver overrides this with its
# library-flavored ``art_cache`` (which adds scraper poster_path support)
# at startup via :func:`set_art_provider`; Minim leaves the default
# embedded-only cache in place.
art_cache = _default_art_provider


def set_art_provider(provider) -> None:
    """Replace the queue widget's art cache.

    *provider* must expose ``get_thumbnail(filepath, size, poster_path="")``
    and ``on_art_ready()`` returning a Signal.  The default is
    :mod:`media_core.ui.widgets.embedded_art_cache`; Quaver swaps in its
    library-flavored ``art_cache`` so video tracks display scraper-
    provided stills in the queue.
    """
    global art_cache
    art_cache = provider

_ART_SIZE = 40
_ROW_HEIGHT = 52
_PADDING = 4

_ROLE_TRACK = Qt.ItemDataRole.UserRole + 1
_ROLE_IS_CURRENT = Qt.ItemDataRole.UserRole + 2
_ROLE_ART_PIXMAP = Qt.ItemDataRole.UserRole + 3

_INTERNAL_MIME = "application/x-quaver-queue-reorder"


_TRACK_FIELD_NAMES = {f.name for f in dataclass_fields(Track)}


def _serialize_tracks(tracks: list[Track]) -> bytes:
    """Encode *tracks* as JSON bytes for cross-instance drag-and-drop.

    Uses :func:`dataclasses.asdict` so any future :class:`Track` fields
    are serialised automatically — no manual list to keep in sync.
    """
    return json.dumps([asdict(t) for t in tracks]).encode()


def _deserialize_tracks(data: bytes) -> list[Track]:
    """Decode the payload written by :func:`_serialize_tracks`.

    Unknown extra keys (e.g. a newer sender, older receiver) are
    discarded so the drop still succeeds with the fields the receiver
    knows about.
    """
    payload = json.loads(data.decode())
    out: list[Track] = []
    for item in payload:
        kwargs = {k: v for k, v in item.items() if k in _TRACK_FIELD_NAMES}
        out.append(Track(**kwargs))
    return out


def _ms_to_mmss(ms: int) -> str:
    total_secs = ms // 1000
    m, s = divmod(total_secs, 60)
    return f"{m}:{s:02d}"


def _stream_action(menu: QMenu, label: str, support: list[bool], idx: int) -> QAction:
    """Checkable track action, disabled when no decoder is installed.

    Switching to an undecodable track hangs the pipeline on its last frame,
    so those tracks are shown (the user should see the track exists) but
    cannot be picked.
    """
    decodable = support[idx] if idx < len(support) else True
    action = QAction(label if decodable else f"{label} — no decoder", menu)
    action.setCheckable(True)
    action.setEnabled(decodable)
    return action


def build_stream_menus(menu: QMenu, engine, parent=None, notify=None) -> None:
    """Append the Audio Track submenu and VLC's Subtitle submenu for the
    active video.  *parent* hosts Add Subtitle File's dialog, and *notify*
    shows its message on screen.

    Shared utility — used by QueueWidget, NowPlayingPanel, AlbumView.
    """
    if not engine:
        return

    audio_tracks = engine.get_audio_tracks()

    menu.addSeparator()

    if len(audio_tracks) > 1:
        audio_menu = menu.addMenu("Audio Track")
        current_audio = engine.get_current_audio()
        audio_support = engine.get_audio_track_support()
        for idx, label in audio_tracks:
            action = _stream_action(menu, label, audio_support, idx)
            action.setChecked(idx == current_audio)
            action.triggered.connect(
                lambda checked, i=idx: engine.set_current_audio(i)
            )
            audio_menu.addAction(action)

    # Always there, as in VLC: Add Subtitle File is wanted most when a
    # video has no subtitles of its own.
    add_subtitle_menu(menu, engine, parent, notify)


# ── Model ────────────────────────────────────────────────────────────


class _QueueModel(QAbstractListModel):
    """Stores Track objects for the queue."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks: list[Track] = []
        self._current: int = -1
        self._art: dict[int, QPixmap | None] = {}  # row → pixmap
        art_cache.on_art_ready().connect(self._on_art_ready)

    def load(
        self, tracks: list[Track], current: int,
        poster_paths: dict[str, str] | None = None,
    ) -> None:
        self.beginResetModel()
        self._tracks = list(tracks)
        self._current = current
        self._art.clear()
        self._poster_paths = poster_paths or {}
        self.endResetModel()
        # Queue art loading for all tracks.
        for i, t in enumerate(tracks):
            poster = self._poster_paths.get(t.filepath, "")
            pm = art_cache.get_thumbnail(t.filepath, _ART_SIZE, poster_path=poster)
            if pm is not None:
                self._art[i] = pm

    def refresh(self) -> None:
        """Repaint every row from its Track without resetting the model."""
        if self._tracks:
            self.dataChanged.emit(self.index(0), self.index(len(self._tracks) - 1))

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._tracks)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._tracks):
            return None
        track = self._tracks[row]

        if role == _ROLE_TRACK:
            return track
        if role == _ROLE_IS_CURRENT:
            return row == self._current
        if role == _ROLE_ART_PIXMAP:
            return self._art.get(row)
        if role == Qt.ItemDataRole.DisplayRole:
            return track.title or "Unknown"
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        default = super().flags(index)
        if index.isValid():
            return default | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        return default | Qt.ItemFlag.ItemIsDropEnabled

    # ── Drag/drop ────────────────────────────────────────────────────

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def mimeTypes(self) -> list[str]:
        return [
            _INTERNAL_MIME,
            QUEUE_TRACKS_MIME,
            TRACK_IDS_MIME,
            EPISODE_IDS_MIME,
            VIDEO_IDS_MIME,
        ]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        mime = QMimeData()
        rows = sorted({idx.row() for idx in indexes})
        # _INTERNAL_MIME — same-view reorder fast path (just row indices).
        mime.setData(_INTERNAL_MIME, json.dumps(rows).encode())
        # QUEUE_TRACKS_MIME — full track payload for cross-window /
        # cross-process drops where row indices alone are meaningless.
        tracks = [self._tracks[r] for r in rows if 0 <= r < len(self._tracks)]
        if tracks:
            mime.setData(QUEUE_TRACKS_MIME, _serialize_tracks(tracks))
        return mime

    # ── Art cache callback ───────────────────────────────────────────

    def _on_art_ready(self, filepath: str, size: int, pixmap: QPixmap | None) -> None:
        if size != _ART_SIZE:
            return
        for i, t in enumerate(self._tracks):
            if t.filepath == filepath:
                self._art[i] = pixmap
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [_ROLE_ART_PIXMAP])
                break


# ── Delegate ─────────────────────────────────────────────────────────


def _byline(track: Track) -> str:
    """A queue row's second line: the artist, then the album if it adds anything."""
    if track.album and track.album != track.artist:
        return f"{track.artist} — {track.album}" if track.artist else track.album
    return track.artist or ""


class _QueueDelegate(ConsistentItemDelegate):
    """Paints each queue row: [art] [title / artist — album] [duration]."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), _ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        track: Track | None = index.data(_ROLE_TRACK)
        is_current: bool = index.data(_ROLE_IS_CURRENT) or False
        art: QPixmap | None = index.data(_ROLE_ART_PIXMAP)

        # Consistent selection/hover background.
        self.paint_item_background(painter, option, index)

        if not track:
            painter.restore()
            return

        # Palette-based colors.
        text_color = option.palette.text().color()
        sub_color = option.palette.placeholderText().color()

        x = rect.x() + _PADDING
        y = rect.y() + _PADDING
        available_w = rect.width() - _PADDING * 2

        # Album art thumbnail.
        art_rect_size = _ROW_HEIGHT - _PADDING * 2
        if art and not art.isNull():
            painter.drawPixmap(x, y, art.scaled(
                art_rect_size, art_rect_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            painter.fillRect(x, y, art_rect_size, art_rect_size, QColor(80, 80, 80))

        text_x = x + art_rect_size + _PADDING * 2
        text_w = available_w - art_rect_size - _PADDING * 2

        # Duration (right-aligned).
        dur_text = _ms_to_mmss(track.duration_ms) if track.duration_ms else ""
        dur_font = QFont(option.font)
        dur_font.setPointSize(max(8, option.font.pointSize() - 1))
        dur_fm = QFontMetrics(dur_font)
        dur_w = dur_fm.horizontalAdvance(dur_text) + _PADDING * 2 if dur_text else 0

        if dur_text:
            painter.setFont(dur_font)
            painter.setPen(sub_color)
            dur_rect_y = rect.y() + (rect.height() - dur_fm.height()) // 2
            painter.drawText(
                rect.right() - dur_w, dur_rect_y,
                dur_w, dur_fm.height(),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                dur_text,
            )

        title_w = text_w - dur_w

        # Title (bold if current).
        title_font = QFont(option.font)
        if is_current:
            title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(text_color)
        title_fm = QFontMetrics(title_font)
        title_text = title_fm.elidedText(track.title or "Unknown", Qt.TextElideMode.ElideRight, title_w)
        painter.drawText(text_x, y, title_w, title_fm.height(), Qt.AlignmentFlag.AlignLeft, title_text)

        # Artist — album (smaller, lighter).
        artist_font = QFont(option.font)
        artist_font.setPointSize(max(7, option.font.pointSize() - 2))
        painter.setFont(artist_font)
        painter.setPen(sub_color)
        artist_fm = QFontMetrics(artist_font)
        artist_text = artist_fm.elidedText(_byline(track), Qt.TextElideMode.ElideRight, title_w)
        artist_y = y + title_fm.height() + 1
        painter.drawText(text_x, artist_y, title_w, artist_fm.height(), Qt.AlignmentFlag.AlignLeft, artist_text)

        # Now-playing indicator: small accent bar on left edge.
        if is_current:
            accent = option.palette.highlight().color()
            painter.fillRect(rect.x(), rect.y() + 4, 3, rect.height() - 8, accent)

        painter.restore()


# ── View ─────────────────────────────────────────────────────────────


class _QueueView(DropIndicatorMixin, HoverTrackingMixin, QListView):
    """QListView that accepts external track-ID and episode-ID drops."""

    external_drop = Signal(str, list, int)  # mime_type, ids, insert_row
    internal_reorder = Signal(list, int)    # source_rows, dest_row
    # Cross-instance queue drop — the dragged tracks came from a
    # different QueueWidget (different window or different process).
    cross_instance_drop = Signal(list, int)        # tracks, insert_row
    # Emitted on the SOURCE view after a successful cross-instance Move
    # so the host can pull those rows out of this queue.
    cross_instance_move_remove = Signal(list)      # rows to remove

    _ACCEPTED_MIMES = (TRACK_IDS_MIME, EPISODE_IDS_MIME, PLAYLIST_IDS_MIME, VIDEO_IDS_MIME)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_hover_tracking()
        # Set inside dropEvent on a same-view internal reorder so the
        # subsequent ``startDrag`` exec-result check doesn't double-act
        # (the model already moved the rows during the reorder).
        self._drag_was_internal_reorder: bool = False

    def _accepted_mime(self, mime_data) -> str | None:
        for mt in self._ACCEPTED_MIMES:
            if mime_data.hasFormat(mt):
                return mt
        return None

    def startDrag(self, supportedActions) -> None:
        """Create a lightweight QDrag to avoid Qt's render-to-pixmap pipeline.

        The default startDrag renders selected rows into a drag pixmap,
        which triggers paintEvent in a nested context. With translucent
        windows this corrupts the compositor state, blanking other panels.
        """
        from PySide6.QtGui import QDrag
        indexes = self.selectedIndexes()
        if not indexes:
            return
        mime = self.model().mimeData(indexes)
        if mime is None:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Small semi-transparent pixmap as drag cursor hint.
        rows = sorted({idx.row() for idx in indexes})
        hint = QPixmap(120, 24)
        hint.fill(QColor(255, 255, 255, 40))
        p = QPainter(hint)
        p.setPen(QColor(255, 255, 255, 180))
        p.drawText(hint.rect(), Qt.AlignmentFlag.AlignCenter, f"{len(rows)} track(s)")
        p.end()
        drag.setPixmap(hint)

        self._drag_was_internal_reorder = False
        action = drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)

        # If the drop landed on a *different* queue (different window or
        # process) and used MoveAction, remove the dragged rows from
        # this queue so the user genuinely "moved" them. Internal
        # reorders already mutated the model directly and must be
        # skipped here.
        if (
            action == Qt.DropAction.MoveAction
            and not self._drag_was_internal_reorder
            and rows
        ):
            self.cross_instance_move_remove.emit(rows)

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if (
            self._accepted_mime(mime)
            or mime.hasFormat(_INTERNAL_MIME)
            or mime.hasFormat(QUEUE_TRACKS_MIME)
        ):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        # Route to DropIndicatorMixin for drop-line tracking.
        super().dragMoveEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._drop_row >= 0:
            p = QPainter(self.viewport())
            self._paint_drop_indicator(p)
            p.end()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        drop_index = self.indexAt(event.position().toPoint())
        insert_row = drop_index.row() if drop_index.isValid() else self.model().rowCount()

        self._reset_drop_indicator()

        # Same-view reorder — row indices are meaningful only here.
        # ``event.source() is self`` is the discriminator: cross-window
        # (same process) or cross-process drops never satisfy it, so
        # those fall through to the QUEUE_TRACKS_MIME branch below.
        if mime.hasFormat(_INTERNAL_MIME) and event.source() is self:
            rows = json.loads(bytes(mime.data(_INTERNAL_MIME)).decode())
            self.internal_reorder.emit(rows, insert_row)
            self._drag_was_internal_reorder = True
            event.acceptProposedAction()
            return

        # Cross-instance queue drop — full track payload.
        if mime.hasFormat(QUEUE_TRACKS_MIME):
            try:
                tracks = _deserialize_tracks(bytes(mime.data(QUEUE_TRACKS_MIME)))
            except (ValueError, TypeError):
                event.ignore()
                return
            if tracks:
                self.cross_instance_drop.emit(tracks, insert_row)
            event.acceptProposedAction()
            return

        # External library drops (track-ids, episode-ids, etc.).
        mt = self._accepted_mime(mime)
        if mt:
            data = bytes(mime.data(mt))
            ids = json.loads(data.decode())
            self.external_drop.emit(mt, ids, insert_row)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


# ── Public widget ────────────────────────────────────────────────────


class QueueWidget(QWidget):
    """Global queue — the single play order for the entire application."""

    track_activated = Signal(int)     # index in queue
    remove_requested = Signal(int)    # index in queue
    move_requested = Signal(int, int) # from_index, to_index
    move_batch_requested = Signal(list, int)  # source_rows, dest_row
    clear_requested = Signal()
    save_as_playlist = Signal()                    # save queue as new playlist
    add_to_playlist = Signal(int)                  # add queue to existing playlist_id
    external_tracks_dropped = Signal(list, int)    # track_ids, insert_position
    external_episodes_dropped = Signal(list, int)  # episode_ids, insert_position
    external_playlists_dropped = Signal(list, int) # playlist_ids, insert_position
    external_videos_dropped = Signal(list, int)    # video_db_ids, insert_position
    # Cross-instance: tracks dragged in from another QueueWidget (a
    # different window or a different process). Carries the actual
    # Track objects (id may be -1 for transient tracks) and the
    # insert position in this queue.
    tracks_dropped = Signal(list, int)             # tracks (list[Track]), insert_position

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_index: int = -1
        # _playlists semantics:
        #   None — no library / Minim mode: hide all playlist actions
        #   []   — library mode with zero saved playlists: show "Save as new"
        #   [..] — library mode with playlists: show "Save as new" + "Add to"
        self._playlists: list[tuple[int, str]] | None = None
        self._engine = None  # AudioEngine ref for video stream menus
        # Host-injected per-track context-menu actions.  Quaver uses
        # this to add "Open in Minim".  Minim leaves it None.
        self._track_extra_actions = None
        self._build_ui()

    def set_engine(self, engine) -> None:
        """Set the audio engine reference for audio/subtitle track menus.

        Pass ``None`` to hide stream selection menus (Minim case when no
        video is playing).
        """
        self._engine = engine

    def set_track_extra_actions(self, factory) -> None:
        """Install a host-supplied factory of extra track-context items.

        *factory* takes ``(rows: list[int], tracks: list[Track])`` and
        returns a list of ``(label, callback)`` pairs appended to the
        per-track right-click menu (after the standard Play Now /
        Remove / Clear Queue items).  Called fresh each time the menu
        opens so labels can reflect mutable host state.

        Quaver uses this to add "Open in Minim".  Minim leaves the
        factory unset.
        """
        self._track_extra_actions = factory

    def set_playlists(self, playlists: list[tuple[int, str]] | None) -> None:
        """Update the saved-playlists list for the header context menu.

        Pass ``None`` to disable all playlist actions (Minim mode).  Pass
        ``[]`` to enable "Save Queue as New Playlist" without an "Add to"
        submenu.  Pass a list of ``(id, name)`` to enable both.
        """
        self._playlists = None if playlists is None else list(playlists)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Header label — right-click for queue options.
        self._header = QLabel("Queue")
        self._header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._header.customContextMenuRequested.connect(self._show_header_menu)
        layout.addWidget(self._header)

        # Model + delegate + view.
        self._model = _QueueModel(self)
        self._delegate = _QueueDelegate(self)
        self._view = _QueueView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)

        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.setDragEnabled(True)
        self._view.setAcceptDrops(True)
        self._view.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self._view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._view.doubleClicked.connect(
            lambda idx: self.track_activated.emit(idx.row())
        )
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)

        # Internal reorder and external drops.
        self._view.internal_reorder.connect(self._on_internal_reorder)
        self._view.external_drop.connect(self._on_external_drop)
        # Cross-instance queue drops (between separate Minim windows
        # for example) and the source-side row-removal that follows a
        # successful Move.
        self._view.cross_instance_drop.connect(self._on_cross_instance_drop)
        self._view.cross_instance_move_remove.connect(self._on_cross_instance_remove)

        layout.addWidget(self._view)

    def load_queue(
        self, tracks: list[Track], current_index: int,
        poster_paths: dict[str, str] | None = None,
    ) -> None:
        """Rebuild the queue — single atomic swap, no flicker."""
        self._current_index = current_index
        self._view.setUpdatesEnabled(False)
        self._model.load(tracks, current_index, poster_paths=poster_paths)
        self._view.setUpdatesEnabled(True)

    def refresh_tracks(self) -> None:
        """Repaint the rows after their tracks' metadata changed in place
        (e.g. tags read in the background), keeping scroll and selection."""
        self._model.refresh()

    def _show_header_menu(self, pos) -> None:
        """Right-click menu on the Queue header label."""
        menu = QMenu(self)

        # Saved-playlist actions appear only when a library is hosting us.
        if self._playlists is not None:
            save_action = QAction("Save Queue as New Playlist", self)
            save_action.triggered.connect(self.save_as_playlist.emit)
            save_action.setEnabled(self._model.rowCount() > 0)
            menu.addAction(save_action)

            if self._playlists:
                add_menu = menu.addMenu("Add Queue to Playlist")
                for pid, name in self._playlists:
                    action = QAction(name, self)
                    action.triggered.connect(lambda _, p=pid: self.add_to_playlist.emit(p))
                    add_menu.addAction(action)

            menu.addSeparator()

        clear_action = QAction("Clear Queue", self)
        clear_action.triggered.connect(self.clear_requested.emit)
        clear_action.setEnabled(self._model.rowCount() > 0)
        menu.addAction(clear_action)

        menu.exec(self._header.mapToGlobal(pos))

    def _show_context_menu(self, pos) -> None:
        index = self._view.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        track: Track | None = index.data(_ROLE_TRACK)

        # Selection-aware: act on every selected row, not just the
        # right-clicked one.
        sel_rows = sorted(
            {i.row() for i in self._view.selectionModel().selectedIndexes()}
        )
        if not sel_rows or row not in sel_rows:
            sel_rows = [row]
        sel_tracks: list[Track] = []
        for r in sel_rows:
            t: Track | None = self._model.index(r).data(_ROLE_TRACK)
            if t is not None:
                sel_tracks.append(t)

        menu = QMenu(self)

        play_action = QAction("Play Now", self)
        play_action.triggered.connect(lambda: self.track_activated.emit(row))
        menu.addAction(play_action)

        remove_action = QAction("Remove", self)
        remove_action.triggered.connect(lambda: self.remove_requested.emit(row))
        menu.addAction(remove_action)

        # Host-injected extras (e.g. Quaver's "Open in Minim").
        if self._track_extra_actions is not None and sel_tracks:
            try:
                items = self._track_extra_actions(sel_rows, sel_tracks) or []
            except Exception:
                items = []
            if items:
                menu.addSeparator()
                for label, callback in items:
                    a = QAction(label, self)
                    a.triggered.connect(callback)
                    menu.addAction(a)

        menu.addSeparator()

        clear_action = QAction("Clear Queue", self)
        clear_action.triggered.connect(self.clear_requested.emit)
        menu.addAction(clear_action)

        # Audio/subtitle stream selection for the currently-playing video/YouTube track.
        if (
            track
            and row == self._current_index
            and (
                track.media_type in ("movie", "series_episode")
                or track.filepath.startswith("youtube://")
            )
            and self._engine
        ):
            self._add_stream_menus(menu)

        menu.exec(self._view.viewport().mapToGlobal(pos))

    def _add_stream_menus(self, menu: QMenu) -> None:
        """Append Audio Track and Subtitles submenus for the active video."""
        build_stream_menus(menu, self._engine, parent=self)

    def _on_internal_reorder(self, source_rows: list[int], dest_row: int) -> None:
        """Handle drag-reorder within the queue (supports multi-row)."""
        if not source_rows:
            return
        if len(source_rows) == 1:
            from_idx = source_rows[0]
            to_idx = dest_row if dest_row < from_idx else max(0, dest_row - 1)
            if from_idx != to_idx:
                self.move_requested.emit(from_idx, to_idx)
        else:
            self.move_batch_requested.emit(source_rows, dest_row)

    def _on_external_drop(self, mime_type: str, ids: list, insert_row: int) -> None:
        if mime_type == TRACK_IDS_MIME:
            self.external_tracks_dropped.emit(ids, insert_row)
        elif mime_type == EPISODE_IDS_MIME:
            self.external_episodes_dropped.emit(ids, insert_row)
        elif mime_type == PLAYLIST_IDS_MIME:
            self.external_playlists_dropped.emit(ids, insert_row)
        elif mime_type == VIDEO_IDS_MIME:
            self.external_videos_dropped.emit(ids, insert_row)

    def _on_cross_instance_drop(self, tracks: list, insert_row: int) -> None:
        """Forward a queue-to-queue drop up to the host so it can call
        the player's :meth:`insert_tracks`. The receiver is responsible
        for connecting :attr:`tracks_dropped`.
        """
        self.tracks_dropped.emit(tracks, insert_row)

    def _on_cross_instance_remove(self, rows: list) -> None:
        """Source-side cleanup after a successful cross-instance Move.

        Emits :attr:`remove_requested` for each row in descending order
        so the host's removal handler sees stable indices (removing
        row N first never shifts the meaning of row N+1).
        """
        for row in sorted({int(r) for r in rows}, reverse=True):
            self.remove_requested.emit(row)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            rows = sorted(
                {idx.row() for idx in self._view.selectionModel().selectedIndexes()},
                reverse=True,
            )
            for row in rows:
                self.remove_requested.emit(row)
        else:
            super().keyPressEvent(event)
