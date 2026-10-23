"""Podcast panel — feed list + episode list + description viewer.

Episodes use QTableView + QAbstractTableModel + QSortFilterProxyModel
for native sorting and drag-to-queue support.
"""

from __future__ import annotations

import json
from email.utils import parsedate_to_datetime

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

from quaver.core.library import EPISODE_IDS_MIME
from quaver.ui.widgets.delegates import ConsistentItemDelegate, HoverTrackingMixin, SimpleItemDelegate
from quaver.core.podcast import Episode, Podcast, artwork_path

_FEED_ART_SIZE = 40
_FEED_ROW_HEIGHT = 52
_FEED_PADDING = 4
_ROLE_PODCAST_ID = Qt.ItemDataRole.UserRole + 10
_ROLE_FEED_ART = Qt.ItemDataRole.UserRole + 11
_ROLE_FEED_DESCRIPTION = Qt.ItemDataRole.UserRole + 12


class _FeedDelegate(ConsistentItemDelegate):
    """Paints podcast feed items: [art] [title / description snippet]."""

    _show_urls: bool = True

    def set_show_urls(self, show: bool) -> None:
        self._show_urls = show
        # Trigger repaint of the parent view.
        parent = self.parent()
        if parent and hasattr(parent, "viewport"):
            parent.viewport().update()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), _FEED_ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        p = _FEED_PADDING

        # Consistent selection/hover background.
        self.paint_item_background(painter, option, index)

        x = rect.x() + p
        y = rect.y() + p
        art_s = _FEED_ROW_HEIGHT - p * 2

        text_color = option.palette.text().color()
        sub_color = option.palette.placeholderText().color()

        # Art thumbnail.
        art: QPixmap | None = index.data(_ROLE_FEED_ART)
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
            desc = index.data(_ROLE_FEED_DESCRIPTION) or ""
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


# ── Episode model ─────────────────────────────────────────────────────

_COL_TITLE = 0
_COL_DATE = 1
_COL_DURATION = 2
_COL_DOWNLOADED = 3
_COL_PLAYED = 4
_N_COLS = 5

_ROLE_EPISODE = Qt.ItemDataRole.UserRole + 1
_ROLE_EPISODE_ID = Qt.ItemDataRole.UserRole + 2
_ROLE_SORT = Qt.ItemDataRole.UserRole + 3


