"""libass renderer — ctypes binding for subtitle rendering at display resolution.

Uses libass (the same library VLC and Kodi use) to render ASS/SSA subtitles
correctly: positioning, fonts, colors, drawings, animations, karaoke — all
handled natively.  The renderer outputs RGBA bitmaps at whatever resolution
we specify, so text stays crisp regardless of source video resolution.

For non-ASS subtitles (SRT, VTT), the SubtitleOverlay falls back to its
QPainter text renderer.

No PySide6 imports — this module must be usable without a display.
"""

from __future__ import annotations

import ctypes
import logging
from typing import NamedTuple

import numpy as np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# libass C struct definitions
# ---------------------------------------------------------------------------

class _ASS_Image(ctypes.Structure):
    """Mirrors the ASS_Image linked-list node from libass."""
    pass


_ASS_Image._fields_ = [
    ("w", ctypes.c_int),
    ("h", ctypes.c_int),
    ("stride", ctypes.c_int),
    ("bitmap", ctypes.POINTER(ctypes.c_ubyte)),
    ("color", ctypes.c_uint32),   # 0xRRGGBBAA (AA: 0=opaque, FF=transparent)
    ("dst_x", ctypes.c_int),
    ("dst_y", ctypes.c_int),
    ("next", ctypes.POINTER(_ASS_Image)),
    ("type", ctypes.c_int),
]


