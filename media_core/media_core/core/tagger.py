"""Tag reader/writer using mutagen.

No PySide6 imports — must be testable without a display.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis


@dataclass
class TrackTags:
    filepath: str
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: int | None = None
    genre: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int = 0
    bitrate: int = 0
    format: str = ""
    replay_gain_track: float | None = None
    replay_gain_album: float | None = None
    media_type: str = "audio"


def _first(tags: dict, key: str, default: str = "") -> str:
    """Get the first value for *key*, or *default*."""
    val = tags.get(key)
    if val is None:
        return default
    if isinstance(val, list):
        return str(val[0]) if val else default
    return str(val)


def _parse_int(s: str) -> int | None:
    """Parse an integer from a string like '3' or '3/12'."""
    if not s:
        return None
    try:
        return int(s.split("/")[0])
    except (ValueError, IndexError):
        return None


def _parse_rg(s: str) -> float | None:
    """Parse a ReplayGain value like '-6.54 dB'."""
    if not s:
        return None
    try:
        return float(s.replace("dB", "").strip())
    except ValueError:
        return None


def _format_name(audio: mutagen.FileType) -> str:
    """Return a short format name like 'MP3', 'FLAC', etc."""
    cls = type(audio).__name__
    mapping = {
        "MP3": "MP3", "FLAC": "FLAC", "OggVorbis": "Vorbis",
        "OggOpus": "Opus", "MP4": "AAC", "WavPack": "WavPack",
        "Musepack": "Musepack", "TrueAudio": "TTA",
        "ASF": "WMA", "WAVE": "WAV",
    }
    return mapping.get(cls, cls)


def read_tags(path: str | Path) -> TrackTags | None:
    """Read tags from an audio file. Returns None if unreadable."""
    path = Path(path)
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception:
        return None

    if audio is None:
        return None

    duration_ms = int((audio.info.length or 0) * 1000)
    bitrate = getattr(audio.info, "bitrate", 0) or 0

    tags = TrackTags(
        filepath=str(path),
        title=_first(audio, "title", default=path.stem),
        artist=_first(audio, "artist"),
        album=_first(audio, "album"),
        album_artist=_first(audio, "albumartist") or _first(audio, "artist"),
        year=_parse_int(_first(audio, "date")),
        genre=_first(audio, "genre"),
        track_number=_parse_int(_first(audio, "tracknumber")),
        disc_number=_parse_int(_first(audio, "discnumber")),
        duration_ms=duration_ms,
        bitrate=bitrate,
        format=_format_name(audio),
    )

    # ReplayGain — stored differently per format.
    rg_track = _first(audio, "replaygain_track_gain")
    rg_album = _first(audio, "replaygain_album_gain")
    tags.replay_gain_track = _parse_rg(rg_track)
    tags.replay_gain_album = _parse_rg(rg_album)

    return tags


# EasyID3/EasyMP4/Vorbis key mapping — all share the same easy interface.
_EASY_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "albumartist",
    "year": "date",
    "genre": "genre",
    "track_number": "tracknumber",
    "disc_number": "discnumber",
}


def write_tags(path: str | Path, changes: dict[str, str | None]) -> bool:
    """Write only the changed tag fields to an audio file.

    *changes* maps our field names (title, artist, album, album_artist,
    year, genre, track_number, disc_number) to new string values.
    A value of None or "" clears the tag.

    Returns True on success, False on failure.
    """
    path = Path(path)
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception:
        return False

    if audio is None:
        return False

    for field, value in changes.items():
        easy_key = _EASY_KEYS.get(field)
        if easy_key is None:
            continue
        if value is None or value == "":
            audio.pop(easy_key, None)
        else:
            audio[easy_key] = value

    try:
        audio.save()
    except Exception:
        return False
    return True
