"""Match picker dialog — search scraper APIs and pick the correct match.

Used for correcting wrong show/movie matches (e.g. Ghost Stories mapped
to the wrong TVMaze/TMDb entry).  Follows the same search-results-list
pattern as PodcastSearchDialog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import httpx

from quaver.core.tmdb import TMDbClient, TMDbSearchResult
from quaver.core.tvmaze import TVMazeClient, TVMazeSearchResult

log = logging.getLogger(__name__)

_ART_SIZE = 68  # poster thumbnail height


# ------------------------------------------------------------------
# Result type returned to the caller
# ------------------------------------------------------------------

@dataclass
class MatchChoice:
    """The user's choice from the match picker."""

    scraper: str  # "tvmaze" or "tmdb"
    scraper_id: int
    title: str
    year: int | None


# ------------------------------------------------------------------
# Background workers
# ------------------------------------------------------------------

class _SearchWorker(QThread):
    """Run API search in background thread."""

    finished = Signal(list)  # list of TVMazeSearchResult | TMDbSearchResult
    error = Signal(str)

    def __init__(
        self,
        query: str,
        provider: str,
        media_type: str,
        tmdb_api_key: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._query = query
        self._provider = provider
        self._media_type = media_type
        self._tmdb_api_key = tmdb_api_key

    def run(self) -> None:
        try:
            if self._provider == "tvmaze":
                client = TVMazeClient()
                try:
                    results = client.search_series_all(self._query)
                finally:
                    client.close()
                self.finished.emit(results)
            elif self._provider == "tmdb":
                client = TMDbClient(self._tmdb_api_key)
                try:
                    if self._media_type == "movie":
                        results = client.search_movie_all(self._query)
                    else:
                        results = client.search_series_all(self._query)
                finally:
                    client.close()
                self.finished.emit(results)
            else:
                self.error.emit(f"Unknown provider: {self._provider}")
        except Exception as e:
            self.error.emit(str(e))


class _PosterLoader(QThread):
    """Download poster thumbnails in background."""

    loaded = Signal(int, QPixmap)  # row, pixmap

    def __init__(self, items: list[tuple[int, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self._items = items

    def run(self) -> None:
        for row, url in self._items:
            try:
                resp = httpx.get(url, timeout=10, follow_redirects=True)
                resp.raise_for_status()
                pixmap = QPixmap()
                pixmap.loadFromData(resp.content)
                if not pixmap.isNull():
                    self.loaded.emit(row, pixmap)
            except Exception:
                continue


# ------------------------------------------------------------------
# Dialog
# ------------------------------------------------------------------

class MatchPickerDialog(QDialog):
    """Search a metadata API and let the user pick the correct match."""

    def __init__(
        self,
        show: dict,
        provider: str,
        media_type: str = "series",
        tmdb_api_key: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._show = show
        self._provider = provider
        self._media_type = media_type
        self._tmdb_api_key = tmdb_api_key
        self._results: list = []
        self._choice: MatchChoice | None = None

        title = show.get("title") or show.get("folder_title", "")
        self.setWindowTitle(f"Change Match \u2014 {title}")
        self.setMinimumSize(580, 480)
        self._build_ui(title)

        # Auto-search on open.
        self._do_search()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self, initial_query: str) -> None:
        layout = QVBoxLayout(self)

        # Search bar.
        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setText(initial_query)
        self._search_input.setPlaceholderText("Search title\u2026")
        self._search_input.returnPressed.connect(self._do_search)
        btn_search = QPushButton("Search")
        btn_search.clicked.connect(self._do_search)
        search_row.addWidget(self._search_input, stretch=1)
        search_row.addWidget(btn_search)
        layout.addLayout(search_row)

        # Status.
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self._status_label)

        # Results list.
        self._result_list = QListWidget()
        self._result_list.setIconSize(QSize(int(_ART_SIZE * 0.67), _ART_SIZE))
        self._result_list.currentRowChanged.connect(self._on_selection_changed)
        self._result_list.doubleClicked.connect(self._select_and_scrape)
        layout.addWidget(self._result_list, stretch=1)

        # Detail area.
        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet("font-size: 11px; color: #aaa;")
        self._detail_label.setMaximumHeight(80)
        layout.addWidget(self._detail_label)

        # Current binding info.
        binding = self._format_binding()
        if binding:
            bind_label = QLabel(binding)
            bind_label.setStyleSheet("font-size: 11px; color: #666;")
            layout.addWidget(bind_label)

        # Buttons.
        self._buttons = QDialogButtonBox()
        self._btn_select = self._buttons.addButton(
            "Select && Scrape", QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self._btn_select.setEnabled(False)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._btn_select.clicked.connect(self._select_and_scrape)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _format_binding(self) -> str:
        scraper = self._show.get("scraper")
        scraper_id = self._show.get("scraper_id")
        if scraper and scraper_id:
            return f"Current binding: {scraper} #{scraper_id}"
        return ""

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _do_search(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            return

        self._status_label.setText("Searching\u2026")
        self._result_list.clear()
        self._results = []
        self._btn_select.setEnabled(False)
        self._detail_label.setText("")

        self._worker = _SearchWorker(
            query, self._provider, self._media_type,
            self._tmdb_api_key, self,
        )
        self._worker.finished.connect(self._on_results)
        self._worker.error.connect(
            lambda e: self._status_label.setText(f"Error: {e}"),
        )
        self._worker.start()

    def _on_results(self, results: list) -> None:
        self._results = results
        n = len(results)
        self._status_label.setText(
            f"{n} result{'s' if n != 1 else ''}" if n else "No results found",
        )

        poster_jobs: list[tuple[int, str]] = []

        for i, r in enumerate(results):
            label = self._format_result_label(r)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._result_list.addItem(item)

            url = self._get_poster_url(r)
            if url:
                poster_jobs.append((i, url))

        if poster_jobs:
            self._poster_loader = _PosterLoader(poster_jobs, self)
            self._poster_loader.loaded.connect(self._on_poster_loaded)
            self._poster_loader.start()

    def _on_poster_loaded(self, row: int, pixmap: QPixmap) -> None:
        item = self._result_list.item(row)
        if item:
            scaled = pixmap.scaled(
                int(_ART_SIZE * 0.67), _ART_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            item.setIcon(QIcon(scaled))

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_selection_changed(self, row: int) -> None:
        self._btn_select.setEnabled(row >= 0)
        if 0 <= row < len(self._results):
            r = self._results[row]
            self._detail_label.setText(self._get_summary(r))

    def _select_and_scrape(self) -> None:
        row = self._result_list.currentRow()
        if row < 0 or row >= len(self._results):
            return

        r = self._results[row]
        self._choice = self._make_choice(r)
        self.accept()

    def selected_match(self) -> MatchChoice | None:
        """Return the user's choice after dialog closes, or None if cancelled."""
        return self._choice

    # ------------------------------------------------------------------
    # Provider-agnostic helpers
    # ------------------------------------------------------------------

    def _format_result_label(self, r) -> str:
        if isinstance(r, TVMazeSearchResult):
            year = f" ({r.year})" if r.year else ""
            network = f"  \u00b7  {r.network}" if r.network else ""
            status = f"  \u00b7  {r.status}" if r.status else ""
            return f"{r.title}{year}{network}{status}"
        elif isinstance(r, TMDbSearchResult):
            year = f" ({r.year})" if r.year else ""
            return f"{r.title}{year}"
        return str(r)

    def _get_poster_url(self, r) -> str | None:
        if isinstance(r, TVMazeSearchResult):
            return r.poster_url
        elif isinstance(r, TMDbSearchResult):
            return r.poster_url
        return None

    def _get_summary(self, r) -> str:
        if isinstance(r, TVMazeSearchResult):
            return r.summary[:300] if r.summary else ""
        elif isinstance(r, TMDbSearchResult):
            return r.overview[:300] if r.overview else ""
        return ""

    def _make_choice(self, r) -> MatchChoice:
        if isinstance(r, TVMazeSearchResult):
            return MatchChoice(
                scraper="tvmaze",
                scraper_id=r.tvmaze_id,
                title=r.title,
                year=r.year,
            )
        elif isinstance(r, TMDbSearchResult):
            return MatchChoice(
                scraper="tmdb",
                scraper_id=r.tmdb_id,
                title=r.title,
                year=r.year,
            )
        raise TypeError(f"Unknown result type: {type(r)}")
