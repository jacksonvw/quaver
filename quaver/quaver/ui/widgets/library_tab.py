"""Three-panel library tab: artist list | album view.

The queue is a fixed panel in the main window, outside this widget.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Callable

from PySide6.QtCore import QMimeData, QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from quaver.core.library import TRACK_IDS_MIME, Track
from quaver.ui.widgets import art_cache
from quaver.ui.widgets.album_view import AlbumView
from quaver.ui.widgets.delegates import ConsistentItemDelegate, HoverTrackingMixin
from quaver.ui.widgets.folder_tree_view import FolderTreeView

_ARTIST_ART_SIZE = 40
_ARTIST_ROW_HEIGHT = 52
_ARTIST_PADDING = 4
_ROLE_ALBUM_COUNT = Qt.ItemDataRole.UserRole + 10
_ROLE_ART_FILEPATH = Qt.ItemDataRole.UserRole + 11
_ROLE_ART_PIXMAP = Qt.ItemDataRole.UserRole + 12
_ROLE_EPISODE_COUNT = Qt.ItemDataRole.UserRole + 13
_ROLE_SHOW_ID = Qt.ItemDataRole.UserRole + 14


_ALBUM_LABEL = {"music": "album", "series": "season", "movies": "movie"}


class _ArtistDelegate(ConsistentItemDelegate):
    """Paints artist list items: [art] [name / X albums]."""

    def __init__(self, parent=None, media_type: str = "music") -> None:
        super().__init__(parent)
        self._media_type = media_type

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), _ARTIST_ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        p = _ARTIST_PADDING

        # Consistent selection/hover background.
        self.paint_item_background(painter, option, index)

        x = rect.x() + p
        y = rect.y() + p
        art_s = _ARTIST_ROW_HEIGHT - p * 2

        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        album_count = index.data(_ROLE_ALBUM_COUNT)
        art: QPixmap | None = index.data(_ROLE_ART_PIXMAP)
        is_all = index.data(Qt.ItemDataRole.UserRole) is None

        # Use palette colors so we work with any theme.
        text_color = option.palette.text().color()
        sub_color = option.palette.placeholderText().color()

        # Art thumbnail (skip for "All" entry).
        if not is_all:
            if art and not art.isNull():
                painter.drawPixmap(x, y, art.scaled(
                    art_s, art_s,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
            else:
                painter.fillRect(x, y, art_s, art_s, QColor(80, 80, 80))
            text_x = x + art_s + p * 2
        else:
            text_x = x + p

        text_w = rect.right() - text_x - p

        # Name (bold).
        name_font = QFont(option.font)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(text_color)
        fm = QFontMetrics(name_font)
        elided = fm.elidedText(name, Qt.TextElideMode.ElideRight, text_w)
        painter.drawText(text_x, y, text_w, fm.height(), Qt.AlignmentFlag.AlignLeft, elided)

        # Subtitle (lighter, smaller): episode/season counts or album count.
        episode_count = index.data(_ROLE_EPISODE_COUNT)
        if self._media_type == "series" and episode_count is not None:
            sub_font = QFont(option.font)
            sub_font.setPointSize(max(7, option.font.pointSize() - 2))
            painter.setFont(sub_font)
            painter.setPen(sub_color)
            sub_fm = QFontMetrics(sub_font)
            parts = [f"{episode_count} episode{'s' if episode_count != 1 else ''}"]
            if album_count and album_count > 0:
                parts.append(f"{album_count} season{'s' if album_count != 1 else ''}")
            count_text = " \u00b7 ".join(parts)
            painter.drawText(text_x, y + fm.height() + 1, text_w, sub_fm.height(),
                             Qt.AlignmentFlag.AlignLeft, count_text)
        elif album_count is not None and album_count > 0:
            sub_font = QFont(option.font)
            sub_font.setPointSize(max(7, option.font.pointSize() - 2))
            painter.setFont(sub_font)
            painter.setPen(sub_color)
            sub_fm = QFontMetrics(sub_font)
            noun = _ALBUM_LABEL.get(self._media_type, "album")
            count_text = f"{album_count} {noun}{'s' if album_count != 1 else ''}"
            painter.drawText(text_x, y + fm.height() + 1, text_w, sub_fm.height(),
                             Qt.AlignmentFlag.AlignLeft, count_text)

        painter.restore()


class _DraggableArtistList(HoverTrackingMixin, QListWidget):
    """Artist list that produces track-ID MIME data when dragged."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_hover_tracking()
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self._track_resolver: Callable[[str | None], list[Track]] | None = None

    def set_track_resolver(
        self, fn: Callable[[str | None], list[Track]]
    ) -> None:
        self._track_resolver = fn

    def startDrag(self, supportedActions) -> None:
        """Lightweight QDrag to avoid compositor blanking with translucent windows."""
        items = self.selectedItems()
        if not items:
            return
        mime = self.mimeData(items)
        if mime is None:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        label = items[0].text() if len(items) == 1 else f"{len(items)} artist(s)"
        hint = QPixmap(140, 24)
        hint.fill(QColor(255, 255, 255, 40))
        p = QPainter(hint)
        p.setPen(QColor(255, 255, 255, 180))
        p.drawText(hint.rect(), Qt.AlignmentFlag.AlignCenter, label)
        p.end()
        drag.setPixmap(hint)
        drag.exec(Qt.DropAction.CopyAction)

    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:
        if not self._track_resolver or not items:
            return super().mimeData(items)

        all_ids: list[int] = []
        for item in items:
            artist_filter = item.data(Qt.ItemDataRole.UserRole)
            tracks = self._track_resolver(artist_filter)
            all_ids.extend(t.id for t in tracks if t.id >= 0)

        mime = QMimeData()
        mime.setData(TRACK_IDS_MIME, json.dumps(all_ids).encode())
        mime.setText(", ".join(item.text() for item in items))
        return mime


