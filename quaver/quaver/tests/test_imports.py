"""Smoke test: every module imports without error."""

import importlib

import pytest

# Bootstrap media_core onto sys.path (Quaver's __init__.py does this on
# package import, but this test runs before any quaver.* module is imported).
import quaver  # noqa: F401  (side-effect: adds media_core to sys.path)

MODULES = [
    # media_core (Phase 1 extracted)
    "media_core.core.blur",
    "media_core.core.freedesktop_thumbs",
    "media_core.core.media_exts",
    "media_core.core.paths",
    "media_core.core.track",
    "media_core.core.embedded_art",
    "media_core.core.tagger",
    "media_core.core.network",
    "media_core.core.libass_renderer",
    "media_core.core.replaygain",
    "media_core.core.audio",
    "media_core.services.player_service",
    "media_core.services.mpris2",
    "media_core.core.config",
    "media_core.ui.widgets.icons",
    "media_core.ui.widgets.delegates",
    "media_core.ui.widgets.spectrum",
    "media_core.ui.widgets.transport",
    "media_core.ui.widgets.eq_popup",
    "media_core.ui.widgets.dsp_popup",
    "media_core.ui.widgets.subtitle_overlay",
    "media_core.ui.widgets.fullscreen_overlay",
    "media_core.ui.widgets.embedded_art_cache",
    "media_core.ui.widgets.queue_widget",
    "media_core.ui.widgets.video_surface",
    "media_core.ui.panels.nowplaying_panel",
    # quaver
    "quaver.core.audio",
    "quaver.core.config",
    "quaver.core.libass_renderer",
    "quaver.core.library",
    "quaver.core.musicbrainz",
    "quaver.core.network",
    "quaver.core.nfo",
    "quaver.core.paths",
    "quaver.core.podcast",
    "quaver.core.replaygain",
    "quaver.core.tagger",
    "quaver.core.tmdb",
    "quaver.core.tvmaze",
    "quaver.core.video_parser",
    "quaver.core.youtube",
    "quaver.services.autodj_service",
    "quaver.services.library_service",
    "quaver.services.metadata_service",
    "quaver.services.mpris2",
    "quaver.services.player_service",
    "quaver.services.podcast_service",
    "quaver.services.youtube_service",
    "quaver.ui.icons",
    "quaver.ui.main_window",
    "quaver.ui.dialogs.library_tabs_dialog",
    "quaver.ui.dialogs.musicbrainz_dialog",
    "quaver.ui.dialogs.podcast_search",
    "quaver.ui.dialogs.podcast_settings",
    "quaver.ui.dialogs.preferences",
    "quaver.ui.dialogs.sources",
    "quaver.ui.dialogs.tag_editor",
    "quaver.ui.panels.nowplaying_panel",
    "quaver.ui.panels.playlists_panel",
    "quaver.ui.panels.podcast_panel",
    "quaver.ui.panels.youtube_panel",
    "quaver.ui.widgets.album_view",
    "quaver.ui.widgets.art_cache",
    "quaver.ui.widgets.delegates",
    "quaver.ui.widgets.folder_tree_view",
    "quaver.ui.widgets.fullscreen_overlay",
    "quaver.ui.widgets.library_tab",
    "quaver.ui.widgets.queue_widget",
    "quaver.ui.widgets.subtitle_overlay",
    "quaver.ui.widgets.transport",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    importlib.import_module(name)
