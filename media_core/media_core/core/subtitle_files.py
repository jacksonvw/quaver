"""External subtitle files: finding them beside a video, and reading them.

Found VLC's way, so a player built on this behaves like VLC: files beside
the video whose names start with the video's (``Film.srt``, ``Film.en.srt``,
``Film.forced.srt``), and the same in ``Subs`` or ``Subtitles`` folders next
to it, each become a track of their own.  They are read here rather than by
GStreamer, whose playbin takes one external file, chosen before playback
starts; this way any number can be added and switching between them is
instant.

SRT, WebVTT, ASS/SSA, MicroDVD and SubViewer are read.  An ASS/SSA file is
kept whole, for libass to render with its styles; the others become timed
plain-text cues.

No GStreamer or Qt imports.
"""

from __future__ import annotations

import bisect
import codecs
import re
import stat
from dataclasses import dataclass
from pathlib import Path

# In priority order: a .srt beats a .sub of the same name.
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")

# VLC's sub-autodetect-path.
_SUB_DIRS = ("Subs", "subs", "Subtitles", "subtitles")

# Anything bigger is not a subtitle file.
_MAX_BYTES = 20 * 1024 * 1024

# MicroDVD counts frames; this is the usual rate when the file doesn't say.
_DEFAULT_FPS = 23.976


class SubtitleError(Exception):
    """A file that can't be read as subtitles."""


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str


class SubtitleFile:
    """One external subtitle file, ready to draw.

    ``script`` holds a whole ASS/SSA file for libass; the text formats
    have ``cues`` instead, sorted by start.
    """

    def __init__(self, path: Path, label: str, *,
                 script: bytes | None = None, cues: list[Cue] | None = None) -> None:
        self.path = path
        self.label = label
        self.script = script
        self.cues = sorted(cues or [], key=lambda c: (c.start_ms, c.end_ms))
        self._starts = [c.start_ms for c in self.cues]
        self._longest = max((c.end_ms - c.start_ms for c in self.cues), default=0)

    @property
    def is_ass(self) -> bool:
        return self.script is not None

    def text_at(self, ms: int) -> str:
        """The text of every cue showing at *ms*, earliest first."""
        shown = []
        j = bisect.bisect_right(self._starts, ms) - 1
        # Only a cue that started within the longest cue's length can
        # still be showing.
        while j >= 0 and self._starts[j] > ms - self._longest:
            cue = self.cues[j]
            if cue.end_ms > ms:
                shown.append(cue.text)
            j -= 1
        return "\n".join(reversed(shown))


def is_subtitle_path(path: str | Path) -> bool:
    return Path(path).suffix.casefold() in SUBTITLE_EXTENSIONS


def find_beside(video: str | Path) -> list[Path]:
    """The subtitle files for *video*, found VLC's way, best first.

    Beside the video, and in its Subs/Subtitles folders, a file counts if
    its name starts with the video's.  A folder named after the video
    inside Subs (``Subs/Film/English.srt``, as releases often have it)
    counts whole.  An exact name match comes first, then by extension.
    """
    video = Path(video)
    folder = video.parent
    stem = video.stem.casefold()
    places = [(folder, False)]
    places += [(folder / d, False) for d in _SUB_DIRS]
    places += [(folder / d / video.stem, True) for d in _SUB_DIRS]

    found: dict[tuple[int, int], tuple[tuple, Path]] = {}
    for place, any_name in places:
        try:
            entries = list(place.iterdir())
        except OSError:
            continue
        for f in entries:
            ext = f.suffix.casefold()
            if ext not in SUBTITLE_EXTENSIONS:
                continue
            name = f.stem.casefold()
            if not any_name and not name.startswith(stem):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            # Keyed by inode, so "Subs" and "subs" on a case-insensitive
            # share don't list the same file twice.
            rank = (0 if name == stem else 1, SUBTITLE_EXTENSIONS.index(ext),
                    str(f).casefold())
            found.setdefault((st.st_dev, st.st_ino), (rank, f))
    return [f for _, f in sorted(found.values())]


def label_for(path: Path, video: Path | None = None) -> str:
    """The track's name in menus: the file name, with the folder it's in
    if that isn't the video's own."""
    if video is not None:
        try:
            return str(path.relative_to(video.parent))
        except ValueError:
            pass
    return path.name


def load(path: str | Path, label: str | None = None,
         fps: float | None = None) -> SubtitleFile:
    """Read *path* as subtitles.  *fps* times MicroDVD's frame counts when
    the file doesn't give its own rate.  Raises SubtitleError."""
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > _MAX_BYTES:
            raise SubtitleError("too large to be a subtitle file")
        data = path.read_bytes()
    except OSError as exc:
        raise SubtitleError(exc.strerror or str(exc)) from exc
    text = _decode(data)
    label = label or path.name

    head = text[:4096].casefold()
    if "[script info]" in head or "[v4+ styles]" in head or "[v4 styles]" in head:
        return SubtitleFile(path, label, script=text.encode("utf-8"))

    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if _MICRODVD.match(first):
        cues = _parse_microdvd(text, fps or _DEFAULT_FPS)
    elif "[information]" in head or _SUBVIEWER_TIME.match(first):
        cues = _parse_subviewer(text)
    else:
        cues = _parse_timed_blocks(text)  # SRT and WebVTT
    if not cues:
        raise SubtitleError("no subtitles found in it")
    return SubtitleFile(path, label, cues=cues)