def _format_date(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        if raw.startswith("20") or raw.startswith("19"):
            return raw[:10]
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return raw[:10] if len(raw) >= 10 else raw


def _sortable_date(raw: str | None) -> str:
    if not raw:
        return ""
    if raw.startswith("20") or raw.startswith("19"):
        return raw
    try:
        dt = parsedate_to_datetime(raw)
        return dt.isoformat()
    except (ValueError, TypeError):
        return raw


def _format_duration(ms: int) -> str:
    if not ms:
        return ""
    total_secs = ms // 1000
    m, s = divmod(total_secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _CachedEpisode:
    """Episode with pre-formatted display strings for fast rendering."""
    __slots__ = ("ep", "date_display", "date_sort", "dur_display")

    def __init__(self, ep: Episode) -> None:
        self.ep = ep
        self.date_display = _format_date(ep.published_at)
        self.date_sort = _sortable_date(ep.published_at)
        self.dur_display = _format_duration(ep.duration_ms)


class _EpisodeTableModel(QAbstractTableModel):
    """Flat table model for podcast episodes with drag support and pagination."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[_CachedEpisode] = []

    def load(self, episodes: list[Episode]) -> None:
        self.beginResetModel()
        self._items = [_CachedEpisode(ep) for ep in episodes]
        self.endResetModel()

    def append_episodes(self, episodes: list[Episode]) -> None:
        """Append a page of episodes (for lazy loading)."""
        if not episodes:
            return
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(episodes) - 1)
        self._items.extend(_CachedEpisode(ep) for ep in episodes)
        self.endInsertRows()

    def episode_at(self, row: int) -> Episode | None:
        if 0 <= row < len(self._items):
            return self._items[row].ep
        return None

    def update_download_progress(self, episode_id: int, pct: int) -> None:
        for i, item in enumerate(self._items):
            if item.ep.id == episode_id:
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
            return ["Title", "Date", "Duration", "Downloaded", "Played"][section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row >= len(self._items):
            return None
        item = self._items[row]
        ep = item.ep
        col = index.column()

        if role == _ROLE_EPISODE:
            return ep
        if role == _ROLE_EPISODE_ID:
            return ep.id

        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_TITLE:
                return ep.title
            if col == _COL_DATE:
                return item.date_display
            if col == _COL_DURATION:
                return item.dur_display
            if col == _COL_DOWNLOADED:
                return "Yes" if ep.downloaded else ""
            if col == _COL_PLAYED:
                return "Yes" if ep.played else ""

        if role == _ROLE_SORT:
            if col == _COL_TITLE:
                return (ep.title or "").lower()
            if col == _COL_DATE:
                return item.date_sort
            if col == _COL_DURATION:
                return ep.duration_ms or 0
            if col == _COL_DOWNLOADED:
                return int(ep.downloaded)
            if col == _COL_PLAYED:
                return int(ep.played)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (_COL_DATE, _COL_DURATION, _COL_DOWNLOADED, _COL_PLAYED):
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
        return [EPISODE_IDS_MIME]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        ids: list[int] = []
        seen: set[int] = set()
        for idx in indexes:
            if idx.column() != 0:
                continue
            ep = self._items[idx.row()].ep
            if ep.id not in seen:
                ids.append(ep.id)
                seen.add(ep.id)
        mime = QMimeData()
        mime.setData(EPISODE_IDS_MIME, json.dumps(ids).encode())
        return mime


class _EpisodeSortProxy(QSortFilterProxyModel):
    """Sort proxy using _ROLE_SORT for correct ordering."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        lv = left.data(_ROLE_SORT)
        rv = right.data(_ROLE_SORT)
        if lv is not None and rv is not None:
            return lv < rv
        return super().lessThan(left, right)


# ── Hover-tracking view subclasses ────────────────────────────────────


class _FeedListWidget(HoverTrackingMixin, QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_hover_tracking()
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


class _EpisodeTableView(HoverTrackingMixin, QTableView):
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
        p.drawText(hint.rect(), Qt.AlignmentFlag.AlignCenter, f"{len(rows)} episode(s)")
        p.end()
        drag.setPixmap(hint)
        drag.exec(Qt.DropAction.CopyAction)


# ── Podcast panel ─────────────────────────────────────────────────────


class PodcastPanel(QWidget):
    """Two-pane podcast view: feed list | episodes + description."""

    # Signals to main window.
    add_feed_requested = Signal(str)
    browse_requested = Signal()
    remove_feed_requested = Signal(int)
    refresh_requested = Signal(int)
    play_feed_requested = Signal(int)      # podcast_id — double-click on feed
    play_episode_requested = Signal(int)
    add_to_queue_requested = Signal(int)
    download_requested = Signal(int)
    delete_download_requested = Signal(int)
    mark_played_requested = Signal(int)
    settings_requested = Signal(int)
    load_more_episodes = Signal(int, int, int)  # podcast_id, limit, offset

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._podcasts: list[Podcast] = []
        self._current_podcast_id: int | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Top toolbar.
        toolbar = QHBoxLayout()
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self.browse_requested.emit)
        btn_add = QPushButton("Add URL")
        btn_add.clicked.connect(self._on_add_feed)
        toolbar.addWidget(btn_browse)
        toolbar.addWidget(btn_add)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Main splitter: feed list | episodes + description.
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: feed list.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Podcasts"))
        self._feed_list = _FeedListWidget()
        self._feed_delegate = _FeedDelegate(self._feed_list)
        self._feed_list.setItemDelegate(self._feed_delegate)
        self._feed_list.currentRowChanged.connect(self._on_feed_selected)
        self._feed_list.itemDoubleClicked.connect(self._on_feed_double_click)
        self._feed_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._feed_list.customContextMenuRequested.connect(self._feed_context_menu)
        left_layout.addWidget(self._feed_list)
        self._splitter.addWidget(left)

        # Right: episodes (table) + description (text browser), stacked vertically.
        right_splitter = QSplitter(Qt.Orientation.Vertical)

        # Episode table (QTableView + model).
        self._ep_model = _EpisodeTableModel(self)
        self._ep_proxy = _EpisodeSortProxy(self)
        self._ep_proxy.setSourceModel(self._ep_model)

        self._ep_table = _EpisodeTableView()
        self._ep_table.setModel(self._ep_proxy)
        self._ep_table.setItemDelegate(SimpleItemDelegate(self._ep_table))
        self._ep_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._ep_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._ep_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._ep_table.setSortingEnabled(True)
        self._ep_table.setDragEnabled(True)
        self._ep_table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self._ep_table.setShowGrid(False)
        self._ep_table.verticalHeader().setVisible(False)
        self._ep_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._ep_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        h = self._ep_table.horizontalHeader()
        h.setSectionResizeMode(_COL_TITLE, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(_COL_DATE, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_COL_DURATION, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_COL_DOWNLOADED, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(_COL_PLAYED, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionsMovable(True)

        self._ep_table.doubleClicked.connect(self._on_episode_double_click)
        self._ep_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._ep_table.customContextMenuRequested.connect(self._episode_context_menu)

        # Show description when episode selection changes.
        self._ep_table.selectionModel().currentRowChanged.connect(self._on_episode_selected)

        # Pagination: load more episodes when scrolling near the bottom.
        self._ep_table.verticalScrollBar().valueChanged.connect(self._on_scroll)

        right_splitter.addWidget(self._ep_table)

        # Description panel.
        self._description = QTextBrowser()
        self._description.setOpenExternalLinks(True)
        self._description.setPlaceholderText("Select an episode to see its description.")
        right_splitter.addWidget(self._description)

        right_splitter.setStretchFactor(0, 3)  # episodes
        right_splitter.setStretchFactor(1, 1)  # description

        self._splitter.addWidget(right_splitter)

        self._splitter.setStretchFactor(0, 1)  # feed list
        self._splitter.setStretchFactor(1, 3)  # episodes + description

        layout.addWidget(self._splitter, stretch=1)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_podcasts(self, podcasts: list[Podcast]) -> None:
        self._podcasts = podcasts
        self._feed_list.clear()
        for p in podcasts:
            item = QListWidgetItem(p.title)
            item.setSizeHint(QSize(0, _FEED_ROW_HEIGHT))
            item.setData(Qt.ItemDataRole.UserRole, p.id)
            item.setData(_ROLE_PODCAST_ID, p.id)

            # Load cached artwork.
            art_pixmap = self._load_feed_art(p.id)
            if art_pixmap:
                item.setData(_ROLE_FEED_ART, art_pixmap)

            # Use feed_url as a short description placeholder.
            item.setData(_ROLE_FEED_DESCRIPTION, p.feed_url)

            self._feed_list.addItem(item)

    def _load_feed_art(self, podcast_id: int) -> QPixmap | None:
        """Load cached podcast artwork from disk."""
        path = artwork_path(podcast_id)
        pm = QPixmap()
        if path is None or not pm.load(str(path)):
            return None
        return pm.scaled(
            _FEED_ART_SIZE, _FEED_ART_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    _PAGE_SIZE = 100
    _loaded_count: int = 0
    _total_count: int = 0
    _loading_more: bool = False

    def load_episodes(self, episodes: list[Episode]) -> None:
        self._loaded_count = len(episodes)
        self._ep_model.load(episodes)
        # Default sort: date descending.
        self._ep_proxy.sort(_COL_DATE, Qt.SortOrder.DescendingOrder)
        self._description.clear()

    def load_episodes_paginated(self, episodes: list[Episode], total: int) -> None:
        """Load first page of episodes with total count for pagination."""
        self._loaded_count = len(episodes)
        self._total_count = total
        self._ep_model.load(episodes)
        self._ep_proxy.sort(_COL_DATE, Qt.SortOrder.DescendingOrder)
        self._description.clear()

    def append_episodes(self, episodes: list[Episode]) -> None:
        """Append more episodes (pagination)."""
        self._loaded_count += len(episodes)
        self._ep_model.append_episodes(episodes)
        self._loading_more = False

    @property
    def has_more_episodes(self) -> bool:
        return self._loaded_count < self._total_count

    def update_download_progress(self, episode_id: int, pct: int) -> None:
        self._ep_model.update_download_progress(episode_id, pct)

    def set_show_urls(self, show: bool) -> None:
        """Show or hide feed URLs in the feed list."""
        self._feed_delegate.set_show_urls(show)

    def _on_scroll(self, value: int) -> None:
        """Load more episodes when scrolling near the bottom."""
        if self._loading_more or not self.has_more_episodes:
            return
        sb = self._ep_table.verticalScrollBar()
        if value >= sb.maximum() - 50:  # Within 50px of bottom.
            self._loading_more = True
            pid = self._current_podcast_id
            if pid:
                self.load_more_episodes.emit(pid, self._PAGE_SIZE, self._loaded_count)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _source_episode_at(self, proxy_index: QModelIndex) -> Episode | None:
        source_idx = self._ep_proxy.mapToSource(proxy_index)
        return self._ep_model.episode_at(source_idx.row())

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_add_feed(self) -> None:
        url, ok = QInputDialog.getText(self, "Add Podcast", "RSS Feed URL:")
        if ok and url.strip():
            self.add_feed_requested.emit(url.strip())

    def _on_feed_double_click(self, item: QListWidgetItem) -> None:
        podcast_id = item.data(Qt.ItemDataRole.UserRole)
        if podcast_id is not None:
            self.play_feed_requested.emit(podcast_id)

    def _on_feed_selected(self, row: int) -> None:
        if 0 <= row < len(self._podcasts):
            self._current_podcast_id = self._podcasts[row].id
        else:
            self._current_podcast_id = None
            self._ep_model.load([])
            self._description.clear()

    def _on_episode_selected(self, current: QModelIndex, previous: QModelIndex) -> None:
        ep = self._source_episode_at(current)
        if ep and ep.description:
            self._description.setHtml(ep.description)
        else:
            self._description.clear()

    def _feed_context_menu(self, pos) -> None:
        item = self._feed_list.itemAt(pos)
        if not item:
            return
        podcast_id = item.data(Qt.ItemDataRole.UserRole)

        menu = QMenu(self)
        refresh = QAction("Refresh", self)
        refresh.triggered.connect(lambda: self.refresh_requested.emit(podcast_id))
        menu.addAction(refresh)

        settings = QAction("Settings\u2026", self)
        settings.triggered.connect(lambda: self.settings_requested.emit(podcast_id))
        menu.addAction(settings)

        menu.addSeparator()
        remove = QAction("Remove Podcast", self)
        remove.triggered.connect(lambda: self._confirm_remove(podcast_id))
        menu.addAction(remove)

        menu.exec(self._feed_list.viewport().mapToGlobal(pos))

    def _confirm_remove(self, podcast_id: int) -> None:
        resp = QMessageBox.question(
            self, "Remove Podcast",
            "Remove this podcast and all downloaded episodes?",
        )
        if resp == QMessageBox.StandardButton.Yes:
            self.remove_feed_requested.emit(podcast_id)

    def _on_episode_double_click(self, proxy_index: QModelIndex) -> None:
        ep = self._source_episode_at(proxy_index)
        if ep:
            self.play_episode_requested.emit(ep.id)

    def _episode_context_menu(self, pos) -> None:
        proxy_index = self._ep_table.indexAt(pos)
        if not proxy_index.isValid():
            return
        ep = self._source_episode_at(proxy_index)
        if ep is None:
            return

        menu = QMenu(self)

        play = QAction("Play", self)
        play.triggered.connect(lambda: self.play_episode_requested.emit(ep.id))
        menu.addAction(play)

        add_to_queue = QAction("Add to Queue", self)
        add_to_queue.triggered.connect(lambda: self.add_to_queue_requested.emit(ep.id))
        menu.addAction(add_to_queue)

        menu.addSeparator()

        if not ep.downloaded:
            dl = QAction("Download", self)
            dl.triggered.connect(lambda: self.download_requested.emit(ep.id))
            menu.addAction(dl)
        else:
            delete_dl = QAction("Delete Download", self)
            delete_dl.triggered.connect(
                lambda: self.delete_download_requested.emit(ep.id)
            )
            menu.addAction(delete_dl)

        if not ep.played:
            mark = QAction("Mark as Played", self)
            mark.triggered.connect(lambda: self.mark_played_requested.emit(ep.id))
            menu.addAction(mark)

        menu.exec(self._ep_table.viewport().mapToGlobal(pos))
