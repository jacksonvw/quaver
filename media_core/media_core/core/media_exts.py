"""Canonical playable-file extension sets.

Lifted from Minim's :mod:`minim.tracks` so the Quaver folder-tree view
and the Minim drop handler share the same definition of "what's a
playable media file".  Without this, the two apps would silently drift
on edge formats (``.mka``, ``.mpg``, ``.mpc``, …) and the Quaver
``files`` tab and Minim's drop handler would disagree on what to show.

Both bare (``mp3``) and dotted (``.mp3``) forms are exposed.  Callers
that work from :class:`pathlib.Path` use the dotted form
(``p.suffix.lower() in AUDIO_EXTS_DOTTED``); callers that work with
extracted strings use the bare form.
"""

from __future__ import annotations

from pathlib import Path


# Bare (no leading dot, lowercase) — canonical form.
AUDIO_EXTS = frozenset({
    "mp3", "flac", "ogg", "opus", "m4a", "aac",
    "wav", "wma", "alac", "ape", "mka",
    "wv", "mpc", "tta",
})

VIDEO_EXTS = frozenset({
    "mp4", "mkv", "m4v", "avi", "mov", "webm",
    "mpg", "mpeg", "wmv", "flv", "ts", "vob",
})

PLAYABLE_EXTS = AUDIO_EXTS | VIDEO_EXTS


# Dotted variants for ``Path.suffix.lower() in …`` comparisons.
AUDIO_EXTS_DOTTED = frozenset(f".{e}" for e in AUDIO_EXTS)
VIDEO_EXTS_DOTTED = frozenset(f".{e}" for e in VIDEO_EXTS)
PLAYABLE_EXTS_DOTTED = AUDIO_EXTS_DOTTED | VIDEO_EXTS_DOTTED


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS_DOTTED


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS_DOTTED


def is_playable(path: Path) -> bool:
    """Return True if *path*'s extension is something Quaver / Minim
    can hand to GStreamer with a reasonable expectation of success.
    """
    return path.suffix.lower() in PLAYABLE_EXTS_DOTTED
