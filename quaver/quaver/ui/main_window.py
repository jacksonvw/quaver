"""Main window — tabbed UI with global playlist and transport controls."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, QTimer, Qt, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QSplitter,
    QStatusBar,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from media_core.core.blur import set_blur_behind

from quaver.core.audio import PlaybackState
from quaver.core.config import load_config, save_config, LibraryTabConfig
from quaver.core.library import LibraryDB, Track
from quaver.core.podcast import artwork_path as podcast_artwork_path
from quaver.core.replaygain import RGMode
from quaver.services.library_service import LibraryService
try:
    from quaver.services.mpris2 import Mpris2Service
except ImportError:
    Mpris2Service = None  # type: ignore[assignment,misc]
from quaver.services.player_service import PlayerService, RepeatMode
from quaver.services.podcast_service import PodcastService
from quaver.services.metadata_service import MetadataService, EnrichmentJob
from quaver.services.youtube_service import YouTubeService
from quaver.ui.dialogs.library_tabs_dialog import LibraryTabsDialog
from quaver.ui.dialogs.musicbrainz_dialog import MusicBrainzDialog
from quaver.ui.dialogs.podcast_search import PodcastSearchDialog
from quaver.ui.dialogs.podcast_settings import PodcastSettingsDialog
from quaver.ui.dialogs.preferences import PreferencesDialog
from quaver.ui.dialogs.tag_editor import TagEditorDialog
from quaver.ui.panels.nowplaying_panel import NowPlayingPanel
from quaver.ui.panels.playlists_panel import PlaylistsPanel
from quaver.ui.panels.podcast_panel import PodcastPanel
from quaver.ui.panels.youtube_panel import YouTubePanel
from quaver.ui.widgets.eq_popup import EqPopup
from quaver.ui.widgets.fullscreen_overlay import FullscreenOverlay
from quaver.ui.widgets.library_tab import LibraryTabWidget
from quaver.ui.widgets.queue_widget import QueueWidget
from quaver.ui.widgets.transport import TransportWidget


class _QuaverTabBar(QTabBar):
    """Custom tab bar: compact menu tab, tab-0 pinned, optional lock."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pressed_tab = -1

    # -- Compact hamburger tab ----------------------------------------

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        if index == 0:
            # Icon-only tab — icon width + symmetric 12px padding each side.
            icon_w = self.iconSize().width() or 16
            size.setWidth(icon_w + 24)
        return size

    # -- Prevent dragging tab 0 --------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed_tab = self.tabAt(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed_tab == 0:
            return  # Hamburger tab is not draggable.
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressed_tab = -1
        super().mouseReleaseEvent(event)

    # -- Right-click context menu -------------------------------------

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        lock_action = menu.addAction("Lock Tabs")
        lock_action.setCheckable(True)
        lock_action.setChecked(not self.isMovable())
        lock_action.toggled.connect(lambda locked: self.setMovable(not locked))
        menu.exec(event.globalPos())


