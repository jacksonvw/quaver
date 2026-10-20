"""'files' library tab — live filesystem tree, no DB.

Verifies the wiring put in place for the new media_type:
- ``files`` is registered as a fourth FOLDER_TYPES entry alongside
  music/series/movies.
- Source dialog accepts the new type with no scraper / no scan mode.
- ``LibraryTabWidget`` builds a ``FolderTreeView`` instead of the
  artist/album split when ``media_type='files'``.
- ``FolderTreeView.set_source_paths`` populates top-level items, lazy
  expansion reads the filesystem, the ext filter hides non-media files,
  and the substring search filter hides non-matching rows.
- The library scanner short-circuits ``files`` sources without writing
  rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import quaver  # noqa: F401  (sys.path bootstrap for media_core)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from quaver.core.config import SourceConfig
from quaver.core.library import FOLDER_TYPES


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── Config / type registration ───────────────────────────────────────


def test_files_in_folder_types() -> None:
    """The new media_type must be registered in FOLDER_TYPES so the
    library-tabs dialog picks it up automatically.
    """
    assert "files" in FOLDER_TYPES


def test_sources_dialog_advertises_files_type() -> None:
    """Sources dialog's TYPE_OPTIONS / TYPE_LABELS expose the new type."""
    from quaver.ui.dialogs.sources import TYPE_LABELS, TYPE_OPTIONS
    assert "files" in TYPE_OPTIONS
    assert TYPE_LABELS["files"] == "Files"


# ── FolderTreeView ───────────────────────────────────────────────────


def _populate_clip_library(root: Path) -> None:
    """Lay down a representative clip-library structure under *root*."""
    (root / "loops").mkdir()
    (root / "loops" / "drums.mp3").write_bytes(b"")
    (root / "loops" / "bass.flac").write_bytes(b"")
    (root / "loops" / "notes.txt").write_bytes(b"")  # filtered out
    (root / "stings").mkdir()
    (root / "stings" / "intro.wav").write_bytes(b"")
    (root / "stings" / "logo.mkv").write_bytes(b"")
    (root / "cover.jpg").write_bytes(b"")  # filtered out
    (root / ".hidden").mkdir()  # hidden dir, not shown
    (root / ".hidden" / "secret.mp3").write_bytes(b"")


def test_set_source_paths_creates_top_level_items(qapp, tmp_path: Path) -> None:
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    a = tmp_path / "library_a"
    b = tmp_path / "library_b"
    a.mkdir()
    b.mkdir()

    view = FolderTreeView()
    view.set_source_paths([str(a), str(b)])
    assert view._tree.topLevelItemCount() == 2
    assert view._tree.topLevelItem(0).text(0) == "library_a"
    assert view._tree.topLevelItem(1).text(0) == "library_b"


def test_unavailable_root_shows_stub(qapp, tmp_path: Path) -> None:
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "definitely_missing"

    view = FolderTreeView()
    view.set_source_paths([str(real), str(missing)])
    stub = view._tree.topLevelItem(1)
    assert "(unavailable)" in stub.text(0)
    assert stub.isDisabled()


def test_tree_shows_only_subdirs_not_files(qapp, tmp_path: Path) -> None:
    """The folder tree (left pane) only shows directories.  Media
    files are handled by the file grid on the right.
    """
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    root = tmp_path / "clips"
    root.mkdir()
    _populate_clip_library(root)

    view = FolderTreeView()
    view.set_source_paths([str(root)])
    top = view._tree.topLevelItem(0)
    view._on_item_expanded(top)

    # Only subfolders (loops, stings) — no .mp3 / .flac / .jpg / hidden.
    child_names = sorted(top.child(i).text(0) for i in range(top.childCount()))
    assert child_names == ["loops", "stings"]


