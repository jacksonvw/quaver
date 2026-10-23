"""Embedded album art extraction — pure tag/file IO, no Qt.

Reads cover images from audio file tags (FLAC pictures, ID3 APIC, MP4
covr atoms, Vorbis ``metadata_block_picture``) and from sibling
``cover.jpg``/``folder.jpg``-style files.  No Qt imports.

Library shells layer scraper-poster fallbacks (TVMaze stills, TMDb
posters) on top of these helpers — see Quaver's ``art_cache.py``.
"""

from __future__ import annotations

import base64
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4


_FOLDER_ART_NAMES = [
    "cover.jpg", "cover.png", "folder.jpg", "folder.png",
    "front.jpg", "front.png", "album.jpg", "album.png",
    "artwork.jpg", "artwork.png",
]


def find_folder_art(filepath: str) -> bytes | None:
    """Look for a high-res cover image in the same directory as the audio file."""
    folder = Path(filepath).parent
    for name in _FOLDER_ART_NAMES:
        candidate = folder / name
        if candidate.is_file():
            try:
                return candidate.read_bytes()
            except OSError:
                continue
    # Also check case-insensitive (common on mixed-OS libraries).
    try:
        lower_map = {f.name.lower(): f for f in folder.iterdir() if f.is_file()}
    except OSError:
        return None
    for name in _FOLDER_ART_NAMES:
        if name in lower_map:
            try:
                return lower_map[name].read_bytes()
            except OSError:
                continue
    return None


def extract_embedded_art(filepath: str) -> bytes | None:
    """Extract embedded art from audio file tags."""
    try:
        audio = mutagen.File(filepath)
    except Exception:
        return None
    if audio is None:
        return None

    if isinstance(audio, FLAC) and audio.pictures:
        return audio.pictures[0].data

    if isinstance(audio, MP4):
        covers = audio.tags.get("covr") if audio.tags else None
        if covers:
            return bytes(covers[0])

    # Only ID3 (MP3, WAV, AIFF) has APIC frames.  Vorbis comments (FLAC,
    # Ogg Vorbis, Opus) iterate as (key, value) tuples, so scanning keys
    # generically raised and skipped both the picture and folder-art paths.
    if hasattr(getattr(audio, "tags", None), "getall"):
        frames = audio.tags.getall("APIC")
        if frames:
            return frames[0].data

    if hasattr(audio, "tags") and audio.tags:
        pics = (
            audio.tags.get("metadata_block_picture")
            or audio.get("metadata_block_picture")
        )
        if pics:
            try:
                pic = Picture(base64.b64decode(pics[0]))
                return pic.data
            except Exception:
                pass

    return None


def extract_art(filepath: str, prefer_hires: bool = False) -> bytes | None:
    """Extract album art for an audio file.

    When *prefer_hires* is True, check for folder art first and prefer it
    if it's larger than the embedded art. Used by Now Playing for full-res
    background; the album view uses the default (embedded-first) path.
    """
    embedded = extract_embedded_art(filepath)
    if not prefer_hires:
        return embedded or find_folder_art(filepath)

    # For Now Playing: prefer whichever source gives a bigger image.
    folder = find_folder_art(filepath)
    if folder and embedded:
        # Quick heuristic: larger byte size ≈ higher resolution.
        return folder if len(folder) > len(embedded) else embedded
    return folder or embedded
