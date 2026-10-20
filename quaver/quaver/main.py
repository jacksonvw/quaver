"""Quaver — desktop music player."""

import os
import sys

from PySide6.QtWidgets import QApplication

from quaver.ui.main_window import MainWindow


def main() -> None:
    # Migrate images from cache_dir to permanent data_dir (one-time).
    from quaver.core.paths import migrate_images_from_cache
    migrate_images_from_cache()

    app = QApplication(sys.argv)
    app.setApplicationName("Quaver")

    from quaver.ui.icons import icon_app
    app.setWindowIcon(icon_app())

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
