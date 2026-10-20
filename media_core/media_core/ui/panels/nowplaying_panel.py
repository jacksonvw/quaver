"""Now Playing panel — large album art with configurable background.

Distraction-free view: centered, scaling art + track info.
Background modes: blurred album art / color gradient / solid dark.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QLabel,
    QMenu,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from typing import Callable

from media_core.core.config import PlayerConfig
from media_core.core.embedded_art import extract_art as _extract_art

import httpx

from media_core.core.track import Track


# Default callables used when no host overrides are supplied — Minim
# can construct ``NowPlayingPanel()`` with no args and the panel will
# read built-in defaults and silently no-op on save.  Quaver wires its
# real ``load_config`` / ``save_config`` so user changes persist to
# ``~/.config/quaver/config.toml``.
def _default_load_config() -> PlayerConfig:
    return PlayerConfig()


def _default_save_config(config: PlayerConfig) -> None:
    pass  # no-op


def _default_poster_reader(poster_path: str) -> bytes | None:
    """Embedded-only fallback — no scraper / no remote URL handling.

    Quaver overrides this with the wrapper that knows how to fetch
    TVMaze stills and TMDb posters.
    """
    return None


_ART_MIN = 100


class _ThumbnailLoader(QThread):
    """Download a YouTube thumbnail in the background."""

    loaded = Signal(QPixmap)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url

    def run(self):
        try:
            resp = httpx.get(self._url, timeout=10, follow_redirects=True)
            resp.raise_for_status()
            pixmap = QPixmap()
            pixmap.loadFromData(resp.content)
            if not pixmap.isNull():
                self.loaded.emit(pixmap)
        except Exception:
            pass


def _blur_pixmap(pixmap: QPixmap, strength: int = 10) -> QPixmap:
    """Blur a pixmap using QGraphicsBlurEffect (real Gaussian blur).

    *strength* is the blur radius (1-20). Falls back to multi-pass
    downscale/upscale if QGraphicsBlurEffect is unavailable.
    """
    if pixmap.isNull():
        return pixmap

    from PySide6.QtWidgets import QGraphicsScene, QGraphicsPixmapItem
    from PySide6.QtGui import QImage
    try:
        from PySide6.QtWidgets import QGraphicsBlurEffect

        # Render via QGraphicsScene with blur effect.
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(pixmap)
        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(float(strength * 3))  # Scale radius for visible effect.
        item.setGraphicsEffect(effect)
        scene.addItem(item)

        result = QImage(pixmap.size(), QImage.Format.Format_ARGB32_Premultiplied)
        result.fill(0)
        painter = QPainter(result)
        scene.render(painter)
        painter.end()
        return QPixmap.fromImage(result)
    except Exception:
        pass

    # Fallback: multi-pass downscale/upscale.
    factor = max(2, strength)
    current = pixmap
    for _ in range(2):
        small = current.scaled(
            max(1, current.width() // factor),
            max(1, current.height() // factor),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        current = small.scaled(
            pixmap.width(), pixmap.height(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return current


def _extract_palette(pixmap: QPixmap) -> list[tuple[int, int, int]]:
    """Extract 4 distinct, vibrant colors from the album art.

    Samples a grid of points across the image, clusters them by hue,
    and picks the most saturated representative from each cluster.
    Falls back to boosted averages if the image is too monochrome.
    """
    img = pixmap.toImage()
    if img.isNull():
        return [(30, 30, 30)] * 4
    w, h = img.width(), img.height()

    # Dense sampling: 8x8 grid.
    samples: list[QColor] = []
    for xi in range(8):
        for yi in range(8):
            px = int((xi + 0.5) / 8 * w)
            py = int((yi + 0.5) / 8 * h)
            px = max(0, min(w - 1, px))
            py = max(0, min(h - 1, py))
            samples.append(QColor(img.pixel(px, py)))

    # Bucket by hue into 6 sectors (0-60, 60-120, ..., 300-360) + low-saturation bucket.
    buckets: dict[int, list[QColor]] = {i: [] for i in range(7)}
    for c in samples:
        h_val, s, v = c.hsvHue(), c.hsvSaturation(), c.value()
        if s < 30 or v < 20:
            buckets[6].append(c)  # Gray/black bucket.
        else:
            buckets[h_val // 60 if h_val >= 0 else 0].append(c)

    # Pick the most saturated color from each non-empty bucket.
    picks: list[tuple[int, int, int]] = []
    for bucket_id in range(7):
        bucket = buckets[bucket_id]
        if not bucket:
            continue
        best = max(bucket, key=lambda c: c.hsvSaturation() + c.value())
        picks.append((best.red(), best.green(), best.blue()))

    # If we have fewer than 4 distinct colors, boost what we have.
    if len(picks) < 2:
        # Very monochrome image — create variations by shifting hue.
        base = samples[len(samples) // 2]  # Center sample.
        h_val, s, v = base.hsvHue(), base.hsvSaturation(), base.value()
        if h_val < 0:
            h_val = 0
        picks = []
        for shift in [0, 90, 180, 270]:
            c = QColor.fromHsv((h_val + shift) % 360, max(s, 80), max(v, 100))
            picks.append((c.red(), c.green(), c.blue()))

    # Pad to exactly 4 by repeating.
    while len(picks) < 4:
        picks.append(picks[len(picks) % len(picks)])

    return picks[:4]


def _boost_color(r: int, g: int, b: int, saturation_boost: float = 1.4, value_boost: float = 1.2) -> tuple[int, int, int]:
    """Boost saturation and brightness of an RGB color."""
    c = QColor(r, g, b)
    h, s, v = c.hsvHue(), c.hsvSaturation(), c.value()
    if h < 0:
        h = 0
    s = min(255, int(s * saturation_boost))
    v = min(255, int(v * value_boost))
    boosted = QColor.fromHsv(h, s, v)
    return (boosted.red(), boosted.green(), boosted.blue())


def _shift_warm(r: int, g: int, b: int) -> tuple[int, int, int]:
    """Shift color toward warm tones (reds/oranges/yellows)."""
    c = QColor(r, g, b)
    h, s, v = c.hsvHue(), c.hsvSaturation(), c.value()
    if h < 0:
        h = 30
    # Pull hue toward 30 (orange).
    if h > 180:
        h = (h + 30) % 360  # Wrap purples/blues toward red.
    else:
        h = max(0, h - 20)  # Pull greens toward yellow.
    s = min(255, int(s * 1.3))
    v = min(255, int(v * 1.15))
    warm = QColor.fromHsv(h % 360, s, v)
    return (warm.red(), warm.green(), warm.blue())


def _shift_cool(r: int, g: int, b: int) -> tuple[int, int, int]:
    """Shift color toward cool tones (blues/teals/purples)."""
    c = QColor(r, g, b)
    h, s, v = c.hsvHue(), c.hsvSaturation(), c.value()
    if h < 0:
        h = 220
    # Pull hue toward 220 (blue).
    if h < 180:
        h = h + 40  # Push reds/yellows toward cyan.
    else:
        h = min(280, h + 10)  # Push further into blue/purple.
    s = min(255, int(s * 1.3))
    v = min(255, int(v * 1.1))
    cool = QColor.fromHsv(h % 360, s, v)
    return (cool.red(), cool.green(), cool.blue())


def _pastel(r: int, g: int, b: int) -> tuple[int, int, int]:
    """Lighten and desaturate to create a pastel version."""
    c = QColor(r, g, b)
    h, s, v = c.hsvHue(), c.hsvSaturation(), c.value()
    if h < 0:
        h = 0
    s = min(255, max(40, int(s * 0.5)))  # Reduce saturation.
    v = min(255, max(180, int(v * 0.6 + 140)))  # Lighten significantly.
    pastel_c = QColor.fromHsv(h, s, v)
    return (pastel_c.red(), pastel_c.green(), pastel_c.blue())


# Background mode labels for the context menu.
_BG_MODES = [
    ("blur", "Blurred Album Art"),
    ("gradient_natural", "Gradient — Natural"),
    ("gradient_vivid", "Gradient — Vivid"),
    ("gradient_warm", "Gradient — Warm"),
    ("gradient_cool", "Gradient — Cool"),
    ("gradient_pastel", "Gradient — Pastel"),
    ("solid", "Solid Dark"),
    ("transparent", "Transparent"),
]

_INTENSITY_PRESETS = [
    ("Light", 80),
    ("Medium", 140),
    ("Heavy", 200),
    ("Very Heavy", 230),
]


_BLUR_PRESETS = [
    ("Light", 4),
    ("Medium", 10),
    ("Heavy", 18),
]

_OPACITY_PRESETS = [
    ("Full", 1.0),
    ("75%", 0.75),
    ("50%", 0.5),
    ("25%", 0.25),
]

_ART_SIZE_PRESETS = [
    ("Small", 30),
    ("Medium", 50),
    ("Large", 60),
    ("Extra Large", 75),
    ("Dominant", 85),
]


class NowPlayingPanel(QWidget):
    """Distraction-free Now Playing with configurable background."""

    youtube_mode_changed = Signal(str)  # "thumbnail" or "video"
    volume_change_requested = Signal(float)  # delta (-0.05 or +0.05)
    fullscreen_requested = Signal()  # double-click on either audio or video page
    video_active_changed = Signal(bool)  # True when video rendering starts/stops
    edit_tags_requested = Signal(list)  # [track_id]
    add_to_saved_playlist = Signal(list, int)  # (track_ids, playlist_id)
    mark_watched_requested = Signal(list, bool)  # (track_ids, watched)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        load_config: Callable[[], PlayerConfig] | None = None,
        save_config: Callable[[PlayerConfig], None] | None = None,
        poster_reader: Callable[[str], bytes | None] | None = None,
        extra_context_actions: Callable[
            [], list[tuple[str, Callable[[], None]]]
        ] | None = None,
    ) -> None:
        """Construct the panel.

        *load_config* / *save_config* are host-supplied callables for
        persisting Now Playing settings.  Quaver passes its own TOML
        round-trip; Minim either omits them (settings revert to
        defaults each launch) or wires QSettings-based persistence.

        *poster_reader* fetches scraper poster bytes by URL or file
        path (used for video tracks with episode stills).  Quaver
        wires its art_cache helper; Minim omits it.

        *extra_context_actions* is a host-supplied factory that returns
        a list of ``(label, callable)`` pairs appended to the bottom of
        the right-click context menu.  Called fresh each time the menu
        is built so labels can reflect mutable host state (e.g. Minim's
        "Enter / Exit Minimal Interface" toggle).  Defaults to no
        injection — Quaver passes nothing, Minim passes its toggle.
        """
        super().__init__(parent)
        self._load_config = load_config or _default_load_config
        self._save_config = save_config or _default_save_config
        self._poster_reader = poster_reader or _default_poster_reader
        self._extra_context_actions = extra_context_actions
        self._original_pixmap: QPixmap | None = None
        self._blurred_bg: QPixmap | None = None
        self._palette: list[tuple[int, int, int]] = [(30, 30, 30)] * 4
        self._bg_mode: str = "blur"
        self._tint_alpha: int = 140
        self._blur_strength: int = 10
        self._art_size_pct: int = 60
        self._bg_opacity: float = 1.0
        self._video_mode: str = "video"  # "video" or "thumbnail"
        self._is_youtube_track: bool = False
        self._is_local_video: bool = False
        self._video_active = False
        self._show_labels: bool = True
        self._thumb_loader: _ThumbnailLoader | None = None
        self._current_video_url: str | None = None  # for video mode
        self._current_engine = None  # AudioEngine reference for video rendering
        self._bg_scaled: QPixmap | None = None  # cached scaled blur for paintEvent
        self._current_track: Track | None = None
        self._is_watched: bool = False
        self._saved_playlists: list[tuple[int, str, int]] = []  # (id, name, count)
        self._build_ui()
        self._reload_config()

    def _build_ui(self) -> None:
        # QStackedLayout: page 0 = audio, page 1 = video.
        # The panel's paintEvent draws the background underneath both pages.
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.setStackingMode(QStackedLayout.StackingMode.StackOne)

        # --- Audio page (index 0) ---
        self._audio_page = QWidget()
        self._audio_page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        audio_layout = QVBoxLayout(self._audio_page)
        audio_layout.setContentsMargins(20, 20, 20, 20)

        audio_layout.addStretch(1)

        self._art_label = QLabel()
        self._art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._art_label.setStyleSheet(
            "background-color: rgba(0, 0, 0, 40); border: 1px solid rgba(255,255,255,20);"
        )
        self._art_label.setScaledContents(False)
        audio_layout.addWidget(self._art_label, alignment=Qt.AlignmentFlag.AlignCenter)

        self._title_label = QLabel("\u2014")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setWordWrap(True)
        self._title_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: white; background: transparent;"
        )

        self._artist_label = QLabel("")
        self._artist_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._artist_label.setWordWrap(True)
        self._artist_label.setStyleSheet(
            "font-size: 14px; color: #ddd; background: transparent;"
        )

        self._album_label = QLabel("")
        self._album_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._album_label.setWordWrap(True)
        self._album_label.setStyleSheet(
            "font-size: 12px; color: #bbb; background: transparent;"
        )

        self._genre_label = QLabel("")
        self._genre_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._genre_label.setWordWrap(True)
        self._genre_label.setStyleSheet(
            "font-size: 11px; color: rgba(255,255,255,160); "
            "background: transparent;"
        )
        self._genre_label.setVisible(False)

        self._plot_label = QLabel("")
        self._plot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._plot_label.setWordWrap(True)
        self._plot_label.setMaximumWidth(600)
        self._plot_label.setStyleSheet(
            "font-size: 11px; color: rgba(255,255,255,140); "
            "background: transparent; padding: 8px 0;"
        )
        self._plot_label.setVisible(False)

        audio_layout.addWidget(self._title_label)
        audio_layout.addWidget(self._artist_label)
        audio_layout.addWidget(self._album_label)
        audio_layout.addWidget(self._genre_label)
        audio_layout.addWidget(self._plot_label, alignment=Qt.AlignmentFlag.AlignCenter)

        # Spectrum visualizer at the bottom of Now Playing.
        from media_core.ui.widgets.spectrum import SpectrumWidget
        self._spectrum = SpectrumWidget(bar_count=64, bar_gap=2)
        self._spectrum.setFixedHeight(80)
        self._spectrum_visible = True
        audio_layout.addStretch(1)
        audio_layout.addWidget(self._spectrum)

        self._stack.addWidget(self._audio_page)  # index 0

        # --- Video page (index 1) ---
        self._video_page = QWidget()
        self._video_page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        video_layout = QVBoxLayout(self._video_page)
        video_layout.setContentsMargins(0, 0, 0, 0)

        from media_core.ui.widgets.video_surface import VideoSurface
        self._video_surface = VideoSurface(self._video_page)
        self._video_surface.fullscreen_requested.connect(self.fullscreen_requested)
        self._video_surface._context_menu_callback = self._show_context_menu
        video_layout.addWidget(self._video_surface)

        # Subtitle overlay — floats above the video surface (not in the layout).
        from media_core.ui.widgets.subtitle_overlay import SubtitleOverlay
        self._subtitle_overlay = SubtitleOverlay(self._video_page)
        self._subtitle_overlay.raise_()

        self._stack.addWidget(self._video_page)  # index 1

        # Start on the audio page.
        self._stack.setCurrentIndex(0)

    @property
    def art_pixmap(self) -> QPixmap | None:
        return self._original_pixmap

    @property
    def blurred_bg(self) -> QPixmap | None:
        return self._blurred_bg

    @property
    def palette_colors(self) -> list[tuple[int, int, int]]:
        return self._palette

    @property
    def is_video_active(self) -> bool:
        return self._video_active

    @property
    def current_engine(self):
        return self._current_engine

    @current_engine.setter
    def current_engine(self, engine):
        self._current_engine = engine

    @property
    def spectrum_widget(self) -> object:
        """The embedded spectrum visualizer widget."""
        return self._spectrum

    def set_playlists(self, playlists: list[tuple[int, str, int]]) -> None:
        self._saved_playlists = playlists

    def set_show_labels(self, visible: bool) -> None:
        self._show_labels = visible
        self._apply_label_visibility()

    def _apply_label_visibility(self) -> None:
        # Guard: labels may not exist yet during __init__.
        for attr in ("_title_label", "_artist_label", "_album_label"):
            w = getattr(self, attr, None)
            if w is not None:
                w.setVisible(self._show_labels)
        # Genre label is only visible when labels are shown AND there's text.
        genre = getattr(self, "_genre_label", None)
        if genre is not None:
            genre.setVisible(self._show_labels and bool(genre.text()))
        # Plot label is only visible when labels are shown AND there's text.
        plot = getattr(self, "_plot_label", None)
        if plot is not None:
            plot.setVisible(self._show_labels and bool(plot.text()))

    def set_spectrum_visible(self, visible: bool) -> None:
        self._spectrum_visible = visible
        self._spectrum.setVisible(visible)
        if not visible:
            self._spectrum.clear()

    def _reload_config(self) -> None:
        config = self._load_config()
        self._bg_mode = config.nowplaying_bg_mode
        self._tint_alpha = config.nowplaying_tint_alpha
        self._blur_strength = config.nowplaying_blur_strength
        self._bg_opacity = config.nowplaying_bg_opacity
        self._art_size_pct = max(10, min(90, config.nowplaying_art_size))
        self._show_labels = config.nowplaying_show_labels
        self._apply_label_visibility()
        self.set_spectrum_visible(config.nowplaying_show_spectrum)
        self._video_mode = config.youtube_nowplaying_mode
        self._subtitle_overlay.set_font_size(config.subtitle_font_size)
        self._subtitle_overlay.set_outline(config.subtitle_outline)
        self._subtitle_overlay.set_bg_bar(config.subtitle_bg)
        # Migrate old mode names.
        if self._bg_mode in ("gradient", "gradient_vertical"):
            self._bg_mode = "gradient_natural"
        elif self._bg_mode in ("gradient_radial", "gradient_horizontal", "gradient_diagonal"):
            self._bg_mode = "gradient_natural"

    def set_bg_mode(self, mode: str) -> None:
        self._bg_mode = mode
        self.update()

    def set_tint_alpha(self, alpha: int) -> None:
        self._tint_alpha = max(0, min(255, alpha))
        self.update()

    def set_blur_strength(self, strength: int) -> None:
        self._blur_strength = max(1, min(20, strength))
        if self._original_pixmap:
            self._blurred_bg = _blur_pixmap(self._original_pixmap, self._blur_strength)
        self._bg_scaled = None  # Invalidate cache.
        self.update()

    def set_bg_opacity(self, opacity: float) -> None:
        self._bg_opacity = max(0.0, min(1.0, opacity))
        self.update()

    def set_art_size(self, pct: int) -> None:
        self._art_size_pct = max(10, min(90, pct))
        self._rescale_art()
        self.update()

    def update_track(
        self,
        track: Track | None,
        poster_path: str = "",
        plot: str = "",
        genre: str = "",
        watched: bool = False,
    ) -> None:
        self._current_track = track
        self._is_watched = watched
        if track is None:
            self._title_label.setText("\u2014")
            self._artist_label.setText("")
            self._album_label.setText("")
            self._genre_label.setText("")
            self._genre_label.setVisible(False)
            self._plot_label.setText("")
            self._plot_label.setVisible(False)
            self._art_label.clear()
            self._original_pixmap = None
            self._blurred_bg = None
            self._palette = [(30, 30, 30)] * 4
            self._is_youtube_track = False
            self._current_video_url = None
            self._stop_video()
            self.update()
            return

        self._title_label.setText(track.title or "Unknown Title")
        self._artist_label.setText(track.artist or "Unknown Artist")
        album_text = track.album or ""
        if track.year:
            album_text += f" ({track.year})"
        self._album_label.setText(album_text)

        # Genre text for video tracks.
        if genre:
            self._genre_label.setText(genre)
            self._genre_label.setVisible(self._show_labels)
        else:
            self._genre_label.setText("")
            self._genre_label.setVisible(False)

        # Plot text for video tracks.
        if plot:
            self._plot_label.setText(plot)
            self._plot_label.setVisible(self._show_labels)
        else:
            self._plot_label.setText("")
            self._plot_label.setVisible(False)

        self._is_youtube_track = track.filepath.startswith("youtube://")
        self._is_local_video = track.media_type in ("movie", "series_episode")
        if self._is_youtube_track:
            video_id = track.filepath.replace("youtube://", "")
            self._current_video_url = None
            self._load_youtube_thumbnail(video_id)
        elif self._is_local_video:
            self._current_video_url = None
            # Load episode still / poster art.
            if poster_path:
                self._load_art(track.filepath, poster_path=poster_path)
            # Respect video display mode (same as YouTube toggle).
            if self._video_mode == "video" and self._current_engine:
                self._video_surface.start_rendering(self._current_engine)
                self._subtitle_overlay.setGeometry(self._video_page.rect())
                self._subtitle_overlay.start(self._current_engine, self._video_surface)
                self._subtitle_overlay.raise_()
                self._stack.setCurrentIndex(1)
                if not self._video_active:
                    self._video_active = True
                    self.video_active_changed.emit(True)
            else:
                self._stop_video()
                if not poster_path:
                    self._load_art(track.filepath)
        else:
            self._current_video_url = None
            self._stop_video()
            self._load_art(track.filepath)

    def _load_youtube_thumbnail(self, video_id: str) -> None:
        """Load a YouTube video thumbnail as the art display."""
        # Use YouTube's standard thumbnail URL.
        thumb_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
        self._thumb_loader = _ThumbnailLoader(thumb_url, self)
        self._thumb_loader.loaded.connect(self._on_thumbnail_loaded)
        self._thumb_loader.start()

    def _on_thumbnail_loaded(self, pixmap: QPixmap) -> None:
        """Handle downloaded YouTube thumbnail."""
        self._original_pixmap = pixmap
        self._palette = _extract_palette(pixmap)
        self._blurred_bg = _blur_pixmap(pixmap, self._blur_strength)
        self._bg_scaled = None  # Invalidate cache.
        self._rescale_art()
        self.update()

    def set_video_url(self, url: str) -> None:
        """Store the resolved video URL for mode-switching."""
        self._current_video_url = url

    def play_video(self, video_url: str, engine=None) -> None:
        """Switch to the video page and start pulling frames."""
        self._current_video_url = video_url
        if engine is not None:
            self._current_engine = engine
        if self._video_mode != "video":
            return
        self._video_surface.start_rendering(self._current_engine)
        self._subtitle_overlay.setGeometry(self._video_page.rect())
        self._subtitle_overlay.start(self._current_engine, self._video_surface)
        self._subtitle_overlay.raise_()
        self._stack.setCurrentIndex(1)  # Switch to video page.
        if not self._video_active:
            self._video_active = True
            self.video_active_changed.emit(True)

    def stop_video(self) -> None:
        """Switch back to the audio page (external API)."""
        self._stop_video()

    def _stop_video(self) -> None:
        was_active = self._video_active
        if self._video_active:
            self._video_surface.stop_rendering()
            self._subtitle_overlay.stop()
            self._video_active = False
        self._stack.setCurrentIndex(0)  # Switch to audio page.
        if was_active:
            self.video_active_changed.emit(False)

    def pause_video_rendering(self) -> None:
        """Pause inline video rendering (e.g. when fullscreen overlay takes over)."""
        if self._video_active:
            self._video_surface.stop_rendering()
            self._subtitle_overlay.pause()

    def resume_video_rendering(self) -> None:
        """Resume inline video rendering (e.g. when fullscreen overlay closes)."""
        if self._video_active and self._current_engine:
            self._video_surface.start_rendering(self._current_engine)
            self._subtitle_overlay.resume()

    def clear_subtitles(self) -> None:
        """Clear any displayed subtitle text (e.g. after a seek)."""
        self._subtitle_overlay.clear()

    def set_video_mode(self, mode: str) -> None:
        """Switch between 'thumbnail' and 'video' display modes.

        Emits youtube_mode_changed so main_window can orchestrate the
        stop → sink swap → play sequence correctly.
        """
        self._video_mode = mode
        self.youtube_mode_changed.emit(mode)

    def _load_art(self, filepath: str, poster_path: str = "") -> None:
        # For video tracks with a poster (episode still), prefer it over
        # embedded art which is typically just the generic show poster.
        art_data = None
        if poster_path:
            art_data = self._poster_reader(poster_path)
        if not art_data:
            art_data = _extract_art(filepath, prefer_hires=True)
        if art_data:
            pixmap = QPixmap()
            pixmap.loadFromData(art_data)
            self._original_pixmap = pixmap
            self._palette = _extract_palette(pixmap)
            self._blurred_bg = _blur_pixmap(pixmap, self._blur_strength)
            self._bg_scaled = None  # Invalidate cache.
            self._rescale_art()
        else:
            self._original_pixmap = None
            self._blurred_bg = None
            self._bg_scaled = None
            self._palette = [(30, 30, 30)] * 4
            self._art_label.clear()
            self._art_label.setText("No Art")
        self.update()

    def _rescale_art(self) -> None:
        if not self._original_pixmap:
            return
        # Derive art size from the panel dimensions and the configured percentage.
        panel_w = self.width() - 40   # account for layout margins
        panel_h = self.height() - 40
        target = int(min(panel_w, panel_h) * self._art_size_pct / 100)
        size = max(_ART_MIN, target)
        scaled = self._original_pixmap.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._art_label.setFixedSize(scaled.size())
        self._art_label.setPixmap(scaled)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.fullscreen_requested.emit()
        else:
            super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta > 0:
            self.volume_change_requested.emit(0.05)
        elif delta < 0:
            self.volume_change_requested.emit(-0.05)
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._bg_scaled = None  # Invalidate — panel size changed.
        self._rescale_art()
        # Keep subtitle overlay covering the full video page.
        self._subtitle_overlay.setGeometry(self._video_page.rect())

    # ------------------------------------------------------------------
    # Background painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()

        # Fullscreen video: solid black for proper letterbox/pillarbox bars.
        if self._video_active and self.window().isFullScreen():
            painter.fillRect(rect, QColor(0, 0, 0))
            painter.end()
            return

        alpha = self._tint_alpha

        if self._bg_mode == "blur":
            # Blur uses painter opacity for the blurred image layer.
            painter.setOpacity(self._bg_opacity)
            self._paint_blur(painter, rect, alpha)
            painter.setOpacity(1.0)
        elif self._bg_mode.startswith("gradient_"):
            # Gradient bakes opacity into color alpha directly.
            self._paint_gradient(painter, rect, alpha, self._bg_mode)
        elif self._bg_mode == "transparent":
            pass
        else:  # "solid"
            a = int(self._bg_opacity * 255)
            painter.fillRect(rect, QColor(30, 30, 30, a))

        painter.end()

    def _paint_blur(self, painter: QPainter, rect, alpha: int) -> None:
        if self._blurred_bg and not self._blurred_bg.isNull():
            # Cache the scaled pixmap — rescaling every paint is expensive at
            # fullscreen resolution.
            if self._bg_scaled is None or self._bg_scaled.size() != rect.size():
                scaled = self._blurred_bg.scaled(
                    rect.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.FastTransformation,
                )
                # Crop to exact panel size (KeepAspectRatioByExpanding overshoots).
                x = (scaled.width() - rect.width()) // 2
                y = (scaled.height() - rect.height()) // 2
                self._bg_scaled = scaled.copy(x, y, rect.width(), rect.height())
            painter.drawPixmap(0, 0, self._bg_scaled)
            if alpha > 0:
                painter.fillRect(rect, QColor(0, 0, 0, alpha))
        else:
            painter.fillRect(rect, QColor(30, 30, 30, int(self._bg_opacity * 255)))

    def _paint_gradient(self, painter, rect, alpha: int, mode: str) -> None:
        """Paint a vertical gradient with color treatment based on mode.

        The gradient colors carry alpha derived from bg_opacity so that
        window transparency / compositor blur shows through.
        """
        colors = self._palette[:]

        if mode == "gradient_vivid":
            colors = [_boost_color(*c, saturation_boost=1.8, value_boost=1.4) for c in colors]
        elif mode == "gradient_warm":
            colors = [_shift_warm(*c) for c in colors]
        elif mode == "gradient_cool":
            colors = [_shift_cool(*c) for c in colors]
        elif mode == "gradient_pastel":
            colors = [_pastel(*c) for c in colors]
        # "gradient_natural" uses palette as-is.

        # Bake opacity into the color alpha so compositor blur shows through.
        color_alpha = int(self._bg_opacity * 255)

        grad = QLinearGradient(0, 0, 0, rect.height())
        grad.setColorAt(0.0, QColor(*colors[0], color_alpha))
        grad.setColorAt(0.33, QColor(*colors[1], color_alpha))
        grad.setColorAt(0.66, QColor(*colors[2], color_alpha))
        grad.setColorAt(1.0, QColor(*colors[3], color_alpha))
        painter.fillRect(rect, grad)

        # Tint overlay (also respects opacity).
        if alpha > 0:
            tint_a = int(alpha * self._bg_opacity)
            painter.fillRect(rect, QColor(0, 0, 0, tint_a))

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def contextMenuEvent(self, event) -> None:
        self._show_context_menu(event.globalPos())

    def _show_context_menu(self, global_pos) -> None:
        # Non-blocking popup so video keeps rendering.
        self._active_menu = menu = QMenu()
        track = self._current_track
        has_track = track is not None

        # ── Track actions (top level) ────────────────────────────
        if has_track:
            is_video = track.media_type in ("movie", "series_episode")

            # Video stream selection (audio tracks / subtitles).
            if (is_video or self._is_youtube_track) and self._current_engine:
                from media_core.ui.widgets.queue_widget import build_stream_menus
                build_stream_menus(menu, self._current_engine)

            # Mark as Watched / Unwatched (video only).
            if is_video and track.id >= 0:
                if self._is_watched:
                    w_action = QAction("Mark as Unwatched", self)
                    w_action.triggered.connect(
                        lambda: self.mark_watched_requested.emit([track.id], False)
                    )
                else:
                    w_action = QAction("Mark as Watched", self)
                    w_action.triggered.connect(
                        lambda: self.mark_watched_requested.emit([track.id], True)
                    )
                menu.addAction(w_action)

            # Video display mode (YouTube + local video).
            if self._is_youtube_track or self._is_local_video:
                vid_menu = menu.addMenu("Video Display")
                for mode_key, mode_label in [("thumbnail", "Thumbnail"), ("video", "Video Player")]:
                    action = QAction(mode_label, self)
                    action.setCheckable(True)
                    action.setChecked(self._video_mode == mode_key)
                    action.triggered.connect(
                        lambda checked, m=mode_key: self._set_and_save(
                            "youtube_nowplaying_mode", m, self.set_video_mode
                        )
                    )
                    vid_menu.addAction(action)

            # Add to Saved Playlist.
            if track.id >= 0 and self._saved_playlists:
                menu.addSeparator()
                saved_menu = menu.addMenu("Add to Saved Playlist")
                for pid, pname, _ in self._saved_playlists:
                    action = QAction(pname, self)
                    action.triggered.connect(
                        lambda checked, _pid=pid: self.add_to_saved_playlist.emit(
                            [track.id], _pid
                        )
                    )
                    saved_menu.addAction(action)

            # Edit Tags (music and video with real DB IDs, not transient).
            if track.id >= 0 and not self._is_youtube_track:
                edit_action = QAction("Edit Tags\u2026", self)
                edit_action.triggered.connect(
                    lambda: self.edit_tags_requested.emit([track.id])
                )
                menu.addAction(edit_action)

        # ── Appearance (submenu) ─────────────────────────────────
        menu.addSeparator()
        appear_menu = menu.addMenu("Appearance")

        bg_menu = appear_menu.addMenu("Background")
        for mode_key, mode_label in _BG_MODES:
            action = QAction(mode_label, self)
            action.setCheckable(True)
            action.setChecked(self._bg_mode == mode_key)
            action.triggered.connect(lambda checked, m=mode_key: self._set_and_save("nowplaying_bg_mode", m, self.set_bg_mode))
            bg_menu.addAction(action)

        intensity_menu = appear_menu.addMenu("Overlay Intensity")
        for label, value in _INTENSITY_PRESETS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self._tint_alpha == value)
            action.triggered.connect(lambda checked, v=value: self._set_and_save("nowplaying_tint_alpha", v, self.set_tint_alpha))
            intensity_menu.addAction(action)

        if self._bg_mode == "blur":
            blur_menu = appear_menu.addMenu("Blur Strength")
            for label, value in _BLUR_PRESETS:
                action = QAction(label, self)
                action.setCheckable(True)
                action.setChecked(self._blur_strength == value)
                action.triggered.connect(lambda checked, v=value: self._set_and_save("nowplaying_blur_strength", v, self.set_blur_strength))
                blur_menu.addAction(action)

        opacity_menu = appear_menu.addMenu("Background Opacity")
        for label, value in _OPACITY_PRESETS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(abs(self._bg_opacity - value) < 0.05)
            action.triggered.connect(lambda checked, v=value: self._set_and_save("nowplaying_bg_opacity", v, self.set_bg_opacity))
            opacity_menu.addAction(action)

        art_menu = appear_menu.addMenu("Art Size")
        for label, value in _ART_SIZE_PRESETS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self._art_size_pct == value)
            action.triggered.connect(lambda checked, v=value: self._set_and_save("nowplaying_art_size", v, self.set_art_size))
            art_menu.addAction(action)

        labels_action = QAction("Show Track Info", self)
        labels_action.setCheckable(True)
        labels_action.setChecked(self._show_labels)
        labels_action.triggered.connect(
            lambda checked: self._set_and_save("nowplaying_show_labels", checked, self.set_show_labels)
        )
        appear_menu.addAction(labels_action)

        vis_action = QAction("Show Visualizer", self)
        vis_action.setCheckable(True)
        vis_action.setChecked(self._spectrum_visible)
        vis_action.triggered.connect(
            lambda checked: self._set_and_save("nowplaying_show_spectrum", checked, self.set_spectrum_visible)
        )
        appear_menu.addAction(vis_action)

        # ── Host-injected actions (e.g. Minim's "Minimal Interface" toggle) ──
        if self._extra_context_actions is not None:
            try:
                items = self._extra_context_actions() or []
            except Exception:
                items = []
            if items:
                menu.addSeparator()
                for label, callback in items:
                    action = QAction(label, self)
                    action.triggered.connect(callback)
                    menu.addAction(action)

        menu.popup(global_pos)

    def _set_and_save(self, config_key: str, value, setter) -> None:
        setter(value)
        config = self._load_config()
        setattr(config, config_key, value)
        self._save_config(config)


