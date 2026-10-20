"""Shared pytest configuration — runs Qt headless."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_sessionfinish(session, exitstatus):
    """Tear down Qt widgets deterministically before interpreter exit.

    Widget tests share a QApplication that is never quit; without this,
    top-level widgets are destroyed in GC order at interpreter shutdown,
    which can abort ("QObject: shared QObject was deleted directly").
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    app.processEvents()
    app.processEvents()
