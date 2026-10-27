"""VLC's subtitle controls, for whatever window shows an AudioEngine's video.

The Subtitle menu VLC puts on its right-click (Add Subtitle File…, and Sub
Track with Disable and every track), and what VLC's keys do: V and Alt+V
cycle the tracks, Shift+V turns them off and back on, G and H move them
50 ms earlier or later.  Each key returns the message VLC shows on screen.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QWidget

from media_core.core import subtitle_files

log = logging.getLogger(__name__)

SUBTITLE_FILTER = "Subtitle files (*.srt *.ass *.ssa *.vtt *.sub);;All files (*)"
DELAY_STEP_MS = 50  # VLC's G and H

is_subtitle_path = subtitle_files.is_subtitle_path


def add_subtitle_menu(menu: QMenu, engine, parent: QWidget | None = None,
                      notify: Callable[[str], None] | None = None) -> QMenu:
    """Append VLC's Subtitle submenu to *menu* and return it."""
    sub_menu = menu.addMenu("Subtitle")
    add = sub_menu.addAction("Add Subtitle File…")
    add.triggered.connect(lambda: choose_subtitle_files(engine, parent, notify))

    track_menu = sub_menu.addMenu("Sub Track")
    tracks = engine.get_subtitle_tracks()
    track_menu.setEnabled(bool(tracks))
    current = engine.get_current_subtitle()
    support = engine.get_subtitle_track_support()
    group = QActionGroup(track_menu)  # one checked at a time, as radio buttons

    disable = QAction("Disable", track_menu)
    disable.setCheckable(True)
    disable.setChecked(current < 0)
    disable.triggered.connect(lambda: engine.set_subtitle_enabled(False))
    group.addAction(disable)
    track_menu.addAction(disable)

    for idx, label in tracks:
        # Switching to a track with no decoder hangs the pipeline on its
        # last frame, so those are shown but can't be picked.
        decodable = support[idx] if idx < len(support) else True
        action = QAction(label if decodable else f"{label} — no decoder", track_menu)
        action.setCheckable(True)
        action.setEnabled(decodable)
        action.setChecked(idx == current)
        action.triggered.connect(lambda checked, i=idx: engine.set_current_subtitle(i))
        group.addAction(action)
        track_menu.addAction(action)
    return sub_menu


def choose_subtitle_files(engine, parent: QWidget | None = None,
                          notify: Callable[[str], None] | None = None) -> None:
    """VLC's Add Subtitle File…: pick files, starting in the video's folder."""
    dialog = QFileDialog(parent or QApplication.activeWindow(), "Open subtitles…")
    dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
    dialog.setNameFilters(SUBTITLE_FILTER.split(";;"))
    folder = playing_folder(engine)
    if folder is not None:
        # Not the constructor's directory argument: that drops a folder this
        # sandbox can't see (a NAS share, say), where setDirectoryUrl passes
        # it on to the portal's dialog as current_folder, and that dialog
        # can open it.
        dialog.setDirectoryUrl(QUrl.fromLocalFile(str(folder)))
    if not dialog.exec():
        return
    message = add_subtitle_paths(engine, dialog.selectedFiles())
    if message and notify is not None:
        notify(message)


def add_subtitle_paths(engine, paths: Iterable[str | Path]) -> str:
    """Add each of *paths* as a subtitle track and choose the first, as
    VLC does with files added or dropped.  Returns the message to show."""
    first = None
    failed: list[tuple[str, str]] = []
    for path in paths:
        try:
            index = engine.add_subtitle_file(path)
        except subtitle_files.SubtitleError as exc:
            log.warning("Couldn't read subtitles from %s: %s", path, exc)
            failed.append((Path(path).name, str(exc)))
            continue
        if first is None:
            first = index
    if first is not None:
        engine.set_current_subtitle(first)
        return track_message(engine, first)
    if failed:
        name, why = failed[0]
        return f"Couldn't read {name}: {why}"
    return ""


def track_message(engine, index: int) -> str:
    """VLC's message for a track change, e.g. "Subtitle track: English"."""
    label = dict(engine.get_subtitle_tracks()).get(index, "Disable")
    return f"Subtitle track: {label}"


def cycle(engine, step: int = 1) -> str:
    """V (step 1) or Alt+V (step -1)."""
    return track_message(engine, engine.cycle_subtitle(step))


def toggle(engine) -> str:
    """Shift+V."""
    return track_message(engine, engine.toggle_subtitles())


def shift_delay(engine, delta_ms: int) -> str:
    """G (earlier) or H (later)."""
    delay = engine.get_subtitle_delay() + delta_ms
    engine.set_subtitle_delay(delay)
    return f"Subtitle delay {delay} ms"


def playing_folder(engine) -> Path | None:
    """The folder of the file playing, where VLC opens its subtitle dialog."""
    path = engine.current_path
    if path is None:
        return None
    return _host_path(path).parent


def _host_path(path: Path) -> Path:
    """Where a file the desktop handed this Flatpak really is.

    A file the sandbox can't see (one on a NAS, say) arrives through the
    document portal as a stand-in under $XDG_RUNTIME_DIR/doc/<id>/; the
    portal says where it came from.  Any other path is returned as it is.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        parts = path.relative_to(Path(runtime) / "doc").parts
    except ValueError:
        return path
    if parts[:1] == ("by-app",):
        parts = parts[2:]
    if not parts:
        return path
    try:
        import dbus

        documents = dbus.Interface(
            dbus.SessionBus().get_object(
                "org.freedesktop.portal.Documents", "/org/freedesktop/portal/documents"),
            "org.freedesktop.portal.Documents",
        )
        found = documents.GetHostPaths([parts[0]])
        return Path(os.fsdecode(bytes(found[parts[0]]).rstrip(b"\0")))
    except Exception:
        log.debug("The document portal didn't say where %s is", path, exc_info=True)
        return path
