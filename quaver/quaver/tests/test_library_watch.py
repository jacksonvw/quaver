"""A source whose watch can't start leaves the other sources watched."""

import errno

from watchdog.observers.api import BaseObserver, EventEmitter

from quaver.core.library import LibraryDB
from quaver.services import library_service


class _Emitter(EventEmitter):
    """Stands in for the inotify emitter.  The "full" folder fails to start
    the way inotify does once the per-user instance limit is reached."""

    def on_thread_start(self) -> None:
        if self.watch.path.endswith("full"):
            raise OSError(errno.EMFILE, "inotify instance limit reached")

    def queue_events(self, timeout: float) -> None:
        self.stopped_event.wait(timeout)


class _Observer(BaseObserver):
    def __init__(self) -> None:
        super().__init__(_Emitter)


def test_watch_failure_skips_only_that_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(library_service, "Observer", _Observer)
    db = LibraryDB(tmp_path / "test.db")
    for name in ("full", "music"):
        (tmp_path / name).mkdir()
        db.add_source(str(tmp_path / name))
    service = library_service.LibraryService(db)
    service.start_watching()
    try:
        assert service._observer.is_alive()
        assert [e.watch.path for e in service._observer.emitters] == [str(tmp_path / "music")]
    finally:
        service.stop_watching()
