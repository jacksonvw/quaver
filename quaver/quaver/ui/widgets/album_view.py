"""MusicBee-style album-centric library view — QTableView + setSpan().

Flat table model with row spanning: column 0 (art) spans all of an album's
rows as a single cell, painted once.  Columns 1-3 hold track info and receive
independent selection highlighting.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDrag,
    QFontMetrics,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from quaver.core.library import TRACK_IDS_MIME, Track
from quaver.ui.widgets import art_cache

# Layout constants.
_ART_THUMB = 80
_ART_COL_W = _ART_THUMB + 8
_NUM_COL_W = 30
_DUR_COL_W = 50
_ALBUM_ROW_H = 28
_TRACK_ROW_H = 20
_YEAR_ROW_H = 32
_MIN_SPACER_H = 8  # Breathing room after every album.
_MOVIE_ROW_H = 65  # Taller rows for inline poster art.
_MOVIE_ART_H = 56  # Poster height inside movie row.
_MOVIE_ART_W = 38  # Poster width (poster aspect ratio ~2:3).

# Row types.
_TYPE_YEAR = 0
_TYPE_ALBUM_HEADER = 1
_TYPE_TRACK = 2
_TYPE_SPACER = 3

# Custom data roles.
_ROLE_ROW_TYPE = Qt.ItemDataRole.UserRole + 1
_ROLE_TRACK = Qt.ItemDataRole.UserRole + 2
_ROLE_ALBUM_PIXMAP = Qt.ItemDataRole.UserRole + 3
_ROLE_ALBUM_TRACKS = Qt.ItemDataRole.UserRole + 4
_ROLE_IS_PLAYING = Qt.ItemDataRole.UserRole + 5
_ROLE_IS_LAST_IN_ALBUM = Qt.ItemDataRole.UserRole + 6
_ROLE_ALBUM_INDEX = Qt.ItemDataRole.UserRole + 7  # For alternating bands.
_ROLE_WATCHED = Qt.ItemDataRole.UserRole + 8  # bool: watched state
_ROLE_PROGRESS = Qt.ItemDataRole.UserRole + 9  # float 0.0–1.0: watch progress
_ROLE_ALBUM_GENRE = Qt.ItemDataRole.UserRole + 10  # str: genre for album header
_ROLE_TRACK_PIXMAP = Qt.ItemDataRole.UserRole + 11  # QPixmap: per-track poster (movies)


_ARTICLES = ("the ", "a ", "an ")


def _strip_articles(title: str) -> str:
    """Strip leading articles (The, A, An) for sort purposes."""
    lower = title.lower()
    for article in _ARTICLES:
        if lower.startswith(article):
            return title[len(article):]
    return title


def _ms_to_mmss(ms: int) -> str:
    total_secs = ms // 1000
    m, s = divmod(total_secs, 60)
    return f"{m}:{s:02d}"


def _format_album_duration(ms: int) -> str:
    """Format total album duration: '42 mins' or '1 hr 18 mins'."""
    if ms <= 0:
        return ""
    total_mins = round(ms / 60_000)
    if total_mins < 60:
        return _mins(total_mins)
    h, m = divmod(total_mins, 60)
    return f"{h} hr {_mins(m)}" if m else f"{h} hr"


def _mins(n: int) -> str:
    return f"{n} min" if n == 1 else f"{n} mins"


# ── Art loading (via shared cache) ───────────────────────────────────


# ── Node types ────────────────────────────────────────────────────────


@dataclass
class _TrackNode:
    track: Track
    album: _AlbumNode
    pixmap: QPixmap | None = None  # Per-track poster (used for movie flat layout).


@dataclass
class _AlbumNode:
    name: str
    year: int | None
    track_nodes: list[_TrackNode] = field(default_factory=list)
    pixmap: QPixmap | None = None
    index: int = 0  # Sequential album index for alternating bands.
    art_filepath: str = ""
    total_duration_ms: int = 0
    genre: str = ""  # Enriched genre from video metadata.

    @property
    def tracks(self) -> list[Track]:
        return [tn.track for tn in self.track_nodes]


@dataclass
class _YearNode:
    year: int | None
    label: str


@dataclass
class _FlatRow:
    row_type: int
    year_node: _YearNode | None = None
    album_node: _AlbumNode | None = None
    track_node: _TrackNode | None = None
    is_last_in_album: bool = False


# ── Model ─────────────────────────────────────────────────────────────


class _AlbumTableModel(QAbstractTableModel):
    """Flat table model: year headers, album headers, and track rows."""

    def __init__(self, parent=None, media_type: str = "music") -> None:
        super().__init__(parent)
        self._media_type = media_type
        self._rows: list[_FlatRow] = []
        self._spans: list[tuple[int, int, int, int]] = []
        self._playing_filepath: str | None = None
        self._album_nodes: list[_AlbumNode] = []
        # {track_id: (watched, watched_position_ms)}
        self._video_states: dict[int, tuple[bool, int]] = {}
        # Connect shared art cache signal.
        art_cache.on_art_ready().connect(self._on_art_cache_ready)

    def load_tracks(
        self, tracks: list[Track], poster_paths: dict[str, str] | None = None,
        video_states: dict[int, tuple[bool, int]] | None = None,
        video_genres: dict[int, str] | None = None,
        show_posters: dict[int, str] | None = None,
    ) -> None:
        self.beginResetModel()
        self._rows.clear()
        self._spans.clear()
        self._album_nodes.clear()
        self._video_states = video_states or {}
        self._video_genres = video_genres or {}
        self._show_posters = show_posters or {}

        if not tracks:
            self.endResetModel()
            return

        posters = poster_paths or {}

        if self._media_type == "movies":
            self._load_movies(tracks, posters)
            self.endResetModel()
            return

        # Group by album.
        albums_map: dict[str, list[Track]] = defaultdict(list)
        for t in tracks:
            albums_map[t.album or ""].append(t)

        # Sort albums by year desc, then name.
        # For series: sort by season number (numeric) so Season 2 < Season 10.
        if self._media_type == "series":
            import re
            def _season_key(kv):
                m = re.search(r"(\d+)", kv[0])
                return int(m.group(1)) if m else 0
            sorted_albums = sorted(albums_map.items(), key=_season_key)
        else:
            sorted_albums = sorted(
                albums_map.items(),
                key=lambda kv: (-(kv[1][0].year or 0), kv[0].lower()),
            )

        # Build nodes and flat row list.
        current_year = object()  # sentinel
        album_counter = 0
        for album_name, atracks in sorted_albums:
            atracks.sort(key=lambda t: (t.disc_number or 0, t.track_number or 0))
            year = atracks[0].year

            # Year header (skip for series — seasons provide the grouping).
            if self._media_type != "series" and year != current_year:
                current_year = year
                yn = _YearNode(year=year, label=str(year) if year else "Unknown Year")
                yr_row = len(self._rows)
                self._rows.append(_FlatRow(row_type=_TYPE_YEAR, year_node=yn))
                self._spans.append((yr_row, 0, 1, 4))

            # Album node.
            an = _AlbumNode(name=album_name, year=year, index=album_counter)
            album_counter += 1
            for t in atracks:
                an.track_nodes.append(_TrackNode(track=t, album=an))
            an.total_duration_ms = sum(t.duration_ms or 0 for t in atracks)

            # Collect genre: prefer enriched video metadata, fall back to file tags.
            for t in atracks:
                g = self._video_genres.get(t.id, "") if self._video_genres else ""
                if not g:
                    g = t.genre or ""
                if g:
                    an.genre = g
                    break

            # Queue async art loading via shared cache.
            if atracks:
                fp = atracks[0].filepath
                an.art_filepath = fp
                # For series: prefer show-level poster for consistent season art.
                poster = ""
                if self._show_posters and atracks[0].show_id:
                    poster = self._show_posters.get(atracks[0].show_id, "")
                if not poster:
                    poster = posters.get(fp, "")
                cached = art_cache.get_thumbnail(fp, _ART_THUMB, poster_path=poster)
                if cached is not None:
                    an.pixmap = cached
                # Otherwise, art_cache fires on_art_ready() when done.

            self._album_nodes.append(an)

            # Album header row.
            header_row = len(self._rows)
            self._rows.append(_FlatRow(row_type=_TYPE_ALBUM_HEADER, album_node=an))

            # Track rows.
            for tn in an.track_nodes:
                self._rows.append(_FlatRow(row_type=_TYPE_TRACK, album_node=an, track_node=tn))

            # Spacer row — always present for breathing room.
            n_tracks = len(an.track_nodes)
            self._rows.append(_FlatRow(row_type=_TYPE_SPACER, album_node=an))

            # Mark the last content row before the spacer.
            last_content = len(self._rows) - 2  # Last track row.
            if last_content >= 0 and self._rows[last_content].row_type == _TYPE_TRACK:
                self._rows[last_content].is_last_in_album = True

            # Spans: art cell spans album header + tracks + spacer in col 0.
            span_rows = 1 + n_tracks + 1  # header + tracks + spacer
            self._spans.append((header_row, 0, span_rows, 1))
            # Album title spans cols 1-3 on header row.
            self._spans.append((header_row, 1, 1, 3))

        self.endResetModel()

    def _load_movies(self, tracks: list[Track], posters: dict[str, str]) -> None:
        """Flat movie layout: alphabetical list, no album grouping."""
        sorted_tracks = sorted(
            tracks, key=lambda t: _strip_articles(t.title or "").lower()
        )

        # Single album node for all movies (needed by context menu/drag).
        an = _AlbumNode(name="", year=None, index=0)
        self._album_nodes.append(an)

        for t in sorted_tracks:
            tn = _TrackNode(track=t, album=an)
            an.track_nodes.append(tn)

            # Queue per-track poster art via shared cache.
            poster = posters.get(t.filepath, "")
            cached = art_cache.get_thumbnail(
                t.filepath, _MOVIE_ART_H, poster_path=poster,
            )
            if cached is not None:
                tn.pixmap = cached

            self._rows.append(
                _FlatRow(row_type=_TYPE_TRACK, album_node=an, track_node=tn)
            )

    def _on_art_cache_ready(self, filepath: str, size: int, pixmap: QPixmap | None) -> None:
        """Called when the shared art cache finishes loading a thumbnail."""
        # Movie flat layout: per-track poster art.
        if self._media_type == "movies" and size == _MOVIE_ART_H:
            for i, r in enumerate(self._rows):
                if r.track_node and r.track_node.track.filepath == filepath:
                    r.track_node.pixmap = pixmap
                    idx = self.index(i, 0)
                    self.dataChanged.emit(idx, idx)
                    break
            return
        if size != _ART_THUMB:
            return
        for an in self._album_nodes:
            if an.art_filepath == filepath:
                an.pixmap = pixmap
                # Find the header row and repaint.
                for i, r in enumerate(self._rows):
                    if r.album_node is an and r.row_type == _TYPE_ALBUM_HEADER:
                        idx = self.index(i, 0)
                        self.dataChanged.emit(idx, idx)
                        break
                break

    @property
    def spans(self) -> list[tuple[int, int, int, int]]:
        return self._spans

    # -- Video state tracking ------------------------------------------

    def update_video_state(
        self, track_id: int, watched: bool, position_ms: int,
    ) -> None:
        """Update a single track's video state and repaint its row."""
        self._video_states[track_id] = (watched, position_ms)
        for i, r in enumerate(self._rows):
            if r.row_type == _TYPE_TRACK and r.track_node.track.id == track_id:
                tl = self.index(i, 0)
                br = self.index(i, 3)
                self.dataChanged.emit(tl, br)
                break

    # -- Now-playing tracking ------------------------------------------

    def set_playing_filepath(self, filepath: str | None) -> None:
        old = self._playing_filepath
        self._playing_filepath = filepath
        # Emit dataChanged for affected rows.
        for i, r in enumerate(self._rows):
            if r.row_type != _TYPE_TRACK:
                continue
            fp = r.track_node.track.filepath
            if fp == old or fp == filepath:
                tl = self.index(i, 0)
                br = self.index(i, 3)
                self.dataChanged.emit(tl, br)

    # -- QAbstractTableModel interface ---------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 4

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._rows[index.row()]
        col = index.column()

        if role == _ROLE_ROW_TYPE:
            return row_data.row_type

        if role == _ROLE_IS_PLAYING:
            if row_data.row_type == _TYPE_TRACK and self._playing_filepath:
                return row_data.track_node.track.filepath == self._playing_filepath
            return False

        if role == _ROLE_IS_LAST_IN_ALBUM:
            return row_data.is_last_in_album

        if role == _ROLE_ALBUM_INDEX:
            return row_data.album_node.index if row_data.album_node else 0

        if role == _ROLE_WATCHED:
            if row_data.row_type == _TYPE_TRACK:
                tid = row_data.track_node.track.id
                state = self._video_states.get(tid)
                return state[0] if state else False
            return False

        if role == _ROLE_PROGRESS:
            if row_data.row_type == _TYPE_TRACK:
                tid = row_data.track_node.track.id
                dur = row_data.track_node.track.duration_ms
                state = self._video_states.get(tid)
                if state and dur > 0:
                    return min(state[1] / dur, 1.0)
            return 0.0

        # Year header.
        if row_data.row_type == _TYPE_YEAR:
            if role == Qt.ItemDataRole.DisplayRole and col == 0:
                return row_data.year_node.label
            return None

        # Album header.
        if row_data.row_type == _TYPE_ALBUM_HEADER:
            an = row_data.album_node
            if role == _ROLE_ALBUM_GENRE:
                return an.genre
            if col == 0:
                if role == _ROLE_ALBUM_PIXMAP:
                    return an.pixmap
                if role == _ROLE_ALBUM_TRACKS:
                    return an.tracks
                return None
            if col == 1:
                if role == Qt.ItemDataRole.DisplayRole:
                    text = an.name or "Unknown Album"
                    if an.year:
                        text += f"  ({an.year})"
                    return text
                if role == _ROLE_ALBUM_TRACKS:
                    return an.tracks
            return None

        # Track row.
        if row_data.row_type == _TYPE_TRACK:
            t = row_data.track_node.track
            if role == _ROLE_TRACK:
                return t
            if role == _ROLE_TRACK_PIXMAP:
                return row_data.track_node.pixmap
            if role == _ROLE_ALBUM_TRACKS:
                return row_data.album_node.tracks
            if role == _ROLE_ALBUM_GENRE:
                return self._video_genres.get(t.id, "") or t.genre or ""
            if role == Qt.ItemDataRole.DisplayRole:
                if col == 1:
                    if self._media_type == "movies":
                        return ""  # Year is shown in year headers.
                    if self._media_type == "series" and t.track_number:
                        return f"E{t.track_number}"
                    return str(t.track_number) if t.track_number else ""
                if col == 2:
                    return t.title or "Unknown"
                if col == 3:
                    return _ms_to_mmss(t.duration_ms) if t.duration_ms else ""
            if role == Qt.ItemDataRole.TextAlignmentRole:
                if col == 1:
                    return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 3:
                    return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if role == Qt.ItemDataRole.ForegroundRole:
                if col in (1, 3):
                    return QColor(160, 160, 160)

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        row_data = self._rows[index.row()]
        if row_data.row_type in (_TYPE_YEAR, _TYPE_SPACER):
            return Qt.ItemFlag.ItemIsEnabled
        if row_data.row_type == _TYPE_ALBUM_HEADER:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDragEnabled
        # Track.
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )

    # -- Drag support --------------------------------------------------

    def mimeTypes(self) -> list[str]:
        return [TRACK_IDS_MIME]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        track_ids: list[int] = []
        seen: set[int] = set()
        for idx in indexes:
            row_data = self._rows[idx.row()]
            if row_data.row_type == _TYPE_ALBUM_HEADER:
                # Dragging album header → all tracks in the album.
                for tn in row_data.album_node.track_nodes:
                    t = tn.track
                    if t.id >= 0 and t.id not in seen:
                        track_ids.append(t.id)
                        seen.add(t.id)
            elif row_data.row_type == _TYPE_TRACK:
                if idx.column() != 1:  # Avoid duplicates from multi-column selection.
                    continue
                t = row_data.track_node.track
                if t.id >= 0 and t.id not in seen:
                    track_ids.append(t.id)
                    seen.add(t.id)
        mime = QMimeData()
        mime.setData(TRACK_IDS_MIME, json.dumps(track_ids).encode())
        return mime

    # -- Helpers -------------------------------------------------------

    def album_tracks_for_row(self, row: int) -> tuple[list[Track], int] | None:
        """Return (album_tracks, track_index_within_album) for a row."""
        if row < 0 or row >= len(self._rows):
            return None
        rd = self._rows[row]
        if rd.row_type == _TYPE_TRACK:
            an = rd.album_node
            tracks = an.tracks
            idx = an.track_nodes.index(rd.track_node)
            return tracks, idx
        if rd.row_type == _TYPE_ALBUM_HEADER:
            return rd.album_node.tracks, 0
        return None

    def album_track_rows(self, row: int) -> list[int]:
        """Return all row indices for tracks in the same album as *row*."""
        if row < 0 or row >= len(self._rows):
            return []
        rd = self._rows[row]
        an = rd.album_node
        if an is None:
            return []
        return [
            i for i, r in enumerate(self._rows)
            if r.album_node is an and r.row_type == _TYPE_TRACK
        ]