class LibraryTabWidget(QWidget):
    """Three-panel library tab.

    Left: artist list (click to filter)
    Center: album-centric view (filtered by selected artist)
    Playlist is a fixed panel in the main window, outside this widget.
    """

    # Signals forwarded to main window.
    play_album = Signal(list, int)  # album tracks, start index
    add_to_queue = Signal(list)  # tracks to append
    add_to_saved_playlist = Signal(list, int)  # track_ids, playlist_id
    edit_tags_requested = Signal(list)  # track IDs
    mark_watched_requested = Signal(list, bool)  # track_ids, watched
    view_online_requested = Signal(int)  # track_id
    refresh_metadata_requested = Signal()  # trigger enrichment for this tab
    scrape_metadata_requested = Signal(list)  # track_ids to re-scrape
    scrape_show_requested = Signal(int)  # show_id to re-scrape
    change_match_requested = Signal(int)  # show_id to change match
    show_info_requested = Signal(int)  # show_id to display info
    movie_info_requested = Signal(int)  # track_id for movie info dialog
    mark_show_watched_requested = Signal(int, bool)  # show_id, watched
    remove_show_requested = Signal(int)  # show_id
    open_in_minim_requested = Signal(list)  # filesystem paths (str)

    def __init__(self, parent: QWidget | None = None, media_type: str = "music") -> None:
        super().__init__(parent)
        self._media_type = media_type
        self._all_tracks: list[Track] = []
        self._poster_paths: dict[str, str] = {}
        self._video_states: dict[int, tuple[bool, int]] = {}
        self._video_genres: dict[int, str] = {}  # track_id → genre string
        self._show_data: list[dict] = []  # video_shows rows with counts
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Files tab: live filesystem tree, no DB, no scraping. ──
        # Bypass the artist/genre sidebar + album view entirely; the
        # FolderTreeView owns its own search bar and context menu.
        if self._media_type == "files":
            self._folder_tree = FolderTreeView(self)
            self._folder_tree.play_tracks_requested.connect(self.play_album.emit)
            self._folder_tree.add_to_queue_requested.connect(self.add_to_queue.emit)
            self._folder_tree.open_in_minim_requested.connect(
                self.open_in_minim_requested.emit
            )
            layout.addWidget(self._folder_tree)
            # The fields the rest of the class touches (filter bar,
            # combos, splitter, sidebars, album view) stay None — every
            # method that uses them must guard on ``self._media_type``.
            self._filter_bar = None
            self._search_edit = None
            self._genre_combo = None
            self._watched_combo = None
            self._refresh_meta_btn = None
            self._splitter = None
            self._artist_list = None
            self._artist_delegate = None
            self._album_view = None
            return

        # Filter bar — compact, fixed height.
        self._filter_bar = QWidget()
        self._filter_bar.setFixedHeight(32)
        fb_layout = QHBoxLayout(self._filter_bar)
        fb_layout.setContentsMargins(4, 2, 4, 2)
        fb_layout.setSpacing(6)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search\u2026")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMaximumWidth(250)
        self._search_edit.textChanged.connect(self._apply_filters)
        fb_layout.addWidget(self._search_edit)

        self._genre_combo = QComboBox()
        self._genre_combo.setMinimumWidth(120)
        self._genre_combo.setMaximumWidth(200)
        self._genre_combo.addItem("All Genres")
        self._genre_combo.currentIndexChanged.connect(self._apply_filters)
        fb_layout.addWidget(self._genre_combo)

        # Watched filter — only shown for video tabs.
        self._watched_combo = QComboBox()
        self._watched_combo.addItems(["All", "Unwatched", "Watched"])
        self._watched_combo.setMaximumWidth(120)
        self._watched_combo.currentIndexChanged.connect(self._apply_filters)
        self._watched_combo.setVisible(self._media_type in ("series", "movies"))
        fb_layout.addWidget(self._watched_combo)

        fb_layout.addStretch()

        # Refresh Metadata button — only for video tabs.
        self._refresh_meta_btn = QPushButton("Rescan")
        self._refresh_meta_btn.setMaximumWidth(100)
        self._refresh_meta_btn.setVisible(self._media_type in ("series", "movies"))
        self._refresh_meta_btn.clicked.connect(self.refresh_metadata_requested.emit)
        fb_layout.addWidget(self._refresh_meta_btn)

        layout.addWidget(self._filter_bar)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setObjectName("library_splitter")

        # Left: artist/genre list (draggable — produces track IDs).
        self._artist_list = _DraggableArtistList()
        self._artist_delegate = _ArtistDelegate(self._artist_list, media_type=self._media_type)
        self._artist_list.setItemDelegate(self._artist_delegate)
        self._artist_list.currentRowChanged.connect(self._on_artist_selected)
        self._artist_list.doubleClicked.connect(self._on_artist_double_clicked)
        # Context menu for scraping (video tabs only).
        if self._media_type in ("series", "movies"):
            self._artist_list.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu
            )
            self._artist_list.customContextMenuRequested.connect(
                self._on_sidebar_context_menu
            )
        self._splitter.addWidget(self._artist_list)

        # Connect shared art cache for artist thumbnails.
        art_cache.on_art_ready().connect(self._on_artist_art_ready)

        # Center: album view.
        self._album_view = AlbumView(media_type=self._media_type)
        self._album_view.play_album.connect(self.play_album.emit)
        self._album_view.add_to_queue.connect(self.add_to_queue.emit)
        self._album_view.add_to_saved_playlist.connect(self.add_to_saved_playlist.emit)
        self._album_view.edit_tags_requested.connect(self.edit_tags_requested.emit)
        self._album_view.mark_watched_requested.connect(self.mark_watched_requested.emit)
        self._album_view.view_online_requested.connect(self.view_online_requested.emit)
        self._album_view.scrape_metadata_requested.connect(self.scrape_metadata_requested.emit)
        self._album_view.movie_info_requested.connect(self.movie_info_requested.emit)
        self._album_view.open_in_minim_requested.connect(
            self.open_in_minim_requested.emit
        )
        self._splitter.addWidget(self._album_view)

        self._splitter.setStretchFactor(0, 1)  # artist list
        self._splitter.setStretchFactor(1, 3)  # album view

        layout.addWidget(self._splitter)

    def set_source_paths(self, paths: list[str]) -> None:
        """Point a ``files`` tab at its source folders.  No-op otherwise.

        Files tabs are live filesystem trees — they never call
        ``load_tracks`` because there's nothing in the DB to load.
        """
        if self._media_type != "files":
            return
        self._folder_tree.set_source_paths(paths)

    def set_hide_extensions(self, hide: bool) -> None:
        """Files-tab only — hide file extensions in the grid display.
        No-op for music/series/movies tabs.
        """
        if self._media_type != "files":
            return
        self._folder_tree.set_hide_extensions(hide)

    def load_tracks(
        self, tracks: list[Track], poster_paths: dict[str, str] | None = None,
        video_states: dict[int, tuple[bool, int]] | None = None,
        video_genres: dict[int, str] | None = None,
        show_data: list[dict] | None = None,
    ) -> None:
        """Load tracks and rebuild the artist list."""
        # Files tabs are live filesystem trees — no DB-backed tracks.
        if self._media_type == "files":
            return
        self._all_tracks = tracks
        self._poster_paths = poster_paths or {}
        self._video_states = video_states or {}
        self._video_genres = video_genres or {}
        self._show_data = show_data or []
        self._artist_list.set_track_resolver(self._resolve_artist_tracks)

        if self._media_type == "movies":
            self._build_genre_sidebar(tracks)
        elif self._media_type == "series" and self._show_data:
            self._build_show_sidebar()
        else:
            self._build_artist_sidebar(tracks)

        # Populate genre filter combo (hidden for movies — sidebar replaces it).
        self._genre_combo.blockSignals(True)
        self._genre_combo.clear()
        self._genre_combo.addItem("All Genres")
        if self._media_type != "movies":
            genres_set: set[str] = set()
            for t in tracks:
                g = self._video_genres.get(t.id, "") or t.genre or ""
                if g:
                    for part in g.split(","):
                        part = part.strip()
                        if part:
                            genres_set.add(part)
            for g in sorted(genres_set, key=str.lower):
                self._genre_combo.addItem(g)
        self._genre_combo.setVisible(self._media_type not in ("movies",))
        self._genre_combo.blockSignals(False)

    def _build_artist_sidebar(self, tracks: list[Track]) -> None:
        """Populate the sidebar with artists (for music/series tabs)."""
        # Save current selection for restoration after rebuild.
        cur = self._artist_list.currentItem()
        saved = cur.data(Qt.ItemDataRole.UserRole) if cur else None

        artist_albums: dict[str, set[str]] = defaultdict(set)
        artist_art_fp: dict[str, str] = {}
        for t in tracks:
            artist = t.album_artist or t.artist or "Unknown Artist"
            artist_albums[artist].add(t.album or "")
            if artist not in artist_art_fp:
                artist_art_fp[artist] = t.filepath

        self._artist_list.blockSignals(True)
        self._artist_list.clear()

        all_item = QListWidgetItem("All")
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        all_item.setSizeHint(QSize(0, _ARTIST_ROW_HEIGHT))
        self._artist_list.addItem(all_item)

        posters = self._poster_paths
        for name in sorted(artist_albums.keys(), key=str.lower):
            item = QListWidgetItem(name)
            item.setSizeHint(QSize(0, _ARTIST_ROW_HEIGHT))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(_ROLE_ALBUM_COUNT, len(artist_albums[name]))
            fp = artist_art_fp.get(name, "")
            item.setData(_ROLE_ART_FILEPATH, fp)
            if fp:
                poster = posters.get(fp, "")
                pm = art_cache.get_thumbnail(fp, _ARTIST_ART_SIZE, poster_path=poster)
                if pm is not None:
                    item.setData(_ROLE_ART_PIXMAP, pm)
            self._artist_list.addItem(item)

        self._artist_list.blockSignals(False)
        self._restore_sidebar_selection(saved)

    def _build_show_sidebar(self) -> None:
        """Populate the sidebar from video_shows data (canonical titles, posters)."""
        # Save current selection (show_id) for restoration.
        cur = self._artist_list.currentItem()
        saved = cur.data(Qt.ItemDataRole.UserRole) if cur else None

        self._artist_list.blockSignals(True)
        self._artist_list.clear()

        for show in self._show_data:
            show_id = show["id"]
            title = show["title"] or show["folder_title"]
            item = QListWidgetItem(title)
            item.setSizeHint(QSize(0, _ARTIST_ROW_HEIGHT))
            item.setData(Qt.ItemDataRole.UserRole, show_id)
            item.setData(_ROLE_SHOW_ID, show_id)
            item.setData(_ROLE_ALBUM_COUNT, show.get("season_count", 0))
            item.setData(_ROLE_EPISODE_COUNT, show.get("episode_count", 0))
            # Use show poster for thumbnail.
            poster = show.get("poster_path") or ""
            cache_key = f"show:{show_id}"
            item.setData(_ROLE_ART_FILEPATH, cache_key)
            if poster:
                pm = art_cache.get_thumbnail(cache_key, _ARTIST_ART_SIZE, poster_path=poster)
                if pm is not None:
                    item.setData(_ROLE_ART_PIXMAP, pm)
            self._artist_list.addItem(item)

        self._artist_list.blockSignals(False)
        self._restore_sidebar_selection(saved)

    def _build_genre_sidebar(self, tracks: list[Track]) -> None:
        """Populate the sidebar with genres (for movie tabs)."""
        # Save current selection (genre name) for restoration.
        cur = self._artist_list.currentItem()
        saved = cur.data(Qt.ItemDataRole.UserRole) if cur else None

        genre_counts: dict[str, int] = defaultdict(int)
        for t in tracks:
            g = self._video_genres.get(t.id, "") or t.genre or ""
            if g:
                for part in g.split(","):
                    part = part.strip()
                    if part:
                        genre_counts[part] += 1

        self._artist_list.blockSignals(True)
        self._artist_list.clear()

        all_item = QListWidgetItem("All")
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        all_item.setSizeHint(QSize(0, _ARTIST_ROW_HEIGHT))
        self._artist_list.addItem(all_item)

        for genre in sorted(genre_counts.keys(), key=str.lower):
            item = QListWidgetItem(genre)
            item.setSizeHint(QSize(0, _ARTIST_ROW_HEIGHT))
            item.setData(Qt.ItemDataRole.UserRole, genre)
            item.setData(_ROLE_ALBUM_COUNT, genre_counts[genre])
            self._artist_list.addItem(item)

        self._artist_list.blockSignals(False)
        self._restore_sidebar_selection(saved)

    def _restore_sidebar_selection(self, saved_value: object) -> None:
        """Restore sidebar selection after rebuild, or fall back to row 0."""
        if saved_value is not None:
            for i in range(self._artist_list.count()):
                item = self._artist_list.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == saved_value:
                    self._artist_list.setCurrentRow(i)
                    return
        self._artist_list.setCurrentRow(0)

    def _on_artist_double_clicked(self, index) -> None:
        """Double-click artist: replace playlist with all their tracks, play from start."""
        row = index.row()
        if row < 0:
            return
        item = self._artist_list.item(row)
        if not item:
            return

        sidebar_filter = item.data(Qt.ItemDataRole.UserRole)
        if sidebar_filter is None:
            tracks = list(self._all_tracks)
        elif self._media_type == "series" and self._show_data:
            tracks = [t for t in self._all_tracks if t.show_id == sidebar_filter]
        else:
            tracks = [
                t for t in self._all_tracks
                if (t.album_artist or t.artist or "Unknown Artist") == sidebar_filter
            ]

        if tracks:
            # Sort by album year, then disc/track for consistent playback order.
            tracks.sort(key=lambda t: (
                t.year or 0, (t.album or "").lower(),
                t.disc_number or 0, t.track_number or 0,
            ))
            self.play_album.emit(tracks, 0)

    def _resolve_artist_tracks(self, sidebar_filter) -> list[Track]:
        """Resolve a sidebar filter to a sorted list of tracks (used by drag MIME).

        Sorted by year desc, album name, disc number, track number — matching
        the album view's display order.
        """
        if sidebar_filter is None:
            tracks = list(self._all_tracks)
        elif self._media_type == "movies":
            genre_lower = sidebar_filter.lower()
            tracks = [
                t for t in self._all_tracks
                if genre_lower in (self._video_genres.get(t.id, "") or t.genre or "").lower()
            ]
        elif self._media_type == "series" and self._show_data:
            tracks = [t for t in self._all_tracks if t.show_id == sidebar_filter]
        else:
            tracks = [
                t for t in self._all_tracks
                if (t.album_artist or t.artist or "Unknown Artist") == sidebar_filter
            ]
        tracks.sort(key=lambda t: (
            -(t.year or 0), (t.album or "").lower(),
            t.disc_number or 0, t.track_number or 0,
        ))
        return tracks

    def set_playing_track(self, filepath: str | None) -> None:
        """Forward now-playing indicator to the album view."""
        if self._media_type == "files":
            return
        self._album_view.set_playing_track(filepath)

    def update_video_state(
        self, track_id: int, watched: bool, position_ms: int,
    ) -> None:
        """Update a single track's watched/progress state in the album view."""
        if self._media_type == "files":
            return
        self._video_states[track_id] = (watched, position_ms)
        self._album_view.update_video_state(track_id, watched, position_ms)

    def set_show_genre(self, show: bool) -> None:
        """Toggle genre display in album headers."""
        if self._media_type == "files":
            return
        self._album_view.set_show_genre(show)

    def set_empty_hint(self, text: str) -> None:
        """Text for an empty album view.  No-op on files tabs."""
        if self._media_type == "files":
            return
        self._album_view.set_empty_hint(text)

    def set_engine(self, engine) -> None:
        """Pass-through engine reference for video stream menus.

        No-op on files tabs (no album view).
        """
        if self._media_type == "files":
            return
        self._album_view.set_engine(engine)

    def set_saved_playlists(
        self, playlists: list[tuple[int, str, int]],
    ) -> None:
        """Update saved playlist list for context menu."""
        if self._media_type == "files":
            return
        self._album_view.set_saved_playlists(playlists)

    def _on_artist_art_ready(self, filepath: str, size: int, pixmap: QPixmap | None) -> None:
        """Update artist list items when art finishes loading."""
        if size != _ARTIST_ART_SIZE or pixmap is None:
            return
        for i in range(self._artist_list.count()):
            item = self._artist_list.item(i)
            if item and item.data(_ROLE_ART_FILEPATH) == filepath:
                item.setData(_ROLE_ART_PIXMAP, pixmap)
                break

    def _get_filtered_tracks(self) -> list[Track]:
        """Apply search, genre, and watched filters to the current track set."""
        # Start with sidebar-filtered tracks (artist for music/series, genre for movies).
        item = self._artist_list.currentItem()
        sidebar_filter = item.data(Qt.ItemDataRole.UserRole) if item else None
        if sidebar_filter is None:
            tracks = list(self._all_tracks)
        elif self._media_type == "movies":
            # Sidebar holds genre names for movie tabs.
            genre_lower = sidebar_filter.lower()
            tracks = [
                t for t in self._all_tracks
                if genre_lower in (self._video_genres.get(t.id, "") or t.genre or "").lower()
            ]
        elif self._media_type == "series" and self._show_data:
            # Sidebar holds show_id (int) for series tabs.
            tracks = [t for t in self._all_tracks if t.show_id == sidebar_filter]
        else:
            tracks = [
                t for t in self._all_tracks
                if (t.album_artist or t.artist or "Unknown Artist") == sidebar_filter
            ]

        # Text search filter.
        query = self._search_edit.text().strip().lower()
        if query:
            tracks = [
                t for t in tracks
                if query in (t.title or "").lower()
                or query in (t.artist or "").lower()
                or query in (t.album or "").lower()
                or query in (t.album_artist or "").lower()
            ]

        # Genre filter.
        genre_idx = self._genre_combo.currentIndex()
        if genre_idx > 0:
            selected_genre = self._genre_combo.currentText().lower()
            def _has_genre(t: Track) -> bool:
                g = self._video_genres.get(t.id, "") or t.genre or ""
                return selected_genre in g.lower()
            tracks = [t for t in tracks if _has_genre(t)]

        # Watched filter (video tabs only).
        watched_idx = self._watched_combo.currentIndex()
        if watched_idx > 0 and self._video_states:
            if watched_idx == 1:  # Unwatched
                tracks = [
                    t for t in tracks
                    if not self._video_states.get(t.id, (False, 0))[0]
                ]
            elif watched_idx == 2:  # Watched
                tracks = [
                    t for t in tracks
                    if self._video_states.get(t.id, (False, 0))[0]
                ]

        return tracks

    def _apply_filters(self) -> None:
        """Refilter album view based on current filter bar state."""
        tracks = self._get_filtered_tracks()
        show_posters = {
            s["id"]: s["poster_path"]
            for s in self._show_data if s.get("poster_path")
        } if self._show_data else {}
        self._album_view.load_tracks(
            tracks, poster_paths=self._poster_paths,
            video_states=self._video_states,
            video_genres=self._video_genres,
            show_posters=show_posters,
        )

    def _on_artist_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self._artist_list.item(row)
        if not item:
            return
        self._apply_filters()

    def _on_sidebar_context_menu(self, pos) -> None:
        """Context menu on sidebar: scrape metadata for a show or genre group."""
        item = self._artist_list.itemAt(pos)
        if not item:
            return
        sidebar_filter = item.data(Qt.ItemDataRole.UserRole)
        if sidebar_filter is None:
            return  # "All" entry — no action.

        menu = QMenu(self)

        if self._media_type == "series":
            # sidebar_filter is show_id (int) when show_data is loaded,
            # or artist name (str) for fallback — resolve accordingly.
            if self._show_data and isinstance(sidebar_filter, int):
                show_id = sidebar_filter
            else:
                tracks = self._resolve_artist_tracks(sidebar_filter)
                show_ids = {t.show_id for t in tracks if t.show_id}
                show_id = next(iter(show_ids)) if show_ids else None

            if show_id is not None:
                label = item.text()
                info = QAction(f"Show Information\u2026 \u2014 {label}", self)
                info.triggered.connect(lambda: self.show_info_requested.emit(show_id))
                menu.addAction(info)
                menu.addSeparator()
                scrape = QAction(f"Scrape Metadata \u2014 {label}", self)
                scrape.triggered.connect(lambda: self.scrape_show_requested.emit(show_id))
                menu.addAction(scrape)
                change = QAction(f"Change Match\u2026 \u2014 {label}", self)
                change.triggered.connect(lambda: self.change_match_requested.emit(show_id))
                menu.addAction(change)
                menu.addSeparator()
                # Check watched state for all episodes.
                ep_ids = [t.id for t in self._all_tracks if t.show_id == show_id]
                all_watched = all(
                    self._video_states.get(eid, (False, 0))[0] for eid in ep_ids
                ) if ep_ids else False
                if all_watched:
                    mark = QAction(f"Mark All Unwatched \u2014 {label}", self)
                    mark.triggered.connect(
                        lambda: self.mark_show_watched_requested.emit(show_id, False))
                else:
                    mark = QAction(f"Mark All Watched \u2014 {label}", self)
                    mark.triggered.connect(
                        lambda: self.mark_show_watched_requested.emit(show_id, True))
                menu.addAction(mark)
                menu.addSeparator()
                remove = QAction(f"Remove from Library \u2014 {label}", self)
                remove.triggered.connect(
                    lambda: self.remove_show_requested.emit(show_id))
                menu.addAction(remove)
        else:
            # For movies: scrape selected tracks by ID.
            tracks = self._resolve_artist_tracks(sidebar_filter)
            video_ids = [t.id for t in tracks if t.id >= 0]
            if video_ids:
                scrape = QAction(f"Scrape Metadata \u2014 {sidebar_filter}", self)
                scrape.triggered.connect(lambda: self.scrape_metadata_requested.emit(video_ids))
                menu.addAction(scrape)

        if menu.actions():
            menu.exec(self._artist_list.viewport().mapToGlobal(pos))
