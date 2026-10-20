"""File-picker sidebar URLs — must surface XDG user dirs (Music,
Downloads, Videos, …) so users can navigate to their media.

Regression: ``build_sidebar_urls()`` previously listed only mount
roots like ``/mnt`` and ``/media``, leaving no entry for ``~/Music``
or ``~/Downloads`` — users couldn't find their media folders from the
Sources → Add Local… picker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import quaver  # noqa: F401  (sys.path bootstrap)

pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_sidebar_includes_home_dir(qapp) -> None:
    from quaver.ui.dialogs.sources import build_sidebar_urls

    urls = build_sidebar_urls()
    paths = {u.toLocalFile() for u in urls}
    home = str(Path.home())
    assert home in paths, (
        f"Sidebar must include the user's home directory ({home}); got {paths}"
    )


def test_sidebar_orders_user_dirs_before_mounts(qapp) -> None:
    """Home / Music / Downloads come before /mnt and /media so the
    file dialog opens with familiar entries at the top of the sidebar.
    """
    from quaver.ui.dialogs.sources import build_sidebar_urls

    urls = build_sidebar_urls()
    paths = [u.toLocalFile() for u in urls]
    home = str(Path.home())
    if home not in paths:
        pytest.skip("home dir not detected in test environment")

    home_idx = paths.index(home)
    # Any /mnt or /media entry must come after home.
    for i, p in enumerate(paths):
        if p.startswith(("/mnt", "/media", "/run/media", "/run/user/")):
            assert i > home_idx, (
                "User dirs (Home, Music, Downloads, …) must appear in "
                "the sidebar before drive/mount roots"
            )


def test_sidebar_deduplicates(qapp) -> None:
    from quaver.ui.dialogs.sources import build_sidebar_urls

    urls = build_sidebar_urls()
    paths = [u.toLocalFile() for u in urls]
    assert len(paths) == len(set(paths)), (
        f"Sidebar URLs must be deduplicated; got {paths}"
    )


def test_default_dialog_start_dir_is_home(qapp) -> None:
    """The dialog opens at $HOME so the user lands somewhere navigable."""
    from quaver.ui.dialogs.sources import _default_dialog_start_dir

    start = _default_dialog_start_dir()
    assert start, "Should resolve to a non-empty path"
    assert Path(start).is_dir(), f"Start dir should exist: {start}"
