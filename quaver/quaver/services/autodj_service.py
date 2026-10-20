"""Auto-DJ service — automatic queue population from the library.

Picks tracks intelligently: avoids recently played, filters by source
folder and genre. Tops up the queue whenever it runs low.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, Signal, Slot

from quaver.core.library import LibraryDB, Track
from quaver.services.player_service import PlayerService


class AutoDJService(QObject):
    """Monitors the queue and appends tracks when it runs low."""

    enabled_changed = Signal(bool)

    def __init__(
        self,
        db: LibraryDB,
        player: PlayerService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._player = player
        self._enabled = False

        # Filters.
        self._source_folders: list[str] = []  # empty = whole library
        self._genre_filter: str = ""           # empty = all genres
        self._avoid_hours: int = 4             # skip tracks played within N hours
        self._lookahead: int = 5               # tracks to keep ahead of current position
        self._recently_queued: list[int] = []  # track IDs we've added (avoid repeats)
        self._topping_up: bool = False         # re-entrancy guard

        # React to queue changes to top up.
        self._player.queue_changed.connect(self._maybe_top_up)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        self.enabled_changed.emit(on)
        if on:
            self._recently_queued.clear()
            self._maybe_top_up()

    def toggle(self) -> None:
        self.set_enabled(not self._enabled)

    @property
    def source_folders(self) -> list[str]:
        return list(self._source_folders)

    def set_source_folders(self, folders: list[str]) -> None:
        self._source_folders = list(folders)

    @property
    def genre_filter(self) -> str:
        return self._genre_filter

    def set_genre_filter(self, genre: str) -> None:
        self._genre_filter = genre.strip()

    @property
    def avoid_hours(self) -> int:
        return self._avoid_hours

    def set_avoid_hours(self, hours: int) -> None:
        self._avoid_hours = max(0, hours)

    @property
    def lookahead(self) -> int:
        return self._lookahead

    def set_lookahead(self, count: int) -> None:
        self._lookahead = max(1, count)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @Slot()
    def _maybe_top_up(self) -> None:
        if not self._enabled or self._topping_up:
            return

        queue = self._player.queue
        idx = self._player.queue_index
        remaining = len(queue) - (idx + 1)

        if remaining >= self._lookahead:
            return

        needed = self._lookahead - remaining
        picks = self._pick_tracks(needed)
        if picks:
            self._topping_up = True
            try:
                for track in picks:
                    self._player.enqueue(track)
                    self._recently_queued.append(track.id)
                # Keep memory bounded.
                if len(self._recently_queued) > 200:
                    self._recently_queued = self._recently_queued[-100:]
            finally:
                self._topping_up = False

    def _pick_tracks(self, count: int) -> list[Track]:
        """Select *count* tracks from the library, applying filters."""
        # Get candidate pool.
        if self._source_folders:
            candidates = self._db.get_tracks_by_folders(self._source_folders)
        else:
            candidates = self._db.get_all_tracks()

        # Genre filter.
        if self._genre_filter:
            genre_lower = self._genre_filter.lower()
            candidates = [
                t for t in candidates
                if genre_lower in t.genre.lower()
            ]

        if not candidates:
            return []

        # Exclude recently queued (by ID).
        recent_set = set(self._recently_queued)
        candidates = [t for t in candidates if t.id not in recent_set]

        # Exclude recently played (by timestamp).
        if self._avoid_hours > 0:
            cutoff = datetime.now() - timedelta(hours=self._avoid_hours)
            cutoff_str = cutoff.isoformat()
            candidates = [
                t for t in candidates
                if not t.last_played or t.last_played < cutoff_str
            ]

        # Also exclude anything already in the current queue.
        queue_ids = {t.id for t in self._player.queue}
        candidates = [t for t in candidates if t.id not in queue_ids]

        if not candidates:
            # Fallback: if filters are too strict, relax recency.
            if self._source_folders:
                candidates = self._db.get_tracks_by_folders(self._source_folders)
            else:
                candidates = self._db.get_all_tracks()
            candidates = [t for t in candidates if t.id not in queue_ids]

        if not candidates:
            return []

        # Weighted random: favor less-played tracks.
        # Weight = 1 / (play_count + 1), so unplayed tracks are most likely.
        weights = [1.0 / (t.play_count + 1) for t in candidates]
        count = min(count, len(candidates))

        picks: list[Track] = []
        pool = list(zip(candidates, weights))
        for _ in range(count):
            if not pool:
                break
            total = sum(w for _, w in pool)
            r = random.uniform(0, total)
            cumulative = 0.0
            chosen_idx = 0
            for j, (_, w) in enumerate(pool):
                cumulative += w
                if cumulative >= r:
                    chosen_idx = j
                    break
            picks.append(pool[chosen_idx][0])
            pool.pop(chosen_idx)

        return picks
