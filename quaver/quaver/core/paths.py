"""Quaver-side path conveniences — wraps :class:`media_core.core.paths.AppPaths`.

This module exists so existing Quaver call sites can keep doing
``from quaver.core.paths import config_dir, data_dir, ...`` (bound to
``app_name="quaver"``).  The shared :class:`AppPaths` class lives in
``media_core``; instantiate it directly in sibling apps (e.g. Minim
constructs ``AppPaths("minim")``).

The :func:`migrate_images_from_cache` helper is Quaver-specific and
stays here — it migrates the old Quaver cache layout to the new
permanent image storage and updates the SQLite ``video_*.poster_path``
columns.
"""

from __future__ import annotations

from pathlib import Path

# Re-export AppPaths so old ``from quaver.core.paths import AppPaths``
# imports keep working during the media_core migration.
from media_core.core.paths import AppPaths  # noqa: F401


_APP = "quaver"

# Default paths instance for Quaver — module-level functions delegate here.
_default = AppPaths(_APP)


def config_dir() -> Path:
    return _default.config_dir()


def data_dir() -> Path:
    return _default.data_dir()


def cache_dir() -> Path:
    return _default.cache_dir()


def image_dir() -> Path:
    return _default.image_dir()


def migrate_images_from_cache() -> None:
    """One-time migration: move image files from cache_dir to image_dir.

    Moves .jpg/.png/.webp files from the old cache subdirectories to the
    new permanent image_dir subdirectories.  JSON caches stay in cache_dir.
    Also updates absolute paths stored in the SQLite database.

    Quaver-only — Minim has no scrapers, no SQLite, no image cache to migrate.
    """
    import logging
    import shutil

    log = logging.getLogger(__name__)

    old_base = cache_dir()
    new_base = image_dir()

    migrations = [
        ("tvmaze", (".jpg", ".png", ".webp")),
        ("tmdb", (".jpg", ".png", ".webp")),
        ("youtube_thumbs", (".jpg", ".png", ".webp")),
        ("podcast_art", (".jpg", ".png", ".webp")),
    ]

    moved = 0
    for subdir, extensions in migrations:
        old = old_base / subdir
        if not old.is_dir():
            continue
        new = new_base / subdir
        new.mkdir(parents=True, exist_ok=True)
        for f in old.iterdir():
            if f.suffix.lower() in extensions and f.is_file():
                dest = new / f.name
                if not dest.exists():
                    shutil.move(str(f), str(dest))
                else:
                    f.unlink()  # Already migrated.
                moved += 1

    if moved == 0:
        return  # Nothing to migrate.

    log.info("Migrated %d image files from cache to permanent storage", moved)

    # Update absolute paths in the database.
    db_path = data_dir() / "quaver.db"
    if not db_path.exists():
        return

    import sqlite3
    old_prefix = str(old_base) + "/"
    new_prefix = str(new_base) + "/"

    try:
        conn = sqlite3.connect(str(db_path))
        # video_metadata.poster_path and video_shows.poster_path.
        for table, col in [
            ("video_metadata", "poster_path"),
            ("video_shows", "poster_path"),
        ]:
            try:
                conn.execute(
                    f"UPDATE {table} SET {col} = REPLACE({col}, ?, ?) "
                    f"WHERE {col} LIKE ?",
                    (old_prefix, new_prefix, old_prefix + "%"),
                )
            except sqlite3.OperationalError:
                pass  # Table may not exist yet.
        conn.commit()
        conn.close()
        log.info("Updated database poster paths to new image directory")
    except sqlite3.Error as e:
        log.warning("Failed to update database paths: %s", e)
