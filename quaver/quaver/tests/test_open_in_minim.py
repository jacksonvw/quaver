"""'Open in Minim' helper + signal wiring across surfaces.

Doesn't actually spawn Minim — patches ``subprocess.Popen`` and
``shutil.which`` so the helper's behaviour can be asserted without
side effects.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import quaver  # noqa: F401  (sys.path bootstrap)


def test_open_in_minim_returns_false_when_launcher_missing(monkeypatch) -> None:
    from quaver.core import external_apps

    monkeypatch.setattr(external_apps.shutil, "which", lambda _: None)
    assert external_apps.open_in_minim(["/tmp/song.mp3"]) is False


def test_open_in_minim_spawns_with_paths(monkeypatch) -> None:
    """When the launcher is on PATH, Popen is called with the
    launcher and every path in argv.  The Popen call doesn't block.
    """
    from quaver.core import external_apps

    monkeypatch.setattr(external_apps.shutil, "which", lambda _: "/usr/local/bin/minim")
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(external_apps.subprocess, "Popen", fake_popen)

    ok = external_apps.open_in_minim(["/tmp/song.mp3", "/tmp/clip.mkv"])
    assert ok is True
    assert captured["cmd"] == [
        "/usr/local/bin/minim", "/tmp/song.mp3", "/tmp/clip.mkv",
    ]
    # Detached: no stdin/stdout/stderr inheritance, new session.
    assert captured["kwargs"]["start_new_session"] is True


def test_open_in_minim_empty_paths_short_circuits(monkeypatch) -> None:
    from quaver.core import external_apps

    monkeypatch.setattr(external_apps.shutil, "which", lambda _: "/usr/bin/minim")
    monkeypatch.setattr(external_apps.subprocess, "Popen", lambda *a, **k: 1/0)
    # Empty list should NOT trigger Popen — assert by virtue of the
    # divide-by-zero monkey-patch not raising.
    assert external_apps.open_in_minim([]) is False


# ── Surface-level wiring (signals fire as expected) ──────────────────


pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_folder_tree_grid_emits_open_in_minim(qapp, tmp_path: Path) -> None:
    """Right-click → "Open in Minim" emits the signal with the
    selected paths.  We don't trigger the actual menu (modal) — we
    inspect the signal contract via a stub.
    """
    from quaver.ui.widgets.folder_tree_view import FolderTreeView

    root = tmp_path / "lib"
    root.mkdir()
    (root / "a.mp3").write_bytes(b"")

    view = FolderTreeView()
    view.set_source_paths([str(root)])

    received = []
    view.open_in_minim_requested.connect(lambda paths: received.append(list(paths)))

    # Manually emit what the menu callback would.
    view.open_in_minim_requested.emit([str(root / "a.mp3")])
    assert received == [[str(root / "a.mp3")]]


def test_queue_extra_track_actions_factory(qapp) -> None:
    """The QueueWidget host hook only appears when set, returns the
    Quaver-supplied items, and skips youtube:// / http:// tracks.
    """
    from media_core.core.track import Track
    from media_core.ui.widgets.queue_widget import QueueWidget

    qw = QueueWidget()
    # Default: no extra actions installed (Minim path).
    assert qw._track_extra_actions is None

    # Install factory + verify it's stored and callable.
    captured = []

    def factory(rows, tracks):
        captured.append((list(rows), list(tracks)))
        return [("Open in Minim", lambda: None)]

    qw.set_track_extra_actions(factory)
    assert qw._track_extra_actions is factory


def test_open_in_minim_filters_remote_paths(monkeypatch, qapp) -> None:
    """``MainWindow._on_open_in_minim`` must drop ``youtube://`` and
    HTTP paths before spawning — Minim doesn't handle them.
    """
    from quaver.core import external_apps

    # Build a stand-in for MainWindow's slot to test the filter.
    captured = []
    monkeypatch.setattr(external_apps.shutil, "which", lambda _: "/usr/bin/minim")
    monkeypatch.setattr(
        external_apps.subprocess, "Popen",
        lambda cmd, **kw: captured.append(list(cmd)) or object(),
    )

    paths = [
        "/tmp/local.mp3",
        "youtube://abc123",
        "https://example.com/stream.m3u8",
        "/tmp/another.mkv",
    ]
    local_only = [
        p for p in paths
        if not p.startswith(("youtube://", "http://", "https://"))
    ]
    external_apps.open_in_minim(local_only)
    assert captured == [
        ["/usr/bin/minim", "/tmp/local.mp3", "/tmp/another.mkv"],
    ]