# ── Delegate ──────────────────────────────────────────────────────────


class _AlbumArtDelegate(QStyledItemDelegate):
    """Custom painting for year headers, album art cells, album titles,
    and track rows with hover / now-playing / separator polish."""

    _HOVER_BG = QColor(255, 255, 255, 12)
    _SELECT_BG = QColor(255, 255, 255, 25)
    _BAND_BG = QColor(255, 255, 255, 4)
    _ART_BORDER = QColor(255, 255, 255, 18)
    _PLAYING_BAR_W = 3
    _PROGRESS_H = 2  # Height of progress bar at bottom of track row.
    _PROGRESS_COLOR = QColor(255, 255, 255, 40)
    _WATCHED_OPACITY = 0.45  # Dim watched tracks.
    _GENRE_COLOR = QColor(180, 180, 180)  # Muted genre text.

    show_genre: bool = True  # Controlled by config setting.

    def __init__(self, parent=None, media_type: str = "music") -> None:
        super().__init__(parent)
        self._media_type = media_type

    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Strip system selection highlight — we paint our own in _paint_track."""
        super().initStyleOption(option, index)
        # Always strip selection so the system palette highlight never shows.
        option.state &= ~QStyle.StateFlag.State_Selected
        # Also strip focus rect — we don't want dotted borders.
        option.state &= ~QStyle.StateFlag.State_HasFocus

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row_type = index.data(_ROLE_ROW_TYPE)

        # Alternating album band background.
        album_idx = index.data(_ROLE_ALBUM_INDEX)
        if album_idx is not None and album_idx % 2 == 1 and row_type != _TYPE_YEAR:
            painter.fillRect(option.rect, self._BAND_BG)

        if row_type == _TYPE_YEAR:
            self._paint_year(painter, option, index)
            return

        if row_type == _TYPE_ALBUM_HEADER:
            col = index.column()
            if col == 0:
                self._paint_art(painter, option, index)
            else:
                self._paint_album_title(painter, option, index)
            return

        if row_type == _TYPE_SPACER:
            return  # Breathing room — paint nothing.

        # Track rows — hover + now-playing + default painting.
        if self._media_type == "movies" and index.column() == 0:
            self._paint_movie_art(painter, option, index)
            return
        self._paint_track(painter, option, index)

    def _paint_track(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Paint a track row with custom selection, hover, now-playing, and video state."""
        is_playing = index.data(_ROLE_IS_PLAYING)
        is_watched = index.data(_ROLE_WATCHED)
        progress = index.data(_ROLE_PROGRESS)
        # Use the view's row-level hover tracking instead of per-cell State_MouseOver.
        view = option.widget
        is_hovered = hasattr(view, '_hovered_row') and view._hovered_row == index.row()
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        # Paint our own selection/hover background.
        if is_selected:
            painter.fillRect(option.rect, self._SELECT_BG)
        elif is_hovered:
            painter.fillRect(option.rect, self._HOVER_BG)

        # Now-playing accent bar (left edge of column 1 only, to avoid art column).
        if is_playing and index.column() == 1:
            accent = option.palette.highlight().color()
            bar_rect = option.rect.adjusted(0, 1, 0, -1)
            bar_rect.setWidth(self._PLAYING_BAR_W)
            painter.fillRect(bar_rect, accent)

        # Dim watched tracks (reduce opacity).
        if is_watched and not is_playing:
            painter.save()
            painter.setOpacity(self._WATCHED_OPACITY)

        # Movie flat layout: custom two-line painting for title column.
        if self._media_type == "movies" and index.column() == 2:
            painter.save()
            rect = option.rect.adjusted(4, 0, 0, 0)
            # Title — bold, upper half.
            title_font = painter.font()
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.setPen(QColor(230, 230, 230))
            title = index.data(Qt.ItemDataRole.DisplayRole) or ""
            painter.drawText(rect.adjusted(0, 4, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, title)
            # Genre — muted, lower half.
            genre = index.data(_ROLE_ALBUM_GENRE) or ""
            if genre:
                genre_font = painter.font()
                genre_font.setBold(False)
                genre_font.setPixelSize(max(genre_font.pixelSize() - 2, 9) if genre_font.pixelSize() > 0 else 10)
                painter.setFont(genre_font)
                painter.setPen(self._GENRE_COLOR)
                fm = QFontMetrics(genre_font)
                painter.drawText(rect.adjusted(0, fm.height() + 8, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, genre)
            painter.restore()
            if is_watched and not is_playing:
                painter.restore()
            # Progress bar.
            if progress > 0.0 and not is_watched:
                bar_y = option.rect.bottom() - self._PROGRESS_H
                bar_w = int(option.rect.width() * progress)
                painter.fillRect(option.rect.x(), bar_y, bar_w, self._PROGRESS_H, self._PROGRESS_COLOR)
            return

        # Strip selected state so super().paint() doesn't double-paint its own highlight.
        opt = QStyleOptionViewItem(option)
        opt.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, opt, index)

        if is_watched and not is_playing:
            painter.restore()

        # Progress bar at bottom of row (column 2 = title column, widest).
        if progress > 0.0 and not is_watched and index.column() == 2:
            rect = option.rect
            bar_y = rect.bottom() - self._PROGRESS_H
            bar_w = int(rect.width() * progress)
            painter.fillRect(rect.x(), bar_y, bar_w, self._PROGRESS_H, self._PROGRESS_COLOR)

    def _paint_movie_art(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Paint inline poster thumbnail for a movie track row (col 0)."""
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        view = option.widget
        is_hovered = hasattr(view, '_hovered_row') and view._hovered_row == index.row()
        if is_selected:
            painter.fillRect(option.rect, self._SELECT_BG)
        elif is_hovered:
            painter.fillRect(option.rect, self._HOVER_BG)

        pixmap = index.data(_ROLE_TRACK_PIXMAP)
        if pixmap and not pixmap.isNull():
            # Scale to fit within the art column, maintaining aspect ratio.
            scaled = pixmap.scaled(
                _MOVIE_ART_W, _MOVIE_ART_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = option.rect.x() + (option.rect.width() - scaled.width()) // 2
            y = option.rect.y() + (option.rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)

    def _paint_year(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setPen(QColor(180, 180, 180))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        # Draw near the bottom of the taller year row for visual weight.
        painter.drawText(
            option.rect.adjusted(8, 0, 0, -4),
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
            text,
        )
        painter.restore()

    def _paint_art(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        rect = option.rect
        view = option.widget

        # Check if this album's art is being hovered.
        is_art_hovered = hasattr(view, '_hovered_art_row') and view._hovered_art_row == index.row()

        pixmap = index.data(_ROLE_ALBUM_PIXMAP)
        if pixmap and not pixmap.isNull():
            x = rect.x() + (rect.width() - pixmap.width()) // 2
            y = rect.y() + 4

            # Hover highlight: subtle rounded rect behind the art.
            if is_art_hovered:
                highlight_margin = 3
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 25))
                painter.drawRoundedRect(
                    x - highlight_margin, y - highlight_margin,
                    pixmap.width() + highlight_margin * 2,
                    pixmap.height() + highlight_margin * 2,
                    4, 4,
                )

            painter.drawPixmap(x, y, pixmap)
            # Border — slightly brighter when hovered.
            border_color = QColor(255, 255, 255, 50) if is_art_hovered else self._ART_BORDER
            painter.setPen(QPen(border_color, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(x, y, pixmap.width() - 1, pixmap.height() - 1)
        else:
            ph = min(_ART_THUMB, rect.height() - 8)
            x = rect.x() + (rect.width() - ph) // 2
            y = rect.y() + 4
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(80, 80, 80) if is_art_hovered else QColor(60, 60, 60))
            painter.drawRect(x, y, ph, ph)

        painter.restore()

    def _paint_album_title(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        rect = option.rect
        view = option.widget
        is_title_hovered = hasattr(view, '_hovered_title_row') and view._hovered_title_row == index.row()

        # Brighter text when hovered to indicate interactivity.
        if is_title_hovered:
            color = option.palette.highlightedText().color()
        else:
            color = option.palette.text().color()
        painter.setPen(color)

        font = painter.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 1)
        painter.setFont(font)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""

        # Genre text — shown on the same line after title, muted.
        genre = index.data(_ROLE_ALBUM_GENRE) or "" if self.show_genre else ""

        # Measure title width to place genre after it.
        fm = QFontMetrics(font)
        title_width = fm.horizontalAdvance(text)
        painter.drawText(rect.adjusted(4, 4, -4, 0), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, text)

        if genre:
            genre_font = painter.font()
            genre_font.setBold(False)
            genre_font.setPointSize(genre_font.pointSize() - 1)
            painter.setFont(genre_font)
            painter.setPen(self._GENRE_COLOR)
            genre_x = rect.x() + 4 + title_width + 8  # 8px gap after title
            genre_rect = rect.adjusted(genre_x - rect.x(), 5, -60, 0)
            if genre_rect.width() > 30:
                painter.drawText(genre_rect, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, genre)

        # Album total duration — right-aligned, muted color.
        tracks = index.data(_ROLE_ALBUM_TRACKS)
        if tracks:
            total_ms = sum(t.duration_ms or 0 for t in tracks)
            if total_ms > 0:
                dur_text = _format_album_duration(total_ms)
                dur_font = painter.font()
                dur_font.setBold(False)
                dur_font.setPointSize(dur_font.pointSize() - 1)
                painter.setFont(dur_font)
                painter.setPen(option.palette.placeholderText().color())
                painter.drawText(rect.adjusted(4, 4, -8, 0), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, dur_text)

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        row_type = index.data(_ROLE_ROW_TYPE)
        if row_type == _TYPE_YEAR:
            return QSize(0, _YEAR_ROW_H)
        if row_type == _TYPE_ALBUM_HEADER:
            return QSize(0, _ALBUM_ROW_H)
        return QSize(0, _TRACK_ROW_H)


# ── Table view ────────────────────────────────────────────────────────


class _AlbumTableView(QTableView):
    """Configured table view with art spanning, click handlers, and drag."""

    play_album = Signal(list, int)
    add_to_queue = Signal(list)
    add_to_saved_playlist = Signal(list, int)  # track_ids, playlist_id
    edit_tags_requested = Signal(list)
    mark_watched_requested = Signal(list, bool)  # track_ids, watched
    view_online_requested = Signal(int)  # track_id — open TMDb/IMDb in browser
    scrape_metadata_requested = Signal(list)  # track_ids to re-scrape
    movie_info_requested = Signal(int)  # track_id for movie info dialog
    open_in_minim_requested = Signal(list)  # filesystem paths (str)

    _hovered_row: int = -1
    _hovered_art_row: int = -1   # Album header row whose art is hovered.
    _hovered_title_row: int = -1  # Album header row whose title is hovered.
    _drag_start_pos = None  # Set on art/title press to enable drag initiation.
    _saved_playlists: list[tuple[int, str, int]] = []  # (id, name, count)
    _engine = None  # GStreamer engine ref, set when a video track is playing.
    _empty_hint = ""  # Shown centred while the model has no rows.

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setShowGrid(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setMouseTracking(True)  # Enable hover detection.

        # Override system selection color to prevent coral-pink bleed.
        # Use a subtle white highlight consistent with our delegate painting.
        self.setStyleSheet("""
            _AlbumTableView {
                selection-background-color: rgba(255, 255, 255, 25);
                selection-color: palette(text);
            }
            _AlbumTableView::item:selected {
                background: rgba(255, 255, 255, 25);
            }
            _AlbumTableView::item:hover:!selected {
                background: transparent;
            }
        """)

        # Headers.
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(_TRACK_ROW_H)
        self.horizontalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(False)

        self.customContextMenuRequested.connect(self._show_context_menu)
        # Note: clicked/doubleClicked signals NOT used — we override
        # mousePressEvent/mouseDoubleClickEvent directly for precise control.

    # -- Hit testing helpers -------------------------------------------

    def _art_rect_for_header_row(self, header_row: int):
        """Return the visual rect of the art thumbnail for an album header row."""
        cell_rect = self.visualRect(self.model().index(header_row, 0))
        art_x = cell_rect.x() + (cell_rect.width() - _ART_THUMB) // 2
        art_y = cell_rect.y() + 4
        from PySide6.QtCore import QRect
        return QRect(art_x, art_y, _ART_THUMB, _ART_THUMB)

    def _header_row_for_pos(self, pos):
        """Find the album header row for a position in column 0.

        Returns the header row index if pos is within the art thumbnail,
        or -1 if in dead space.
        """
        model = self.model()
        if not isinstance(model, _AlbumTableModel):
            return -1
        idx = self.indexAt(pos)
        if not idx.isValid():
            return -1
        # Column 0 is spanned — find the album node's header row.
        row = idx.row()
        if row >= len(model._rows):
            return -1
        rd = model._rows[row]
        if rd.album_node is None:
            return -1
        # Find the header row for this album.
        for i, r in enumerate(model._rows):
            if r.album_node is rd.album_node and r.row_type == _TYPE_ALBUM_HEADER:
                return i
        return -1

    def _select_album_tracks(self, header_row: int, modifiers) -> None:
        """Programmatically select all track rows for an album."""
        model = self.model()
        if not isinstance(model, _AlbumTableModel):
            return
        track_rows = model.album_track_rows(header_row)
        if not track_rows:
            return
        sel = QItemSelection()
        for r in track_rows:
            left = model.index(r, 1)
            right = model.index(r, 3)
            sel.select(left, right)
        sm = self.selectionModel()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            sm.select(sel, QItemSelectionModel.SelectionFlag.Toggle | QItemSelectionModel.SelectionFlag.Rows)
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            sm.select(sel, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        else:
            sm.select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)

    # -- Mouse events --------------------------------------------------

    def mousePressEvent(self, event) -> None:
        self._drag_start_pos = None
        pos = event.pos()
        idx = self.indexAt(pos)
        if not idx.isValid():
            # Clicked empty area below all content — clear selection.
            self.selectionModel().clearSelection()
            event.accept()
            return

        model = self.model()
        if not isinstance(model, _AlbumTableModel):
            super().mousePressEvent(event)
            return

        row = idx.row()
        if row >= len(model._rows):
            super().mousePressEvent(event)
            return

        rd = model._rows[row]

        # Column 0: intercept all clicks — art or dead space.
        if idx.column() == 0:
            header_row = self._header_row_for_pos(pos)
            if header_row >= 0:
                art_rect = self._art_rect_for_header_row(header_row)
                if art_rect.contains(pos):
                    self._select_album_tracks(header_row, event.modifiers())
                    self._drag_start_pos = pos  # Enable drag.
                    event.accept()
                    return
            # Dead space — clear selection (like clicking empty space in a file browser).
            self.selectionModel().clearSelection()
            event.accept()
            return

        # Album header row (columns 1-3): check if click is on the title text.
        if rd.row_type == _TYPE_ALBUM_HEADER:
            # Compute title text bounds to limit the clickable area.
            cell_rect = self.visualRect(model.index(row, 1))
            title_text = model.data(model.index(row, 1), Qt.ItemDataRole.DisplayRole) or ""
            font = self.font()
            font.setBold(True)
            font.setPointSize(font.pointSize() + 1)
            fm = QFontMetrics(font)
            text_width = fm.horizontalAdvance(title_text)
            title_rect = cell_rect.adjusted(4, 4, 0, 0)
            title_rect.setWidth(min(text_width + 8, cell_rect.width()))
            title_rect.setHeight(fm.height())

            if title_rect.contains(pos):
                self._select_album_tracks(row, event.modifiers())
                self._drag_start_pos = pos
                event.accept()
                return
            else:
                # Clicked empty space in album header row — clear selection.
                self.selectionModel().clearSelection()
                event.accept()
                return

        # Spacer row: clear selection (empty space).
        if rd.row_type == _TYPE_SPACER:
            self.selectionModel().clearSelection()
            event.accept()
            return

        # Year header row: clear selection (empty space).
        if rd.row_type == _TYPE_YEAR:
            self.selectionModel().clearSelection()
            event.accept()
            return

        # Track rows: let Qt handle selection normally (including drag).
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        pos = event.pos()
        idx = self.indexAt(pos)
        if not idx.isValid():
            super().mouseDoubleClickEvent(event)
            return

        model = self.model()
        if not isinstance(model, _AlbumTableModel):
            super().mouseDoubleClickEvent(event)
            return

        row = idx.row()
        if row >= len(model._rows):
            super().mouseDoubleClickEvent(event)
            return

        rd = model._rows[row]

        # Column 0: for non-movie views, only respond if on art thumbnail.
        if idx.column() == 0 and model._media_type != "movies":
            header_row = self._header_row_for_pos(pos)
            if header_row >= 0:
                art_rect = self._art_rect_for_header_row(header_row)
                if art_rect.contains(pos):
                    result = model.album_tracks_for_row(header_row)
                    if result:
                        self.play_album.emit(*result)
            event.accept()
            return

        # Album header row: play album from track 1.
        if rd.row_type == _TYPE_ALBUM_HEADER:
            result = model.album_tracks_for_row(row)
            if result:
                self.play_album.emit(*result)
            event.accept()
            return

        # Track row: play album from this track.
        if rd.row_type == _TYPE_TRACK:
            result = model.album_tracks_for_row(row)
            if result:
                self.play_album.emit(*result)
            event.accept()
            return

        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = event.pos()

        # Check if we should initiate a drag from an art/title click.
        if self._drag_start_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            dist = (pos - self._drag_start_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._drag_start_pos = None
                self.startDrag(Qt.DropAction.CopyAction)
                return

        idx = self.indexAt(pos)
        model = self.model()
        if not isinstance(model, _AlbumTableModel):
            super().mouseMoveEvent(event)
            return

        new_row = idx.row() if idx.isValid() else -1
        new_art_row = -1
        new_title_row = -1

        if idx.isValid() and new_row < len(model._rows):
            rd = model._rows[new_row]
            actual_row = new_row  # Preserve before we clear new_row.

            # Art hover: check if cursor is within the art thumbnail bounds.
            if idx.column() == 0:
                new_row = -1  # Never hover-highlight column 0 as a track row.
                header_row = self._header_row_for_pos(pos)
                if header_row >= 0:
                    art_rect = self._art_rect_for_header_row(header_row)
                    if art_rect.contains(pos):
                        new_art_row = header_row
            elif rd.row_type == _TYPE_ALBUM_HEADER:
                # Title hover — only on the text itself, not the full row.
                cell_rect = self.visualRect(model.index(actual_row, 1))
                title_text = model.data(model.index(actual_row, 1), Qt.ItemDataRole.DisplayRole) or ""
                font = self.font()
                font.setBold(True)
                font.setPointSize(font.pointSize() + 1)
                fm = QFontMetrics(font)
                text_width = fm.horizontalAdvance(title_text)
                title_rect = cell_rect.adjusted(4, 4, 0, 0)
                title_rect.setWidth(min(text_width + 8, cell_rect.width()))
                title_rect.setHeight(fm.height())
                if title_rect.contains(pos):
                    new_title_row = actual_row
                new_row = -1
            elif rd.row_type == _TYPE_TRACK:
                pass  # new_row stays as the hovered track row.
            else:
                new_row = -1  # Spacer, year — no hover.
        else:
            new_row = -1

        # Repaint changed rows.
        changed = False
        if new_row != self._hovered_row:
            old = self._hovered_row
            self._hovered_row = new_row
            self._repaint_row(old)
            self._repaint_row(new_row)
            changed = True

        if new_art_row != self._hovered_art_row:
            old = self._hovered_art_row
            self._hovered_art_row = new_art_row
            self._repaint_row(old)
            self._repaint_row(new_art_row)
            changed = True

        if new_title_row != self._hovered_title_row:
            old = self._hovered_title_row
            self._hovered_title_row = new_title_row
            self._repaint_row(old)
            self._repaint_row(new_title_row)
            changed = True

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_start_pos = None
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        old_rows = [self._hovered_row, self._hovered_art_row, self._hovered_title_row]
        self._hovered_row = -1
        self._hovered_art_row = -1
        self._hovered_title_row = -1
        for r in old_rows:
            self._repaint_row(r)
        super().leaveEvent(event)

    def _repaint_row(self, row: int) -> None:
        if row < 0 or not self.model():
            return
        cols = self.model().columnCount()
        self.viewport().update(
            self.visualRect(self.model().index(row, 0)).united(
                self.visualRect(self.model().index(row, cols - 1)))
        )

    def apply_layout(self, model: _AlbumTableModel) -> None:
        """Apply spans and row heights after model load."""
        self.clearSpans()

        for row, col, rs, cs in model.spans:
            self.setSpan(row, col, rs, cs)

        # Column widths.
        h = self.horizontalHeader()
        h.resizeSection(0, _ART_COL_W)
        h.resizeSection(1, _NUM_COL_W)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.resizeSection(3, _DUR_COL_W)

        # Row heights.
        is_movies = model._media_type == "movies"
        vh = self.verticalHeader()
        for i in range(model.rowCount()):
            rd = model._rows[i]
            if rd.row_type == _TYPE_YEAR:
                vh.resizeSection(i, _YEAR_ROW_H)
            elif rd.row_type == _TYPE_TRACK and is_movies:
                vh.resizeSection(i, _MOVIE_ROW_H)
            elif rd.row_type == _TYPE_ALBUM_HEADER:
                vh.resizeSection(i, _ALBUM_ROW_H)
            elif rd.row_type == _TYPE_SPACER:
                # Space for art + breathing room.
                n_tracks = len(rd.album_node.track_nodes)
                used = _ALBUM_ROW_H + n_tracks * _TRACK_ROW_H
                art_space = max(0, _ART_THUMB + 8 - used)
                vh.resizeSection(i, max(_MIN_SPACER_H, art_space))

    # Click/double-click handled in mousePressEvent/mouseDoubleClickEvent above.

    def set_engine(self, engine) -> None:
        """Store a reference to the GStreamer engine for stream menus."""
        self._engine = engine

    def set_empty_hint(self, text: str) -> None:
        self._empty_hint = text
        self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._empty_hint or self.model() is None or self.model().rowCount():
            return
        p = QPainter(self.viewport())
        p.setPen(self.palette().placeholderText().color())
        p.drawText(
            self.viewport().rect().adjusted(32, 32, -32, -32),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            self._empty_hint,
        )
        p.end()

    def _show_context_menu(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return
        model = self.model()
        if not isinstance(model, _AlbumTableModel):
            return

        selected_tracks = self._selected_tracks()
        clicked_result = model.album_tracks_for_row(index.row())

        menu = QMenu(self)

        if clicked_result:
            album_tracks, click_idx = clicked_result
            play_action = QAction("Play from Here", self)
            play_action.triggered.connect(
                lambda: self.play_album.emit(album_tracks, click_idx)
            )
            menu.addAction(play_action)
            menu.addSeparator()

        if selected_tracks:
            n = len(selected_tracks)
            label = f"Add to Queue ({n})" if n > 1 else "Add to Queue"
            add_action = QAction(label, self)
            add_action.triggered.connect(
                lambda: self.add_to_queue.emit(selected_tracks)
            )
            menu.addAction(add_action)

        if clicked_result:
            album_tracks, _ = clicked_result
            add_album = QAction("Add Entire Album to Queue", self)
            add_album.triggered.connect(
                lambda: self.add_to_queue.emit(album_tracks)
            )
            menu.addAction(add_album)

        # "Add to Saved Playlist" submenu.
        tracks_for_playlist = selected_tracks or (album_tracks if clicked_result else [])
        if tracks_for_playlist and self._saved_playlists:
            menu.addSeparator()
            saved_menu = menu.addMenu("Add to Saved Playlist")
            track_ids = [t.id for t in tracks_for_playlist if t.id >= 0]
            for pid, pname, _ in self._saved_playlists:
                action = QAction(pname, self)
                action.triggered.connect(
                    lambda checked, _ids=track_ids, _pid=pid:
                        self.add_to_saved_playlist.emit(_ids, _pid)
                )
                saved_menu.addAction(action)

        # "Open in Minim" — sends the selected tracks (or the clicked
        # album row) to a fresh Minim instance so the user can preview
        # one piece while keeping the Quaver queue untouched.
        oim_tracks = selected_tracks or (
            list(album_tracks) if clicked_result else []
        )
        oim_paths = [t.filepath for t in oim_tracks if t.filepath]
        if oim_paths:
            menu.addSeparator()
            n = len(oim_paths)
            label = f"Open in Minim ({n})" if n > 1 else "Open in Minim"
            oim_action = QAction(label, self)
            oim_action.triggered.connect(
                lambda _=False, _p=oim_paths: self.open_in_minim_requested.emit(_p)
            )
            menu.addAction(oim_action)

        if selected_tracks:
            menu.addSeparator()
            ids = [t.id for t in selected_tracks if t.id >= 0]
            n = len(ids)
            label = f"Edit Tags ({n})…" if n > 1 else "Edit Tags…"
            edit_action = QAction(label, self)
            edit_action.triggered.connect(
                lambda: self.edit_tags_requested.emit(ids)
            )
            menu.addAction(edit_action)

        # Watched/unwatched toggle for video tracks.
        video_tracks = [
            t for t in (selected_tracks or [])
            if t.media_type in ("movie", "series_episode") and t.id >= 0
        ]
        if video_tracks:
            video_ids = [t.id for t in video_tracks]
            vs = model._video_states
            any_unwatched = any(not vs.get(tid, (False, 0))[0] for tid in video_ids)
            if any_unwatched:
                w_action = QAction("Mark as Watched", self)
                w_action.triggered.connect(
                    lambda: self.mark_watched_requested.emit(video_ids, True)
                )
            else:
                w_action = QAction("Mark as Unwatched", self)
                w_action.triggered.connect(
                    lambda: self.mark_watched_requested.emit(video_ids, False)
                )
            menu.addAction(w_action)

            # "Movie Information..." — single movie track info dialog.
            if len(video_tracks) == 1 and video_tracks[0].media_type == "movie":
                info_action = QAction("Movie Information\u2026", self)
                info_action.triggered.connect(
                    lambda: self.movie_info_requested.emit(video_tracks[0].id)
                )
                menu.addAction(info_action)

            # "View Online" — opens TMDb/IMDb page for the first selected video.
            if len(video_tracks) == 1:
                view_action = QAction("View Online\u2026", self)
                view_action.triggered.connect(
                    lambda: self.view_online_requested.emit(video_tracks[0].id)
                )
                menu.addAction(view_action)

            # "Scrape Metadata" — re-fetch metadata for selected video tracks.
            scrape_action = QAction("Scrape Metadata", self)
            scrape_action.triggered.connect(
                lambda: self.scrape_metadata_requested.emit(video_ids)
            )
            menu.addAction(scrape_action)

        # Stream selection for the currently-playing video track.
        model_obj = self.model()
        playing_fp = getattr(model_obj, '_playing_filepath', None) if isinstance(model_obj, _AlbumTableModel) else None
        if (
            playing_fp
            and len(selected_tracks) == 1
            and selected_tracks[0].media_type in ("movie", "series_episode")
            and selected_tracks[0].filepath == playing_fp
            and self._engine
        ):
            from quaver.ui.widgets.queue_widget import build_stream_menus
            build_stream_menus(menu, self._engine)

        if menu.actions():
            menu.exec(self.viewport().mapToGlobal(pos))

    def _selected_tracks(self) -> list[Track]:
        model = self.model()
        if not isinstance(model, _AlbumTableModel):
            return []
        tracks: list[Track] = []
        seen: set[int] = set()
        for idx in self.selectionModel().selectedRows(1):
            t = idx.data(_ROLE_TRACK)
            if t and t.id not in seen:
                tracks.append(t)
                seen.add(t.id)
        return tracks

    def startDrag(self, supportedActions) -> None:
        indexes = self.selectionModel().selectedRows(1)
        # If no selected rows (e.g. dragging an album header), use current index.
        if not indexes:
            current = self.currentIndex()
            if current.isValid():
                indexes = [current]
            else:
                return
        model = self.model()
        mime = model.mimeData(indexes)
        if not mime:
            return
        data = bytes(mime.data(TRACK_IDS_MIME))
        count = len(json.loads(data.decode())) if data else 0
        if count == 0:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self._make_drag_pixmap(count))
        drag.exec(Qt.DropAction.CopyAction)

    @staticmethod
    def _make_drag_pixmap(count: int) -> QPixmap:
        text = f"{count} track{'s' if count != 1 else ''}"
        fm = QFontMetrics(QApplication.font())
        w = fm.horizontalAdvance(text) + 16
        h = fm.height() + 8
        pixmap = QPixmap(w, h)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QApplication.palette().highlight().color()
        bg.setAlpha(200)
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, w, h, 4, 4)
        painter.setPen(QApplication.palette().highlightedText().color())
        painter.drawText(8, fm.ascent() + 4, text)
        painter.end()
        return pixmap


# ── Public wrapper ────────────────────────────────────────────────────


class AlbumView(QWidget):
    """Scrollable album-centric library view.

    Public API:
      - load_tracks(tracks: list[Track])
      - set_playing_track(filepath: str | None)
      - play_album signal
      - add_to_queue signal
      - edit_tags_requested signal
    """

    play_album = Signal(list, int)
    add_to_queue = Signal(list)
    add_to_saved_playlist = Signal(list, int)  # track_ids, playlist_id
    edit_tags_requested = Signal(list)
    mark_watched_requested = Signal(list, bool)  # track_ids, watched
    view_online_requested = Signal(int)  # track_id
    scrape_metadata_requested = Signal(list)  # track_ids to re-scrape
    movie_info_requested = Signal(int)  # track_id for movie info dialog
    open_in_minim_requested = Signal(list)  # filesystem paths (str)

    def __init__(self, parent: QWidget | None = None, media_type: str = "music") -> None:
        super().__init__(parent)
        self._media_type = media_type
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._model = _AlbumTableModel(self, media_type=media_type)
        self._delegate = _AlbumArtDelegate(self, media_type=media_type)
        self._table = _AlbumTableView(self)
        self._table.setModel(self._model)
        self._table.setItemDelegate(self._delegate)

        # Forward signals.
        self._table.play_album.connect(self.play_album.emit)
        self._table.add_to_queue.connect(self.add_to_queue.emit)
        self._table.add_to_saved_playlist.connect(self.add_to_saved_playlist.emit)
        self._table.edit_tags_requested.connect(self.edit_tags_requested.emit)
        self._table.mark_watched_requested.connect(self.mark_watched_requested.emit)
        self._table.view_online_requested.connect(self.view_online_requested.emit)
        self._table.scrape_metadata_requested.connect(self.scrape_metadata_requested.emit)
        self._table.open_in_minim_requested.connect(self.open_in_minim_requested.emit)
        self._table.movie_info_requested.connect(self.movie_info_requested.emit)

        layout.addWidget(self._table)

    def load_tracks(
        self, tracks: list[Track], poster_paths: dict[str, str] | None = None,
        video_states: dict[int, tuple[bool, int]] | None = None,
        video_genres: dict[int, str] | None = None,
        show_posters: dict[int, str] | None = None,
    ) -> None:
        """Rebuild the view with albums grouped from *tracks*."""
        self._model.load_tracks(
            tracks, poster_paths=poster_paths, video_states=video_states,
            video_genres=video_genres, show_posters=show_posters,
        )
        self._table.apply_layout(self._model)

    def set_playing_track(self, filepath: str | None) -> None:
        """Update the now-playing indicator to highlight *filepath*."""
        self._model.set_playing_filepath(filepath)

    def set_empty_hint(self, text: str) -> None:
        """Text to show while the view has no albums; empty for none."""
        self._table.set_empty_hint(text)

    def set_engine(self, engine) -> None:
        """Pass the GStreamer engine to the table for stream selection menus."""
        self._table.set_engine(engine)

    def update_video_state(
        self, track_id: int, watched: bool, position_ms: int,
    ) -> None:
        """Update a single track's watched/progress state."""
        self._model.update_video_state(track_id, watched, position_ms)

    def set_show_genre(self, show: bool) -> None:
        """Toggle genre display in album headers."""
        self._delegate.show_genre = show
        self._table.viewport().update()

    def set_saved_playlists(self, playlists: list[tuple[int, str, int]]) -> None:
        """Update the saved playlist list for the context menu submenu."""
        self._table._saved_playlists = playlists
