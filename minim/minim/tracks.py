"""Convert filesystem paths (argv, drag-drop, File→Open) into Tracks.

Minim has no library, so every track is *transient* — ``id == -1`` —
and is reconstructed on each launch from QSettings or the command line.
The :class:`Track` dataclass lives in :mod:`media_core.core.track` and
is shared with Quaver, so a Minim-built track can be dropped into any
queue widget unchanged.
"""

from __future__ import annotations

from pathlib import Path

from media_core.core.media_exts import (
    AUDIO_EXTS,
    PLAYABLE_EXTS,
    VIDEO_EXTS,
    is_playable,
)
from media_core.core.track import Track


def _ext(path: Path) -> str:
    return path.suffix.lstrip(".").lower()


def _media_type(ext: str) -> str:
    if ext in VIDEO_EXTS:
        return "movie"
    return "audio"


def track_from_path(path: str | Path) -> Track:
    """Build a transient :class:`Track` from a filesystem path.

    No file I/O, so a folder of thousands of files queues instantly:
    the title is the basename's stem and every other field gets an
    empty default.  The main window hands new tracks to
    :class:`media_core.services.tag_reader.TagReader`, which fills in
    title, artist, album and duration from the file's tags.
    """
    p = Path(path).expanduser()
    ext = _ext(p)
    return Track(
        id=-1,
        filepath=str(p),
        title=p.stem or p.name,
        artist="",
        album="",
        album_artist="",
        year=None,
        genre="",
        track_number=None,
        disc_number=None,
        duration_ms=0,
        bitrate=0,
        format=ext,
        replay_gain_track=None,
        replay_gain_album=None,
        play_count=0,
        last_played=None,
        date_added="",
        rating=0,
        fingerprint=None,
        media_type=_media_type(ext),
    )


def expand_paths(paths: list[str | Path]) -> list[Path]:
    """Expand argv / drop-target paths.

    Files are returned as-is.  Directories are walked one level deep
    and every playable file inside is included.  Non-existent paths
    and non-playable files are skipped.  Order from the input list is
    preserved; directory contents are sorted by name so a folder drop
    has a predictable play order.
    """
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            continue
        if p.is_file():
            if is_playable(p):
                out.append(p)
        elif p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and is_playable(child):
                    out.append(child)
    return out


def tracks_from_paths(paths: list[str | Path]) -> list[Track]:
    """End-to-end: argv / drop-target paths → list of transient tracks."""
    return [track_from_path(p) for p in expand_paths(paths)]
