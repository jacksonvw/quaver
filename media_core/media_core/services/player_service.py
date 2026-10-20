"""Player service — coordinates audio engine, play queue, shuffle, repeat, ReplayGain.

All queue manipulation and playback control flows through here.
The UI and MPRIS2 both talk to this service rather than the engine directly.
"""

from __future__ import annotations

import random
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from media_core.core.audio import AudioEngine, PlaybackState
from media_core.core.track import Track
from media_core.core.replaygain import (
    RGMode, apply_rg_mode, apply_eq_bands, apply_compressor,
    insert_audio_filters,
)


class _DurationSink(Protocol):
    """Minimal protocol the ``db`` parameter satisfies.

    PlayerService only persists one thing back to the host shell: a
    discovered duration that wasn't known at scan time.  Quaver's
    ``LibraryDB`` matches this protocol structurally; Minim and other
    library-less shells pass ``None`` and the persistence path is
    skipped via the ``self._db is not None`` guard.
    """

    def update_track_duration(self, track_id: int, duration_ms: int) -> None: ...


class RepeatMode(Enum):
    OFF = auto()
    ALL = auto()
    ONE = auto()


class PlayerService(QObject):
    """High-level playback service wrapping AudioEngine + queue."""

    # Signals for UI binding.
    state_changed = Signal(PlaybackState)
    track_changed = Signal(object)  # Track | None
    queue_changed = Signal()
    position_updated = Signal(int, int)  # position_ms, duration_ms
    spectrum_updated = Signal(list)  # list[float] magnitude values (0.0–1.0)
    error_occurred = Signal(str)
    shuffle_changed = Signal(bool)
    repeat_changed = Signal(RepeatMode)
    resolve_needed = Signal(object)  # Track — emitted when a youtube:// URI needs resolution

    def __init__(self, db: _DurationSink | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # db is optional — Minim and other shells without a library pass None.
        # Only the lazy duration backfill in _on_position_updated touches the DB.
        self._db = db
        self._engine = AudioEngine()

        # Queue state.
        self._queue: list[Track] = []
        self._queue_index: int = -1
        # Where playback was when stop() tore the engine down, so
        # toggle_play() can restart from the same place.
        self._stopped_index: int = -1
        self._shuffle = False
        self._shuffle_order: list[int] = []
        self._repeat = RepeatMode.OFF

        # Audio filter chain.
        self._rg_mode = RGMode.OFF
        self._filters = insert_audio_filters(self._engine._playbin)
        self._rgvolume = self._filters.rgvolume
        self._equalizer = self._filters.equalizer
        self._compressor = self._filters.compressor
        self._spectrum = self._filters.spectrum

        # Wire engine callbacks → Qt signals (thread-safe bridge).
        self._engine.on_state_changed = lambda s: self.state_changed.emit(s)
        self._engine.on_error = lambda e: self.error_occurred.emit(e)
        self._engine.on_about_to_finish = self._on_about_to_finish
        self._engine.on_gapless_transition = self._on_gapless_transition
        self._engine.on_track_finished = self._on_track_finished

        # Spectrum bus messages → spectrum_updated signal.
        if self._spectrum:
            bus = self._engine._playbin.get_bus()
            bus.connect("message::element", self._on_spectrum_message)

        # Position polling timer.
        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(250)
        self._pos_timer.timeout.connect(self._emit_position)

        self.state_changed.connect(self._on_state_changed_internal)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def engine(self) -> AudioEngine:
        return self._engine

    @property
    def current_track(self) -> Track | None:
        if 0 <= self._queue_index < len(self._queue):
            return self._queue[self._queue_index]
        return None

    @property
    def queue(self) -> list[Track]:
        return list(self._queue)

    @property
    def queue_index(self) -> int:
        return self._queue_index

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def repeat(self) -> RepeatMode:
        return self._repeat

    @property
    def rg_mode(self) -> RGMode:
        return self._rg_mode

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    def play_track(self, track: Track) -> None:
        """Play a single track (replaces queue with just this track)."""
        self._queue = [track]
        self._queue_index = 0
        self._rebuild_shuffle_order()
        self._start_current()

    def play_tracks(self, tracks: list[Track], start_index: int = 0) -> None:
        """Replace the queue with *tracks* and start at *start_index*."""
        self._queue = list(tracks)
        self._queue_index = start_index
        self._rebuild_shuffle_order()
        self._start_current()

    def load_queue(self, tracks: list[Track], current_index: int = 0) -> None:
        """Restore a queue without starting playback (e.g. on app startup)."""
        self._queue = list(tracks)
        self._queue_index = max(0, min(current_index, len(tracks) - 1)) if tracks else -1
        self._rebuild_shuffle_order()
        self.queue_changed.emit()

    def pause(self) -> None:
        self._engine.pause()

    def resume(self) -> None:
        self._engine.resume()

    def toggle_pause(self) -> None:
        self._engine.toggle_pause()

    def toggle_play(self) -> None:
        """Play/pause toggle that also restarts after Stop.

        toggle_pause() is a no-op on a stopped (NULL) pipeline, so a plain
        pause toggle leaves the Play button dead after Stop. When stopped,
        restart from the current queue position (a restored-but-not-started
        queue), the position remembered at stop(), or the top of the queue.
        """
        if self._engine.state != PlaybackState.STOPPED:
            self._engine.toggle_pause()
            return
        if not self._queue:
            return
        if not (0 <= self._queue_index < len(self._queue)):
            if 0 <= self._stopped_index < len(self._queue):
                self._queue_index = self._stopped_index
            else:
                self._queue_index = 0
        self._start_current()

    def stop(self) -> None:
        self._stopped_index = self._queue_index
        self._engine.stop()
        self._queue_index = -1
        self.track_changed.emit(None)

    def seek(self, position_ms: int) -> None:
        self._engine.seek(position_ms)

    def set_volume(self, level: float) -> None:
        self._engine.set_volume(level)

    def get_volume(self) -> float:
        return self._engine.get_volume()

    def next_track(self) -> None:
        idx = self._next_index()
        if idx is not None:
            self._queue_index = idx
            self._start_current()
        else:
            self.stop()

    def prev_track(self) -> None:
        # If more than 3 seconds in, restart current track.
        if self._engine.get_position_ms() > 3000:
            self._engine.seek(0)
            return
        idx = self._prev_index()
        if idx is not None:
            self._queue_index = idx
            self._start_current()

    # ------------------------------------------------------------------
    # Queue manipulation
    # ------------------------------------------------------------------

    def enqueue(self, track: Track) -> None:
        """Append a track to the end of the queue."""
        self._queue.append(track)
        self._rebuild_shuffle_order()
        self._queue_gapless_next()
        self.queue_changed.emit()

    def insert_tracks(self, tracks: list[Track], position: int) -> None:
        """Insert *tracks* at *position* in the queue."""
        position = max(0, min(position, len(self._queue)))
        for i, track in enumerate(tracks):
            self._queue.insert(position + i, track)
        if position <= self._queue_index:
            self._queue_index += len(tracks)
        self._rebuild_shuffle_order()
        self._queue_gapless_next()
        self.queue_changed.emit()

    def enqueue_next(self, track: Track) -> None:
        """Insert a track right after the current position."""
        insert_pos = self._queue_index + 1 if self._queue_index >= 0 else 0
        self._queue.insert(insert_pos, track)
        self._rebuild_shuffle_order()
        self._queue_gapless_next()
        self.queue_changed.emit()

    def remove_from_queue(self, index: int) -> None:
        if not (0 <= index < len(self._queue)):
            return
        removing_current = index == self._queue_index
        self._queue.pop(index)
        if removing_current:
            # The engine is still playing the removed track; leaving it be
            # would desync the highlighted row and arm gapless one track
            # too far. Advance to the track that took this slot, or stop
            # if the tail was removed.
            if index < len(self._queue):
                self._queue_index = index
                self._rebuild_shuffle_order()
                self._start_current()
            else:
                self.stop()
            self.queue_changed.emit()
            return
        if index < self._queue_index:
            self._queue_index -= 1
        self._rebuild_shuffle_order()
        self._queue_gapless_next()
        self.queue_changed.emit()

    def move_in_queue(self, from_index: int, to_index: int) -> None:
        """Move a queue item from one position to another."""
        if from_index == to_index:
            return
        if not (0 <= from_index < len(self._queue)):
            return
        to_index = max(0, min(to_index, len(self._queue) - 1))

        track = self._queue.pop(from_index)
        self._queue.insert(to_index, track)

        # Adjust current index.
        if self._queue_index == from_index:
            self._queue_index = to_index
        elif from_index < self._queue_index <= to_index:
            self._queue_index -= 1
        elif to_index <= self._queue_index < from_index:
            self._queue_index += 1

        self._rebuild_shuffle_order()
        self._queue_gapless_next()
        self.queue_changed.emit()

    def move_batch_in_queue(self, source_rows: list[int], dest_row: int) -> None:
        """Move multiple queue items to a destination as a contiguous block."""
        rows = sorted(source_rows)
        if not rows:
            return
        # Validate.
        if any(r < 0 or r >= len(self._queue) for r in rows):
            return

        # Extract the tracks and find where the current index lands.
        moved = [self._queue[r] for r in rows]
        current_track = self._queue[self._queue_index] if 0 <= self._queue_index < len(self._queue) else None

        # Remove from highest index first to preserve lower indices.
        for r in reversed(rows):
            self._queue.pop(r)

        # Adjust dest for removed items that were before it.
        adjusted_dest = dest_row - sum(1 for r in rows if r < dest_row)
        adjusted_dest = max(0, min(adjusted_dest, len(self._queue)))

        # Insert in original order at destination.
        for i, track in enumerate(moved):
            self._queue.insert(adjusted_dest + i, track)

        # Restore current index by finding the current track.
        if current_track is not None:
            try:
                self._queue_index = self._queue.index(current_track)
            except ValueError:
                self._queue_index = -1

        self._rebuild_shuffle_order()
        self._queue_gapless_next()
        self.queue_changed.emit()

    def clear_queue(self) -> None:
        was_playing = self._engine.state == PlaybackState.PLAYING
        self._queue.clear()
        self._queue_index = -1
        self._shuffle_order.clear()
        self._engine.set_next_uri(None)
        if was_playing:
            self._engine.stop()
        self.queue_changed.emit()
        self.track_changed.emit(None)

    # ------------------------------------------------------------------
    # Shuffle / Repeat
    # ------------------------------------------------------------------

    def set_shuffle(self, enabled: bool) -> None:
        self._shuffle = enabled
        self._rebuild_shuffle_order()
        self._queue_gapless_next()
        self.shuffle_changed.emit(enabled)

    def toggle_shuffle(self) -> None:
        self.set_shuffle(not self._shuffle)

    def set_repeat(self, mode: RepeatMode) -> None:
        self._repeat = mode
        self._queue_gapless_next()
        self.repeat_changed.emit(mode)

    def cycle_repeat(self) -> None:
        modes = list(RepeatMode)
        idx = modes.index(self._repeat)
        self.set_repeat(modes[(idx + 1) % len(modes)])

    # ------------------------------------------------------------------
    # ReplayGain
    # ------------------------------------------------------------------

    def set_rg_mode(self, mode: RGMode) -> None:
        self._rg_mode = mode
        apply_rg_mode(self._rgvolume, mode)

    def set_eq_bands(self, gains: list[float]) -> None:
        apply_eq_bands(self._equalizer, gains)

    def set_compressor(
        self, enabled: bool, ratio: float = 4.0, threshold: float = 0.5
    ) -> None:
        apply_compressor(self._compressor, enabled, ratio, threshold)

    def set_playback_speed(self, rate: float) -> None:
        self._engine.set_playback_rate(rate)

    def get_playback_speed(self) -> float:
        return self._engine.get_playback_rate()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_current(self) -> None:
        track = self.current_track
        if track is None:
            return
        if track.filepath.startswith("youtube://"):
            # Don't call the engine — emit resolve_needed so MainWindow
            # can resolve the stream URL and call play_resolved_uri().
            self.track_changed.emit(track)
            self.queue_changed.emit()
            self.resolve_needed.emit(track)
        else:
            self._engine.play_file(track.filepath)
            self.track_changed.emit(track)
            self.queue_changed.emit()
            self._queue_gapless_next()

    def _queue_gapless_next(self) -> None:
        idx = self._next_index()
        if idx is not None:
            next_path = self._queue[idx].filepath
            # Can't pre-resolve youtube:// URIs for gapless (URLs expire).
            if next_path.startswith("youtube://"):
                self._engine.set_next_uri(None)
            else:
                self._engine.set_next_uri(next_path)
        else:
            self._engine.set_next_uri(None)

    def _next_index(self) -> int | None:
        if not self._queue:
            return None
        if self._repeat == RepeatMode.ONE:
            return self._queue_index
        if self._shuffle:
            return self._next_shuffle_index()
        nxt = self._queue_index + 1
        if nxt < len(self._queue):
            return nxt
        if self._repeat == RepeatMode.ALL:
            return 0
        return None

    def _prev_index(self) -> int | None:
        if not self._queue:
            return None
        if self._shuffle:
            return self._prev_shuffle_index()
        prev = self._queue_index - 1
        if prev >= 0:
            return prev
        if self._repeat == RepeatMode.ALL:
            return len(self._queue) - 1
        return None

    def _rebuild_shuffle_order(self) -> None:
        if not self._shuffle or not self._queue:
            self._shuffle_order = []
            return
        indices = list(range(len(self._queue)))
        if self._queue_index in indices:
            indices.remove(self._queue_index)
        random.shuffle(indices)
        # Put current track at the front so "next" picks from shuffled remainder.
        if 0 <= self._queue_index < len(self._queue):
            self._shuffle_order = [self._queue_index] + indices
        else:
            self._shuffle_order = indices

    def _next_shuffle_index(self) -> int | None:
        if not self._shuffle_order:
            return None
        try:
            pos = self._shuffle_order.index(self._queue_index)
        except ValueError:
            pos = -1
        nxt = pos + 1
        if nxt < len(self._shuffle_order):
            return self._shuffle_order[nxt]
        if self._repeat == RepeatMode.ALL:
            self._rebuild_shuffle_order()
            return self._shuffle_order[0] if self._shuffle_order else None
        return None

    def _prev_shuffle_index(self) -> int | None:
        if not self._shuffle_order:
            return None
        try:
            pos = self._shuffle_order.index(self._queue_index)
        except ValueError:
            return None
        if pos > 0:
            return self._shuffle_order[pos - 1]
        if self._repeat == RepeatMode.ALL:
            return self._shuffle_order[-1]
        return None

    def _on_about_to_finish(self) -> None:
        """GStreamer thread — next URI already set via _queue_gapless_next."""
        pass

    def _on_gapless_transition(self) -> None:
        """Called when GStreamer completes a gapless transition to the next stream."""
        idx = self._next_index()
        if idx is not None:
            self._queue_index = idx
            self.track_changed.emit(self.current_track)
            self.queue_changed.emit()
            self._queue_gapless_next()

    def _on_track_finished(self) -> None:
        """Called on EOS without gapless successor."""
        idx = self._next_index()
        if idx is not None:
            self._queue_index = idx
            self._start_current()

    @Slot(PlaybackState)
    def _on_state_changed_internal(self, state: PlaybackState) -> None:
        if state == PlaybackState.PLAYING:
            self._pos_timer.start()
        else:
            self._pos_timer.stop()

    @Slot()
    def _emit_position(self) -> None:
        dur = self._engine.get_duration_ms()
        # Fallback: for streaming URLs (YouTube), GStreamer may not know the
        # duration. Use the track's metadata duration if available.
        if dur <= 0:
            track = self.current_track
            if track and track.duration_ms > 0:
                dur = track.duration_ms
        else:
            # Lazy duration backfill: if the track has no duration in the DB
            # (e.g. video scanned without duration discovery), persist what
            # GStreamer discovered during playback.
            track = self.current_track
            if track and track.duration_ms == 0 and dur > 0 and track.id > 0 and self._db is not None:
                track.duration_ms = dur
                self._db.update_track_duration(track.id, dur)
        self.position_updated.emit(self._engine.get_position_ms(), dur)

    def _on_spectrum_message(self, _bus, message: object) -> None:
        """Handle spectrum element bus messages from GStreamer thread."""
        struct = message.get_structure()
        if struct is None or struct.get_name() != "spectrum":
            return
        # GstValueList can't be accessed via get_value(); use get_list().
        ok, val_array = struct.get_list("magnitude")
        if not ok or val_array is None:
            return
        threshold = -80.0
        normalized = []
        for i in range(val_array.n_values):
            m = float(val_array.get_nth(i))
            normalized.append(max(0.0, min(1.0, (m - threshold) / -threshold)))
        self.spectrum_updated.emit(normalized)

    def shutdown(self) -> None:
        self._pos_timer.stop()
        self._engine.shutdown()
