"""Minimal Interface mode (Ctrl+H) — toggle behaviour + label switching.

Doesn't construct the full MainWindow (that requires GStreamer); tests
the toggle helpers in isolation by patching the bits MainWindow's
``_panel_extra_actions`` and ``_toggle_minimal_ui`` rely on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import minim  # noqa: F401  (sys.path bootstrap)

pytest.importorskip("PySide6")

from minim.ui.main_window import MainWindow


def test_panel_extra_actions_label_flips() -> None:
    """The Minimal-Interface label tracks ``_minimal_ui`` and the
    panel right-click also exposes Preferences (the only reliable
    entry point — the QMainWindow context menu rarely fires because
    the panel/queue/transport eat every right-click).
    """
    stub = SimpleNamespace(
        _minimal_ui=False,
        _toggle_minimal_ui=lambda: None,
        _show_preferences=lambda: None,
    )
    items = MainWindow._panel_extra_actions(stub)
    labels = [label for label, _ in items]
    # First entry is the minimal-mode toggle, second is Preferences.
    assert any("Minimal Interface" in lbl and "Exit" not in lbl for lbl in labels)
    assert any("Preferences" in lbl for lbl in labels)

    stub._minimal_ui = True
    items = MainWindow._panel_extra_actions(stub)
    labels = [label for label, _ in items]
    assert any(lbl.startswith("Exit Minimal Interface") for lbl in labels)
    assert any("Preferences" in lbl for lbl in labels)


def test_toggle_remembers_queue_visibility() -> None:
    """Entering minimal mode records the queue's prior visibility, and
    exiting restores it (so users with the queue hidden don't have it
    forced visible after a Ctrl+H round-trip).
    """
    # Track calls.
    queue_visible_state = [True]
    transport_visible_state = [True]
    queue_checked_state = [True]

    class _StubQueue:
        def isVisible(self):
            return queue_visible_state[0]
        def setVisible(self, v):
            queue_visible_state[0] = v

    class _StubTransport:
        def setVisible(self, v):
            transport_visible_state[0] = v
        def set_queue_checked(self, v):
            queue_checked_state[0] = v

    stub = SimpleNamespace(
        _minimal_ui=False,
        _pre_minimal_queue_visible=True,
        _fullscreen_overlay=None,
        _queue=_StubQueue(),
        _transport=_StubTransport(),
    )

    # Start with queue hidden.
    queue_visible_state[0] = False

    # Enter minimal — should remember queue was hidden.
    MainWindow._toggle_minimal_ui(stub)
    assert stub._minimal_ui is True
    assert stub._pre_minimal_queue_visible is False
    assert queue_visible_state[0] is False  # already hidden, stays so
    assert transport_visible_state[0] is False

    # Exit minimal — queue should stay hidden (matching pre-minimal state).
    MainWindow._toggle_minimal_ui(stub)
    assert stub._minimal_ui is False
    assert queue_visible_state[0] is False
    assert transport_visible_state[0] is True
    # Transport's queue checkbox tracks the queue's restored state.
    assert queue_checked_state[0] is False


def test_toggle_no_op_during_fullscreen() -> None:
    """While the FullscreenOverlay owns the panel/transport, the
    minimal-mode toggle is a no-op so we don't corrupt overlay state.
    """
    stub = SimpleNamespace(
        _minimal_ui=False,
        _pre_minimal_queue_visible=True,
        _fullscreen_overlay=object(),  # truthy = currently fullscreen
        _queue=None,
        _transport=None,
    )
    MainWindow._toggle_minimal_ui(stub)
    # Flag unchanged, no widget calls (would have AttributeError'd).
    assert stub._minimal_ui is False
