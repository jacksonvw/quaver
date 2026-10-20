"""Minim application configuration — TOML-based, PlayerConfig only.

Minim has no library, podcasts, or YouTube subscriptions, so this layer
serializes the shared :class:`media_core.core.config.PlayerConfig`
straight to TOML — no extra fields.  The on-disk layout deliberately
matches Quaver's ``[playback]`` / ``[appearance]`` / ``[video]`` section
naming so a future shared loader can be lifted into ``media_core``
without touching either app.

Config lives at ``~/.config/minim/config.toml`` (XDG on Linux, the
platform equivalent elsewhere via :class:`AppPaths`).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w

from media_core.core.config import PlayerConfig
from media_core.core.paths import AppPaths


_PATHS = AppPaths("minim")


def config_path() -> Path:
    """Return the absolute path to ``~/.config/minim/config.toml``."""
    return _PATHS.config_dir() / "config.toml"


def load_config(path: Path | None = None) -> PlayerConfig:
    """Load Minim config from TOML.  Returns defaults if missing."""
    p = path or config_path()
    if not p.exists():
        return PlayerConfig()
    with open(p, "rb") as f:
        data = tomllib.load(f)

    playback = data.get("playback", {})
    appearance = data.get("appearance", {})
    video = data.get("video", {})

    return PlayerConfig(
        # Playback.
        restore_queue=bool(playback.get("restore_queue", True)),
        replaygain_mode=str(playback.get("replaygain_mode", "off")),
        playback_speed=float(playback.get("playback_speed", 1.0)),
        eq_bands=list(playback.get("eq_bands", [0.0] * 10)),
        compressor_enabled=bool(playback.get("compressor_enabled", False)),
        compressor_ratio=float(playback.get("compressor_ratio", 4.0)),
        compressor_threshold=float(playback.get("compressor_threshold", 0.5)),
        # Now Playing appearance.
        nowplaying_bg_mode=str(appearance.get("nowplaying_bg_mode", "blur")),
        nowplaying_tint_alpha=int(appearance.get("nowplaying_tint_alpha", 140)),
        nowplaying_blur_strength=int(appearance.get("nowplaying_blur_strength", 10)),
        nowplaying_bg_opacity=float(appearance.get("nowplaying_bg_opacity", 1.0)),
        nowplaying_art_size=int(appearance.get("nowplaying_art_size", 60)),
        nowplaying_show_labels=bool(appearance.get("nowplaying_show_labels", True)),
        nowplaying_show_spectrum=bool(appearance.get("nowplaying_show_spectrum", True)),
        # Window chrome.
        window_opacity=float(appearance.get("window_opacity", 1.0)),
        blur_behind=bool(appearance.get("blur_behind", True)),
        bg_color=list(appearance.get("bg_color", [30, 30, 30])),
        bg_tint_strength=int(appearance.get("bg_tint_strength", 70)),
        # Subtitles.
        subtitle_font_size=int(video.get("subtitle_font_size", 24)),
        subtitle_outline=bool(video.get("subtitle_outline", True)),
        subtitle_bg=bool(video.get("subtitle_bg", False)),
        # Now Playing video render mode (legacy field name kept for
        # cross-app TOML compatibility).
        youtube_nowplaying_mode=str(video.get("nowplaying_mode", "video")),
    )


def save_config(config: PlayerConfig, path: Path | None = None) -> None:
    """Write Minim config to TOML."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "playback": {
            "restore_queue": config.restore_queue,
            "replaygain_mode": config.replaygain_mode,
            "playback_speed": config.playback_speed,
            "eq_bands": list(config.eq_bands),
            "compressor_enabled": config.compressor_enabled,
            "compressor_ratio": config.compressor_ratio,
            "compressor_threshold": config.compressor_threshold,
        },
        "appearance": {
            "nowplaying_bg_mode": config.nowplaying_bg_mode,
            "nowplaying_tint_alpha": config.nowplaying_tint_alpha,
            "nowplaying_blur_strength": config.nowplaying_blur_strength,
            "nowplaying_bg_opacity": config.nowplaying_bg_opacity,
            "nowplaying_art_size": config.nowplaying_art_size,
            "nowplaying_show_labels": config.nowplaying_show_labels,
            "nowplaying_show_spectrum": config.nowplaying_show_spectrum,
            "window_opacity": config.window_opacity,
            "blur_behind": config.blur_behind,
            "bg_color": list(config.bg_color),
            "bg_tint_strength": config.bg_tint_strength,
        },
        "video": {
            "subtitle_font_size": config.subtitle_font_size,
            "subtitle_outline": config.subtitle_outline,
            "subtitle_bg": config.subtitle_bg,
            "nowplaying_mode": config.youtube_nowplaying_mode,
        },
    }

    with open(p, "wb") as f:
        tomli_w.dump(data, f)