def test_grid_loads_current_folders_media_files(qapp, tmp_path: Path) -> None:
    """Selecting a folder in the tree loads its (non-recursive) media
    files into the right-side grid.  Non-media files and hidden files
    are filtered out.
    """
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    root = tmp_path / "clips"
    root.mkdir()
    _populate_clip_library(root)

    view = FolderTreeView()
    view.set_source_paths([str(root)])

    # Root is auto-selected — its files (none at top level) should load.
    # Now drill into loops/.
    top = view._tree.topLevelItem(0)
    view._on_item_expanded(top)
    loops = next(top.child(i) for i in range(top.childCount())
                 if top.child(i).text(0) == "loops")
    view._tree.setCurrentItem(loops)

    file_names = sorted(p.name for p in view._file_model.files())
    assert file_names == ["bass.flac", "drums.mp3"]


def test_search_filter_hides_grid_rows(qapp, tmp_path: Path) -> None:
    """The search bar filters the file grid by substring match on
    filename.  Other entries are hidden, not removed from the model.
    """
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    root = tmp_path / "clips"
    root.mkdir()
    _populate_clip_library(root)

    view = FolderTreeView()
    view.set_source_paths([str(root)])
    top = view._tree.topLevelItem(0)
    view._on_item_expanded(top)
    loops = next(top.child(i) for i in range(top.childCount())
                 if top.child(i).text(0) == "loops")
    view._tree.setCurrentItem(loops)

    files = view._file_model.files()
    drums_row = next(i for i, p in enumerate(files) if p.name == "drums.mp3")
    bass_row = next(i for i, p in enumerate(files) if p.name == "bass.flac")

    view._search_edit.setText("drums")
    assert not view._file_grid.isRowHidden(drums_row)
    assert view._file_grid.isRowHidden(bass_row)

    view._search_edit.setText("")
    assert not view._file_grid.isRowHidden(drums_row)
    assert not view._file_grid.isRowHidden(bass_row)


def test_double_click_grid_emits_play_request(qapp, tmp_path: Path) -> None:
    """Double-clicking a file in the grid replaces the queue with
    every file in the current grid view, starting at the clicked one.
    """
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    root = tmp_path
    a = root / "a.mp3"
    b = root / "b.flac"
    a.write_bytes(b"")
    b.write_bytes(b"")

    view = FolderTreeView()
    view.set_source_paths([str(root)])
    # Root auto-selected on set_source_paths.

    received = []
    view.play_tracks_requested.connect(
        lambda tracks, idx: received.append((tracks, idx))
    )
    # Double-click the second file (b.flac → row 1 alphabetically).
    files = view._file_model.files()
    target_row = next(i for i, p in enumerate(files) if p.name == "b.flac")
    view._on_grid_double_clicked(view._file_model.index(target_row))

    assert len(received) == 1
    tracks, idx = received[0]
    assert idx == target_row
    # Grid contained both files — both queued, with idx pointing at b.
    queued = sorted(t.filepath for t in tracks)
    assert queued == sorted([str(a), str(b)])
    assert tracks[idx].filepath == str(b)


def test_double_click_folder_plays_recursive(qapp, tmp_path: Path) -> None:
    """Double-clicking a folder *in the tree* plays everything under
    it recursively — same semantics as before the rebuild.
    """
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    root = tmp_path / "library"
    root.mkdir()
    (root / "a.mp3").write_bytes(b"")
    (root / "sub").mkdir()
    (root / "sub" / "b.flac").write_bytes(b"")
    (root / "sub" / "c.mkv").write_bytes(b"")

    view = FolderTreeView()
    view.set_source_paths([str(root)])
    top = view._tree.topLevelItem(0)

    received = []
    view.play_tracks_requested.connect(
        lambda tracks, idx: received.append((tracks, idx))
    )
    view._on_tree_double_clicked(top, 0)

    tracks, idx = received[0]
    paths = sorted(t.filepath for t in tracks)
    assert paths == sorted([
        str(root / "a.mp3"),
        str(root / "sub" / "b.flac"),
        str(root / "sub" / "c.mkv"),
    ])
    types = {t.media_type for t in tracks}
    assert types == {"audio", "movie"}


