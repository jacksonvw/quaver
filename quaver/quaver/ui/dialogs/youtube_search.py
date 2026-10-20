"""YouTube channel search dialog — find and subscribe to channels."""

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

from quaver.core.youtube import search_youtube

_ART_SIZE = 60


class _SearchWorker(QThread):
    """Search YouTube for channels in the background."""

    finished = Signal(list)
    error = Signal(str)

    def __init__(self, query: str, cookies_browser: str = "", parent=None):
        super().__init__(parent)
        self._query = query
        self._cookies = cookies_browser

    def run(self):
        try:
            results = search_youtube(
                self._query, limit=25,
                cookies_browser=self._cookies,
            )
            # Filter to channels only for this dialog.
            channels = [r for r in results if r.get("type") == "channel"]
            self.finished.emit(channels)
        except Exception as e:
            self.error.emit(str(e))


class _ThumbnailLoader(QThread):
    """Download thumbnail images in the background."""

    loaded = Signal(int, QPixmap)

    def __init__(self, items: list[tuple[int, str]], parent=None):
        super().__init__(parent)
        self._items = items

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


class YouTubeSearchDialog(QDialog):
    """Search YouTube channels and subscribe."""

    channel_selected = Signal(str)  # channel URL

    def __init__(self, cookies_browser: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Browse YouTube Channels")
        self.setMinimumSize(550, 450)
        self._results: list[dict] = []
        self._cookies = cookies_browser
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Search bar.
        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search YouTube channels...")
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

        self._status_label.setText("Searching...")
        self._result_list.clear()
        self._results = []
        self._btn_subscribe.setEnabled(False)

        self._worker = _SearchWorker(query, self._cookies, self)
        self._worker.finished.connect(self._on_results)
        self._worker.error.connect(
            lambda e: self._status_label.setText(f"Error: {e}")
        )
        self._worker.start()

    def _on_results(self, results: list[dict]) -> None:
        self._results = results
        self._status_label.setText(f"{len(results)} channels found")

        thumb_jobs: list[tuple[int, str]] = []

        for i, r in enumerate(results):
            title = r.get("title", "Unknown")
            subs = r.get("subscriber_count", 0)
            sub_text = f"{subs:,} subscribers" if subs else ""
            label = f"{title}\n{sub_text}" if sub_text else title

            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._result_list.addItem(item)

            thumb_url = r.get("thumbnail_url", "")
            if thumb_url:
                thumb_jobs.append((i, thumb_url))

        if thumb_jobs:
            self._thumb_loader = _ThumbnailLoader(thumb_jobs, self)
            self._thumb_loader.loaded.connect(self._on_thumb_loaded)
            self._thumb_loader.start()

    def _on_thumb_loaded(self, row: int, pixmap: QPixmap) -> None:
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
            self._btn_subscribe.setEnabled(bool(r.get("id")))
            title = r.get("title", "")
            self._detail_label.setText(title)
        else:
            self._btn_subscribe.setEnabled(False)
            self._detail_label.setText("")

    def _subscribe(self) -> None:
        row = self._result_list.currentRow()
        if 0 <= row < len(self._results):
            r = self._results[row]
            channel_id = r.get("id", "")
            if channel_id:
                url = f"https://www.youtube.com/channel/{channel_id}"
                self.channel_selected.emit(url)
                self.accept()
