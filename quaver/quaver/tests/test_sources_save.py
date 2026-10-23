"""Saving sources asks about tabs only when there are tabs to choose."""

import pytest
from PySide6.QtWidgets import QApplication

from quaver.core.config import AppConfig, LibraryTabConfig, SourceConfig
from quaver.core.library import LibraryDB
from quaver.ui.dialogs import sources


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _save(monkeypatch, tmp_path, config: AppConfig) -> list:
    asked = []

    class _Assign:
        def __init__(self, unassigned, tabs, parent=None):
            asked.append([s.path for s in unassigned])

        def exec(self):
            return 0

    monkeypatch.setattr(sources, "load_config", lambda: config)
    monkeypatch.setattr(sources, "save_config", lambda cfg: None)
    monkeypatch.setattr(sources, "_TabAssignmentDialog", _Assign)
    dlg = sources.SourcesDialog(LibraryDB(tmp_path / "test.db"))
    dlg._save()
    return asked


def test_first_source_without_tabs_is_not_questioned(qapp, monkeypatch, tmp_path) -> None:
    config = AppConfig(sources=[SourceConfig(path="/music")])
    # The default Library tab shows every source; there is nothing to assign.
    assert _save(monkeypatch, tmp_path, config) == []


def test_unassigned_source_is_questioned_when_tabs_exist(qapp, monkeypatch, tmp_path) -> None:
    config = AppConfig(
        sources=[SourceConfig(path="/music"), SourceConfig(path="/more")],
        library_tabs=[LibraryTabConfig(name="Music", source_paths=["/music"])],
    )
    assert _save(monkeypatch, tmp_path, config) == [["/more"]]