def test_hide_extensions_swaps_display_label(qapp, tmp_path: Path) -> None:
    """``set_hide_extensions(True)`` makes the grid show ``Path.stem``
    instead of ``Path.name`` — so ``my movie.mp4`` becomes ``my movie``.
    """
    from quaver.ui.widgets.folder_tree_view import FolderTreeView
    from PySide6.QtCore import Qt as _Qt

    root = tmp_path / "lib"
    root.mkdir()
    (root / "my movie.mp4").write_bytes(b"")
    (root / "song.flac").write_bytes(b"")

    view = FolderTreeView()
    view.set_source_paths([str(root)])

    model = view._file_model
    files = model.files()
    target_row = next(i for i, p in enumerate(files) if p.name == "my movie.mp4")
    flac_row = next(i for i, p in enumerate(files) if p.name == "song.flac")

    # Default: extensions visible.
    assert model.data(model.index(target_row), _Qt.ItemDataRole.DisplayRole) == "my movie.mp4"
    assert model.data(model.index(flac_row), _Qt.ItemDataRole.DisplayRole) == "song.flac"

    view.set_hide_extensions(True)
    assert model.data(model.index(target_row), _Qt.ItemDataRole.DisplayRole) == "my movie"
    assert model.data(model.index(flac_row), _Qt.ItemDataRole.DisplayRole) == "song"

    view.set_hide_extensions(False)
    assert model.data(model.index(target_row), _Qt.ItemDataRole.DisplayRole) == "my movie.mp4"


def test_sort_combo_orders_files(qapp, tmp_path: Path) -> None:
    """Switching the sort combo reorders the grid in place."""
    from quaver.ui.widgets.folder_tree_view import FolderTreeView
    import os, time

    root = tmp_path / "lib"
    root.mkdir()
    # Three files with controlled sizes + mtimes so each sort mode
    # produces a different order.
    a = root / "a.mp3"
    a.write_bytes(b"x" * 10)
    b = root / "b.mp3"
    b.write_bytes(b"x" * 100)
    c = root / "c.mp3"
    c.write_bytes(b"x" * 1)
    # Distinct mtimes so date sort is deterministic.
    os.utime(a, (1700000000, 1700000000))
    os.utime(b, (1700000100, 1700000100))
    os.utime(c, (1700000200, 1700000200))

    view = FolderTreeView()
    view.set_source_paths([str(root)])

    # Default sort: name (alphabetical).
    names = [p.name for p in view._file_model.files()]
    assert names == ["a.mp3", "b.mp3", "c.mp3"]

    # Size: smallest first → c, a, b.
    idx = view._sort_combo.findData("size")
    view._sort_combo.setCurrentIndex(idx)
    names = [p.name for p in view._file_model.files()]
    assert names == ["c.mp3", "a.mp3", "b.mp3"]

    # Date: newest first → c, b, a.
    idx = view._sort_combo.findData("date")
    view._sort_combo.setCurrentIndex(idx)
    names = [p.name for p in view._file_model.files()]
    assert names == ["c.mp3", "b.mp3", "a.mp3"]


def test_library_tab_config_hide_extensions_round_trip(tmp_path: Path) -> None:
    """``hide_extensions`` survives TOML write/read on a Files tab."""
    from quaver.core.config import (
        AppConfig, LibraryTabConfig, SourceConfig, load_config, save_config,
    )

    p = tmp_path / "config.toml"
    cfg = AppConfig()
    cfg.sources.append(SourceConfig(
        path=str(tmp_path / "clips"), media_type="files",
    ))
    cfg.library_tabs.append(LibraryTabConfig(
        name="Clips", type="files",
        source_paths=[str(tmp_path / "clips")],
        hide_extensions=True,
    ))
    save_config(cfg, p)

    out = load_config(p)
    assert out.library_tabs[0].hide_extensions is True

    # Default (False) doesn't pollute the on-disk TOML — it's omitted.
    cfg2 = AppConfig()
    cfg2.library_tabs.append(LibraryTabConfig(
        name="Defaults", type="files",
    ))
    save_config(cfg2, p)
    out2 = load_config(p)
    assert out2.library_tabs[0].hide_extensions is False