class SubtitleBitmap(NamedTuple):
    """One rendered subtitle glyph/region ready for compositing."""
    x: int
    y: int
    w: int
    h: int
    rgba: bytes  # w×h×4 RGBA premultiplied


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class LibassRenderer:
    """Wraps libass for rendering ASS/SSA subtitles to RGBA bitmaps.

    Lifecycle:
        renderer = LibassRenderer()
        renderer.set_frame_size(1920, 1080)
        renderer.new_track(codec_private_bytes)
        renderer.process_chunk(event_data, timecode_ms, duration_ms)
        bitmaps = renderer.render_frame(now_ms)
        renderer.shutdown()

    All methods must be called from the SAME thread (the Qt main thread).
    The GStreamer callback queues raw data; the overlay polls and renders.
    """

    def __init__(self) -> None:
        try:
            self._lib = ctypes.CDLL("libass.so.9")
        except OSError:
            try:
                self._lib = ctypes.CDLL("libass.so")
            except OSError:
                log.warning("libass not found — ASS subtitle rendering unavailable")
                self._lib = None
                self._library = None
                self._renderer = None
                self._track = None
                return

        self._setup_signatures()

        self._library = self._lib.ass_library_init()
        if not self._library:
            log.error("ass_library_init failed")
            self._lib = None
            self._renderer = None
            self._track = None
            return

        self._renderer = self._lib.ass_renderer_init(self._library)
        if not self._renderer:
            log.error("ass_renderer_init failed")
            self._lib.ass_library_done(self._library)
            self._lib = None
            self._library = None
            self._track = None
            return

        # Use fontconfig for font selection (same as VLC/Kodi).
        self._lib.ass_set_fonts(
            self._renderer, None, b"sans-serif",
            1,     # use_fontconfig = true
            None,  # fontconfig config path
            1,     # update fontconfig cache
        )

        self._track = None
        self._frame_w = 0
        self._frame_h = 0
        log.info("libass renderer initialized (version %s)", self._get_version())

    @property
    def available(self) -> bool:
        return self._lib is not None and self._renderer is not None

    # ------------------------------------------------------------------
    # Track management
    # ------------------------------------------------------------------

    def new_track(self, codec_private: bytes | None = None) -> None:
        """Create a new ASS track, optionally seeded with the ASS header.

        The codec_private data is the [Script Info] + [V4+ Styles] + [Events
        Format] header that comes from the container's codec_data field.
        """
        if not self.available:
            return
        if self._track:
            self._lib.ass_free_track(self._track)
        self._track = self._lib.ass_new_track(self._library)
        if codec_private:
            self._lib.ass_process_codec_private(
                self._track, codec_private, len(codec_private),
            )

    def load_script(self, script: bytes) -> bool:
        """Replace the track with a whole ASS/SSA script (an external file).

        Returns False if libass couldn't read it.
        """
        if not self.available:
            return False
        if self._track:
            self._lib.ass_free_track(self._track)
        buf = ctypes.create_string_buffer(script, len(script))
        self._track = self._lib.ass_read_memory(self._library, buf, len(script), None)
        return bool(self._track)

    def process_chunk(self, data: bytes, timecode_ms: int, duration_ms: int) -> None:
        """Feed a single ASS dialogue event to the current track."""
        if not self.available or not self._track:
            return
        self._lib.ass_process_chunk(
            self._track, data, len(data), timecode_ms, duration_ms,
        )

    def flush(self) -> None:
        """Discard all events from the current track (e.g. after seek)."""
        if not self.available or not self._track:
            return
        self._lib.ass_flush_events(self._track)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def set_frame_size(self, width: int, height: int) -> None:
        """Set the rendering resolution (display size, not source video)."""
        if not self.available:
            return
        if width != self._frame_w or height != self._frame_h:
            self._frame_w = width
            self._frame_h = height
            self._lib.ass_set_frame_size(self._renderer, width, height)

    def set_storage_size(self, width: int, height: int) -> None:
        """Set the source video resolution for correct ASS coordinate scaling."""
        if not self.available:
            return
        self._lib.ass_set_storage_size(self._renderer, width, height)

    def render_frame(self, now_ms: int) -> list[SubtitleBitmap]:
        """Render all active subtitle events at *now_ms* and return bitmaps."""
        if not self.available or not self._track or self._frame_w == 0:
            return []

        detect_change = ctypes.c_int(0)
        head = self._lib.ass_render_frame(
            self._renderer, self._track, now_ms, ctypes.byref(detect_change),
        )

        bitmaps: list[SubtitleBitmap] = []
        ptr = head
        while ptr:
            img = ptr.contents
            if img.w > 0 and img.h > 0:
                bitmaps.append(self._image_to_bitmap(img))
            ptr = img.next if img.next else None
        return bitmaps

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        if not self._lib:
            return
        if self._track:
            self._lib.ass_free_track(self._track)
            self._track = None
        if self._renderer:
            self._lib.ass_renderer_done(self._renderer)
            self._renderer = None
        if self._library:
            self._lib.ass_library_done(self._library)
            self._library = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _setup_signatures(self) -> None:
        """Declare ctypes function signatures for libass."""
        L = self._lib
        vp = ctypes.c_void_p
        cp = ctypes.c_char_p

        L.ass_library_init.restype = vp
        L.ass_library_init.argtypes = []

        L.ass_library_done.restype = None
        L.ass_library_done.argtypes = [vp]

        L.ass_renderer_init.restype = vp
        L.ass_renderer_init.argtypes = [vp]

        L.ass_renderer_done.restype = None
        L.ass_renderer_done.argtypes = [vp]

        L.ass_set_frame_size.restype = None
        L.ass_set_frame_size.argtypes = [vp, ctypes.c_int, ctypes.c_int]

        L.ass_set_storage_size.restype = None
        L.ass_set_storage_size.argtypes = [vp, ctypes.c_int, ctypes.c_int]

        L.ass_set_fonts.restype = None
        L.ass_set_fonts.argtypes = [vp, cp, cp, ctypes.c_int, cp, ctypes.c_int]

        L.ass_new_track.restype = vp
        L.ass_new_track.argtypes = [vp]

        L.ass_free_track.restype = None
        L.ass_free_track.argtypes = [vp]

        L.ass_read_memory.restype = vp
        L.ass_read_memory.argtypes = [vp, cp, ctypes.c_size_t, cp]
        L.ass_process_codec_private.restype = None
        L.ass_process_codec_private.argtypes = [vp, cp, ctypes.c_int]

        L.ass_process_chunk.restype = None
        L.ass_process_chunk.argtypes = [
            vp, cp, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong,
        ]

        L.ass_render_frame.restype = ctypes.POINTER(_ASS_Image)
        L.ass_render_frame.argtypes = [
            vp, vp, ctypes.c_longlong, ctypes.POINTER(ctypes.c_int),
        ]

        L.ass_flush_events.restype = None
        L.ass_flush_events.argtypes = [vp]

    @staticmethod
    def _image_to_bitmap(img: _ASS_Image) -> SubtitleBitmap:
        """Convert a libass alpha-map + color into an RGBA bitmap.

        libass renders each glyph as a single-channel alpha coverage map
        plus a flat RGBA color.  We composite them into a premultiplied
        RGBA array suitable for QPainter.
        """
        w, h, stride = img.w, img.h, img.stride
        color = img.color  # 0xRRGGBBAA  (AA: 0=opaque, 255=transparent)
        r = (color >> 24) & 0xFF
        g = (color >> 16) & 0xFF
        b = (color >> 8) & 0xFF
        a = 255 - (color & 0xFF)  # invert: libass 0=opaque → we want 255=opaque

        # Read the alpha coverage map (may have stride > width).
        raw = ctypes.string_at(img.bitmap, stride * h)
        alpha_padded = np.frombuffer(raw, dtype=np.uint8).reshape(h, stride)
        alpha_map = alpha_padded[:, :w].copy()  # trim padding

        # Combine coverage with the glyph's opacity.
        pixel_a = (alpha_map.astype(np.uint16) * a) // 255

        # Build RGBA (4 channels).
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[:, :, 0] = r
        rgba[:, :, 1] = g
        rgba[:, :, 2] = b
        rgba[:, :, 3] = pixel_a

        return SubtitleBitmap(
            x=img.dst_x, y=img.dst_y, w=w, h=h,
            rgba=rgba.tobytes(),
        )

    def _get_version(self) -> str:
        """Return libass version string for logging."""
        try:
            fn = self._lib.ass_library_version
            fn.restype = ctypes.c_int
            v = fn()
            major = (v >> 24) & 0xFF
            minor = (v >> 16) & 0xFF
            patch = v & 0xFFFF
            return f"{major}.{minor}.{patch}"
        except Exception:
            return "unknown"
