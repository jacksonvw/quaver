"""Minim — minimalist on-demand media player.

Entry point.  Parses argv, builds a transient queue from any paths
given, opens the main window.

Usage:
    minim                          # Empty player; drop files later.
    minim path/to/file.mp3         # Plays the file.
    minim album/                   # Queues every playable file in the
                                   #   directory and starts the first.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import traceback


def main() -> None:
    # Importing minim early makes the sys.path bootstrap in
    # minim/__init__.py prepend ``../media_core`` so the imports below
    # resolve from a source checkout.
    import minim  # noqa: F401  (side-effect)

    # Crash diagnostics — surface unhandled segfaults + unhandled
    # Python exceptions to the launcher's log file.  Without these,
    # Minim's nohup-detached process loses every traceback to
    # /dev/null and "briefly opens then closes" gives us nothing to
    # work with.
    faulthandler.enable()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("minim")
    log.info("starting; argv=%r; cwd=%s", sys.argv, os.getcwd())

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        traceback.print_exception(exc_type, exc_value, exc_tb)
        sys.__stderr__.flush()
        sys.__stdout__.flush()
    sys.excepthook = _excepthook

    from pathlib import Path

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from minim.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Minim")
    app.setOrganizationName("Minim")
    # The Wayland app_id; it has to match the installed minim.desktop, or in
    # a Flatpak, the desktop entry and icon named for Minim's app ID. Not
    # FLATPAK_ID, since "Open in Minim" runs Minim inside Quaver's Flatpak.
    app_id = "com.jacksonv.Minim" if "FLATPAK_ID" in os.environ else "minim"
    app.setDesktopFileName(app_id)

    icon_path = Path(__file__).parent / "resources" / "minim.svg"
    if icon_path.is_file():
        # A theme name lets KWin render the icon itself.  Without one, Qt sends
        # pixel buffers whose alpha KWin misreads, blackening anti-aliased edges.
        app.setWindowIcon(QIcon.fromTheme(app_id, QIcon(str(icon_path))))

    # Everything past argv[0] is a path to play.
    initial_paths = sys.argv[1:]

    ret = 1
    try:
        window = MainWindow()
        window.show()
        log.info("window shown")
        if initial_paths:
            log.info("loading initial paths: %r", initial_paths)
            window.play_paths(initial_paths)
        ret = app.exec()
        log.info("event loop exited rc=%s", ret)
    except Exception:
        log.exception("fatal during startup")
        traceback.print_exc()
        os._exit(1)

    # Force-exit without running Python finalizers — avoids segfault
    # from GStreamer / Qt objects being destroyed in unpredictable
    # order (same pattern as Quaver).
    os._exit(ret)


if __name__ == "__main__":
    main()
