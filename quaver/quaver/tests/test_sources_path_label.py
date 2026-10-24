"""The Sources dialog shows a short path on one line, not clipped.

Regression: Breeze lets a form field grow only if its size policy is
Expanding, and a word-wrapped QLabel's size hint is a quarter of its
natural width for short text.  "/home/…/Music/Music" broke after a slash,
and the form gave it one line's height, cutting off the second line.
"""

import pytest
from PySide6.QtWidgets import QApplication, QFormLayout, QProxyStyle, QStyle

from quaver.core.config import AppConfig, SourceConfig
from quaver.core.library import LibraryDB
from quaver.ui.dialogs import sources


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _BreezeFormStyle(QProxyStyle):
    """Answers the form-growth hint as Breeze does, whatever the test style."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_FormLayoutFieldGrowthPolicy:
            return QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow.value
        return super().styleHint(hint, option, widget, returnData)


def test_short_path_fits_on_one_line(qapp, monkeypatch, tmp_path) -> None:
    path = "/home/someone/Music/Music"
    config = AppConfig(sources=[SourceConfig(path=path)])
    monkeypatch.setattr(sources, "load_config", lambda: config)
    dlg = sources.SourcesDialog(LibraryDB(tmp_path / "test.db"))
    style = _BreezeFormStyle()
    dlg.setStyle(style)
    dlg.resize(700, 508)
    dlg.show()
    dlg._source_list.setCurrentRow(0)
    qapp.processEvents()

    label = dlg._path_label
    try:
        assert label.width() > label.fontMetrics().horizontalAdvance(path)
        assert label.height() >= label.heightForWidth(label.width())
    finally:
        dlg.close()
