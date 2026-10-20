"""Minim config TOML round-trip — every PlayerConfig field survives
write/read with no mutation, and the section layout matches Quaver's
on-disk format (so future shared loaders read both apps unchanged).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import minim  # noqa: F401  (sys.path bootstrap)

from media_core.core.config import PlayerConfig
from minim.core.config import config_path, load_config, save_config


def test_default_when_file_missing(tmp_path: Path) -> None:
    p = tmp_path / "missing.toml"
    cfg = load_config(p)
    assert isinstance(cfg, PlayerConfig)
    # Defaults match the dataclass.
    assert cfg.replaygain_mode == "off"
    assert cfg.eq_bands == [0.0] * 10
    assert cfg.nowplaying_bg_mode == "blur"


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    p = tmp_path / "out.toml"
    cfg = PlayerConfig(
        restore_queue=False,
        replaygain_mode="album",
        playback_speed=1.25,
        eq_bands=[1.0, -2.0, 3.0, 4.5, -5.0, 6.0, 0.0, 0.0, 0.0, 0.0],
        compressor_enabled=True,
        compressor_ratio=3.0,
        compressor_threshold=0.4,
        nowplaying_bg_mode="gradient_vivid",
        nowplaying_tint_alpha=200,
        nowplaying_blur_strength=15,
        nowplaying_bg_opacity=0.8,
        nowplaying_art_size=75,
        nowplaying_show_labels=False,
        nowplaying_show_spectrum=False,
        window_opacity=0.85,
        blur_behind=False,
        bg_color=[42, 84, 168],
        bg_tint_strength=55,
        subtitle_font_size=30,
        subtitle_outline=False,
        subtitle_bg=True,
        youtube_nowplaying_mode="thumbnail",
    )
    save_config(cfg, p)
    out = load_config(p)
    # Every field round-trips identically.
    assert out == cfg


def test_section_layout_matches_quaver(tmp_path: Path) -> None:
    """Verifies the on-disk TOML uses the same section names Quaver uses
    so a future shared loader can read either app's file unchanged.
    """
    p = tmp_path / "layout.toml"
    save_config(PlayerConfig(), p)
    with open(p, "rb") as f:
        data = tomllib.load(f)
    # Match Quaver's [playback] / [appearance] / [video] sectioning.
    assert "playback" in data
    assert "appearance" in data
    assert "video" in data
    # No library/podcast/youtube/autodj sections — Minim is library-less.
    assert "sources" not in data
    assert "library_tabs" not in data
    assert "podcasts" not in data
    assert "autodj" not in data


def test_video_mode_uses_nowplaying_mode_key(tmp_path: Path) -> None:
    """The legacy ``youtube_nowplaying_mode`` field round-trips under
    the friendly ``[video] nowplaying_mode`` key in Minim's TOML.
    """
    p = tmp_path / "video.toml"
    cfg = PlayerConfig(youtube_nowplaying_mode="thumbnail")
    save_config(cfg, p)
    with open(p, "rb") as f:
        data = tomllib.load(f)
    assert data["video"]["nowplaying_mode"] == "thumbnail"


def test_default_path_under_xdg_config(monkeypatch, tmp_path: Path) -> None:
    """``config_path()`` must resolve under the per-user config dir.

    We don't assert the exact path (it varies by platform) — only that
    it ends with ``minim/config.toml`` and the parent exists.
    """
    p = config_path()
    assert p.name == "config.toml"
    assert p.parent.name == "minim"
    # AppPaths creates the directory on access.
    assert p.parent.is_dir()