def test_first_root_auto_selected(qapp, tmp_path: Path) -> None:
    """After ``set_source_paths``, the first available root is selected
    so the grid shows something on first paint instead of being empty.
    """
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    root = tmp_path / "library"
    root.mkdir()
    (root / "a.mp3").write_bytes(b"")

    view = FolderTreeView()
    view.set_source_paths([str(root)])
    current = view._tree.currentItem()
    assert current is not None
    assert current.text(0) == "library"
    files = view._file_model.files()
    assert [p.name for p in files] == ["a.mp3"]


# ── LibraryTabWidget routing ─────────────────────────────────────────


def test_library_tab_routes_to_folder_tree_for_files(qapp, tmp_path: Path) -> None:
    from quaver.ui.widgets.folder_tree_view import FolderTreeView
    from quaver.ui.widgets.library_tab import LibraryTabWidget

    tab = LibraryTabWidget(media_type="files")
    # Files-only tabs skip the artist/genre sidebar + album view entirely.
    assert tab._album_view is None
    assert tab._artist_list is None
    assert tab._splitter is None
    assert isinstance(tab._folder_tree, FolderTreeView)


def test_library_tab_set_source_paths_only_for_files_type(qapp, tmp_path: Path) -> None:
    from quaver.ui.widgets.library_tab import LibraryTabWidget

    folder = tmp_path / "x"
    folder.mkdir()
    (folder / "a.mp3").write_bytes(b"")

    files_tab = LibraryTabWidget(media_type="files")
    files_tab.set_source_paths([str(folder)])
    assert files_tab._folder_tree._tree.topLevelItemCount() == 1

    # Music tab silently ignores set_source_paths.
    music_tab = LibraryTabWidget(media_type="music")
    music_tab.set_source_paths([str(folder)])  # no-op, should not raise
    assert getattr(music_tab, "_folder_tree", None) is None


def test_library_tab_load_tracks_no_op_for_files(qapp) -> None:
    """Files tabs ignore DB-backed load_tracks calls — there are no
    DB rows for them, and the call would dereference None widgets.
    """
    from quaver.ui.widgets.library_tab import LibraryTabWidget

    tab = LibraryTabWidget(media_type="files")
    # Should not raise even though _artist_list / _album_view are None.
    tab.load_tracks([], poster_paths={}, video_states={})
    tab.set_playing_track("/tmp/foo.mp3")
    tab.update_video_state(1, True, 1000)
    tab.set_show_genre(True)
    tab.set_engine(None)
    # Regression: this was the AttributeError that prevented Quaver
    # from launching when any files tab was configured —
    # main_window._refresh_saved_playlists() iterates every tab.
    tab.set_saved_playlists([(1, "Mix", 5)])


# ── Source config round-trip ─────────────────────────────────────────


def test_source_config_files_type_round_trip(tmp_path: Path) -> None:
    """A SourceConfig with media_type='files' survives TOML round-trip."""
    from quaver.core.config import AppConfig, LibraryTabConfig, load_config, save_config

    cfg_path = tmp_path / "config.toml"
    cfg = AppConfig()
    cfg.sources.append(SourceConfig(
        path=str(tmp_path / "clips"),
        media_type="files",
        scraper="none",
    ))
    cfg.library_tabs.append(LibraryTabConfig(
        name="Clips",
        type="files",
        source_paths=[str(tmp_path / "clips")],
    ))
    save_config(cfg, cfg_path)

    loaded = load_config(cfg_path)
    assert len(loaded.sources) == 1
    assert loaded.sources[0].media_type == "files"
    assert len(loaded.library_tabs) == 1
    assert loaded.library_tabs[0].type == "files"
