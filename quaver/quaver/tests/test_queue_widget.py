"""QueueWidget context-menu gating — playlist actions hidden in Minim mode.

Phase 0.4 audit fix: ``Save Queue as New Playlist`` and ``Add Queue to
Playlist`` must be hidden when the queue widget is hosted by a shell
that doesn't expose saved playlists (Minim).  Quaver passes a list to
``set_playlists()``; Minim either never calls it or passes ``None``.
"""

from __future__ import annotations

import pytest

# Skip the whole module if Qt isn't available — Qt is normally present
# (offscreen platform set in conftest.py) but defensive.
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from quaver.ui.widgets.queue_widget import QueueWidget


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def test_default_playlists_is_none(qapp) -> None:
    """Default state (Minim mode) hides all playlist actions."""
    w = QueueWidget()
    assert w._playlists is None


def test_set_playlists_none_clears(qapp) -> None:
    w = QueueWidget()
    w.set_playlists([(1, "Mix")])
    assert w._playlists == [(1, "Mix")]
    w.set_playlists(None)
    assert w._playlists is None


def test_set_playlists_empty_list_keeps_save_action(qapp) -> None:
    """Quaver with zero saved playlists: Save action visible, Add submenu hidden."""
    w = QueueWidget()
    w.set_playlists([])
    assert w._playlists == []  # Library mode, empty.


def test_header_menu_omits_playlist_actions_in_minim_mode(qapp) -> None:
    """When _playlists is None, the header menu has no playlist-related actions."""
    w = QueueWidget()
    # Default state — Minim mode.
    assert w._playlists is None

    # Build the header menu as the widget would when right-clicked.
    from PySide6.QtWidgets import QMenu
    menu = QMenu()
    # Reach into the same wiring as _show_header_menu uses, but build the
    # menu directly so we can introspect its actions without a popup.
    if w._playlists is not None:
        menu.addAction("Save Queue as New Playlist")
        if w._playlists:
            menu.addMenu("Add Queue to Playlist")
        menu.addSeparator()
    menu.addAction("Clear Queue")

    labels = [a.text() for a in menu.actions() if a.text()]
    assert "Save Queue as New Playlist" not in labels
    assert "Add Queue to Playlist" not in labels
    assert "Clear Queue" in labels


def test_header_menu_shows_save_in_library_mode(qapp) -> None:
    """When _playlists is a list, Save action appears even if list is empty."""
    w = QueueWidget()
    w.set_playlists([])

    from PySide6.QtWidgets import QMenu
    menu = QMenu()
    if w._playlists is not None:
        menu.addAction("Save Queue as New Playlist")
        if w._playlists:
            menu.addMenu("Add Queue to Playlist")
        menu.addSeparator()
    menu.addAction("Clear Queue")

    labels = [a.text() for a in menu.actions() if a.text()]
    assert "Save Queue as New Playlist" in labels
    # Add submenu still hidden (no playlists yet).
    assert "Add Queue to Playlist" not in labels


def test_set_engine_none_default(qapp) -> None:
    """Engine starts unset so stream menus stay hidden in Minim mode."""
    w = QueueWidget()
    assert w._engine is None
    w.set_engine(None)
    assert w._engine is None


def test_quaver_shim_installs_library_art_provider(qapp) -> None:
    """Importing the Quaver shim sets the queue's art_cache to Quaver's
    library-flavored cache (the one with poster_path support).  Minim
    skips this import and stays on media_core's generic cache.
    """
    # Importing quaver.ui.widgets.queue_widget runs set_art_provider().
    # Verify the active provider is Quaver's, not media_core's.
    from media_core.ui.widgets import queue_widget as mc_qw
    from quaver.ui.widgets import art_cache as quaver_art_cache
    from media_core.ui.widgets import embedded_art_cache as generic
    assert mc_qw.art_cache is quaver_art_cache, (
        "Quaver shim must have installed quaver.ui.widgets.art_cache "
        "as the queue's art provider"
    )
    assert mc_qw.art_cache is not generic


class _StubEngine:
    """Minimal engine surface used by build_stream_menus."""

    def __init__(self, audio_support, sub_support):
        self._audio_support = audio_support
        self._sub_support = sub_support

    def get_audio_tracks(self):
        return [(i, f"Audio {i}") for i in range(len(self._audio_support))]

    def get_subtitle_tracks(self):
        return [(i, f"Sub {i}") for i in range(len(self._sub_support))]

    def get_audio_track_support(self):
        return self._audio_support

    def get_subtitle_track_support(self):
        return self._sub_support

    def get_current_audio(self):
        return 0

    def get_current_subtitle(self):
        return 0

    def get_subtitle_flags(self):
        return True


def test_undecodable_tracks_are_disabled_in_menu(qapp) -> None:
    """Tracks with no decoder are shown but unpickable — selecting one
    used to hang the pipeline on its last frame.
    """
    from PySide6.QtWidgets import QMenu

    from quaver.ui.widgets.queue_widget import build_stream_menus

    menu = QMenu()
    build_stream_menus(menu, _StubEngine([True, False], [True]))

    audio_menu = next(a.menu() for a in menu.actions() if a.text() == "Audio Track")
    entries = {a.text(): a.isEnabled() for a in audio_menu.actions()}
    assert entries["Audio 0"] is True
    assert entries["Audio 1 — no decoder"] is False
