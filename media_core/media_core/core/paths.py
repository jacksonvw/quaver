"""Platform-aware application directories.

Uses ``platformdirs`` so this module stays Qt-free.  Each sibling app
(Quaver, Minim) constructs its own :class:`AppPaths` with its own name;
on Linux the resolved paths are XDG-compliant, on macOS they live under
``~/Library/Application Support`` etc.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir


class AppPaths:
    """Per-app directory resolver.

    Each app (Quaver, Minim) constructs one of these with its own name.
    The XDG/platform layout matches what platformdirs returns; we just
    ensure the directory exists on first access.
    """

    def __init__(self, app_name: str) -> None:
        if not app_name:
            raise ValueError("app_name must be non-empty")
        self.app_name = app_name

    def config_dir(self) -> Path:
        """~/.config/<app>  (Linux) · %LOCALAPPDATA%/<app>  (Win) · ~/Library/Application Support/<app>  (mac)."""
        p = Path(user_config_dir(self.app_name))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def data_dir(self) -> Path:
        """~/.local/share/<app>  (Linux) · %LOCALAPPDATA%/<app>  (Win) · ~/Library/Application Support/<app>  (mac)."""
        p = Path(user_data_dir(self.app_name))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def cache_dir(self) -> Path:
        """~/.cache/<app>  (Linux) · %LOCALAPPDATA%/<app>/Cache  (Win) · ~/Library/Caches/<app>  (mac)."""
        p = Path(user_cache_dir(self.app_name))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def image_dir(self) -> Path:
        """Permanent image storage for scraped posters, thumbnails, artwork.

        Unlike :meth:`cache_dir`, this lives under :meth:`data_dir` because these
        images are downloaded from scrapers and stored permanently —
        re-downloading them is expensive and hammers the NAS.
        """
        p = self.data_dir() / "images"
        p.mkdir(parents=True, exist_ok=True)
        return p
