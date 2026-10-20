"""Shared media-engine config — no PySide6 imports, no library deps.

:class:`PlayerConfig` describes everything the engine + transport +
Now Playing surface need to know about: playback behavior, EQ/DSP,
Now Playing appearance, application chrome, subtitle rendering.

Both shells consume this:

* Quaver embeds it inside its ``AppConfig`` (which also adds
  ``LibraryConfig`` for sources/tabs/scrapers/podcasts/YouTube/AutoDJ).
* Minim instantiates it directly — it has no library settings.

TOML serialization currently lives in Quaver's ``core/config.py``.
Once Minim grows its own config layer, the TOML round-trip for the
shared section can move here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlayerConfig:
    """Settings owned by the playback engine + Now Playing chrome.

    These apply equally to a library shell (Quaver) and a thin player
    shell (Minim) — they describe how playback behaves and how the
    player surface looks, not what's in the library.
    """
    # Playback.
    restore_queue: bool = True  # Restore queue contents on next startup.
    replaygain_mode: str = "off"  # off, track, album
    playback_speed: float = 1.0
    eq_bands: list[float] = field(default_factory=lambda: [0.0] * 10)
    compressor_enabled: bool = False
    compressor_ratio: float = 4.0
    compressor_threshold: float = 0.5
    # Now Playing appearance.
    nowplaying_bg_mode: str = "blur"  # blur, gradient_natural, gradient_vivid, gradient_warm, gradient_cool, gradient_pastel, solid, transparent
    nowplaying_tint_alpha: int = 140  # 0–255, overlay darkness for blur/gradient modes
    nowplaying_blur_strength: int = 10  # 1–20, blur radius for blur mode
    nowplaying_bg_opacity: float = 1.0  # 0.0–1.0, panel background transparency
    nowplaying_art_size: int = 60        # 10–90, percentage of panel height for album art
    nowplaying_show_labels: bool = True  # Show title/artist/album text on Now Playing panel
    nowplaying_show_spectrum: bool = True  # Show spectrum visualizer on Now Playing panel
    # Application chrome.
    window_opacity: float = 1.0       # 0.0–1.0
    blur_behind: bool = True           # KDE blur-behind (requires compositor)
    bg_color: list[int] = field(default_factory=lambda: [30, 30, 30])  # RGB
    bg_tint_strength: int = 70         # 0–100, how opaque the color overlay is on top of blur
    # Subtitle rendering.
    subtitle_font_size: int = 24   # 12–48 pt base size (scales with video height)
    subtitle_outline: bool = True  # Black outline around subtitle text
    subtitle_bg: bool = False      # Semi-transparent background bar behind text
    # Now Playing video render mode — applies to both YouTube and local
    # video.  Legacy field name (originally YouTube-only) preserved to
    # avoid migrating user TOML files; Quaver still serializes it under
    # the ``[youtube]`` section for backwards compatibility.
    youtube_nowplaying_mode: str = "video"  # "video" or "thumbnail"
