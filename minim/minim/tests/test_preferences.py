"""PreferencesDialog construction + apply round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

import minim  # noqa: F401

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from media_core.core.config import PlayerConfig
from minim.core.config import load_config, save_config
from minim.ui.dialogs.preferences import PreferencesDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_dialog_constructs_with_default_config(qapp, tmp_path: Path,
                                                monkeypatch) -> None:
    """A bare PreferencesDialog reads load_config and populates its
    widgets without error.
    """
    # Point load_config at an empty tmp file so the test doesn't read
    # the user's real config.
    fake_path = tmp_path / "config.toml"
    monkeypatch.setattr(
        "minim.ui.dialogs.preferences.load_config",
        lambda *a, **kw: PlayerConfig(),
    )
    monkeypatch.setattr(
        "minim.ui.dialogs.preferences.save_config",
        lambda cfg, path=None: save_config(cfg, fake_path),
    )

    dlg = PreferencesDialog()
    assert dlg.windowTitle() == "Minim Preferences"
    # Three tabs: Appearance / Playback / Video.
    tabs = dlg.findChild(type(dlg.layout().itemAt(0).widget()))
    # We don't introspect the tab widget directly; just sanity-check
    # the underlying config object survives through .config.
    assert isinstance(dlg.config, PlayerConfig)


def test_apply_writes_through_to_save_config(qapp, tmp_path: Path,
                                              monkeypatch) -> None:
    """Hitting Apply should call save_config with the dialog's
    populated PlayerConfig.
    """
    fake_path = tmp_path / "applied.toml"
    captured: dict = {}

    def fake_save(cfg, path=None):
        captured["cfg"] = cfg
        save_config(cfg, fake_path)

    monkeypatch.setattr(
        "minim.ui.dialogs.preferences.load_config",
        lambda *a, **kw: PlayerConfig(),
    )
    monkeypatch.setattr(
        "minim.ui.dialogs.preferences.save_config", fake_save,
    )

    dlg = PreferencesDialog()
    # Mutate a field via the dialog's underlying config + widgets.
    dlg._np_tint_slider.setValue(99)
    dlg._on_apply()

    assert captured["cfg"].nowplaying_tint_alpha == 99
    # Persisted to disk through the real save_config tail.
    on_disk = load_config(fake_path)
    assert on_disk.nowplaying_tint_alpha == 99
