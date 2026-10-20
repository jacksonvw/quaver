"""NowPlayingPanel constructor contract — Phase 2.11 decoupling.

The panel must be constructible without any host wiring (Minim case)
and with Quaver's load_config/save_config/poster_reader injected.  No
``quaver.*`` references should remain in the media_core implementation.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_panel_constructs_without_host_wiring(qapp) -> None:
    """Minim case: no load_config/save_config/poster_reader supplied."""
    from media_core.ui.panels.nowplaying_panel import NowPlayingPanel
    panel = NowPlayingPanel()
    # Defaults are wired and behave as no-ops / built-in defaults.
    assert callable(panel._load_config)
    assert callable(panel._save_config)
    assert callable(panel._poster_reader)
    # Defaults: no poster, no save side-effects.
    assert panel._poster_reader("/anything") is None
    panel.deleteLater()


def test_panel_constructs_with_quaver_wiring(qapp) -> None:
    """Quaver case: subclass auto-wires load_config/save_config/poster_reader."""
    from quaver.core.config import load_config, save_config
    from quaver.ui.panels.nowplaying_panel import NowPlayingPanel
    from quaver.ui.widgets.art_cache import _read_poster_file

    panel = NowPlayingPanel()
    # The Quaver subclass binds the host callables.
    assert panel._load_config is load_config
    assert panel._save_config is save_config
    assert panel._poster_reader is _read_poster_file
    panel.deleteLater()


def test_panel_default_has_no_extra_context_actions(qapp) -> None:
    """Without the kw-only injection, no host actions are appended."""
    from media_core.ui.panels.nowplaying_panel import NowPlayingPanel
    panel = NowPlayingPanel()
    assert panel._extra_context_actions is None
    panel.deleteLater()


def test_panel_accepts_extra_context_actions_factory(qapp) -> None:
    """When the host supplies a factory, the panel stores it for use
    when the context menu is built (called fresh each time).
    """
    from media_core.ui.panels.nowplaying_panel import NowPlayingPanel

    calls = {"count": 0}

    def factory():
        calls["count"] += 1
        return [("Test Action", lambda: None)]

    panel = NowPlayingPanel(extra_context_actions=factory)
    assert panel._extra_context_actions is factory
    # Factory is held but not yet invoked.
    assert calls["count"] == 0
    # Invoking it returns the host-supplied items.
    items = panel._extra_context_actions()
    assert calls["count"] == 1
    assert len(items) == 1
    assert items[0][0] == "Test Action"
    panel.deleteLater()


def test_media_core_panel_module_has_no_quaver_imports() -> None:
    """media_core.ui.panels.nowplaying_panel must not import quaver.*."""
    import inspect
    import media_core.ui.panels.nowplaying_panel as mp
    src = inspect.getsource(mp)
    # Allow string mentions in docstrings/comments (e.g. "Quaver wires…");
    # forbid actual imports.
    assert "from quaver" not in src
    assert "import quaver" not in src