# -- Decoding --------------------------------------------------------------


def _decode(data: bytes) -> str:
    """UTF-8 (with or without a BOM) or UTF-16 with one; anything else is
    taken as Windows-1252, what older subtitle files mostly are."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", "replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace")
    if b"\x00" in data[:4096]:
        # Binary, such as a VobSub .sub (pictures, beside an .idx).
        raise SubtitleError("not a text subtitle file")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


# -- SRT and WebVTT --------------------------------------------------------

_STAMP = r"(?:\d+:)?\d{1,2}:\d{1,2}(?:[.,]\d{1,3})?"
_TIMING = re.compile(rf"^\s*({_STAMP})\s*-->\s*({_STAMP})")
# WebVTT's in-cue timestamps (karaoke).  Other tags the overlay strips.
_VTT_INLINE_TIME = re.compile(r"</?\d[\d:.]*>")


def _clock_ms(stamp: str) -> int:
    """``1:02:03,456``, ``02:03.456`` or ``02:03`` in milliseconds."""
    parts = stamp.replace(",", ".").split(":")
    secs, _, frac = parts[-1].partition(".")
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return ((hours * 60 + minutes) * 60 + int(secs)) * 1000 + int((frac + "000")[:3])


def _parse_timed_blocks(text: str) -> list[Cue]:
    """SRT and WebVTT: a ``start --> end`` line, then text to a blank line.

    Forgiving of what real files get wrong: missing cue numbers, missing
    blank lines between cues, stray spaces.  WebVTT's NOTE, STYLE and
    REGION blocks have no timing line, so they're passed over.
    """
    lines = text.splitlines()
    cues: list[Cue] = []
    i = 0
    while i < len(lines):
        m = _TIMING.match(lines[i])
        i += 1
        if not m:
            continue
        start, end = _clock_ms(m.group(1)), _clock_ms(m.group(2))
        body: list[str] = []
        while i < len(lines) and lines[i].strip():
            if _TIMING.match(lines[i]):
                break
            body.append(lines[i].strip())
            i += 1
        # With the blank line missing, the next cue's number was read as
        # this cue's last line.
        if body and body[-1].isdigit() and i < len(lines) and _TIMING.match(lines[i]):
            body.pop()
        cue_text = _VTT_INLINE_TIME.sub("", "\n".join(body)).strip()
        if cue_text and end > start:
            cues.append(Cue(start, end, cue_text))
    return cues


# -- MicroDVD and SubViewer ------------------------------------------------

_MICRODVD = re.compile(r"^\{(\d+)\}\{(\d*)\}(.*)$")
_MICRODVD_CODE = re.compile(r"\{[^}]*\}")


def _parse_microdvd(text: str, fps: float) -> list[Cue]:
    """``{start}{end}text`` in frames; ``|`` breaks lines.  A first cue of
    ``{1}{1}23.976`` gives the frame rate."""
    cues: list[Cue] = []
    for raw in text.splitlines():
        m = _MICRODVD.match(raw.strip())
        if not m:
            continue
        start, end, body = int(m.group(1)), m.group(2), m.group(3)
        if not cues and start <= 1 and end in ("0", "1"):
            try:
                fps = float(body.strip())
                continue
            except ValueError:
                pass
        end_frame = int(end) if end else start + int(fps * 3)
        body = _MICRODVD_CODE.sub("", body).replace("|", "\n").strip()
        if body and end_frame > start:
            cues.append(Cue(round(start * 1000 / fps), round(end_frame * 1000 / fps), body))
    return cues


_SUBVIEWER_TIME = re.compile(
    r"^(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,3}),(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,3})\s*$"
)


def _parse_subviewer(text: str) -> list[Cue]:
    """SubViewer 2: ``0:00:01.00,0:00:04.00``, then the text; ``[br]``
    breaks lines."""
    lines = text.splitlines()
    cues: list[Cue] = []
    for i, raw in enumerate(lines[:-1]):
        m = _SUBVIEWER_TIME.match(raw.strip())
        if not m:
            continue
        g = m.groups()
        start = _clock_ms(f"{g[0]}:{g[1]}:{g[2]}.{g[3]}")
        end = _clock_ms(f"{g[4]}:{g[5]}:{g[6]}.{g[7]}")
        body = lines[i + 1].replace("[br]", "\n").replace("[BR]", "\n").strip()
        if body and end > start:
            cues.append(Cue(start, end, body))
    return cues