class MainWindow(QMainWindow):
    """Quaver main window."""

    # Desired KWin blur state, recomputed by _apply_appearance. Declared at
    # class level so showEvent is safe even if it fires before the first
    # _apply_appearance call.
    _blur_want: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Quaver")
        self.setMinimumSize(900, 550)

        # Core services.
        self._db = LibraryDB()
        self._player = PlayerService(self._db)
        self._library_service = LibraryService(self._db)
        self._mpris2 = Mpris2Service(self._player) if Mpris2Service else None
        self._podcast_service = PodcastService(self._db)
        self._youtube_service = YouTubeService(self._db)
        self._metadata_service = MetadataService(self._db)

        from quaver.services.autodj_service import AutoDJService
        self._autodj = AutoDJService(self._db, self._player)

        # Podcast episode playback state.
        self._playing_episode_id: int | None = None
        # Filepath/URL the tracked episode plays from, so track_changed can
        # tell "still the episode" apart from "moved to something else".
        self._playing_episode_path: str | None = None

        # Maps episode URL → episode_id for non-downloaded episodes in the queue.
        self._queued_episode_urls: dict[str, int] = {}

        # YouTube playback state: video_id currently being resolved.
        self._resolving_video_id: str | None = None

        # Video position tracking (like podcast position saving).
        self._playing_video_track_id: int | None = None
        self._video_auto_watched: bool = False  # Guard: already auto-marked.
        self._video_pos_timer = QTimer(self)
        self._video_pos_timer.setInterval(10_000)
        self._video_pos_timer.timeout.connect(self._save_video_position)

        # Sync sources table from tab config (tabs are authoritative).
        self._sync_sources_from_config()

        # Library tab state.
        self._library_tabs: list[tuple[LibraryTabConfig | None, LibraryTabWidget]] = []
        self._last_tab_index: int = 1  # First real tab (after hamburger).

        # Global queue widget (shared across all tabs).
        self._queue_widget = QueueWidget()

        # EQ / DSP / AutoDJ popups (lazy-created).
        self._eq_popup: EqPopup | None = None
        self._dsp_popup = None
        self._autodj_popup = None
        self._fullscreen_overlay: FullscreenOverlay | None = None
        self._inhibit_cookie: int | None = None  # Screen saver inhibit (D-Bus).
        self._bg_alpha: int = 255
        self._bg_color: list[int] = [30, 30, 30]
        self._bg_tint_alpha: int = 178  # default 70%

        self._build_ui()
        self._connect_signals()

        # No menu bar — use hamburger popup instead.
        self.menuBar().setVisible(False)

        # No status bar — reclaim vertical space for the transport.

        # Load library from local DB cache immediately (window shows fast).
        self._refresh_library()

        # Mount network folders + start file watching in background.
        QTimer.singleShot(0, self._mount_and_watch)

        # Load saved playlists.
        self._refresh_saved_playlists()

        # Load podcasts and start auto-refresh.
        self._refresh_podcasts()
        self._podcast_service.start_auto_refresh()

        # Load YouTube channels and start auto-refresh.
        self._refresh_youtube_channels()
        self._youtube_service.start_auto_refresh()
        config = load_config()
        self._youtube_panel.set_sync_visible(bool(config.youtube_auth_browser))

        # Restore window state.
        self._restore_state()

        # Apply appearance settings.
        self._apply_appearance()

        # Apply persisted playback settings.
        self._apply_playback_settings()

        # Apply persisted AutoDJ settings.
        self._apply_autodj_settings()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Main content: tabs (left) + playlist (right, fixed).
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setObjectName("main_splitter")

        # QTabWidget handles tab bar + stacked content natively.
        self._tabs = QTabWidget()
        bar = _QuaverTabBar(self._tabs)
        self._tabs.setTabBar(bar)  # Must be set before any addTab calls.
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(True)

        # Uniform tab height — use bottom accent border instead of size change.
        bar.setStyleSheet("""
            QTabBar::tab {
                padding: 6px 12px;
                margin: 0px;
            }
            QTabBar::tab:selected {
                border-bottom: 2px solid palette(highlight);
            }
            QTabBar::tab:!selected {
                border-bottom: 2px solid transparent;
            }
        """)

        # Hamburger menu as the first tab (non-movable, non-switchable).
        from quaver.ui.icons import icon_hamburger
        self._tabs.addTab(QWidget(), icon_hamburger(), "")
        bar.setTabButton(0, bar.ButtonPosition.RightSide, None)
        bar.tabMoved.connect(self._on_tab_bar_moved)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._main_splitter.addWidget(self._tabs)
        self._main_splitter.addWidget(self._queue_widget)

        self._main_splitter.setStretchFactor(0, 3)  # tabs
        self._main_splitter.setStretchFactor(1, 1)  # queue

        layout.addWidget(self._main_splitter, stretch=1)

        # Transport controls (always visible, bottom).
        self._transport = TransportWidget()
        self._transport.set_volume(self._player.get_volume())
        layout.addWidget(self._transport)

        # Create tabs.
        self._create_library_tabs()
        self._create_playlists_tab()
        self._create_nowplaying_tab()
        self._create_podcast_tab()
        self._create_youtube_tab()
        # Hide built-in tabs the user has disabled in Preferences.
        self._apply_tab_visibility()

        # Start on first real tab (after hamburger).
        self._tabs.setCurrentIndex(1)

    def _create_library_tabs(self) -> None:
        """Create library tabs from config (or one default tab)."""
        config = load_config()

        if not config.library_tabs:
            tab = LibraryTabWidget()
            self._connect_library_tab(tab)
            self._tabs.addTab(tab, "Library")
            self._library_tabs.append((None, tab))
        else:
            for tab_cfg in config.library_tabs:
                tab = LibraryTabWidget(media_type=tab_cfg.type)
                self._connect_library_tab(tab)
                self._tabs.addTab(tab, tab_cfg.name)
                self._library_tabs.append((tab_cfg, tab))
                if tab_cfg.type == "files":
                    tab.set_source_paths(list(tab_cfg.source_paths))
                    tab.set_hide_extensions(tab_cfg.hide_extensions)

    def _connect_library_tab(self, tab: LibraryTabWidget) -> None:
        tab.play_album.connect(self._on_play_album)
        tab.add_to_queue.connect(self._on_add_to_queue)
        tab.add_to_saved_playlist.connect(self._on_add_to_saved_playlist)
        tab.edit_tags_requested.connect(self._on_edit_tags)
        tab.mark_watched_requested.connect(self._on_mark_watched)
        tab.view_online_requested.connect(self._on_view_online)
        tab.refresh_metadata_requested.connect(
            lambda _tab=tab: self._on_refresh_metadata(_tab)
        )
        tab.scrape_metadata_requested.connect(self._on_scrape_metadata)
        tab.scrape_show_requested.connect(self._on_scrape_show)
        tab.change_match_requested.connect(self._on_change_match)
        tab.show_info_requested.connect(self._on_show_info)
        tab.movie_info_requested.connect(self._on_movie_info)
        tab.mark_show_watched_requested.connect(self._on_mark_show_watched)
        tab.remove_show_requested.connect(self._on_remove_show)
        tab.open_in_minim_requested.connect(self._on_open_in_minim)

    def _create_playlists_tab(self) -> None:
        self._playlists_panel = PlaylistsPanel()
        self._playlists_panel.play_playlist.connect(self._on_play_saved_playlist)
        self._playlists_panel.add_to_queue.connect(self._on_add_playlist_to_queue)
        self._playlists_panel.add_tracks_to_queue.connect(self._on_add_to_queue)
        self._playlists_panel.playlist_changed.connect(self._on_playlist_changed)
        self._playlists_panel.track_moved.connect(self._on_playlist_track_moved)
        self._playlists_panel.tracks_reordered.connect(self._on_playlist_tracks_reordered)
        self._playlists_panel._list.currentRowChanged.connect(
            self._on_saved_playlist_selected
        )
        self._tabs.addTab(self._playlists_panel, "Playlists")

    def _create_nowplaying_tab(self) -> None:
        self._nowplaying_panel = NowPlayingPanel()
        self._tabs.addTab(self._nowplaying_panel, "Now Playing")

    def _create_podcast_tab(self) -> None:
        self._podcast_panel = PodcastPanel()
        self._tabs.addTab(self._podcast_panel, "Podcasts")

    def _create_youtube_tab(self) -> None:
        self._youtube_panel = YouTubePanel()
        self._tabs.addTab(self._youtube_panel, "YouTube")

    def _rebuild_library_tabs(self) -> None:
        """Tear down and recreate library tabs after config change."""
        # Library tabs live at QTabWidget indices 1..n_old (0 is the hamburger).
        # Remove in reverse so indices don't shift under us.
        for i in range(len(self._library_tabs) - 1, -1, -1):
            _, tab = self._library_tabs[i]
            self._tabs.removeTab(self._tabs.indexOf(tab))
            tab.deleteLater()

        self._library_tabs.clear()

        config = load_config()
        if not config.library_tabs:
            tab = LibraryTabWidget()
            self._connect_library_tab(tab)
            self._tabs.insertTab(1, tab, "Library")  # 1 = after hamburger
            self._library_tabs.append((None, tab))
        else:
            for idx, tab_cfg in enumerate(config.library_tabs):
                tab = LibraryTabWidget(media_type=tab_cfg.type)
                self._connect_library_tab(tab)
                self._tabs.insertTab(idx + 1, tab, tab_cfg.name)  # +1 for hamburger
                self._library_tabs.append((tab_cfg, tab))
                if tab_cfg.type == "files":
                    tab.set_source_paths(list(tab_cfg.source_paths))
                    tab.set_hide_extensions(tab_cfg.hide_extensions)

        self._refresh_library()
        # Re-apply built-in tab visibility — _rebuild_library_tabs
        # changes how many library tabs precede the auto tabs, so the
        # canonical insert position shifts and the auto tabs need to
        # be re-positioned.
        self._apply_tab_visibility()
        self._tabs.setCurrentIndex(1)  # First real tab, not the hamburger

        # Invalidate AutoDJ popup — it holds a stale tab name list.
        if self._autodj_popup is not None:
            self._autodj_popup.close()
            self._autodj_popup = None

    @Slot()
    def _show_menu_popup(self) -> None:
        """Show the application menu as a popup from the hamburger button."""
        menu = QMenu(self)

        # File.
        file_menu = menu.addMenu("&File")
        file_menu.addAction("&Open File…", self._open_file)
        file_menu.addSeparator()
        file_menu.addAction("E&xit", self.close)

        # Library.
        lib_menu = menu.addMenu("&Library")
        lib_menu.addAction("&Add Source…", self._show_sources)
        lib_menu.addAction("Configure Library &Tabs…", self._show_library_tabs)
        lib_menu.addAction("&Rescan Library", self._rescan_library)

        # Playback.
        play_menu = menu.addMenu("&Playback")
        play_menu.addAction("&Play/Pause", self._toggle_play)
        play_menu.addAction("&Stop", self._player.stop)
        play_menu.addAction("&Next Track", self._player.next_track)
        play_menu.addAction("Pre&vious Track", self._player.prev_track)
        play_menu.addSeparator()

        rg_menu = play_menu.addMenu("&ReplayGain")
        rg_menu.addAction("Off", lambda: self._set_rg_mode(RGMode.OFF))
        rg_menu.addAction("Track", lambda: self._set_rg_mode(RGMode.TRACK))
        rg_menu.addAction("Album", lambda: self._set_rg_mode(RGMode.ALBUM))

        menu.addSeparator()
        menu.addAction("&Preferences\u2026", self._show_preferences)

        # Show popup below the hamburger tab.
        bar = self._tabs.tabBar()
        tab_rect = bar.tabRect(0)
        pos = bar.mapToGlobal(tab_rect.bottomLeft())
        menu.exec(pos)

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        # Player → UI.
        self._player.state_changed.connect(self._handle_state_changed)
        self._player.track_changed.connect(self._handle_track_changed)
        self._player.resolve_needed.connect(self._on_resolve_needed)
        self._player.queue_changed.connect(self._handle_queue_changed)
        self._player.position_updated.connect(self._transport.set_position)
        self._player.position_updated.connect(self._check_video_watched)
        self._player.spectrum_updated.connect(
            self._transport.spectrum_widget.update_magnitudes
        )
        self._player.spectrum_updated.connect(
            self._nowplaying_panel.spectrum_widget.update_magnitudes
        )
        self._player.error_occurred.connect(self._handle_error)

        # Now Playing.
        self._nowplaying_panel.youtube_mode_changed.connect(self._on_youtube_mode_changed)
        self._nowplaying_panel.volume_change_requested.connect(self._on_np_volume_change)
        self._nowplaying_panel.fullscreen_requested.connect(self._on_fullscreen_requested)
        self._nowplaying_panel.edit_tags_requested.connect(self._on_edit_tags)
        self._nowplaying_panel.add_to_saved_playlist.connect(self._on_add_to_saved_playlist)
        self._nowplaying_panel.mark_watched_requested.connect(self._on_mark_watched)
        self._nowplaying_panel.video_active_changed.connect(
            lambda _: self._update_screen_inhibit()
        )
        self._player.shuffle_changed.connect(self._transport.set_shuffle_checked)
        self._player.repeat_changed.connect(self._transport.set_repeat_label)

        # Transport → actions.
        self._transport.play_toggled.connect(self._toggle_play)
        self._transport.stop_clicked.connect(self._player.stop)
        self._transport.prev_clicked.connect(self._player.prev_track)
        self._transport.next_clicked.connect(self._player.next_track)
        self._transport.seek_requested.connect(self._player.seek)
        self._transport.seek_requested.connect(
            lambda _ms: self._nowplaying.clear_subtitles()
        )
        self._transport.volume_changed.connect(self._player.set_volume)
        self._transport.shuffle_toggled.connect(self._player.toggle_shuffle)
        self._transport.repeat_cycled.connect(self._player.cycle_repeat)
        self._transport.eq_clicked.connect(self._show_eq)
        self._transport.dsp_clicked.connect(self._show_dsp)
        self._transport.autodj_toggled.connect(self._autodj.toggle)
        self._autodj.enabled_changed.connect(self._transport.set_autodj_checked)
        self._transport.autodj_settings_requested.connect(self._show_autodj_popup)
        self._transport.queue_toggled.connect(self._toggle_queue_panel)
        self._transport.fullscreen_clicked.connect(self._on_fullscreen_requested)
        # F11 → fullscreen toggle.
        fullscreen_action = QAction(self)
        fullscreen_action.setShortcut(QKeySequence(Qt.Key.Key_F11))
        fullscreen_action.triggered.connect(self._toggle_fullscreen)
        self.addAction(fullscreen_action)
        # Global transport shortcuts (Space / Left / Right).
        play_action = QAction(self)
        play_action.setShortcut(QKeySequence(Qt.Key.Key_Space))
        play_action.triggered.connect(self._shortcut_toggle_play)
        self.addAction(play_action)
        prev_action = QAction(self)
        prev_action.setShortcut(QKeySequence(Qt.Key.Key_Left))
        prev_action.triggered.connect(self._shortcut_prev)
        self.addAction(prev_action)
        next_action = QAction(self)
        next_action.setShortcut(QKeySequence(Qt.Key.Key_Right))
        next_action.triggered.connect(self._shortcut_next)
        self.addAction(next_action)
        # Menu button is now a corner widget on _tabs, connected in _build_ui.

        # Global queue → player.
        self._queue_widget.track_activated.connect(self._on_queue_track_activated)
        self._queue_widget.remove_requested.connect(self._player.remove_from_queue)
        self._queue_widget.move_requested.connect(self._player.move_in_queue)
        self._queue_widget.move_batch_requested.connect(self._player.move_batch_in_queue)
        self._queue_widget.clear_requested.connect(self._player.clear_queue)
        self._queue_widget.save_as_playlist.connect(self._save_queue_as_playlist)
        self._queue_widget.add_to_playlist.connect(self._add_queue_to_playlist)
        self._queue_widget.external_tracks_dropped.connect(self._on_external_tracks_dropped)
        self._queue_widget.external_episodes_dropped.connect(self._on_external_episodes_dropped)
        self._queue_widget.external_playlists_dropped.connect(self._on_external_playlists_dropped)
        # Inject the "Open in Minim" track-context action.
        self._queue_widget.set_track_extra_actions(self._queue_extra_track_actions)

        # Library service.
        self._library_service.library_updated.connect(self._refresh_library)
        self._library_service.scan_started.connect(self._on_scan_started)
        self._library_service.scan_progress.connect(self._on_scan_progress)
        self._library_service.scan_status.connect(self._on_scan_status)
        self._library_service.scan_finished.connect(self._on_scan_finished)

        # Metadata enrichment service.
        self._metadata_service.enrichment_started.connect(self._on_enrichment_started)
        self._metadata_service.enrichment_progress.connect(self._on_enrichment_progress)
        self._metadata_service.enrichment_finished.connect(self._on_enrichment_finished)
        self._metadata_service.enrichment_error.connect(self._on_enrichment_error)

        # Podcast panel → service.
        self._podcast_panel.add_feed_requested.connect(self._on_add_podcast)
        self._podcast_panel.browse_requested.connect(self._on_browse_podcasts)
        self._podcast_panel.remove_feed_requested.connect(self._on_remove_podcast)
        self._podcast_panel.refresh_requested.connect(self._on_refresh_podcast)
        self._podcast_panel.play_feed_requested.connect(self._on_play_podcast_feed)
        self._podcast_panel.play_episode_requested.connect(self._on_play_episode)
        self._podcast_panel.add_to_queue_requested.connect(self._on_add_episode_to_queue)
        self._podcast_panel.download_requested.connect(
            self._podcast_service.download_episode
        )
        self._podcast_panel.delete_download_requested.connect(
            self._podcast_service.delete_episode
        )
        self._podcast_panel.mark_played_requested.connect(
            self._podcast_service.mark_played
        )
        self._podcast_panel.settings_requested.connect(self._on_podcast_settings)
        self._podcast_panel.load_more_episodes.connect(self._on_load_more_episodes)

        # Podcast panel ← feed selection triggers episode load.
        self._podcast_panel._feed_list.currentRowChanged.connect(
            self._on_podcast_feed_selected
        )

        # Podcast service → UI.
        self._podcast_service.feeds_updated.connect(self._on_feeds_updated)
        self._podcast_service.download_complete.connect(
            lambda _: self._reload_current_episodes()
        )
        self._podcast_service.download_progress.connect(self._on_download_progress)
        self._podcast_service.refresh_finished.connect(self._on_refresh_finished)

        # YouTube panel → service.
        self._youtube_panel.add_channel_requested.connect(self._on_add_youtube_channel)
        self._youtube_panel.browse_requested.connect(self._on_browse_youtube)
        self._youtube_panel.remove_channel_requested.connect(self._on_remove_youtube_channel)
        self._youtube_panel.refresh_requested.connect(self._on_refresh_youtube_channel)
        self._youtube_panel.play_channel_requested.connect(self._on_play_youtube_channel)
        self._youtube_panel.play_video_requested.connect(self._on_play_youtube_video)
        self._youtube_panel.add_to_queue_requested.connect(self._on_add_youtube_to_queue)
        self._youtube_panel.add_to_playlist_requested.connect(self._on_add_youtube_to_playlist)
        self._youtube_panel.download_requested.connect(
            self._youtube_service.download_video
        )
        self._youtube_panel.delete_download_requested.connect(
            self._youtube_service.delete_download
        )
        self._youtube_panel.mark_watched_requested.connect(
            self._youtube_service.mark_watched
        )
        self._youtube_panel.search_requested.connect(self._youtube_service.search)
        self._youtube_panel.sync_subscriptions_requested.connect(
            self._youtube_service.sync_subscriptions
        )
        self._youtube_panel.load_more_videos.connect(self._on_load_more_youtube_videos)

        # YouTube panel ← channel selection triggers video load.
        self._youtube_panel._channel_list.currentRowChanged.connect(
            self._on_youtube_channel_selected
        )

        # YouTube service → UI.
        self._youtube_service.channels_updated.connect(self._on_youtube_channels_updated)
        self._youtube_service.videos_updated.connect(self._on_youtube_videos_updated)
        self._youtube_service.refresh_status.connect(
            lambda msg: self._youtube_panel._description.setPlainText(msg)
        )
        self._youtube_service.stream_resolved.connect(self._on_stream_resolved)
        self._youtube_service.resolve_error.connect(self._on_resolve_error)
        self._youtube_service.refresh_finished.connect(self._on_youtube_refresh_finished)
        self._youtube_service.search_results.connect(self._on_youtube_search_results)
        self._youtube_service.download_progress.connect(self._on_youtube_download_progress)
        self._youtube_service.download_complete.connect(self._on_youtube_download_complete)
        self._youtube_service.download_error.connect(self._on_youtube_download_error)

        # Queue drop support for YouTube videos.
        self._queue_widget.external_videos_dropped.connect(
            self._on_external_videos_dropped
        )

    # ------------------------------------------------------------------
    # Tab handling
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Menu bar toggle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Tab handling
    # ------------------------------------------------------------------

    @Slot(int, int)
    def _on_tab_bar_moved(self, from_idx: int, to_idx: int) -> None:
        """Prevent any tab from occupying index 0 (hamburger slot)."""
        if to_idx == 0:
            self._tabs.tabBar().moveTab(0, 1)

    @Slot(int)
    def _on_tab_changed(self, index: int) -> None:
        if index == 0:
            # Hamburger tab — show menu, revert to previous tab.
            self._tabs.blockSignals(True)
            self._tabs.setCurrentIndex(self._last_tab_index)
            self._tabs.blockSignals(False)
            self._show_menu_popup()
        else:
            self._last_tab_index = index

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    @Slot(PlaybackState)
    def _handle_state_changed(self, state: PlaybackState) -> None:
        self._transport.set_play_state(state)
        self._update_screen_inhibit()
        if state == PlaybackState.STOPPED:
            self._transport.spectrum_widget.clear()
            self._nowplaying_panel.spectrum_widget.clear()
            # Save final video position on stop.
            if self._playing_video_track_id is not None:
                self._stop_video_tracking()
            # Engine is already torn down — the last periodic save is the
            # resume point; a final query now would return 0.
            if self._playing_episode_id is not None:
                self._stop_podcast_tracking(save_final=False)

    def _stop_podcast_tracking(self, save_final: bool = True) -> None:
        self._podcast_service.stop_tracking_position(save_final=save_final)
        self._playing_episode_id = None
        self._playing_episode_path = None

    @Slot(object)
    def _handle_track_changed(self, track: Track | None) -> None:
        # Stop tracking position for previous video track.
        if self._playing_video_track_id is not None:
            self._stop_video_tracking()
        # Stop podcast position tracking when playback moves off the tracked
        # episode — otherwise the service keeps polling the engine and writes
        # the NEW track's position into the episode's resume point.
        if self._playing_episode_id is not None and (
            track is None or track.filepath != self._playing_episode_path
        ):
            self._stop_podcast_tracking(save_final=False)

        if track:
            self._transport.set_track_label(f"{track.artist} — {track.title}")
        else:
            self._transport.set_track_label("No file loaded")
        # Ensure engine reference is set for video/YouTube tracks before update_track.
        poster_path = ""
        plot = ""
        genre = ""
        resume_ms = 0
        watched = False
        is_video = track and track.media_type in ("movie", "series_episode")
        is_youtube = track and track.filepath.startswith("youtube://")
        if is_video or is_youtube:
            self._nowplaying_panel.current_engine = self._player.engine
            self._queue_widget.set_engine(self._player.engine)
        if is_video:
            # Set engine on all library tabs for stream menus in album view.
            for _, tab in self._library_tabs:
                tab.set_engine(self._player.engine)
            # Look up poster, plot, genre, and resume position from video metadata.
            if track.id >= 0:
                vm = self._db.get_video_metadata(track.id)
                if vm:
                    poster_path = vm.get("poster_path") or ""
                    plot = vm.get("plot") or ""
                    genre = vm.get("genre") or ""
                    resume_ms = vm.get("watched_position_ms") or 0
                    watched = bool(vm.get("watched"))
        elif track and track.id < 0 and not is_youtube:
            # A podcast episode shows its show's artwork.
            poster_path = self._episode_art([track.filepath]).get(track.filepath, "")
        self._nowplaying_panel.update_track(
            track, poster_path=poster_path, plot=plot, genre=genre,
            watched=watched,
        )
        # Start video position tracking and resume if applicable.
        if track and track.media_type in ("movie", "series_episode") and track.id >= 0:
            self._start_video_tracking(track.id)
            if resume_ms > 0:
                QTimer.singleShot(
                    500, lambda: self._player.engine.seek(resume_ms)
                )
        # Update now-playing indicator in all library tabs.
        fp = track.filepath if track else None
        for _, tab in self._library_tabs:
            tab.set_playing_track(fp)
        # Clear engine ref when not playing video/YouTube.
        if not is_video and not is_youtube:
            self._nowplaying_panel.current_engine = None
            self._queue_widget.set_engine(None)
        if not is_video:
            for _, tab in self._library_tabs:
                tab.set_engine(None)

    @Slot()
    def _handle_queue_changed(self) -> None:
        # Look up poster paths for video tracks in the queue.
        queue = self._player.queue
        posters: dict[str, str] = {}
        # Video tracks: episode stills / movie posters from DB.
        video_fps = [
            t.filepath for t in queue
            if t.media_type in ("movie", "series_episode")
        ]
        if video_fps:
            all_posters = self._db.get_poster_paths()
            posters.update({fp: all_posters[fp] for fp in video_fps if fp in all_posters})
        # Podcast episodes: the show's artwork.  Matched by source rather
        # than genre, which a queue restored from QSettings doesn't carry.
        transient_fps = [
            t.filepath for t in queue
            if t.id < 0 and not t.filepath.startswith("youtube://")
        ]
        if transient_fps:
            posters.update(self._episode_art(transient_fps))
        # YouTube tracks: thumbnail URLs (predictable pattern from video ID).
        for t in queue:
            if t.filepath.startswith("youtube://"):
                vid_id = t.filepath.replace("youtube://", "")
                posters[t.filepath] = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
        self._queue_widget.load_queue(
            queue, self._player.queue_index, poster_paths=posters,
        )

    @Slot(str)
    def _handle_error(self, msg: str) -> None:
        self._transport.set_status_message(f"Error: {msg}")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @Slot()
    def _toggle_play(self) -> None:
        self._player.toggle_play()

    def _focus_is_text_input(self) -> bool:
        w = QApplication.focusWidget()
        return isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit))

    @Slot()
    def _shortcut_toggle_play(self) -> None:
        if not self._focus_is_text_input():
            self._toggle_play()

    @Slot()
    def _shortcut_prev(self) -> None:
        if not self._focus_is_text_input():
            self._player.prev_track()

    @Slot()
    def _shortcut_next(self) -> None:
        if not self._focus_is_text_input():
            self._player.next_track()

    @Slot()
    def _open_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open Audio Files", "",
            "Audio Files (*.mp3 *.flac *.ogg *.opus *.m4a *.wav *.wma *.aac);;All Files (*)",
        )
        if not paths:
            return
        tracks: list[Track] = []
        for p in paths:
            t = self._db.get_track_by_path(p)
            if t is None:
                pp = Path(p)
                t = Track(
                    id=-1, filepath=str(pp), title=pp.stem, artist="", album="",
                    album_artist="", year=None, genre="", track_number=None,
                    disc_number=None, duration_ms=0, bitrate=0, format="",
                    replay_gain_track=None, replay_gain_album=None,
                    play_count=0, last_played=None, date_added="", rating=0,
                    fingerprint=None,
                )
            tracks.append(t)
        self._player.play_tracks(tracks, start_index=0)

    @Slot(list, int)
    def _on_play_album(self, tracks: list[Track], start_index: int) -> None:
        """Double-click a track: replace queue with album, play from index."""
        self._player.play_tracks(tracks, start_index=start_index)

    @Slot(list)
    def _on_add_to_queue(self, tracks: list[Track]) -> None:
        """Append tracks to the global queue."""
        for t in tracks:
            self._player.enqueue(t)

    def _queue_extra_track_actions(self, rows, tracks) -> list[tuple[str, object]]:
        """QueueWidget host hook — adds "Open in Minim" to the queue's
        per-track right-click menu.  Filters out unplayable URIs
        (``youtube://``) since Minim only handles filesystem paths.
        """
        del rows  # not used; we work off tracks
        paths = [
            t.filepath for t in tracks
            if t.filepath and not t.filepath.startswith(
                ("youtube://", "http://", "https://")
            )
        ]
        if not paths:
            return []
        n = len(paths)
        label = f"Open in Minim ({n})" if n > 1 else "Open in Minim"
        return [(label, lambda _p=paths: self._on_open_in_minim(_p))]

    @Slot(list)
    def _on_open_in_minim(self, paths: list[str]) -> None:
        """Hand off *paths* (filesystem paths only — no ``youtube://``)
        to a fresh Minim instance.  Quaver's queue stays untouched.
        """
        from quaver.core.external_apps import open_in_minim
        local = [p for p in paths if not p.startswith(("youtube://", "http://", "https://"))]
        if not local:
            return
        ok = open_in_minim(local)
        if not ok:
            self._transport.set_status_message(
                "Minim launcher not found on PATH",
            )

    @Slot(list)
    def _on_edit_tags(self, track_ids: list[int]) -> None:
        tracks = [
            t for tid in track_ids
            if (t := self._db.get_track_by_id(tid)) is not None
        ]
        if not tracks:
            return
        dlg = TagEditorDialog(tracks, self._db, parent=self)
        if dlg.exec():
            self._refresh_library()

    # ------------------------------------------------------------------
    # Video position tracking
    # ------------------------------------------------------------------

    def _start_video_tracking(self, track_id: int) -> None:
        """Start periodic position saving for a video track."""
        self._playing_video_track_id = track_id
        self._video_auto_watched = False
        self._video_pos_timer.start()

    def _stop_video_tracking(self) -> None:
        """Stop video position tracking and save final position."""
        self._video_pos_timer.stop()
        self._save_video_position()
        self._playing_video_track_id = None

    def _save_video_position(self) -> None:
        """Save current playback position for the playing video track."""
        tid = self._playing_video_track_id
        if tid is None or tid < 0:
            return
        pos = self._player.engine.get_position_ms()
        if pos <= 0:
            return
        self._db.save_video_position(tid, pos)
        # Update album view in-place.
        vm = self._db.get_video_metadata(tid)
        if vm:
            for _, tab in self._library_tabs:
                tab.update_video_state(tid, bool(vm["watched"]), pos)

    @Slot(int, int)
    def _check_video_watched(self, position_ms: int, duration_ms: int) -> None:
        """Auto-mark a video as watched when playback passes 90%."""
        tid = self._playing_video_track_id
        if tid is None or self._video_auto_watched or duration_ms <= 0:
            return
        if position_ms / duration_ms >= 0.90:
            self._video_auto_watched = True
            self._db.set_video_watched(tid, True)
            self._db.save_video_position(tid, 0)  # Clear resume position.
            for _, tab in self._library_tabs:
                tab.update_video_state(tid, True, 0)

    @Slot(list, bool)
    def _on_mark_watched(self, track_ids: list[int], watched: bool) -> None:
        """Toggle watched state for video tracks."""
        for tid in track_ids:
            self._db.set_video_watched(tid, watched)
            if watched:
                # Clear resume position when marking as watched.
                self._db.save_video_position(tid, 0)
        # Update the album view in all library tabs.
        for tid in track_ids:
            vm = self._db.get_video_metadata(tid)
            w = bool(vm["watched"]) if vm else watched
            pos = vm["watched_position_ms"] if vm else 0
            for _, tab in self._library_tabs:
                tab.update_video_state(tid, w, pos)

    @Slot(int)
    def _on_view_online(self, track_id: int) -> None:
        """Open TMDb or IMDb page for a video track in the default browser."""
        import webbrowser
        vm = self._db.get_video_metadata(track_id)
        if not vm:
            return
        imdb_id = vm.get("imdb_id")
        tmdb_id = vm.get("tmdb_id")
        if imdb_id:
            webbrowser.open(f"https://www.imdb.com/title/{imdb_id}/")
        elif tmdb_id:
            # Determine type from the track.
            track = self._db.get_track_by_id(track_id)
            media = "movie" if track and track.media_type == "movie" else "tv"
            webbrowser.open(f"https://www.themoviedb.org/{media}/{tmdb_id}")

    @Slot(int)
    def _on_queue_track_activated(self, index: int) -> None:
        queue = self._player.queue
        if not (0 <= index < len(queue)):
            return
        track = queue[index]
        # YouTube tracks: play_tracks → _start_current → resolve_needed.
        # Podcast episodes that aren't downloaded need special handling.
        if not track.filepath.startswith("youtube://"):
            episode_id = self._queued_episode_urls.get(track.filepath)
            if episode_id is not None:
                self._download_then_play(episode_id, track, index)
                return
        self._player.play_tracks(queue, start_index=index)

    def _download_then_play(self, episode_id: int, track: Track, index: int) -> None:
        """Download a podcast episode, then play it from its queue position."""
        self._podcast_service.download_episode(episode_id)

        def on_complete(completed_id: int) -> None:
            if completed_id != episode_id:
                return
            try:
                self._podcast_service.download_complete.disconnect(on_complete)
            except RuntimeError:
                pass
            ep = self._podcast_service.get_episode(episode_id)
            if not ep or not ep.local_path:
                return
            # Update the track in-place — same object is in the player's queue.
            self._queued_episode_urls.pop(track.filepath, None)
            track.filepath = ep.local_path
            # Re-play from the same index with the updated filepath.
            self._player.play_tracks(self._player.queue, start_index=index)

        self._podcast_service.download_complete.connect(on_complete)

    @Slot(list, int)
    def _on_external_tracks_dropped(self, track_ids: list[int], insert_position: int) -> None:
        """Handle tracks dropped from album view or artist list into the queue.

        Preserves the source order (IDs arrive pre-sorted from the drag source).
        """
        # Build a lookup to preserve the order from the drag source.
        id_order = {tid: i for i, tid in enumerate(track_ids)}
        tracks = [
            t for tid in track_ids
            if (t := self._db.get_track_by_id(tid)) is not None
        ]
        tracks.sort(key=lambda t: id_order.get(t.id, 0))
        if tracks:
            self._player.insert_tracks(tracks, insert_position)

    @Slot(list, int)
    def _on_external_episodes_dropped(self, episode_ids: list[int], insert_position: int) -> None:
        """Handle episodes dropped from podcast panel into the queue."""
        tracks: list[Track] = []
        for eid in episode_ids:
            ep = self._podcast_service.get_episode(eid)
            if not ep:
                continue
            if ep.downloaded and ep.local_path:
                source = ep.local_path
            else:
                source = ep.url
                if source:
                    self._queued_episode_urls[source] = eid
            if not source:
                continue
            t = self._episode_track(ep, source)
            tracks.append(t)
        if tracks:
            self._player.insert_tracks(tracks, insert_position)

    @Slot(list, int)
    def _on_external_playlists_dropped(self, playlist_ids: list[int], insert_position: int) -> None:
        """Handle saved playlists dropped into the queue."""
        tracks: list[Track] = []
        for pid in playlist_ids:
            tracks.extend(self._db.get_playlist_tracks(pid))
        if tracks:
            self._player.insert_tracks(tracks, insert_position)

    # ------------------------------------------------------------------
    # EQ
    # ------------------------------------------------------------------

    @Slot()
    def _show_eq(self) -> None:
        if self._eq_popup is None:
            self._eq_popup = EqPopup(parent=self)
            self._eq_popup.bands_changed.connect(self._player.set_eq_bands)
            self._eq_popup.bands_changed.connect(self._save_eq_bands)
            config = load_config()
            self._eq_popup.set_gains(config.eq_bands)
        self._eq_popup.show()
        self._eq_popup.raise_()

    def _show_dsp(self) -> None:
        if self._dsp_popup is None:
            from quaver.ui.widgets.dsp_popup import DspPopup
            self._dsp_popup = DspPopup(parent=self)
            self._dsp_popup.compressor_changed.connect(self._player.set_compressor)
            self._dsp_popup.compressor_changed.connect(self._save_compressor)
            self._dsp_popup.speed_changed.connect(self._player.set_playback_speed)
            self._dsp_popup.speed_changed.connect(self._save_speed)
            config = load_config()
            self._dsp_popup.set_compressor_state(
                config.compressor_enabled, config.compressor_ratio, config.compressor_threshold
            )
            self._dsp_popup.set_speed(config.playback_speed)
        self._dsp_popup.show()
        self._dsp_popup.raise_()

    @Slot()
    def _show_autodj_popup(self) -> None:
        if self._autodj_popup is None:
            from quaver.ui.widgets.autodj_popup import AutoDJPopup
            tab_names = [
                tab_cfg.name if tab_cfg else "Library"
                for tab_cfg, _ in self._library_tabs
            ]
            self._autodj_popup = AutoDJPopup(tab_names=tab_names, parent=self)
            self._autodj_popup.source_tabs_changed.connect(self._on_autodj_source_tabs)
            self._autodj_popup.genre_filter_changed.connect(self._on_autodj_genre_filter)
            self._autodj_popup.avoid_hours_changed.connect(self._on_autodj_avoid_hours)
            self._autodj_popup.lookahead_changed.connect(self._on_autodj_lookahead)
            config = load_config()
            self._autodj_popup.set_state(
                avoid_hours=config.autodj_avoid_hours,
                genre_filter=config.autodj_genre_filter,
                lookahead=config.autodj_lookahead,
                selected_tab_names=config.autodj_tab_names,
            )
        self._autodj_popup.show()
        self._autodj_popup.raise_()

    @Slot(list)
    def _on_autodj_source_tabs(self, tab_names: list) -> None:
        self._autodj.set_source_folders(self._resolve_tab_folders(tab_names))
        config = load_config()
        config.autodj_tab_names = list(tab_names)
        save_config(config)

    @Slot(str)
    def _on_autodj_genre_filter(self, genre: str) -> None:
        self._autodj.set_genre_filter(genre)
        config = load_config()
        config.autodj_genre_filter = genre
        save_config(config)

    @Slot(int)
    def _on_autodj_avoid_hours(self, hours: int) -> None:
        self._autodj.set_avoid_hours(hours)
        config = load_config()
        config.autodj_avoid_hours = hours
        save_config(config)

    @Slot(int)
    def _on_autodj_lookahead(self, count: int) -> None:
        self._autodj.set_lookahead(count)
        config = load_config()
        config.autodj_lookahead = count
        save_config(config)

    def _resolve_tab_folders(self, tab_names: list) -> list:
        """Translate tab names to folder paths for AutoDJService. Empty = whole library."""
        if not tab_names:
            return []
        name_set = set(tab_names)
        folders: list[str] = []
        for tab_cfg, _ in self._library_tabs:
            name = tab_cfg.name if tab_cfg else "Library"
            if name in name_set and tab_cfg:
                folders.extend(tab_cfg.source_paths)
        return folders

    def _apply_autodj_settings(self) -> None:
        """Apply persisted AutoDJ settings at startup."""
        config = load_config()
        self._autodj.set_avoid_hours(config.autodj_avoid_hours)
        self._autodj.set_genre_filter(config.autodj_genre_filter)
        self._autodj.set_lookahead(config.autodj_lookahead)
        self._autodj.set_source_folders(self._resolve_tab_folders(config.autodj_tab_names))

    def _save_eq_bands(self, bands: list) -> None:
        config = load_config()
        config.eq_bands = list(bands)
        save_config(config)

    def _save_compressor(self, enabled: bool, ratio: float, threshold: float) -> None:
        config = load_config()
        config.compressor_enabled = enabled
        config.compressor_ratio = ratio
        config.compressor_threshold = threshold
        save_config(config)

    def _save_speed(self, rate: float) -> None:
        config = load_config()
        config.playback_speed = rate
        save_config(config)

    # ------------------------------------------------------------------
    # Podcasts
    # ------------------------------------------------------------------

    def _on_refresh_finished(self) -> None:
        self._reload_current_episodes()

    def _on_feeds_updated(self) -> None:
        saved_id = self._podcast_panel._current_podcast_id
        self._refresh_podcasts()
        if saved_id:
            for i, p in enumerate(self._podcast_service.get_all_podcasts()):
                if p.id == saved_id:
                    self._podcast_panel._feed_list.setCurrentRow(i)
                    break
        self._reload_current_episodes()

    def _refresh_podcasts(self) -> None:
        podcasts = self._podcast_service.get_all_podcasts()
        self._podcast_panel.load_podcasts(podcasts)

    def _reload_current_episodes(self) -> None:
        pid = self._podcast_panel._current_podcast_id
        if pid:
            page_size = self._podcast_panel._PAGE_SIZE
            eps = self._podcast_service.get_episodes(pid, limit=page_size)
            total = self._podcast_service.get_episode_count(pid)
            self._podcast_panel.load_episodes_paginated(eps, total)

    @Slot()
    def _on_browse_podcasts(self) -> None:
        dlg = PodcastSearchDialog(parent=self)
        dlg.feed_selected.connect(self._on_add_podcast)
        dlg.exec()

    @Slot(int, int, int)
    def _on_download_progress(self, episode_id: int, done: int, total: int) -> None:
        if total > 0:
            pct = int(done * 100 / total)
            self._podcast_panel.update_download_progress(episode_id, pct)

    @Slot(str)
    def _on_add_podcast(self, url: str) -> None:
        self._podcast_service.add_podcast(url)

    @Slot(int)
    def _on_podcast_settings(self, podcast_id: int) -> None:
        podcast = self._podcast_service.pdb.get_podcast(podcast_id)
        if not podcast:
            return
        dlg = PodcastSettingsDialog(podcast, parent=self)
        if dlg.exec():
            self._podcast_service.update_settings(
                podcast_id,
                dlg.auto_download,
                dlg.auto_delete_played,
                dlg.retain_episodes,
                dlg.refresh_interval_hours,
            )
            self._refresh_podcasts()

    @Slot(int)
    def _on_remove_podcast(self, podcast_id: int) -> None:
        self._podcast_service.remove_podcast(podcast_id)

    @Slot(int)
    def _on_refresh_podcast(self, podcast_id: int) -> None:
        if podcast_id == 0:
            self._podcast_service.refresh_all()
        else:
            self._podcast_service.refresh_podcast(podcast_id)

    @Slot(int, int, int)
    def _on_load_more_episodes(self, podcast_id: int, limit: int, offset: int) -> None:
        eps = self._podcast_service.get_episodes(podcast_id, limit=limit, offset=offset)
        self._podcast_panel.append_episodes(eps)

    @Slot(int)
    def _on_podcast_feed_selected(self, row: int) -> None:
        podcasts = self._podcast_service.get_all_podcasts()
        if 0 <= row < len(podcasts):
            pid = podcasts[row].id
            page_size = self._podcast_panel._PAGE_SIZE
            eps = self._podcast_service.get_episodes(pid, limit=page_size)
            total = self._podcast_service.get_episode_count(pid)
            self._podcast_panel.load_episodes_paginated(eps, total)

    def _episode_track(self, ep, source: str) -> Track:
        """A transient queue track for a podcast episode, credited to its show."""
        podcast = self._podcast_service.get_podcast(ep.podcast_id)
        return Track(
            id=-1, filepath=source, title=ep.title,
            artist=podcast.title if podcast else "Podcast",
            album="", album_artist="", year=None, genre="Podcast",
            track_number=None, disc_number=None,
            duration_ms=ep.duration_ms, bitrate=0, format="",
            replay_gain_track=None, replay_gain_album=None,
            play_count=0, last_played=None, date_added="", rating=0,
            fingerprint=None,
        )

    def _episode_art(self, sources: list[str]) -> dict[str, str]:
        """Each podcast episode's show artwork, keyed by its queue filepath."""
        art: dict[str, str] = {}
        for fp, pid in self._podcast_service.podcast_ids_for_sources(sources).items():
            path = podcast_artwork_path(pid)
            if path is not None:
                art[fp] = str(path)
        return art

    @Slot(int)
    def _on_play_episode(self, episode_id: int) -> None:
        ep = self._podcast_service.get_episode(episode_id)
        if not ep:
            return

        if self._playing_episode_id:
            # Engine is still on the previous episode here, so the final
            # position save is valid.
            self._stop_podcast_tracking(save_final=True)

        source = ep.local_path if ep.downloaded and ep.local_path else ep.url
        if not source:
            return

        self._playing_episode_id = episode_id
        self._playing_episode_path = source

        # Build a Track and play through the queue (same path as all other content).
        track = self._episode_track(ep, source)
        self._player.play_tracks([track], start_index=0)

        if ep.played_position_ms > 0:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(
                500, lambda: self._player.engine.seek(ep.played_position_ms)
            )

        self._podcast_service.start_tracking_position(
            episode_id, self._player.engine.get_position_ms
        )

    @Slot(int)
    def _on_add_episode_to_queue(self, episode_id: int) -> None:
        """Append a podcast episode to the queue without replacing it."""
        ep = self._podcast_service.get_episode(episode_id)
        if not ep:
            return
        if ep.downloaded and ep.local_path:
            source = ep.local_path
        else:
            source = ep.url
            if source:
                self._queued_episode_urls[source] = episode_id
        if not source:
            return
        track = self._episode_track(ep, source)
        self._player.enqueue(track)

    @Slot(int)
    def _on_play_podcast_feed(self, podcast_id: int) -> None:
        """Double-click on feed → replace queue with all episodes, play first."""
        episodes = self._podcast_service.get_episodes(podcast_id)
        if not episodes:
            return
        tracks: list[Track] = []
        for ep in episodes:
            source = ep.local_path if ep.downloaded and ep.local_path else ep.url
            if not source:
                continue
            tracks.append(self._episode_track(ep, source))
        if tracks:
            self._player.play_tracks(tracks, start_index=0)

    # ------------------------------------------------------------------
    # YouTube
    # ------------------------------------------------------------------

    def _refresh_youtube_channels(self) -> None:
        channels = self._youtube_service.get_all_channels()
        self._youtube_panel.load_channels(channels)

    def _reload_current_youtube_videos(self) -> None:
        cid = self._youtube_panel._current_channel_db_id
        if cid:
            page_size = self._youtube_panel._PAGE_SIZE
            videos = self._youtube_service.get_videos(cid, limit=page_size)
            total = self._youtube_service.get_video_count(cid)
            self._youtube_panel.load_videos_paginated(videos, total)

    @Slot(str)
    def _on_add_youtube_channel(self, url: str) -> None:
        self._youtube_service.add_channel(url)

    @Slot()
    def _on_browse_youtube(self) -> None:
        from quaver.ui.dialogs.youtube_search import YouTubeSearchDialog
        config = load_config()
        dlg = YouTubeSearchDialog(
            cookies_browser=config.youtube_auth_browser, parent=self
        )
        dlg.channel_selected.connect(self._on_add_youtube_channel)
        dlg.exec()

    @Slot(int)
    def _on_remove_youtube_channel(self, channel_db_id: int) -> None:
        self._youtube_service.remove_channel(channel_db_id)

    @Slot(int)
    def _on_refresh_youtube_channel(self, channel_db_id: int) -> None:
        if channel_db_id == 0:
            self._youtube_service.refresh_all()
        else:
            self._youtube_service.refresh_channel(channel_db_id)

    @Slot(int, int, int)
    def _on_load_more_youtube_videos(
        self, channel_db_id: int, limit: int, offset: int
    ) -> None:
        videos = self._youtube_service.get_videos(
            channel_db_id, limit=limit, offset=offset
        )
        self._youtube_panel.append_videos(videos)

    @Slot(int)
    def _on_youtube_channel_selected(self, row: int) -> None:
        channels = self._youtube_service.get_all_channels()
        if 0 <= row < len(channels):
            cid = channels[row].id
            total = self._youtube_service.get_video_count(cid)
            if total == 0:
                # No videos in DB yet — fetch them now (lazy-load).
                self._youtube_service.refresh_channel(cid)
                return
            page_size = self._youtube_panel._PAGE_SIZE
            videos = self._youtube_service.get_videos(cid, limit=page_size)
            self._youtube_panel.load_videos_paginated(videos, total)

    @Slot()
    def _on_youtube_channels_updated(self) -> None:
        self._refresh_youtube_channels()

    @Slot(int)
    def _on_youtube_videos_updated(self, channel_db_id: int) -> None:
        """A channel just finished refreshing — update if it's the selected one."""
        if self._youtube_panel._current_channel_db_id == channel_db_id:
            self._reload_current_youtube_videos()

    @Slot()
    def _on_youtube_refresh_finished(self) -> None:
        self._reload_current_youtube_videos()

    @Slot(list)
    def _on_youtube_search_results(self, results: list[dict]) -> None:
        """Show search results as videos in the video table area."""
        from quaver.core.youtube import Video
        videos: list[Video] = []
        for r in results:
            if r.get("type") != "video":
                continue
            videos.append(Video(
                id=0, channel_id=0, video_id=r["id"],
                title=r.get("title", ""), duration_ms=r.get("duration_ms", 0),
                thumbnail_url=r.get("thumbnail_url"),
                published_at=None, view_count=r.get("view_count", 0),
                description="", watched=False, local_path=None, downloaded=False,
            ))
        self._youtube_panel.load_videos(videos)

    def _make_youtube_track(self, video) -> Track:
        """Build a transient Track from a YouTube Video dataclass."""
        channel = self._youtube_service.get_channel(video.channel_id)
        artist = channel.title if channel else "YouTube"
        return Track(
            id=-1, filepath=f"youtube://{video.video_id}",
            title=video.title, artist=artist,
            album="", album_artist="", year=None, genre="YouTube",
            track_number=None, disc_number=None,
            duration_ms=video.duration_ms, bitrate=0, format="",
            replay_gain_track=None, replay_gain_album=None,
            play_count=0, last_played=None, date_added="", rating=0,
            fingerprint=None,
        )

    @Slot(object)
    def _on_resolve_needed(self, track: Track) -> None:
        """PlayerService emitted resolve_needed for a youtube:// track."""
        if not track.filepath.startswith("youtube://"):
            return
        video_id = track.filepath.replace("youtube://", "")

        # Check if this video is downloaded — play local file with video.
        video = self._youtube_service.ydb.get_video_by_yt_id(video_id)
        if video and video.downloaded and video.local_path:
            self._nowplaying_panel.play_video(
                video.local_path, self._player.engine
            )
            self._player.engine.play_file(video.local_path)
            return

        self._resolving_video_id = video_id
        self._youtube_service.resolve_stream(video_id)

    @Slot(int)
    def _on_play_youtube_channel(self, channel_db_id: int) -> None:
        """Double-click on channel → replace queue with all videos, play first."""
        videos = self._youtube_service.get_videos(channel_db_id)
        if not videos:
            return
        tracks = [self._make_youtube_track(v) for v in videos]
        if tracks:
            self._player.play_tracks(tracks, start_index=0)

    @Slot(int)
    def _on_play_youtube_video(self, video_db_id: int) -> None:
        """Play a YouTube video (replace queue, resolve stream).

        Always uses youtube:// URI so the track is recognized as YouTube
        content throughout the UI (Now Playing, context menus, etc.).
        _on_resolve_needed handles downloaded vs streaming transparently.
        """
        video = self._youtube_service.get_video(video_db_id)
        if not video:
            return
        track = self._make_youtube_track(video)
        self._player.play_tracks([track], start_index=0)

    @Slot(int)
    def _on_add_youtube_to_queue(self, video_db_id: int) -> None:
        """Append a YouTube video to the queue."""
        video = self._youtube_service.get_video(video_db_id)
        if not video:
            return
        track = self._make_youtube_track(video)
        self._player.enqueue(track)

    @Slot(int, int)
    def _on_add_youtube_to_playlist(
        self, video_db_id: int, playlist_id: int
    ) -> None:
        """Add a YouTube video to a saved playlist."""
        pos = self._youtube_service.ydb.get_next_playlist_position(playlist_id)
        self._youtube_service.ydb.add_to_playlist(playlist_id, video_db_id, pos)
        self._refresh_saved_playlists()

    @Slot(str, str, str)
    def _on_stream_resolved(
        self, video_id: str, audio_url: str, video_url: str
    ) -> None:
        """Stream URL resolved — start actual playback."""
        current = self._player.current_track
        if not current or not current.filepath.startswith("youtube://"):
            return
        current_vid = current.filepath.replace("youtube://", "")
        if current_vid != video_id:
            return

        config = load_config()
        use_video = (
            config.youtube_nowplaying_mode == "video"
            and video_url
        )

        if use_video:
            # Video mode: appsink is always present, just start pulling.
            self._nowplaying_panel.play_video(
                video_url, self._player.engine
            )
            self._player.engine.play_uri(video_url)
        elif audio_url:
            # Thumbnail mode: play audio-only stream (no video overhead).
            self._player.engine.play_uri(audio_url)
            if video_url:
                self._nowplaying_panel.set_video_url(video_url)

        self._resolving_video_id = None

    @Slot(str, str)
    def _on_resolve_error(self, video_id: str, error: str) -> None:
        """Stream resolution failed."""
        self._resolving_video_id = None
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            self, "YouTube Error",
            f"Could not resolve stream for video:\n{error}",
        )

    @Slot(str)
    def _on_youtube_mode_changed(self, mode: str) -> None:
        """Handle runtime toggle between thumbnail and video display.

        With appsink, no pipeline restart is needed — just start/stop
        pulling frames from the same appsink.
        """
        current = self._player.current_track
        if not current:
            return
        is_youtube = current.filepath.startswith("youtube://")
        is_local_video = current.media_type in ("movie", "series_episode")
        if not is_youtube and not is_local_video:
            return
        if mode == "video":
            if is_youtube:
                video_url = self._nowplaying_panel._current_video_url
                if video_url:
                    self._nowplaying_panel.play_video(
                        video_url, self._player.engine
                    )
            elif is_local_video and self._player.engine:
                self._nowplaying_panel._video_surface.start_rendering(
                    self._player.engine
                )
                self._nowplaying_panel._stack.setCurrentIndex(1)
                self._nowplaying_panel._video_active = True
        elif mode == "thumbnail":
            self._nowplaying_panel._stop_video()

    def _on_np_volume_change(self, delta: float) -> None:
        """Adjust volume from Now Playing scroll wheel."""
        current = self._player.get_volume()
        new_vol = max(0.0, min(1.0, current + delta))
        self._player.set_volume(new_vol)
        self._transport.set_volume(new_vol)

    # ------------------------------------------------------------------
    # Fullscreen overlay
    # ------------------------------------------------------------------

    @Slot()
    def _on_fullscreen_requested(self) -> None:
        """Create fullscreen overlay — reparents panel + transport + queue."""
        if self._fullscreen_overlay is not None:
            return  # Already fullscreen.

        # Remember queue visibility and panel's tab index for restore.
        self._queue_was_visible = self._queue_widget.isVisible()
        self._np_tab_index = self._tabs.indexOf(self._nowplaying_panel)

        overlay = FullscreenOverlay(
            nowplaying_panel=self._nowplaying_panel,
            transport_widget=self._transport,
            queue_widget=self._queue_widget,
            parent=self,
        )
        self._fullscreen_overlay = overlay
        overlay.closed.connect(self._on_fullscreen_closed)

    @Slot()
    def _on_fullscreen_closed(self) -> None:
        """Reparent panel + transport + queue back to main window."""
        overlay = self._fullscreen_overlay
        if overlay is None:
            return

        # Reparent Now Playing panel back to its tab and switch to it.
        self._tabs.insertTab(self._np_tab_index, self._nowplaying_panel, "Now Playing")
        self._tabs.setCurrentWidget(self._nowplaying_panel)

        # Reparent transport back to main window bottom layout.
        central_layout = self.centralWidget().layout()
        self._transport.setParent(self.centralWidget())
        central_layout.addWidget(self._transport)
        self._transport._btn_fullscreen.setVisible(True)
        self._transport.show()

        # Reparent queue back to main splitter.
        # Use the overlay's final queue state — the user may have toggled
        # the queue while in fullscreen, so the saved entry state is stale.
        queue_visible = overlay._queue_visible
        self._queue_widget.setParent(self._main_splitter)
        self._main_splitter.addWidget(self._queue_widget)
        self._queue_widget.setVisible(queue_visible)
        self._transport.set_queue_checked(queue_visible)

        overlay.deleteLater()
        self._fullscreen_overlay = None

    def _toggle_fullscreen(self) -> None:
        """F11 handler — enter or exit fullscreen."""
        if self._fullscreen_overlay is not None:
            self._fullscreen_overlay._exit_fullscreen()
        else:
            self._on_fullscreen_requested()

    # ------------------------------------------------------------------
    # Screen saver inhibit (video playback keeps display awake)
    # ------------------------------------------------------------------

    def _update_screen_inhibit(self) -> None:
        """Inhibit screen saver when video is actively playing, uninhibit otherwise."""
        should_inhibit = (
            self._player.engine.state == PlaybackState.PLAYING
            and self._nowplaying_panel.is_video_active
        )
        if should_inhibit and self._inhibit_cookie is None:
            self._inhibit_screensaver()
        elif not should_inhibit and self._inhibit_cookie is not None:
            self._uninhibit_screensaver()

    def _inhibit_screensaver(self) -> None:
        import sys
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000002)
            except Exception:
                pass
            return
        try:
            import dbus
            bus = dbus.SessionBus()
            obj = bus.get_object(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
            )
            iface = dbus.Interface(obj, "org.freedesktop.ScreenSaver")
            self._inhibit_cookie = int(
                iface.Inhibit("quaver", "Video playback")
            )
        except Exception:
            pass

    def _uninhibit_screensaver(self) -> None:
        import sys
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass
            return
        if self._inhibit_cookie is None:
            return
        try:
            import dbus
            bus = dbus.SessionBus()
            obj = bus.get_object(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
            )
            iface = dbus.Interface(obj, "org.freedesktop.ScreenSaver")
            iface.UnInhibit(self._inhibit_cookie)
        except Exception:
            pass
        self._inhibit_cookie = None

    @Slot(list, int)
    def _on_external_videos_dropped(
        self, video_db_ids: list[int], insert_position: int
    ) -> None:
        """Handle YouTube videos dropped from the YouTube panel into the queue."""
        tracks: list[Track] = []
        for vid_id in video_db_ids:
            video = self._youtube_service.get_video(vid_id)
            if not video:
                continue
            tracks.append(self._make_youtube_track(video))
        if tracks:
            self._player.insert_tracks(tracks, insert_position)

    @Slot(int, int, int)
    def _on_youtube_download_progress(
        self, video_db_id: int, done: int, total: int
    ) -> None:
        if total > 0:
            pct = int(done * 100 / total)
            self._youtube_panel.update_download_progress(video_db_id, pct)

    @Slot(int)
    def _on_youtube_download_complete(self, video_db_id: int) -> None:
        self._youtube_panel.mark_download_complete(video_db_id)

    @Slot(int, str)
    def _on_youtube_download_error(self, video_db_id: int, error: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            self, "Download Error",
            f"Failed to download video:\n{error}",
        )

    # ------------------------------------------------------------------
    # Library
    # ------------------------------------------------------------------

    def _refresh_library(self) -> None:
        """Load library tracks + stats in a background thread."""
        from PySide6.QtCore import QThread, Signal as _Sig

        # Collect tab configs (safe to read on main thread).
        # has_config=False means no [[library_tabs]] entries at all (show everything).
        # has_config=True with empty folders means the user hasn't assigned any (show nothing).
        tab_configs = [
            (list(tab_cfg.source_paths) if tab_cfg else None,
             tab_cfg is not None, tab, tab._media_type)
            for tab_cfg, tab in self._library_tabs
        ]
        db = self._db

        class _Loader(QThread):
            done = _Sig(list, tuple)

            def run(self_):
                results = []
                for folders, has_config, tab, mtype in tab_configs:
                    # Files tabs are live filesystem trees — no DB query.
                    if mtype == "files":
                        continue
                    if folders:
                        tracks = db.get_tracks_by_folders(folders)
                        posters = db.get_poster_paths(folders)
                    elif has_config:
                        tracks = []
                        posters = {}
                    else:
                        tracks = db.get_all_tracks()
                        posters = db.get_poster_paths()
                    # Fetch video watch states for video tracks.
                    video_ids = [
                        t.id for t in tracks
                        if t.media_type in ("movie", "series_episode") and t.id >= 0
                    ]
                    vstates = db.get_video_states(video_ids) if video_ids else {}
                    genres = db.get_video_genres(folders) if video_ids else {}
                    # Fetch show data for series tabs.
                    shows = (
                        db.get_shows_with_counts(folders)
                        if mtype == "series" and (folders or not has_config)
                        else []
                    )
                    results.append((tab, tracks, posters, vstates, genres, shows))
                stats = db.get_library_stats()
                self_.done.emit(results, stats)

        loader = _Loader(self)
        loader.done.connect(self._apply_library_load)
        loader.finished.connect(loader.deleteLater)
        loader.start()
        self._lib_loader = loader  # prevent GC

    def _apply_library_load(self, results: list, stats: tuple) -> None:
        """Apply loaded tracks to tabs (runs on main thread)."""
        for tab, tracks, posters, vstates, genres, shows in results:
            tab.load_tracks(
                tracks, poster_paths=posters, video_states=vstates,
                video_genres=genres, show_data=shows,
            )

    def _sync_sources_from_config(self) -> None:
        """Ensure DB sources match config. Run once at startup.

        Handles two cases:
        1. Sources exist in config — sync DB to match (config is authoritative).
        2. No config sources, but DB sources exist — auto-generate config from DB.
        """
        config = load_config()
        if config.sources:
            self._db.sync_sources(config.sources)
            return

        # Case 2: legacy migration from DB-only sources.
        db_sources = self._db.get_sources()
        if not db_sources:
            return
        from quaver.core.config import SourceConfig
        type_names = {"music": "Music", "series": "Series", "movies": "Movies"}
        tabs_by_type: dict[str, LibraryTabConfig] = {}
        for src in db_sources:
            mtype = src.get("type", "music")
            config.sources.append(SourceConfig(
                path=src["path"],
                uri=src.get("uri") or "",
                scraper=src.get("scraper", "none"),
                scan_mode=src.get("scan_mode", "multi"),
                media_type=mtype,
            ))
            if mtype not in tabs_by_type:
                tabs_by_type[mtype] = LibraryTabConfig(
                    name=type_names.get(mtype, "Library"),
                    type=mtype,
                )
            tabs_by_type[mtype].source_paths.append(src["path"])
        config.library_tabs = list(tabs_by_type.values())
        save_config(config)
        self._db.sync_sources(config.sources)

    def _show_sources(self) -> None:
        from quaver.ui.dialogs.sources import SourcesDialog
        dlg = SourcesDialog(self._db, parent=self)
        if dlg.exec():
            self._rebuild_library_tabs()
            self._rescan_library()

    def _show_library_tabs(self) -> None:
        dlg = LibraryTabsDialog(self._db, parent=self)
        if dlg.exec():
            self._rebuild_library_tabs()
            self._rescan_library()

    def _mount_and_watch(self) -> None:
        """Mount network sources + start file watching in background."""
        from PySide6.QtCore import QThread, Signal as _Sig

        class _MountWorker(QThread):
            done = _Sig()

            def __init__(self_, svc):
                super().__init__()
                self_._svc = svc

            def run(self_):
                self_._svc.mount_network_sources()
                self_._svc.start_watching()
                self_.done.emit()

        worker = _MountWorker(self._library_service)
        worker.done.connect(self._refresh_library)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._mount_worker = worker  # prevent GC

    def _rescan_library(self) -> None:
        self._library_service.mount_network_sources()
        self._library_service.start_scan()
        self._library_service.start_watching()

    # ------------------------------------------------------------------
    # Saved playlists
    # ------------------------------------------------------------------

    def _refresh_saved_playlists(self) -> None:
        """Reload saved playlists into the panel, queue widget, library tabs, and YouTube panel."""
        playlists = self._db.get_all_playlists()
        self._playlists_panel.load_playlists(playlists)
        self._queue_widget.set_playlists([(pid, name) for pid, name, _ in playlists])
        for _, tab in self._library_tabs:
            tab.set_saved_playlists(playlists)
        self._youtube_panel.set_playlists([(pid, name) for pid, name, _ in playlists])
        self._nowplaying_panel.set_playlists(playlists)

    @Slot(int)
    def _on_saved_playlist_selected(self, row: int) -> None:
        playlists = self._db.get_all_playlists()
        if 0 <= row < len(playlists):
            pid = playlists[row][0]
            tracks = self._db.get_playlist_tracks(pid)
            self._playlists_panel.load_playlist_tracks(tracks)

    @Slot(int, int)
    def _on_play_saved_playlist(self, playlist_id: int, start_index: int) -> None:
        tracks = self._db.get_playlist_tracks(playlist_id)
        if tracks:
            self._player.play_tracks(tracks, start_index=start_index)

    def _on_add_playlist_to_queue(self, playlist_id: int) -> None:
        """Append all tracks from a saved playlist to the queue."""
        tracks = self._db.get_playlist_tracks(playlist_id)
        if tracks:
            for t in tracks:
                self._player.enqueue(t)

    def _save_queue_as_playlist(self) -> None:
        """Save the current queue as a new saved playlist."""
        from PySide6.QtWidgets import QInputDialog
        queue = self._player.queue
        if not queue:
            return
        name, ok = QInputDialog.getText(self, "Save Queue as Playlist", "Playlist name:")
        if ok and name.strip():
            pid = self._db.create_playlist(name.strip())
            track_ids = [t.id for t in queue if t.id > 0]
            if track_ids:
                self._db.add_tracks_to_playlist(pid, track_ids)
            self._refresh_saved_playlists()

    def _add_queue_to_playlist(self, playlist_id: int) -> None:
        """Append all queue tracks to an existing saved playlist."""
        queue = self._player.queue
        track_ids = [t.id for t in queue if t.id > 0]
        if track_ids:
            self._db.add_tracks_to_playlist(playlist_id, track_ids)
            self._refresh_saved_playlists()

    @Slot()
    def _on_playlist_track_moved(self, playlist_id: int, from_pos: int, to_pos: int) -> None:
        """Persist drag-reorder of a playlist track to the database."""
        self._db.move_playlist_track(playlist_id, from_pos, to_pos)

    def _on_playlist_tracks_reordered(self, playlist_id: int, track_ids: list[int]) -> None:
        """Persist batch drag-reorder of playlist tracks to the database."""
        self._db.reorder_playlist(playlist_id, track_ids)

    def _on_playlist_changed(self) -> None:
        """Handle create/rename/delete/remove requests from playlists panel."""
        panel = self._playlists_panel

        # New playlist.
        new_name = panel.pending_new_playlist
        if new_name:
            self._db.create_playlist(new_name)

        # Rename.
        rename = panel.pending_rename
        if rename:
            self._db.rename_playlist(*rename)

        # Delete.
        delete_id = panel.pending_delete
        if delete_id is not None:
            self._db.delete_playlist(delete_id)

        # Remove track.
        remove_pos = panel.pending_remove
        if remove_pos is not None and panel.current_playlist_id is not None:
            self._db.remove_track_from_playlist(panel.current_playlist_id, remove_pos)

        self._refresh_saved_playlists()
        # Reload current playlist tracks if one is selected.
        if panel.current_playlist_id is not None:
            tracks = self._db.get_playlist_tracks(panel.current_playlist_id)
            panel.load_playlist_tracks(tracks)

    @Slot(list, int)
    def _on_add_to_saved_playlist(self, track_ids: list[int], playlist_id: int) -> None:
        self._db.add_tracks_to_playlist(playlist_id, track_ids)
        self._refresh_saved_playlists()
        # Reload if viewing this playlist.
        if self._playlists_panel.current_playlist_id == playlist_id:
            tracks = self._db.get_playlist_tracks(playlist_id)
            self._playlists_panel.load_playlist_tracks(tracks)

    # ------------------------------------------------------------------
    # Queue panel toggle
    # ------------------------------------------------------------------

    def _toggle_queue_panel(self) -> None:
        if self._fullscreen_overlay is not None:
            return  # Queue toggle handled by fullscreen overlay.
        visible = not self._queue_widget.isVisible()
        self._queue_widget.setVisible(visible)
        self._transport.set_queue_checked(visible)
        s = QSettings("Quaver", "Quaver")
        s.setValue("queue/panel_visible", visible)

    # ------------------------------------------------------------------
    # Library scan progress
    # ------------------------------------------------------------------

    @Slot()
    def _on_scan_started(self) -> None:
        self._transport.show_status("Scanning\u2026")

    @Slot(int, int)
    def _on_scan_progress(self, done: int, total: int) -> None:
        self._transport.show_progress("Scanning\u2026", done, total)

    @Slot(str)
    def _on_scan_status(self, message: str) -> None:
        self._transport.show_status(message)

    @Slot()
    def _on_scan_finished(self) -> None:
        self._transport.hide_progress()
        # Auto-start metadata enrichment if enabled (new tracks only).
        config = load_config()
        if config.auto_enrich:
            self._maybe_start_enrichment(include_failed=False)

    # ------------------------------------------------------------------
    # Metadata enrichment
    # ------------------------------------------------------------------

    def _maybe_start_enrichment(
        self, *, tab_index: int | None = None, include_failed: bool = True,
    ) -> None:
        """Start metadata enrichment based on each tab's metadata_source.

        If *tab_index* is given, only enrich that tab.
        If *include_failed* is False, only process tracks that have never been
        enriched (skip previously-failed ``*:not_found`` tracks).
        """
        if self._metadata_service.is_running:
            return

        config = load_config()

        # Determine which source paths to enrich.
        if tab_index is not None:
            tabs = config.library_tabs
            tab = tabs[tab_index] if 0 <= tab_index < len(tabs) else None
            eligible_paths = set(tab.source_paths) if tab else set()
        else:
            eligible_paths = None  # all sources

        # Build lookup from path → SourceConfig.
        src_by_path = {s.path: s for s in config.sources}

        # Group eligible sources by scraper.
        jobs: list[EnrichmentJob] = []
        by_scraper: dict[str, list[str]] = {}
        for path, src in src_by_path.items():
            if eligible_paths is not None and path not in eligible_paths:
                continue
            if src.scraper == "none":
                continue
            if src.scraper == "tmdb" and not config.tmdb_api_key:
                continue
            by_scraper.setdefault(src.scraper, []).append(src.path)
        for scraper, folder_paths in by_scraper.items():
            jobs.append(EnrichmentJob(
                provider=scraper,
                folder_paths=folder_paths,
                tmdb_api_key=config.tmdb_api_key,
                include_failed=include_failed,
            ))

        if jobs:
            self._metadata_service.start_enrichment(jobs)

    @Slot()
    def _on_enrichment_started(self) -> None:
        self._transport.show_status("Scraping metadata\u2026")

    @Slot(int, int)
    def _on_enrichment_progress(self, done: int, total: int) -> None:
        self._transport.show_progress("Scraping metadata\u2026", done, total)

    @Slot()
    def _on_enrichment_finished(self) -> None:
        self._transport.hide_progress()
        # Reload library so new posters/genres/plots appear.
        self._refresh_library()
        # Reload queue to pick up newly-downloaded poster thumbnails.
        self._handle_queue_changed()
        # Refresh Now Playing art if currently playing a video track — the
        # poster may have just been downloaded by enrichment.
        track = self._player.current_track
        if track and track.media_type in ("movie", "series_episode") and track.id >= 0:
            vm = self._db.get_video_metadata(track.id)
            if vm and vm.get("poster_path"):
                self._nowplaying_panel._load_art(
                    track.filepath, poster_path=vm["poster_path"],
                )

    @Slot(str)
    def _on_enrichment_error(self, msg: str) -> None:
        import logging
        logging.getLogger(__name__).warning("Metadata enrichment error: %s", msg)

    def _on_refresh_metadata(self, tab: LibraryTabWidget) -> None:
        """Rescan the tab's sources (scanning now includes scraper metadata)."""
        for cfg, widget in self._library_tabs:
            if widget is tab and cfg is not None:
                sources = self._db.get_sources()
                folder_set = set(cfg.source_paths)
                source_ids = [s["id"] for s in sources if s["path"] in folder_set]
                if source_ids:
                    self._library_service.start_scan(source_ids=source_ids)
                return

    def _on_scrape_metadata(self, track_ids: list[int]) -> None:
        """Handle 'Scrape Metadata' context menu — re-enrich selected tracks."""
        if not track_ids:
            return
        self._db.clear_metadata_provider(track_ids)
        # Trigger enrichment for all eligible tabs (the cleared tracks will be picked up).
        self._maybe_start_enrichment()

    def _on_scrape_show(self, show_id: int) -> None:
        """Handle per-show 'Scrape Metadata' from sidebar — enrich one show only."""
        if self._metadata_service.is_running:
            return
        show = self._db.get_show(show_id)
        if not show:
            return
        # Use the show's existing scraper binding, or fall back to source config.
        provider = show.get("scraper")
        if not provider:
            # Determine from the source that contains this show's episodes.
            episode_ids = self._db.get_show_episode_ids(show_id)
            if episode_ids:
                track = self._db.get_track_by_id(episode_ids[0])
                if track:
                    provider = self._db.get_scraper_for_path(track.filepath)
        provider = provider or "tvmaze"
        config = load_config()
        api_key = config.tmdb_api_key if provider == "tmdb" else ""
        self._metadata_service.start_show_enrichment(show_id, provider, api_key)

    def _on_show_info(self, show_id: int) -> None:
        """Handle 'Show Information...' — display metadata panel for a show."""
        from quaver.ui.dialogs.show_info import ShowInfoDialog

        show = self._db.get_show(show_id)
        if not show:
            return

        episode_ids = self._db.get_show_episode_ids(show_id)
        dlg = ShowInfoDialog(show, episode_ids, self._db, parent=self)

        # "Change..." button → close info, open match picker.
        dlg.change_match_requested.connect(lambda: dlg.reject())
        dlg.change_match_requested.connect(lambda: self._on_change_match(show_id))
        dlg.scrape_requested.connect(lambda: self._on_scrape_show(show_id))
        dlg.exec()

    def _on_movie_info(self, track_id: int) -> None:
        """Handle 'Movie Information...' — display metadata panel for a movie track."""
        from quaver.ui.dialogs.show_info import ShowInfoDialog

        track = self._db.get_track_by_id(track_id)
        if not track:
            return
        vm = self._db.get_video_metadata(track_id) or {}

        # Build a synthetic show dict from movie track + video_metadata.
        movie_dict = {
            "id": track_id,
            "title": track.title or "",
            "folder_title": track.title or "",
            "year": track.year,
            "genre": vm.get("genre", "") or track.genre or "",
            "plot": vm.get("plot", ""),
            "poster_path": vm.get("poster_path", ""),
            "backdrop_path": vm.get("backdrop_path", ""),
            "scraper": vm.get("metadata_provider", ""),
            "scraper_id": vm.get("tmdb_id"),
            "imdb_id": vm.get("imdb_id", ""),
            "status": "",
        }

        dlg = ShowInfoDialog(movie_dict, is_movie=True, parent=self)
        dlg.scrape_requested.connect(
            lambda: self._on_scrape_metadata([track_id]),
        )
        dlg.exec()

    def _on_change_match(self, show_id: int) -> None:
        """Handle 'Change Match...' — open match picker, re-bind, re-enrich."""
        from quaver.ui.dialogs.match_picker import MatchPickerDialog

        show = self._db.get_show(show_id)
        if not show:
            return

        # Determine provider from show's scraper binding or source config.
        provider = show.get("scraper")
        if not provider:
            episode_ids = self._db.get_show_episode_ids(show_id)
            if episode_ids:
                track = self._db.get_track_by_id(episode_ids[0])
                if track:
                    provider = self._db.get_scraper_for_path(track.filepath)
        provider = provider or "tvmaze"
        config = load_config()
        api_key = config.tmdb_api_key if provider == "tmdb" else ""

        # Determine media type from the show's episodes.
        episode_ids = self._db.get_show_episode_ids(show_id)
        media_type = "series"  # default
        if episode_ids:
            track = self._db.get_track_by_id(episode_ids[0])
            if track and track.media_type == "movie":
                media_type = "movie"

        dlg = MatchPickerDialog(show, provider, media_type, api_key, self)
        if dlg.exec() != MatchPickerDialog.DialogCode.Accepted:
            return

        choice = dlg.selected_match()
        if not choice:
            return

        # Update the show binding with the user's choice.
        self._db.update_show(
            show_id,
            scraper=choice.scraper,
            scraper_id=choice.scraper_id,
            title=choice.title,
            year=choice.year,
        )

        # Clear metadata_provider on all episodes so re-enrichment picks them up.
        if episode_ids:
            self._db.clear_metadata_provider(episode_ids)

        # Trigger re-enrichment for this show.
        re_provider = choice.scraper
        re_key = config.tmdb_api_key if re_provider == "tmdb" else ""
        self._metadata_service.start_show_enrichment(show_id, re_provider, re_key)

    def _on_mark_show_watched(self, show_id: int, watched: bool) -> None:
        """Mark all episodes of a show as watched/unwatched."""
        self._db.mark_show_watched(show_id, watched)
        # Update the album view in all library tabs.
        episode_ids = self._db.get_show_episode_ids(show_id)
        for tid in episode_ids:
            vm = self._db.get_video_metadata(tid)
            w = bool(vm["watched"]) if vm else watched
            pos = vm["watched_position_ms"] if vm else 0
            for _, tab in self._library_tabs:
                tab.update_video_state(tid, w, pos)

    def _on_remove_show(self, show_id: int) -> None:
        """Remove a show and all its episodes from the library."""
        from PySide6.QtWidgets import QMessageBox

        show = self._db.get_show(show_id)
        if not show:
            return
        title = show.get("title") or show.get("folder_title", "")
        reply = QMessageBox.question(
            self, "Remove from Library",
            f"Remove \"{title}\" and all its episodes from the library?\n\n"
            "This will not delete files from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._db.remove_show(show_id)
        self._refresh_library()

    # ------------------------------------------------------------------
    # Playback settings
    # ------------------------------------------------------------------

    def _set_rg_mode(self, mode: RGMode) -> None:
        """Set ReplayGain mode and persist to config."""
        self._player.set_rg_mode(mode)
        from quaver.core.config import load_config, save_config
        config = load_config()
        config.replaygain_mode = mode.name.lower()
        save_config(config)

    def _apply_playback_settings(self) -> None:
        """Apply persisted playback settings (EQ, compressor, speed, RG) on startup."""
        config = load_config()

        # ReplayGain.
        rg_map = {"off": RGMode.OFF, "track": RGMode.TRACK, "album": RGMode.ALBUM}
        rg_mode = rg_map.get(config.replaygain_mode, RGMode.OFF)
        self._player.set_rg_mode(rg_mode)

        # Equalizer.
        if any(b != 0.0 for b in config.eq_bands):
            self._player.set_eq_bands(config.eq_bands)
            if self._eq_popup is not None:
                self._eq_popup.set_gains(config.eq_bands)

        # Compressor.
        if config.compressor_enabled:
            self._player.set_compressor(
                config.compressor_enabled,
                config.compressor_ratio,
                config.compressor_threshold,
            )

        # Playback speed.
        if config.playback_speed != 1.0:
            self._player.set_playback_speed(config.playback_speed)

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------

    def _apply_appearance(self) -> None:
        """Apply transparency and blur-behind settings from config."""
        config = load_config()
        self._bg_alpha = int(config.window_opacity * 255)
        self._bg_color = config.bg_color
        self._bg_tint_alpha = int(config.bg_tint_strength * 255 / 100)

        if config.window_opacity < 1.0:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setStyleSheet(self._build_transparent_stylesheet())
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self._bg_alpha = 255
            self.setStyleSheet("")

        # KDE blur-behind via KWin — works on both Wayland and X11.
        # Only meaningful below full opacity; at 1.0 we deliberately ask for OFF.
        self._blur_want = config.blur_behind and config.window_opacity < 1.0
        self._apply_blur()

        # Now Playing background mode.
        self._nowplaying_panel.set_bg_mode(config.nowplaying_bg_mode)
        self._nowplaying_panel.set_tint_alpha(config.nowplaying_tint_alpha)
        self._nowplaying_panel.set_blur_strength(config.nowplaying_blur_strength)
        self._nowplaying_panel.set_bg_opacity(config.nowplaying_bg_opacity)
        self._nowplaying_panel.set_art_size(config.nowplaying_art_size)

        # Spectrum visualizer visibility on Now Playing.
        self._nowplaying_panel.set_spectrum_visible(config.nowplaying_show_spectrum)

        # Subtitle settings.
        self._nowplaying_panel._subtitle_overlay.set_font_size(config.subtitle_font_size)
        self._nowplaying_panel._subtitle_overlay.set_outline(config.subtitle_outline)
        self._nowplaying_panel._subtitle_overlay.set_bg_bar(config.subtitle_bg)

        # Feed URL visibility in podcast/YouTube panels.
        self._podcast_panel.set_show_urls(config.show_feed_urls)
        self._youtube_panel.set_show_urls(config.show_feed_urls)

        # Genre display in album headers.
        for _, tab in self._library_tabs:
            tab.set_show_genre(config.show_genre_in_headers)

        self.update()

    def paintEvent(self, event) -> None:
        """Paint the window background with configurable color/transparency."""
        from PySide6.QtGui import QPainter, QColor
        painter = QPainter(self)
        r, g, b = self._bg_color
        if self._bg_alpha < 255:
            # Transparent mode: paint the tinted overlay.
            # The compositor blur shows through the transparent areas.
            # Tint strength controls how opaque the color is on top of blur.
            tint_alpha = int(self._bg_tint_alpha * self._bg_alpha / 255)
            painter.fillRect(self.rect(), QColor(r, g, b, tint_alpha))
        else:
            painter.fillRect(self.rect(), QColor(r, g, b))
        painter.end()

    def _build_transparent_stylesheet(self) -> str:
        """Build a stylesheet for transparent mode that keeps UI usable."""
        return """
            /* ── Layout containers: fully transparent ── */
            QMainWindow, QWidget#central, QTabWidget, QTabWidget::pane,
            QSplitter, QStackedWidget
            { background: transparent; }

            /* ── Tab bar ── */
            QTabBar
            { background: transparent; }
            QTabBar::tab
            { background: rgba(255, 255, 255, 8); color: rgba(255, 255, 255, 180);
              padding: 6px 12px; border-bottom: 2px solid transparent; }
            QTabBar::tab:selected
            { background: rgba(255, 255, 255, 18);
              border-bottom: 2px solid palette(highlight); color: white; }
            QTabBar::tab:hover:!selected
            { background: rgba(255, 255, 255, 12); }

            /* ── List/table views: subtle panel background ── */
            QListWidget, QListView, QTableWidget, QTableView,
            QTreeView, QTreeWidget
            { background: rgba(0, 0, 0, 30); border: none;
              color: rgba(255, 255, 255, 210); }

            QListWidget::item, QListView::item,
            QTableWidget::item, QTableView::item
            { background: transparent; }

            QListWidget::item:selected, QListView::item:selected,
            QTableWidget::item:selected, QTableView::item:selected
            { background: rgba(255, 255, 255, 40); }

            QListWidget::item:hover:!selected, QTableView::item:hover:!selected
            { background: rgba(255, 255, 255, 15); }

            /* ── Headers ── */
            QHeaderView
            { background: transparent; }
            QHeaderView::section
            { background: rgba(255, 255, 255, 12); color: rgba(255, 255, 255, 160);
              border: none; border-bottom: 1px solid rgba(255, 255, 255, 20);
              padding: 4px 6px; }

            /* ── Scroll bars ── */
            QScrollBar:vertical, QScrollBar:horizontal
            { background: transparent; width: 8px; height: 8px; }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal
            { background: rgba(255, 255, 255, 40); border-radius: 4px;
              min-height: 20px; min-width: 20px; }
            QScrollBar::handle:hover
            { background: rgba(255, 255, 255, 60); }
            QScrollBar::add-line, QScrollBar::sub-line,
            QScrollBar::add-page, QScrollBar::sub-page
            { background: transparent; height: 0; width: 0; }

            /* ── Splitter handles ── */
            QSplitter::handle
            { background: rgba(255, 255, 255, 20); }
            QSplitter::handle:hover
            { background: rgba(255, 255, 255, 40); }
            QSplitter::handle:horizontal { width: 3px; }
            QSplitter::handle:vertical { height: 3px; }

            /* ── Text browser (episode description) ── */
            QTextBrowser
            { background: rgba(0, 0, 0, 20); border: none;
              color: rgba(255, 255, 255, 190); }

            /* ── Labels ── */
            QLabel
            { background: transparent; color: rgba(255, 255, 255, 200); }

            /* ── Buttons ── */
            QPushButton
            { background: rgba(255, 255, 255, 12); color: rgba(255, 255, 255, 200);
              border: 1px solid rgba(255, 255, 255, 20); padding: 4px 8px;
              border-radius: 3px; }
            QPushButton:hover
            { background: rgba(255, 255, 255, 25); }
            QPushButton:pressed
            { background: rgba(255, 255, 255, 8); }
            QPushButton:checked
            { background: rgba(255, 255, 255, 30);
              border-color: rgba(255, 255, 255, 40); }
            QPushButton:flat
            { border: none; }

            /* ── Sliders ── */
            QSlider::groove:horizontal
            { background: rgba(255, 255, 255, 25); height: 4px;
              border-radius: 2px; }
            QSlider::handle:horizontal
            { background: rgba(255, 255, 255, 180); width: 12px; height: 12px;
              margin: -4px 0; border-radius: 6px; }
            QSlider::sub-page:horizontal
            { background: palette(highlight); border-radius: 2px; }

            /* ── Checkboxes ── */
            QCheckBox
            { background: transparent; color: rgba(255, 255, 255, 200);
              spacing: 6px; }
            QCheckBox::indicator
            { width: 16px; height: 16px;
              border: 1px solid rgba(255, 255, 255, 60);
              border-radius: 3px; background: rgba(0, 0, 0, 40); }
            QCheckBox::indicator:checked
            { background: palette(highlight);
              border-color: palette(highlight); }

            /* ── Status bar ── */
            QStatusBar
            { background: transparent; color: rgba(255, 255, 255, 150); }
        """

    def _apply_blur(self) -> None:
        """Push the current blur setting to KWin.

        No-ops harmlessly before the window is first shown (there is no
        native window to blur yet); showEvent re-applies it.
        """
        set_blur_behind(self, self._blur_want)

    def showEvent(self, ev) -> None:
        # windowHandle() is None until now, so this is the first point at
        # which blur can actually be applied. Qt may also recreate the
        # native window (fullscreen transitions), which lands here again.
        super().showEvent(ev)
        self._apply_blur()

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def _show_preferences(self) -> None:
        dlg = PreferencesDialog(parent=self)
        dlg.settings_changed.connect(self._apply_appearance)
        dlg.settings_changed.connect(self._apply_tab_visibility)
        dlg.exec()

    def _apply_tab_visibility(self) -> None:
        """Hide / show the built-in tabs (Playlists, Now Playing,
        Podcasts, YouTube) based on the four ``show_*_tab`` flags.

        The panels themselves are always constructed — toggling only
        adds or removes them from the QTabWidget.  This keeps every
        ``self._playlists_panel`` / ``self._nowplaying_panel`` reference
        scattered across MainWindow valid regardless of which tabs are
        currently shown.

        Re-enabled tabs are inserted in canonical order (Playlists,
        Now Playing, Podcasts, YouTube) immediately after the library
        tabs.  Reorderings done by the user via the tab bar are reset
        when this runs — that's the cost of toggling.
        """
        config = load_config()
        canonical = [
            (self._playlists_panel, "Playlists", config.show_playlists_tab),
            (self._nowplaying_panel, "Now Playing", config.show_nowplaying_tab),
            (self._podcast_panel, "Podcasts", config.show_podcasts_tab),
            (self._youtube_panel, "YouTube", config.show_youtube_tab),
        ]
        # Pull each panel out of the QTabWidget (if currently shown).
        for panel, _, _ in canonical:
            idx = self._tabs.indexOf(panel)
            if idx >= 0:
                self._tabs.removeTab(idx)
        # Re-insert enabled ones after the library tabs.  Tab 0 is the
        # hamburger; library tabs follow.
        insert_at = 1 + len(self._library_tabs)
        for panel, label, enabled in canonical:
            if enabled:
                self._tabs.insertTab(insert_at, panel, label)
                insert_at += 1

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._uninhibit_screensaver()
        self._save_state()
        self._youtube_service.shutdown()
        self._podcast_service.shutdown()
        self._library_service.shutdown()
        self._player.shutdown()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        import json
        s = QSettings("Quaver", "Quaver")
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("splitter/main", self._main_splitter.saveState())
        for i, (_, tab) in enumerate(self._library_tabs):
            # Files tabs have no inner splitter (single FolderTreeView).
            if tab._splitter is not None:
                s.setValue(f"splitter/library_{i}", tab._splitter.saveState())

        # Save tab order by label (skip hamburger at index 0).
        tab_order = [self._tabs.tabText(i) for i in range(1, self._tabs.count())]
        s.setValue("tabs/order", tab_order)
        s.setValue("tabs/current", self._tabs.currentIndex())
        s.setValue("tabs/locked", not self._tabs.tabBar().isMovable())

        # Save queue if enabled.
        config = load_config()
        if config.restore_queue:
            queue = self._player.queue
            entries = [
                {"id": t.id, "filepath": t.filepath,
                 "title": t.title, "artist": t.artist, "duration_ms": t.duration_ms}
                for t in queue
            ]
            s.setValue("queue/tracks", json.dumps(entries))
            s.setValue("queue/index", self._player.queue_index)
        else:
            s.remove("queue/tracks")
            s.remove("queue/index")

    def _restore_state(self) -> None:
        s = QSettings("Quaver", "Quaver")
        geo = s.value("window/geometry")
        if geo:
            self.restoreGeometry(geo)
        # Restore queue panel visibility.
        queue_visible = s.value("queue/panel_visible")
        if queue_visible is not None:
            visible = queue_visible == "true" or queue_visible is True
            self._queue_widget.setVisible(visible)
            self._transport.set_queue_checked(visible)

        main_state = s.value("splitter/main")
        if main_state:
            self._main_splitter.restoreState(main_state)

        # Restore tab order (offset by 1 to skip hamburger at index 0).
        saved_order = s.value("tabs/order")
        if saved_order and isinstance(saved_order, list):
            bar = self._tabs.tabBar()
            for target_pos, label in enumerate(saved_order):
                actual_pos = target_pos + 1  # +1 for hamburger
                for i in range(actual_pos, self._tabs.count()):
                    if self._tabs.tabText(i) == label:
                        if i != actual_pos:
                            bar.moveTab(i, actual_pos)
                        break
            current = s.value("tabs/current")
            if current is not None:
                idx = int(current)
                if idx >= 1:
                    self._tabs.setCurrentIndex(idx)
        # Restore tab lock state.
        locked = s.value("tabs/locked")
        if locked is not None and (locked == "true" or locked is True):
            self._tabs.setMovable(False)

        for i, (_, tab) in enumerate(self._library_tabs):
            state = s.value(f"splitter/library_{i}")
            if state and tab._splitter is not None:
                tab._splitter.restoreState(state)

        # Restore queue if enabled.
        import json
        config = load_config()
        if config.restore_queue:
            raw = s.value("queue/tracks")
            saved_index = s.value("queue/index")
            if raw:
                try:
                    entries = json.loads(raw)
                    tracks: list[Track] = []
                    for e in entries:
                        t = None
                        if e.get("id", -1) >= 0:
                            t = self._db.get_track_by_id(e["id"])
                        if t is None and e.get("filepath"):
                            t = self._db.get_track_by_path(e["filepath"])
                        if t is None and e.get("filepath"):
                            # Transient track (podcast episode, open-file, etc.)
                            # Reconstruct directly from saved metadata.
                            fp = e["filepath"]
                            t = Track(
                                id=-1, filepath=fp,
                                title=e.get("title") or Path(fp).stem,
                                artist=e.get("artist", ""),
                                album="", album_artist="", year=None, genre="",
                                track_number=None, disc_number=None,
                                duration_ms=e.get("duration_ms", 0),
                                bitrate=0, format="",
                                replay_gain_track=None, replay_gain_album=None,
                                play_count=0, last_played=None, date_added="",
                                rating=0, fingerprint=None,
                            )
                        if t is not None:
                            tracks.append(t)
                    if tracks:
                        idx = int(saved_index) if saved_index is not None else 0
                        self._player.load_queue(tracks, current_index=idx)
                except Exception:
                    pass
