"""iTunes podcast search dialog — browse and subscribe to podcasts."""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QPixmap
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

_SEARCH_URL = "https://itunes.apple.com/search"
_ART_SIZE = 60


class _SearchWorker(QThread):
    """Search iTunes podcast directory in the background."""

    finished = Signal(list)  # list of result dicts
    error = Signal(str)

    def __init__(self, query: str, parent=None):
        super().__init__(parent)
        self._query = query

    def run(self):
        try:
            resp = httpx.get(
                _SEARCH_URL,
                params={
                    "term": self._query,
                    "media": "podcast",
                    "limit": 25,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self.finished.emit(data.get("results", []))
        except Exception as e:
            self.error.emit(str(e))


class _ArtworkLoader(QThread):
    """Download artwork images in the background."""

    loaded = Signal(int, QPixmap)  # row index, pixmap

    def __init__(self, items: list[tuple[int, str]], parent=None):
        super().__init__(parent)
        self._items = items  # [(row, url), ...]

    def run(self):
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


class PodcastSearchDialog(QDialog):
    """Search the iTunes podcast directory and subscribe."""

    feed_selected = Signal(str)  # RSS feed URL

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Browse Podcasts")
        self.setMinimumSize(550, 450)
        self._results: list[dict] = []
        self._selected_feed: str | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Search bar.
        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search podcasts…")
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
        self._result_list.setIconSize(QSize(_ART_SIZE, _ART_SIZE))
        self._result_list.currentRowChanged.connect(self._on_selection_changed)
        layout.addWidget(self._result_list, stretch=1)

        # Details.
        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet("font-size: 11px; color: #666;")
        self._detail_label.setMaximumHeight(60)
        layout.addWidget(self._detail_label)

        # Buttons.
        self._buttons = QDialogButtonBox()
        self._btn_subscribe = self._buttons.addButton(
            "Subscribe", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._btn_subscribe.setEnabled(False)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._btn_subscribe.clicked.connect(self._subscribe)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _do_search(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            return

        self._status_label.setText("Searching…")
        self._result_list.clear()
        self._results = []
        self._btn_subscribe.setEnabled(False)

        self._worker = _SearchWorker(query, self)
        self._worker.finished.connect(self._on_results)
        self._worker.error.connect(
            lambda e: self._status_label.setText(f"Error: {e}")
        )
        self._worker.start()

    def _on_results(self, results: list[dict]) -> None:
        self._results = results
        self._status_label.setText(f"{len(results)} results")

        artwork_jobs: list[tuple[int, str]] = []

        for i, r in enumerate(results):
            title = r.get("collectionName", r.get("trackName", "Unknown"))
            artist = r.get("artistName", "")
            label = f"{title}\n{artist}"

            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._result_list.addItem(item)

            # Queue artwork download.
            art_url = r.get("artworkUrl100") or r.get("artworkUrl60", "")
            if art_url:
                artwork_jobs.append((i, art_url))

        # Load artwork in background.
        if artwork_jobs:
            self._art_loader = _ArtworkLoader(artwork_jobs, self)
            self._art_loader.loaded.connect(self._on_artwork_loaded)
            self._art_loader.start()

    def _on_artwork_loaded(self, row: int, pixmap: QPixmap) -> None:
        if 0 <= row < self._result_list.count():
            item = self._result_list.item(row)
            scaled = pixmap.scaled(
                _ART_SIZE, _ART_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            from PySide6.QtGui import QIcon
            item.setIcon(QIcon(scaled))

    def _on_selection_changed(self, row: int) -> None:
        if 0 <= row < len(self._results):
            r = self._results[row]
            feed_url = r.get("feedUrl", "")
            self._btn_subscribe.setEnabled(bool(feed_url))

            # Show details.
            genres = ", ".join(r.get("genres", []))
            count = r.get("trackCount", "?")
            self._detail_label.setText(
                f"Episodes: {count}  |  Genres: {genres}"
            )
        else:
            self._btn_subscribe.setEnabled(False)
            self._detail_label.setText("")

    def _subscribe(self) -> None:
        row = self._result_list.currentRow()
        if 0 <= row < len(self._results):
            feed_url = self._results[row].get("feedUrl", "")
            if feed_url:
                self._selected_feed = feed_url
                self.feed_selected.emit(feed_url)
                self.accept()
