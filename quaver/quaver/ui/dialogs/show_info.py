"""Show / Movie Information dialog — Kodi-style metadata panel.

Right-click a show in the sidebar → "Show Information..." opens this dialog
with poster, metadata, plot, scraper binding, and episode stats.
Also handles movie info when opened for a movie track.
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

_POSTER_W = 220
_POSTER_H = 330


# ------------------------------------------------------------------
# Dialog
# ------------------------------------------------------------------


class ShowInfoDialog(QDialog):
    """Display show or movie metadata in a Kodi-style info panel.

    For shows: pass show dict + episode_ids + db for watched counts.
    For movies: pass a synthetic show dict built from the movie track.
    """

    change_match_requested = Signal()
    scrape_requested = Signal()

    def __init__(
        self,
        show: dict,
        episode_ids: list[int] | None = None,
        db=None,
        is_movie: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._show = show
        self._episode_ids = episode_ids or []
        self._db = db
        self._is_movie = is_movie

        title = show.get("title") or show.get("folder_title", "Unknown")
        kind = "Movie" if is_movie else "Show"
        self.setWindowTitle(f"{kind} Information \u2014 {title}")
        self.setMinimumSize(620, 420)
        self.resize(680, 500)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Scrollable content area.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)

        main = QHBoxLayout(content)
        main.setContentsMargins(20, 20, 20, 20)
        main.setSpacing(20)

        # --- Left: poster ---
        self._poster_label = QLabel()
        self._poster_label.setFixedSize(_POSTER_W, _POSTER_H)
        self._poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._poster_label.setStyleSheet(
            "background: rgba(255,255,255,8); border-radius: 4px;"
        )
        self._load_poster()
        main.addWidget(self._poster_label, alignment=Qt.AlignmentFlag.AlignTop)

        # --- Right: metadata ---
        right = QVBoxLayout()
        right.setSpacing(8)
        main.addLayout(right, stretch=1)

        # Title.
        title = self._show.get("title") or self._show.get("folder_title", "")
        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        right.addWidget(title_label)

        # Subtitle line: year · genre · status.
        parts: list[str] = []
        year = self._show.get("year")
        if year:
            parts.append(str(year))
        genre = self._show.get("genre", "")
        if genre:
            parts.append(genre)
        status = self._show.get("status", "")
        if status and not self._is_movie:
            parts.append(status)
        if parts:
            sub = QLabel(" \u00b7 ".join(parts))
            sub.setWordWrap(True)
            sub.setStyleSheet("font-size: 13px; color: #aaa;")
            right.addWidget(sub)

        # Episode count (shows only).
        if not self._is_movie and self._episode_ids:
            watched = self._count_watched()
            total = len(self._episode_ids)
            ep_text = f"{total} episode{'s' if total != 1 else ''}"
            if watched > 0:
                ep_text += f" \u00b7 {watched} watched"
            unwatched = total - watched
            if unwatched > 0 and watched > 0:
                ep_text += f" \u00b7 {unwatched} unwatched"
            ep_label = QLabel(ep_text)
            ep_label.setStyleSheet("font-size: 12px; color: #888;")
            right.addWidget(ep_label)

        # Plot.
        plot = self._show.get("plot", "")
        if plot:
            right.addSpacing(4)
            plot_label = QLabel(plot)
            plot_label.setWordWrap(True)
            plot_label.setStyleSheet("font-size: 12px; color: #ccc; line-height: 1.4;")
            plot_label.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
            )
            right.addWidget(plot_label)

        right.addStretch()

        # --- Scraper binding section ---
        right.addSpacing(8)
        self._build_scraper_section(right)

        # --- Bottom button bar (outside scroll) ---
        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(20, 8, 20, 12)

        btn_scrape = QPushButton("Scrape Metadata")
        btn_scrape.clicked.connect(self.scrape_requested.emit)
        btn_bar.addWidget(btn_scrape)

        btn_bar.addStretch()

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_bar.addWidget(btn_close)

        outer.addLayout(btn_bar)

    def _build_scraper_section(self, layout: QVBoxLayout) -> None:
        """Scraper binding display with 'Change...' button."""
        row = QHBoxLayout()
        row.setSpacing(8)

        scraper = self._show.get("scraper", "")
        scraper_id = self._show.get("scraper_id")
        imdb_id = self._show.get("imdb_id", "")

        if scraper and scraper_id:
            binding_text = f"Matched: {scraper} #{scraper_id}"
            if imdb_id:
                binding_text += f" \u00b7 IMDb {imdb_id}"
        else:
            binding_text = "Not matched to any scraper"

        bind_label = QLabel(binding_text)
        bind_label.setStyleSheet("font-size: 11px; color: #777;")
        row.addWidget(bind_label, stretch=1)

        btn_change = QPushButton("Change\u2026")
        btn_change.setFixedWidth(80)
        btn_change.clicked.connect(self.change_match_requested.emit)
        row.addWidget(btn_change)

        layout.addLayout(row)

    # ------------------------------------------------------------------
    # Poster
    # ------------------------------------------------------------------

    def _load_poster(self) -> None:
        poster = self._show.get("poster_path", "")
        if poster and os.path.isfile(poster):
            pm = QPixmap(poster)
            if not pm.isNull():
                scaled = pm.scaled(
                    _POSTER_W, _POSTER_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._poster_label.setPixmap(scaled)
                return
        # Placeholder.
        self._poster_label.setText("No poster")
        self._poster_label.setStyleSheet(
            self._poster_label.styleSheet()
            + " color: #555; font-size: 12px;"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _count_watched(self) -> int:
        """Count how many episodes have been watched."""
        if not self._db or not self._episode_ids:
            return 0
        count = 0
        for eid in self._episode_ids:
            vm = self._db.get_video_metadata(eid)
            if vm and vm.get("watched"):
                count += 1
        return count
