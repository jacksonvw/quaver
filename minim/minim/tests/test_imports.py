"""Smoke test: every Minim module imports without error."""

import importlib

import pytest

# Bootstrap media_core onto sys.path (mirrors Quaver's test pattern).
import minim  # noqa: F401  (side-effect)

MODULES = [
    # media_core (sanity check — Minim only ships if these import)
    "media_core.core.blur",
    "media_core.core.track",
    "media_core.core.audio",
    "media_core.services.player_service",
    "media_core.ui.widgets.queue_widget",
    "media_core.ui.widgets.transport",
    "media_core.ui.widgets.fullscreen_overlay",
    "media_core.ui.panels.nowplaying_panel",
    # minim
    "minim",
    "minim.tracks",
    "minim.main",
    "minim.core",
    "minim.core.config",
    "minim.ui",
    "minim.ui.main_window",
    "minim.ui.dialogs",
    "minim.ui.dialogs.preferences",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    importlib.import_module(name)
