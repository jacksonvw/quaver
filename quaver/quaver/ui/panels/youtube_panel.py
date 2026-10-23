"""YouTube panel — channel list + video list + description viewer.

Videos use QTableView + QAbstractTableModel + QSortFilterProxyModel
for native sorting and drag-to-queue support.  Follows PodcastPanel
patterns exactly.
"""

from __future__ import annotations

import json
import webbrowser

from PySide6.QtCore import (
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QHBoxLayout,
    QTableView,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QStyleOptionViewItem

from quaver.core.library import VIDEO_IDS_MIME
from quaver.core.youtube import Channel, Video, _THUMB_CACHE_DIR
from quaver.ui.widgets.delegates import (
    ConsistentItemDelegate, HoverTrackingMixin, SimpleItemDelegate, TableSelectionMixin,
)

_CHAN_ART_SIZE = 40
_CHAN_ROW_HEIGHT = 52
_CHAN_PADDING = 4
_ROLE_CHANNEL_DB_ID = Qt.ItemDataRole.UserRole + 10
_ROLE_CHAN_ART = Qt.ItemDataRole.UserRole + 11
_ROLE_CHAN_DESCRIPTION = Qt.ItemDataRole.UserRole + 12


class _ChannelDelegate(ConsistentItemDelegate):
    """Paints channel items: [thumbnail] [title / description snippet]."""

    _show_urls: bool = True

    def set_show_urls(self, show: bool) -> None:
        self._show_urls = show
        parent = self.parent()
        if parent and hasattr(parent, "viewport"):
            parent.viewport().update()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), _CHAN_ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        p = _CHAN_PADDING

        self.paint_item_background(painter, option, index)

        x = rect.x() + p
        y = rect.y() + p
        art_s = _CHAN_ROW_HEIGHT - p * 2

        text_color = option.palette.text().color()
        sub_color = option.palette.placeholderText().color()

        # Art thumbnail.
        art: QPixmap | None = index.data(_ROLE_CHAN_ART)
        if art and not art.isNull():
            painter.drawPixmap(x, y, art.scaled(
                art_s, art_s,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            painter.fillRect(x, y, art_s, art_s, QColor(80, 80, 80))
        text_x = x + art_s + p * 2
        text_w = rect.right() - text_x - p

        # Title (bold).
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        name_font = QFont(option.font)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(text_color)
        fm = QFontMetrics(name_font)
        elided = fm.elidedText(name, Qt.TextElideMode.ElideRight, text_w)
        painter.drawText(text_x, y, text_w, fm.height(), Qt.AlignmentFlag.AlignLeft, elided)

        # Description snippet (smaller, muted) — only if URLs are shown.
        if self._show_urls:
            desc = index.data(_ROLE_CHAN_DESCRIPTION) or ""
            if desc:
                sub_font = QFont(option.font)
                sub_font.setPointSize(max(7, option.font.pointSize() - 2))
                painter.setFont(sub_font)
                painter.setPen(sub_color)
                sub_fm = QFontMetrics(sub_font)
                desc_elided = sub_fm.elidedText(desc, Qt.TextElideMode.ElideRight, text_w)
                painter.drawText(text_x, y + fm.height() + 1, text_w, sub_fm.height(),
                                 Qt.AlignmentFlag.AlignLeft, desc_elided)

        painter.restore()


# -- Video model -------------------------------------------------------

_COL_TITLE = 0
_COL_DATE = 1
_COL_DURATION = 2
_COL_VIEWS = 3
_COL_DOWNLOADED = 4
_N_COLS = 5

_ROLE_VIDEO = Qt.ItemDataRole.UserRole + 1
_ROLE_VIDEO_DB_ID = Qt.ItemDataRole.UserRole + 2
_ROLE_SORT = Qt.ItemDataRole.UserRole + 3


def _format_date(raw: str | None) -> str:
    if not raw:
        return ""
    return raw[:10]


def _format_duration(ms: int) -> str:
    if not ms:
        return ""
    total_secs = ms // 1000
    m, s = divmod(total_secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _format_views(count: int) -> str:
    if not count:
        return ""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


class _CachedVideo:
    """Video with pre-formatted display strings for fast rendering."""
    __slots__ = ("video", "date_display", "dur_display", "views_display", "orig_index")

    def __init__(self, video: Video, index: int = 0) -> None:
        self.video = video
        self.date_display = _format_date(video.published_at)
        self.dur_display = _format_duration(video.duration_ms)
        self.views_display = _format_views(video.view_count)
        self.orig_index = index  # Original position (yt-dlp returns newest-first)


class _VideoTableModel(QAbstractTableModel):
    """Flat table model for YouTube videos with drag support and pagination."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[_CachedVideo] = []
        self._download_pcts: dict[int, int] = {}  # video_db_id → percentage

    def load(self, videos: list[Video]) -> None:
        self.beginResetModel()
        self._items = [_CachedVideo(v, i) for i, v in enumerate(videos)]
        self.endResetModel()

    def append_videos(self, videos: list[Video]) -> None:
        if not videos:
            return
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(videos) - 1)
        self._items.extend(_CachedVideo(v, start + i) for i, v in enumerate(videos))
        self.endInsertRows()

    def video_at(self, row: int) -> Video | None:
        if 0 <= row < len(self._items):
            return self._items[row].video
        return None

    def update_download_progress(self, video_db_id: int, pct: int) -> None:
        self._download_pcts[video_db_id] = pct
        for i, item in enumerate(self._items):
            if item.video.id == video_db_id:
                idx = self.index(i, _COL_DOWNLOADED)
                self.dataChanged.emit(idx, idx)
                break

    def mark_download_complete(self, video_db_id: int) -> None:
        self._download_pcts.pop(video_db_id, None)
        for i, item in enumerate(self._items):
            if item.video.id == video_db_id:
                # Update the cached video's downloaded flag.
                item.video = Video(
                    id=item.video.id, channel_id=item.video.channel_id,
                    video_id=item.video.video_id, title=item.video.title,
                    duration_ms=item.video.duration_ms,
                    thumbnail_url=item.video.thumbnail_url,
                    published_at=item.video.published_at,
                    view_count=item.video.view_count,
                    description=item.video.description,
                    watched=item.video.watched,
                    local_path="downloaded", downloaded=True,
                )
                idx = self.index(i, _COL_DOWNLOADED)
                self.dataChanged.emit(idx, idx)
                break

    # -- QAbstractTableModel interface ---------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return _N_COLS

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ["Title", "Date", "Duration", "Views", "Downloaded"][section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row >= len(self._items):
            return None
        item = self._items[row]
        v = item.video
        col = index.column()

        if role == _ROLE_VIDEO:
            return v
        if role == _ROLE_VIDEO_DB_ID:
            return v.id

        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_TITLE:
                return v.title
            if col == _COL_DATE:
                return item.date_display
            if col == _COL_DURATION:
                return item.dur_display
            if col == _COL_VIEWS:
                return item.views_display
            if col == _COL_DOWNLOADED:
                pct = self._download_pcts.get(v.id)
                if pct is not None:
                    return f"{pct}%"
                return "Yes" if v.downloaded else ""

        if role == _ROLE_SORT:
            if col == _COL_TITLE:
                return (v.title or "").lower()
            if col == _COL_DATE:
                # Use published_at if available, otherwise preserve yt-dlp's
                # original order (newest first) via a synthetic sortable value.
                if v.published_at:
                    return v.published_at
                # Pad orig_index into a string that sorts correctly alongside dates.
                # Lower index = newer video, so invert for descending sort.
                return f"0000-{99999 - item.orig_index:05d}"
            if col == _COL_DURATION:
                return v.duration_ms or 0
            if col == _COL_VIEWS:
                return v.view_count or 0
            if col == _COL_DOWNLOADED:
                return int(v.downloaded)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (_COL_DATE, _COL_DURATION, _COL_VIEWS, _COL_DOWNLOADED):
                return int(Qt.AlignmentFlag.AlignCenter)

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )

    # -- Drag support --------------------------------------------------

    def mimeTypes(self) -> list[str]:
        return [VIDEO_IDS_MIME]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        ids: list[int] = []
        seen: set[int] = set()
        for idx in indexes:
            if idx.column() != 0:
                continue
            v = self._items[idx.row()].video
            if v.id not in seen:
                ids.append(v.id)
                seen.add(v.id)
        mime = QMimeData()
        mime.setData(VIDEO_IDS_MIME, json.dumps(ids).encode())
        return mime


class _VideoSortProxy(QSortFilterProxyModel):
    """Sort proxy using _ROLE_SORT for correct ordering."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        lv = left.data(_ROLE_SORT)
        rv = right.data(_ROLE_SORT)
        if lv is not None and rv is not None:
            return lv < rv
        return super().lessThan(left, right)


# -- Hover-tracking view subclasses ------------------------------------


class _ChannelListWidget(HoverTrackingMixin, QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_hover_tracking()
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


class _VideoTableView(TableSelectionMixin, HoverTrackingMixin, QTableView):
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
        hint = QPixmap(140, 24)
        hint.fill(QColor(255, 255, 255, 40))
        p = QPainter(hint)
        p.setPen(QColor(255, 255, 255, 180))
        p.drawText(hint.rect(), Qt.AlignmentFlag.AlignCenter, f"{len(rows)} video(s)")
        p.end()
        drag.setPixmap(hint)
        drag.exec(Qt.DropAction.CopyAction)


# -- YouTube panel ------------------------------------------------------


class YouTubePanel(QWidget):
    """Two-pane YouTube view: channel list | videos + description."""

    # Signals to main window.
    add_channel_requested = Signal(str)
    browse_requested = Signal()
    remove_channel_requested = Signal(int)
    refresh_requested = Signal(int)  # channel_db_id, 0 = all
    play_channel_requested = Signal(int)  # channel_db_id — double-click on channel
    play_video_requested = Signal(int)  # video_db_id
    add_to_queue_requested = Signal(int)  # video_db_id
    add_to_playlist_requested = Signal(int, int)  # video_db_id, playlist_id
    download_requested = Signal(int)  # video_db_id
    delete_download_requested = Signal(int)  # video_db_id
    mark_watched_requested = Signal(int)  # video_db_id
    search_requested = Signal(str)  # query
    sync_subscriptions_requested = Signal()
    load_more_videos = Signal(int, int, int)  # channel_db_id, limit, offset

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._channels: list[Channel] = []
        self._current_channel_db_id: int | None = None
        self._playlists: list[tuple[int, str]] = []  # (id, name) for context menu
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Top toolbar.
        toolbar = QHBoxLayout()
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self.browse_requested.emit)
        btn_add = QPushButton("Add Channel")
        btn_add.clicked.connect(self._on_add_channel)
        self._btn_sync = QPushButton("Sync Subscriptions")
        self._btn_sync.clicked.connect(self.sync_subscriptions_requested.emit)
        toolbar.addWidget(btn_browse)
        toolbar.addWidget(btn_add)
        toolbar.addWidget(self._btn_sync)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Main splitter: channel list | videos + description.
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: channel list + search bar.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # Search bar.
        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("Search YouTube...")
        self._search_bar.returnPressed.connect(self._on_search)
        left_layout.addWidget(self._search_bar)

        left_layout.addWidget(QLabel("Channels"))
        self._channel_list = _ChannelListWidget()
        self._channel_delegate = _ChannelDelegate(self._channel_list)
        self._channel_list.setItemDelegate(self._channel_delegate)
        self._channel_list.currentRowChanged.connect(self._on_channel_selected)
        self._channel_list.itemDoubleClicked.connect(self._on_channel_double_click)
        self._channel_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._channel_list.customContextMenuRequested.connect(self._channel_context_menu)
        left_layout.addWidget(self._channel_list)
        self._splitter.addWidget(left)

        # Right: videos (table) + description (text browser), stacked vertically.
        right_splitter = QSplitter(Qt.Orientation.Vertical)

        # Video table (QTableView + model).
        self._vid_model = _VideoTableModel(self)
        self._vid_proxy = _VideoSortProxy(self)
        self._vid_proxy.setSourceModel(self._vid_model)

        self._vid_table = _VideoTableView()
        self._vid_table.setModel(self._vid_proxy)
        self._vid_table.setItemDelegate(SimpleItemDelegate(self._vid_table))
        self._vid_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._vid_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._vid_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._vid_table.setSortingEnabled(True)
        self._vid_table.setDragEnabled(True)
        self._vid_table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self._vid_table.setShowGrid(False)
        self._vid_table.verticalHeader().setVisible(False)
        self._vid_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._vid_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        h = self._vid_table.horizontalHeader()
        h.setSectionResizeMode(_COL_TITLE, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(_COL_DATE, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_COL_DURATION, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_COL_VIEWS, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_COL_DOWNLOADED, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionsMovable(True)

        self._vid_table.doubleClicked.connect(self._on_video_double_click)
        self._vid_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._vid_table.customContextMenuRequested.connect(self._video_context_menu)

        # Show description when video selection changes.
        self._vid_table.selectionModel().currentRowChanged.connect(self._on_video_selected)

        # Pagination: load more videos when scrolling near the bottom.
        self._vid_table.verticalScrollBar().valueChanged.connect(self._on_scroll)

        right_splitter.addWidget(self._vid_table)

        # Description panel.
        self._description = QTextBrowser()
        self._description.setOpenExternalLinks(True)
        self._description.setPlaceholderText("Select a video to see its description.")
        right_splitter.addWidget(self._description)

        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 1)

        self._splitter.addWidget(right_splitter)

        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 3)

        layout.addWidget(self._splitter, stretch=1)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_channels(self, channels: list[Channel]) -> None:
        self._channels = channels
        self._channel_list.clear()
        for ch in channels:
            item = QListWidgetItem(ch.title)
            item.setSizeHint(QSize(0, _CHAN_ROW_HEIGHT))
            item.setData(Qt.ItemDataRole.UserRole, ch.id)
            item.setData(_ROLE_CHANNEL_DB_ID, ch.id)

            # Load cached thumbnail.
            art_pixmap = self._load_channel_art(ch.id)
            if art_pixmap:
                item.setData(_ROLE_CHAN_ART, art_pixmap)

            # Channel ID as description snippet.
            item.setData(_ROLE_CHAN_DESCRIPTION, f"youtube.com/channel/{ch.channel_id}")
            self._channel_list.addItem(item)

    def _load_channel_art(self, channel_db_id: int) -> QPixmap | None:
        """Load cached channel thumbnail from disk."""
        for ext in (".jpg", ".png"):
            path = _THUMB_CACHE_DIR / f"channel_{channel_db_id}{ext}"
            if path.exists():
                pm = QPixmap()
                if pm.load(str(path)):
                    return pm.scaled(
                        _CHAN_ART_SIZE, _CHAN_ART_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
        return None

    _PAGE_SIZE = 100
    _loaded_count: int = 0
    _total_count: int = 0
    _loading_more: bool = False

    def load_videos(self, videos: list[Video]) -> None:
        self._loaded_count = len(videos)
        self._vid_model.load(videos)
        self._vid_proxy.sort(_COL_DATE, Qt.SortOrder.DescendingOrder)
        self._description.clear()

    def load_videos_paginated(self, videos: list[Video], total: int) -> None:
        self._loaded_count = len(videos)
        self._total_count = total
        self._vid_model.load(videos)
        self._vid_proxy.sort(_COL_DATE, Qt.SortOrder.DescendingOrder)
        self._description.clear()

    def append_videos(self, videos: list[Video]) -> None:
        self._loaded_count += len(videos)
        self._vid_model.append_videos(videos)
        self._loading_more = False

    @property
    def has_more_videos(self) -> bool:
        return self._loaded_count < self._total_count

    def set_sync_visible(self, visible: bool) -> None:
        """Show/hide the Sync Subscriptions button (hidden in guest mode)."""
        self._btn_sync.setVisible(visible)

    def set_playlists(self, playlists: list[tuple[int, str]]) -> None:
        """Update the playlist list for the 'Add to Playlist' context submenu."""
        self._playlists = playlists

    def update_download_progress(self, video_db_id: int, pct: int) -> None:
        self._vid_model.update_download_progress(video_db_id, pct)

    def mark_download_complete(self, video_db_id: int) -> None:
        self._vid_model.mark_download_complete(video_db_id)

    def set_show_urls(self, show: bool) -> None:
        """Show or hide channel URLs in the channel list."""
        self._channel_delegate.set_show_urls(show)

    def _on_scroll(self, value: int) -> None:
        if self._loading_more or not self.has_more_videos:
            return
        sb = self._vid_table.verticalScrollBar()
        if value >= sb.maximum() - 50:
            self._loading_more = True
            cid = self._current_channel_db_id
            if cid:
                self.load_more_videos.emit(cid, self._PAGE_SIZE, self._loaded_count)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _source_video_at(self, proxy_index: QModelIndex) -> Video | None:
        source_idx = self._vid_proxy.mapToSource(proxy_index)
        return self._vid_model.video_at(source_idx.row())

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_channel_double_click(self, item: QListWidgetItem) -> None:
        channel_db_id = item.data(Qt.ItemDataRole.UserRole)
        if channel_db_id is not None:
            self.play_channel_requested.emit(channel_db_id)

    def _on_add_channel(self) -> None:
        url, ok = QInputDialog.getText(
            self, "Add YouTube Channel", "Channel URL (e.g. youtube.com/@name):"
        )
        if ok and url.strip():
            self.add_channel_requested.emit(url.strip())

    def _on_search(self) -> None:
        query = self._search_bar.text().strip()
        if query:
            self.search_requested.emit(query)

    def _on_channel_selected(self, row: int) -> None:
        if 0 <= row < len(self._channels):
            self._current_channel_db_id = self._channels[row].id
        else:
            self._current_channel_db_id = None
            self._vid_model.load([])
            self._description.clear()

    def _on_video_selected(self, current: QModelIndex, previous: QModelIndex) -> None:
        v = self._source_video_at(current)
        if v and v.description:
            # Plain text — YouTube descriptions aren't HTML.
            self._description.setPlainText(v.description)
        else:
            self._description.clear()

    def _channel_context_menu(self, pos) -> None:
        item = self._channel_list.itemAt(pos)
        if not item:
            return
        channel_db_id = item.data(Qt.ItemDataRole.UserRole)

        menu = QMenu(self)

        refresh = QAction("Refresh", self)
        refresh.triggered.connect(lambda: self.refresh_requested.emit(channel_db_id))
        menu.addAction(refresh)

        menu.addSeparator()
        remove = QAction("Remove Channel", self)
        remove.triggered.connect(lambda: self._confirm_remove(channel_db_id))
        menu.addAction(remove)

        menu.exec(self._channel_list.viewport().mapToGlobal(pos))

    def _confirm_remove(self, channel_db_id: int) -> None:
        resp = QMessageBox.question(
            self, "Remove Channel",
            "Remove this channel and all its videos?",
        )
        if resp == QMessageBox.StandardButton.Yes:
            self.remove_channel_requested.emit(channel_db_id)

    def _on_video_double_click(self, proxy_index: QModelIndex) -> None:
        v = self._source_video_at(proxy_index)
        if v:
            self.play_video_requested.emit(v.id)

    def _video_context_menu(self, pos) -> None:
        proxy_index = self._vid_table.indexAt(pos)
        if not proxy_index.isValid():
            return
        v = self._source_video_at(proxy_index)
        if v is None:
            return

        menu = QMenu(self)

        play = QAction("Play", self)
        play.triggered.connect(lambda: self.play_video_requested.emit(v.id))
        menu.addAction(play)

        add_to_queue = QAction("Add to Queue", self)
        add_to_queue.triggered.connect(lambda: self.add_to_queue_requested.emit(v.id))
        menu.addAction(add_to_queue)

        # Add to Playlist submenu.
        if self._playlists:
            playlist_menu = QMenu("Add to Playlist", self)
            for pid, pname in self._playlists:
                act = QAction(pname, self)
                act.triggered.connect(
                    lambda checked=False, _pid=pid: self.add_to_playlist_requested.emit(v.id, _pid)
                )
                playlist_menu.addAction(act)
            menu.addMenu(playlist_menu)

        menu.addSeparator()

        if not v.downloaded:
            dl = QAction("Download", self)
            dl.triggered.connect(lambda: self.download_requested.emit(v.id))
            menu.addAction(dl)
        else:
            del_dl = QAction("Delete Download", self)
            del_dl.triggered.connect(
                lambda: self.delete_download_requested.emit(v.id)
            )
            menu.addAction(del_dl)

        # Quality preference submenu.
        menu.addSeparator()
        from quaver.core.config import load_config, save_config
        config = load_config()
        quality_menu = menu.addMenu("Quality")
        for preset, label in [
            ("best", "Best"),
            ("1080p", "1080p"),
            ("720p", "720p"),
            ("480p", "480p"),
            ("audio_only", "Audio Only"),
        ]:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(config.youtube_video_quality == preset)
            action.triggered.connect(
                lambda checked, q=preset: self._set_quality(q)
            )
            quality_menu.addAction(action)

        menu.addSeparator()
        open_web = QAction("Open in Browser", self)
        open_web.triggered.connect(
            lambda: webbrowser.open(f"https://www.youtube.com/watch?v={v.video_id}")
        )
        menu.addAction(open_web)

        menu.exec(self._vid_table.viewport().mapToGlobal(pos))

    def _set_quality(self, quality: str) -> None:
        """Update the YouTube video quality preference."""
        from quaver.core.config import load_config, save_config
        config = load_config()
        config.youtube_video_quality = quality
        save_config(config)
