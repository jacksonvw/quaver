"""Quaver — desktop music player."""

import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from quaver.ui.main_window import MainWindow


def main() -> None:
    # Migrate images from cache_dir to permanent data_dir (one-time).
    from quaver.core.paths import migrate_images_from_cache
    migrate_images_from_cache()

    app = QApplication(sys.argv)
    app.setApplicationName("Quaver")
    # The Wayland app_id; it has to match the installed quaver.desktop, or
    # in the Flatpak, the desktop entry and icon named for the app ID.
    app_id = "com.jacksonv.Quaver" if "FLATPAK_ID" in os.environ else "quaver"
    app.setDesktopFileName(app_id)

    icon_path = Path(__file__).parent / "resources" / "quaver.svg"
    if icon_path.is_file():
    # A theme name lets KWin render the icon itself.  Without one, Qt sends
    # pixel buffers whose alpha KWin misreads, blackening anti-aliased edges.
        app.setWindowIcon(QIcon.fromTheme(app_id, QIcon(str(icon_path))))

    try:
        window = MainWindow()
        window.show()
        ret = app.exec()
    except Exception:
        import traceback
        traceback.print_exc()
        os._exit(1)

    # Force-exit without running Python finalizers — avoids segfault from
    # GStreamer / Qt objects being destroyed in unpredictable order.
    os._exit(ret)


if __name__ == "__main__":
    main()
