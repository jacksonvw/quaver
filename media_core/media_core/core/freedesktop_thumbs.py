"""Read freedesktop thumbnail-cache PNGs.

The freedesktop thumbnail spec (used by Dolphin, Nautilus, Tumbler,
ffmpegthumbnailer, KDE's kio thumbnailer, etc.) stores per-file
thumbnails under ``~/.cache/thumbnails/<size>/<md5(file_uri)>.png``.
We don't generate them — that's the desktop's job.  We just check if
one is already there and load it.

Used by:

- The Quaver Files-tab grid (right pane), so video files show their
  cached thumbnail next to audio files' embedded album art.
- Both apps' ``QueueWidget``, so a queued video shows its thumbnail
  in the same place audio shows album art.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PySide6.QtGui import QPixmap


# Sizes the freedesktop spec defines.  Quaver renders queue rows + grid
# cells at < 256px so ``large`` (256) is the natural pick — it scales
# down cleanly.  ``normal`` (128) is the fallback for older systems
# whose thumbnailers haven't generated 256-px thumbs.
_PREFERRED_SIZES = ("large", "normal")


def thumbnail_path(file_path: Path | str, size: str = "large") -> Path | None:
    """Resolve the candidate thumbnail file for *file_path*.

    *size* must be ``"normal"`` (128px), ``"large"`` (256px),
    ``"x-large"`` (512px), or ``"xx-large"`` (1024px).  The returned
    path may not exist — caller should ``.is_file()`` check it.
    """
    p = Path(file_path)
    try:
        uri = p.absolute().as_uri()
    except ValueError:
        return None
    md5 = hashlib.md5(uri.encode("utf-8")).hexdigest()
    cache = (
        os.environ.get("XDG_CACHE_HOME")
        or str(Path.home() / ".cache")
    )
    return Path(cache) / "thumbnails" / size / f"{md5}.png"


def load_thumbnail(file_path: Path | str) -> QPixmap | None:
    """Return the cached freedesktop thumbnail as a QPixmap, or None.

    Tries ``large`` (256px) before ``normal`` (128px) — the larger
    one downscales cleanly to small widget sizes while the small one
    would upscale and look fuzzy.  Returns ``None`` if no thumbnail
    has been generated yet or if all candidates fail to load.
    """
    p = Path(file_path)
    for size in _PREFERRED_SIZES:
        candidate = thumbnail_path(p, size=size)
        if candidate is None or not candidate.is_file():
            continue
        pm = QPixmap(str(candidate))
        if pm.isNull():
            continue
        return pm
    return None
