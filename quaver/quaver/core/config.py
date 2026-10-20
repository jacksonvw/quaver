"""Quaver application configuration — TOML-based, no PySide6 imports.

Combines :class:`media_core.core.config.PlayerConfig` (shared with
Minim) with :class:`LibraryConfig` (Quaver-only — sources, tabs,
scrapers, podcasts, YouTube, AutoDJ).  ``AppConfig`` inherits from
both, so every existing call site (``cfg.eq_bands``, ``cfg.sources``,
…) keeps working unchanged.

TOML round-trip (``load_config``/``save_config``) lives here because
Quaver's TOML layout includes both halves — the shared player section
and the library-only sections.  When Minim grows its own config layer
it'll re-implement TOML serialization for ``PlayerConfig`` only.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from media_core.core.config import PlayerConfig
from quaver.core.paths import config_dir

_CONFIG_PATH = config_dir() / "config.toml"


@dataclass
class SourceConfig:
    """Per-source configuration — each source owns its scraper and scan mode."""
    path: str
    uri: str = ""               # SMB URI (empty = local)
    scraper: str = "none"       # none, tvmaze, tmdb
    scan_mode: str = "multi"    # multi, single, deep
    media_type: str = "music"   # music, series, movies


@dataclass
class LibraryTabConfig:
    name: str
    source_paths: list[str] = field(default_factory=list)  # references SourceConfig.path
    type: str = "music"  # music, series, movies, files — determines UI view
    # ``files`` tabs only — hide file extensions in the grid display
    # (``my movie.mp4`` → ``my movie``).  Ignored for other tab types.
    hide_extensions: bool = False


@dataclass
class LibraryConfig:
    """Settings exclusive to a library shell — sources, scrapers, feeds.

    Minim never instantiates this; it has no library, no scanning, no
    podcasts, no YouTube subscriptions.
    """
    sources: list[SourceConfig] = field(default_factory=list)
    library_tabs: list[LibraryTabConfig] = field(default_factory=list)
    # Library views.
    show_feed_urls: bool = True        # Show feed/channel URLs as subtitles in podcast/YouTube lists
    show_genre_in_headers: bool = True  # Show genre text in album/movie/series headers
    # Built-in tab visibility — users can hide the Playlists / Podcasts /
    # YouTube / Now Playing tabs that Quaver creates by default if they
    # don't use those features.  Library tabs (configured under
    # ``[[library_tabs]]``) are always shown — toggle them by removing
    # them from the config in the Library Tabs dialog.
    show_playlists_tab: bool = True
    show_podcasts_tab: bool = True
    show_youtube_tab: bool = True
    show_nowplaying_tab: bool = True
    # Podcasts.
    podcast_default_retain: int = 2
    podcast_default_refresh_hours: int = 24
    # YouTube.
    youtube_auth_browser: str = ""  # empty = guest, "firefox", "chromium", etc.
    youtube_video_quality: str = "best"  # best, 1080p, 720p, 480p, audio_only
    youtube_default_refresh_hours: int = 24
    # Video scraper.
    tmdb_api_key: str = ""
    auto_enrich: bool = True  # Auto-fetch metadata after scanning
    # AutoDJ.
    autodj_avoid_hours: int = 4
    autodj_genre_filter: str = ""
    autodj_lookahead: int = 5
    autodj_tab_names: list[str] = field(default_factory=list)  # empty = all library


@dataclass
class AppConfig(LibraryConfig, PlayerConfig):
    """Combined config for the Quaver library shell.

    Inherits every field from both parents, so existing call sites that
    read ``cfg.replaygain_mode`` or ``cfg.sources`` work unchanged.  When
    Minim is built it constructs a ``PlayerConfig`` directly — no library
    fields involved.
    """
    pass


def load_config(path: Path | None = None) -> AppConfig:
    """Load config from TOML. Returns defaults if file doesn't exist."""
    p = path or _CONFIG_PATH
    if not p.exists():
        return AppConfig()
    with open(p, "rb") as f:
        data = tomllib.load(f)

    # -- Parse sources (top-level or migrate from tabs) --
    all_sources: list[SourceConfig] = []
    tabs: list[LibraryTabConfig] = []
    migrated = False
    seen_paths: set[str] = set()

    if "sources" in data and isinstance(data["sources"], list):
        # Current format: top-level [[sources]] + tabs with source_paths.
        for s in data["sources"]:
            src = SourceConfig(
                path=s.get("path", ""),
                uri=s.get("uri", ""),
                scraper=s.get("scraper", "none"),
                scan_mode=s.get("scan_mode", "multi"),
                media_type=s.get("media_type", "music"),
            )
            if src.path and src.path not in seen_paths:
                all_sources.append(src)
                seen_paths.add(src.path)
        for t in data.get("library_tabs", []):
            tabs.append(LibraryTabConfig(
                name=t.get("name", "Library"),
                source_paths=list(t.get("source_paths", [])),
                type=t.get("type", "music"),
                hide_extensions=bool(t.get("hide_extensions", False)),
            ))
    else:
        # Migration: extract sources from tabs (nested or old folders format).
        migrated = True
        for t in data.get("library_tabs", []):
            tab_type = t.get("type", "music")
            tab_paths: list[str] = []
            if "sources" in t and isinstance(t["sources"], list):
                # Nested SourceConfig dicts inside tab.
                for s in t["sources"]:
                    path = s.get("path", "")
                    if path and path not in seen_paths:
                        all_sources.append(SourceConfig(
                            path=path,
                            uri=s.get("uri", ""),
                            scraper=s.get("scraper", "none"),
                            scan_mode=s.get("scan_mode", "multi"),
                            media_type=tab_type,
                        ))
                        seen_paths.add(path)
                    if path:
                        tab_paths.append(path)
            elif "folders" in t:
                # Oldest format: folders + tab-level metadata_source + uri_map.
                uri_map = dict(t.get("uri_map", {}))
                scraper = t.get("metadata_source", "none")
                for folder in t.get("folders", []):
                    if folder and folder not in seen_paths:
                        all_sources.append(SourceConfig(
                            path=folder,
                            uri=uri_map.get(folder, ""),
                            scraper=scraper,
                            scan_mode="multi",
                            media_type=tab_type,
                        ))
                        seen_paths.add(folder)
                    if folder:
                        tab_paths.append(folder)
            tabs.append(LibraryTabConfig(
                name=t.get("name", "Library"),
                source_paths=tab_paths,
                type=tab_type,
            ))

    playback = data.get("playback", {})
    appearance = data.get("appearance", {})
    podcasts = data.get("podcasts", {})
    youtube = data.get("youtube", {})
    autodj = data.get("autodj", {})
    video = data.get("video", {})
    tmdb = data.get("tmdb", {})  # legacy — migrated to [video]
    cfg = AppConfig(
        sources=all_sources,
        library_tabs=tabs,
        restore_queue=bool(playback.get("restore_queue", True)),
        replaygain_mode=playback.get("replaygain_mode", "off"),
        playback_speed=float(playback.get("playback_speed", 1.0)),
        eq_bands=list(playback.get("eq_bands", [0.0] * 10)),
        compressor_enabled=bool(playback.get("compressor_enabled", False)),
        compressor_ratio=float(playback.get("compressor_ratio", 4.0)),
        compressor_threshold=float(playback.get("compressor_threshold", 0.5)),
        nowplaying_bg_mode=appearance.get("nowplaying_bg_mode", "blur"),
        nowplaying_tint_alpha=int(appearance.get("nowplaying_tint_alpha", 140)),
        nowplaying_blur_strength=int(appearance.get("nowplaying_blur_strength", 10)),
        nowplaying_bg_opacity=float(appearance.get("nowplaying_bg_opacity", 1.0)),
        nowplaying_art_size=int(appearance.get("nowplaying_art_size", 60)),
        nowplaying_show_labels=bool(appearance.get("nowplaying_show_labels", True)),
        nowplaying_show_spectrum=bool(appearance.get("nowplaying_show_spectrum", True)),
        window_opacity=float(appearance.get("window_opacity", 1.0)),
        blur_behind=bool(appearance.get("blur_behind", True)),
        bg_color=list(appearance.get("bg_color", [30, 30, 30])),
        bg_tint_strength=int(appearance.get("bg_tint_strength", 70)),
        show_feed_urls=bool(appearance.get("show_feed_urls", True)),
        show_genre_in_headers=bool(appearance.get("show_genre_in_headers", True)),
        show_playlists_tab=bool(appearance.get("show_playlists_tab", True)),
        show_podcasts_tab=bool(appearance.get("show_podcasts_tab", True)),
        show_youtube_tab=bool(appearance.get("show_youtube_tab", True)),
        show_nowplaying_tab=bool(appearance.get("show_nowplaying_tab", True)),
        podcast_default_retain=int(podcasts.get("default_retain", 2)),
        podcast_default_refresh_hours=int(podcasts.get("default_refresh_hours", 24)),
        youtube_auth_browser=str(youtube.get("auth_browser", "")),
        youtube_video_quality=str(youtube.get("video_quality", "best")),
        youtube_default_refresh_hours=int(youtube.get("default_refresh_hours", 24)),
        youtube_nowplaying_mode=str(youtube.get("nowplaying_mode", "video")),
        tmdb_api_key=str(video.get("tmdb_api_key", tmdb.get("api_key", ""))),
        auto_enrich=bool(video.get("auto_enrich", True)),
        subtitle_font_size=int(video.get("subtitle_font_size", 24)),
        subtitle_outline=bool(video.get("subtitle_outline", True)),
        subtitle_bg=bool(video.get("subtitle_bg", False)),
        autodj_avoid_hours=int(autodj.get("avoid_hours", 4)),
        autodj_genre_filter=str(autodj.get("genre_filter", "")),
        autodj_lookahead=int(autodj.get("lookahead", 5)),
        autodj_tab_names=list(autodj.get("tab_names", [])),
    )

    # Persist migration from old folders format to new sources format.
    if migrated:
        save_config(cfg, p)

    return cfg


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Write config to TOML."""
    p = path or _CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}

    # Top-level sources.
    if config.sources:
        source_dicts = []
        for s in config.sources:
            sd: dict = {"path": s.path}
            if s.uri:
                sd["uri"] = s.uri
            if s.scraper != "none":
                sd["scraper"] = s.scraper
            if s.scan_mode != "multi":
                sd["scan_mode"] = s.scan_mode
            if s.media_type != "music":
                sd["media_type"] = s.media_type
            source_dicts.append(sd)
        data["sources"] = source_dicts

    # Library tabs (reference sources by path).
    if config.library_tabs:
        tab_dicts = []
        for t in config.library_tabs:
            entry: dict = {
                "name": t.name,
                "type": t.type,
                "source_paths": list(t.source_paths),
            }
            # Files-tab specific — only emit when set, to keep the
            # TOML clean for music/series/movies tabs.
            if t.hide_extensions:
                entry["hide_extensions"] = True
            tab_dicts.append(entry)
        data["library_tabs"] = tab_dicts

    data["playback"] = {
        "restore_queue": config.restore_queue,
        "replaygain_mode": config.replaygain_mode,
        "playback_speed": config.playback_speed,
        "eq_bands": config.eq_bands,
        "compressor_enabled": config.compressor_enabled,
        "compressor_ratio": config.compressor_ratio,
        "compressor_threshold": config.compressor_threshold,
    }

    data["appearance"] = {
        "nowplaying_bg_mode": config.nowplaying_bg_mode,
        "nowplaying_tint_alpha": config.nowplaying_tint_alpha,
        "nowplaying_blur_strength": config.nowplaying_blur_strength,
        "nowplaying_bg_opacity": config.nowplaying_bg_opacity,
        "nowplaying_art_size": config.nowplaying_art_size,
        "nowplaying_show_labels": config.nowplaying_show_labels,
        "nowplaying_show_spectrum": config.nowplaying_show_spectrum,
        "window_opacity": config.window_opacity,
        "blur_behind": config.blur_behind,
        "bg_color": config.bg_color,
        "bg_tint_strength": config.bg_tint_strength,
        "show_feed_urls": config.show_feed_urls,
        "show_genre_in_headers": config.show_genre_in_headers,
        "show_playlists_tab": config.show_playlists_tab,
        "show_podcasts_tab": config.show_podcasts_tab,
        "show_youtube_tab": config.show_youtube_tab,
        "show_nowplaying_tab": config.show_nowplaying_tab,
    }

    data["podcasts"] = {
        "default_retain": config.podcast_default_retain,
        "default_refresh_hours": config.podcast_default_refresh_hours,
    }

    data["youtube"] = {
        "auth_browser": config.youtube_auth_browser,
        "video_quality": config.youtube_video_quality,
        "default_refresh_hours": config.youtube_default_refresh_hours,
        "nowplaying_mode": config.youtube_nowplaying_mode,
    }

    data["video"] = {
        "tmdb_api_key": config.tmdb_api_key,
        "auto_enrich": config.auto_enrich,
        "subtitle_font_size": config.subtitle_font_size,
        "subtitle_outline": config.subtitle_outline,
        "subtitle_bg": config.subtitle_bg,
    }

    data["autodj"] = {
        "avoid_hours": config.autodj_avoid_hours,
        "genre_filter": config.autodj_genre_filter,
        "lookahead": config.autodj_lookahead,
        "tab_names": config.autodj_tab_names,
    }

    with open(p, "wb") as f:
        tomli_w.dump(data, f)
