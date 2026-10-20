"""AppPaths parameterization — both the default Quaver instance and
arbitrary app names should resolve to platform-correct directories.
"""

from pathlib import Path

import pytest

from quaver.core.paths import (
    AppPaths,
    cache_dir,
    config_dir,
    data_dir,
    image_dir,
)


def test_module_functions_match_quaver_defaults() -> None:
    """The legacy module-level functions are still bound to "quaver"."""
    assert config_dir().name == "quaver"
    assert data_dir().name == "quaver"
    assert cache_dir().name == "quaver"
    # image_dir() is data_dir()/images.
    assert image_dir().parent == data_dir()
    assert image_dir().name == "images"


def test_apppaths_uses_distinct_directories_per_app() -> None:
    quaver = AppPaths("quaver")
    minim = AppPaths("minim")
    # Distinct app names must produce distinct paths.
    assert quaver.config_dir() != minim.config_dir()
    assert quaver.data_dir() != minim.data_dir()
    assert quaver.cache_dir() != minim.cache_dir()
    # And the leaf name should be the app name.
    assert quaver.config_dir().name == "quaver"
    assert minim.config_dir().name == "minim"


def test_apppaths_image_dir_under_data_dir() -> None:
    paths = AppPaths("minim")
    img = paths.image_dir()
    assert img.parent == paths.data_dir()
    assert img.name == "images"


def test_apppaths_creates_directories(tmp_path, monkeypatch) -> None:
    """First access should mkdir the resolved directory (parents=True, exist_ok=True)."""
    fake_root = tmp_path / "root"
    monkeypatch.setattr(
        "media_core.core.paths.user_config_dir",
        lambda app: str(fake_root / "config" / app),
    )
    monkeypatch.setattr(
        "media_core.core.paths.user_data_dir",
        lambda app: str(fake_root / "data" / app),
    )
    monkeypatch.setattr(
        "media_core.core.paths.user_cache_dir",
        lambda app: str(fake_root / "cache" / app),
    )
    paths = AppPaths("test_app")
    assert paths.config_dir().is_dir()
    assert paths.data_dir().is_dir()
    assert paths.cache_dir().is_dir()
    assert paths.image_dir().is_dir()
    # image_dir() lives under data_dir().
    assert paths.image_dir() == paths.data_dir() / "images"


def test_apppaths_rejects_empty_name() -> None:
    with pytest.raises(ValueError):
        AppPaths("")
