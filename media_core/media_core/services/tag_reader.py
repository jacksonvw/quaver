"""Fill tracks' metadata from their files' tags, off the UI thread.

Tracks built straight from file paths (Minim's command line, drops and
File → Open) start with just a filename title, so building them stays
instant even for a folder of thousands of files.  TagReader then reads
the real tags on a worker thread and fills the tracks in place.  The
track about to play is read on the spot instead, so nothing that shows
it — Now Playing, the transport, MPRIS — ever sees it untagged.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)

from media_core.core.network import is_network_path
from media_core.core.tagger import TrackTags, apply_tags, read_tags
from media_core.core.track import Track

# Background results are announced at most this often, so a large
# folder repaints the queue a few times a second, not once per file.
_NOTIFY_INTERVAL_MS = 150


class _Batch:
    """Cancellation flag shared by one queue() call's worker and results."""

    cancelled = False


class _Bridge(QObject):
    """Carries results from the worker thread to the reader's thread."""

    read = Signal(object, str, object)  # _Batch, filepath, TrackTags | None


class _ReadTask(QRunnable):
    """Reads one batch of files' tags, in order, until cancelled."""

    def __init__(self, bridge: _Bridge, batch: _Batch, paths: list[str]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._bridge = bridge
        self._batch = batch
        self._paths = paths

    def run(self) -> None:
        for path in self._paths:
            if self._batch.cancelled:
                return
            tags = read_tags(path)
            try:
                self._bridge.read.emit(self._batch, path, tags)
            except RuntimeError:  # the reader is already gone
                return


class TagReader(QObject):
    """Reads tags for queued tracks in the background, in queue order.

    Connect :meth:`ensure` to ``PlayerService.track_changed`` before
    anything that displays the track, and :attr:`tracks_updated` to
    whatever repaints tracks, such as ``QueueWidget.refresh_tracks``.
    """

    tracks_updated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # filepath → tracks still waiting for that file's tags.
        self._pending: dict[str, list[Track]] = {}
        self._batch = _Batch()
        # Created before the bridge, so Qt destroys it first: the pool
        # waits for a running read before the bridge it emits on goes.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # one file at a time, in order
        self._bridge = _Bridge(self)
        self._bridge.read.connect(self._on_read)
        self._notify = QTimer(self)
        self._notify.setSingleShot(True)
        self._notify.setInterval(_NOTIFY_INTERVAL_MS)
        self._notify.timeout.connect(self.tracks_updated)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def queue(self, tracks: list[Track]) -> None:
        """Read *tracks*' tags in the background, in the order given.

        Files on network mounts (KIO FUSE / GVFS) wait until they play,
        as queue art does, so a big remote folder doesn't hammer the NAS.
        """
        paths: list[str] = []
        for track in tracks:
            path = track.filepath
            if "://" in path:  # streams have no local tags
                continue
            waiting = self._pending.setdefault(path, [])
            if not waiting and not is_network_path(path):
                paths.append(path)
            waiting.append(track)
        if paths:
            self._pool.start(_ReadTask(self._bridge, self._batch, paths))

    def cancel(self) -> None:
        """Forget every pending track and stop the background read."""
        self._batch.cancelled = True
        self._batch = _Batch()
        self._pending.clear()
        self._pool.clear()

    @Slot(object)
    def ensure(self, track: Track | None) -> None:
        """Read *track*'s tags now if they're still pending."""
        if track is None:
            return
        waiting = self._pending.pop(track.filepath, None)
        if waiting is not None:
            self._apply(waiting, read_tags(track.filepath))

    @Slot()
    def shutdown(self) -> None:
        """Stop reading and wait for the worker to finish its file."""
        self.cancel()
        self._pool.waitForDone()

    @Slot(object, str, object)
    def _on_read(self, batch: _Batch, path: str, tags: TrackTags | None) -> None:
        if batch.cancelled:
            return
        waiting = self._pending.pop(path, None)
        if waiting is not None:
            self._apply(waiting, tags)

    def _apply(self, tracks: list[Track], tags: TrackTags | None) -> None:
        changed = False
        for track in tracks:
            changed = apply_tags(track, tags) or changed
        if changed and not self._notify.isActive():
            self._notify.start()
