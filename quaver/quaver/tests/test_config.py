"""Config TOML round-trip + dataclass split."""

from quaver.core.config import (
    AppConfig,
    LibraryConfig,
    PlayerConfig,
    SourceConfig,
    load_config,
    save_config,
)


def test_player_config_standalone() -> None:
    """PlayerConfig must be usable without library settings (Minim case)."""
    pc = PlayerConfig()
    # Player-side fields are present.
    assert pc.replaygain_mode == "off"
    assert pc.eq_bands == [0.0] * 10
    assert pc.nowplaying_bg_mode == "blur"
    assert pc.subtitle_font_size == 24
    # Now Playing video display mode (used by both YouTube and local video).
    assert pc.youtube_nowplaying_mode == "video"
    # Library-side fields must NOT be present on the player-only config.
    assert not hasattr(pc, "sources")
    assert not hasattr(pc, "library_tabs")
    assert not hasattr(pc, "tmdb_api_key")
    assert not hasattr(pc, "podcast_default_retain")


def test_library_config_standalone() -> None:
    lc = LibraryConfig()
    assert lc.sources == []
    assert lc.library_tabs == []
    assert lc.podcast_default_retain == 2
    # Player-side fields must NOT be present on the library-only config.
    assert not hasattr(lc, "replaygain_mode")
    assert not hasattr(lc, "eq_bands")
    assert not hasattr(lc, "nowplaying_bg_mode")
    # youtube_nowplaying_mode is a Now Playing render setting → PlayerConfig.
    assert not hasattr(lc, "youtube_nowplaying_mode")


def test_app_config_inherits_from_both() -> None:
    """AppConfig is the combined type — isinstance both parents."""
    cfg = AppConfig()
    assert isinstance(cfg, PlayerConfig)
    assert isinstance(cfg, LibraryConfig)
    # All fields from both parents are reachable.
    assert cfg.replaygain_mode == "off"
    assert cfg.sources == []
    assert cfg.eq_bands == [0.0] * 10
    assert cfg.tmdb_api_key == ""


def test_player_config_is_media_core_canonical() -> None:
    """``quaver.core.config.PlayerConfig`` re-exports the media_core class."""
    import media_core.core.config as mcc
    assert PlayerConfig is mcc.PlayerConfig, (
        "Quaver's PlayerConfig must be the SAME object as media_core's "
        "so isinstance checks and shared widgets stay consistent."
    )


def test_round_trip(tmp_path) -> None:
    p = tmp_path / "config.toml"

    cfg = AppConfig()
    cfg.sources.append(SourceConfig(
        path="/srv/Music", uri="", scraper="none",
        scan_mode="multi", media_type="music",
    ))
    cfg.sources.append(SourceConfig(
        path="/srv/Series", uri="smb://nas/series",
        scraper="tvmaze", scan_mode="multi", media_type="series",
    ))
    save_config(cfg, p)

    cfg2 = load_config(p)
    assert len(cfg2.sources) == 2
    assert cfg2.sources[0].path == "/srv/Music"
    assert cfg2.sources[1].uri == "smb://nas/series"
    assert cfg2.sources[1].scraper == "tvmaze"


def test_load_missing_returns_defaults(tmp_path) -> None:
    cfg = load_config(tmp_path / "does_not_exist.toml")
    assert isinstance(cfg, AppConfig)
    assert cfg.sources == []


def test_show_tab_flags_default_true() -> None:
    """Built-in tab visibility flags ship enabled — fresh installs see
    Playlists / Now Playing / Podcasts / YouTube unchanged.
    """
    cfg = AppConfig()
    assert cfg.show_playlists_tab is True
    assert cfg.show_nowplaying_tab is True
    assert cfg.show_podcasts_tab is True
    assert cfg.show_youtube_tab is True


def test_show_tab_flags_round_trip(tmp_path) -> None:
    """Disabled tabs survive write/read so the user's preference
    persists across launches.
    """
    p = tmp_path / "tabs.toml"
    cfg = AppConfig()
    cfg.show_playlists_tab = False
    cfg.show_youtube_tab = False
    save_config(cfg, p)
    out = load_config(p)
    assert out.show_playlists_tab is False
    assert out.show_podcasts_tab is True   # untouched, default
    assert out.show_youtube_tab is False
    assert out.show_nowplaying_tab is True
