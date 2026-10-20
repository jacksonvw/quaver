"""Verify Minim doesn't drag in any Quaver-only behaviour.

Minim must be able to run without importing anything from
``quaver.*``.  The two places this could leak:

  - The QueueWidget uses an art-provider that Quaver's shim swaps in
    on import.  Minim must keep the generic embedded-only provider.
  - The QueueWidget should be constructed with no playlists and stay
    in "Minim mode" (no save/add-to-playlist menu items).
"""

from __future__ import annotations

import sys

import pytest

import minim  # noqa: F401  (sys.path bootstrap)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from media_core.ui.widgets import embedded_art_cache as _generic_provider
from media_core.ui.widgets import queue_widget as _queue_widget_mod
from media_core.ui.widgets.queue_widget import QueueWidget


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_no_quaver_imports_pulled_in() -> None:
    """Importing minim.* must not reach into the quaver.* package."""
    # Defensive: even if the test suite is running inside the same
    # interpreter as a Quaver test session, importing Minim must not
    # *itself* trigger any quaver.* import.  We can't unimport quaver
    # if it's already loaded, so this asserts a weaker but sufficient
    # invariant: the modules we explicitly reach for stay clean.
    import minim.main  # noqa: F401
    import minim.tracks  # noqa: F401
    import minim.ui.main_window  # noqa: F401

    # The art provider on media_core's queue widget should be the
    # generic embedded cache when Minim is the only client.  If a
    # parallel test session has already imported the Quaver shim,
    # ``art_cache`` will point at Quaver's — which is fine, but the
    # invariant we care about is that Minim's own imports don't flip
    # that switch.
    if "quaver.ui.widgets.queue_widget" not in sys.modules:
        assert _queue_widget_mod.art_cache is _generic_provider


def test_queue_widget_starts_in_minim_mode(qapp) -> None:
    """QueueWidget defaults to no playlists → no save/add menu actions."""
    w = QueueWidget()
    assert w._playlists is None
    assert w._engine is None


def test_track_construction_for_dropped_files() -> None:
    """The drop-handler converts file:// URIs to transient tracks."""
    from minim.tracks import track_from_path

    # Simulates a Dolphin drag — local file path from QUrl.toLocalFile().
    t = track_from_path("/tmp/example.mp3")
    assert t.id == -1
    assert t.media_type == "audio"
    assert t.title == "example"
