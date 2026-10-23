"""Minim main window — distraction-free Now Playing + queue + transport.

Embeds the media_core widgets directly:
  - :class:`NowPlayingPanel` fills the centre.
  - :class:`QueueWidget` lives on the right (toggleable via transport).
  - :class:`TransportWidget` is pinned to the bottom.
  - :class:`FullscreenOverlay` is constructed on demand.

No library, no scanning, no scrapers.  File drops (Dolphin double-click,
``minim path/to/file``, File → Open, drop on the window) all funnel
through :func:`minim.tracks.tracks_from_paths` and replace the queue.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, QTimer, Qt, Slot
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMenu,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from media_core.core.audio import PlaybackState
from media_core.core.blur import set_blur_behind
from media_core.core.replaygain import RGMode
from media_core.core.track import Track
from media_core.services.player_service import PlayerService, RepeatMode
from media_core.services.tag_reader import TagReader
from media_core.ui.panels.nowplaying_panel import NowPlayingPanel
from media_core.ui.widgets.dsp_popup import DspPopup
from media_core.ui.widgets.eq_popup import EqPopup
from media_core.ui.widgets.fullscreen_overlay import FullscreenOverlay
from media_core.ui.widgets.queue_widget import QueueWidget
from media_core.ui.widgets.transport import TransportWidget
from media_core.ui.window import size_for_first_run

from minim.core.config import load_config, save_config
from minim.tracks import tracks_from_paths
from minim.ui.dialogs.preferences import PreferencesDialog

try:
    from media_core.services.mpris2 import Mpris2Service
except ImportError:  # pydbus / dbus-python missing.
    Mpris2Service = None  # type: ignore[assignment,misc]

_log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """The whole Minim UI."""

    # Desired KWin blur state, recomputed by _apply_appearance. Declared at
    # class level so showEvent is safe even if it fires before the first
    # _apply_appearance call.
    _blur_want: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Minim")
        self.setMinimumSize(720, 480)
        self.setAcceptDrops(True)

        # Core service.  No DB — Minim is library-less.
        self._player = PlayerService(db=None)

        # Tracks opened from paths start with filename titles and get their
        # tags in the background.  ensure() reads the track about to play on
        # the spot, so it's connected before MPRIS and the UI, which all
        # read the track's metadata on track_changed.
        self._tag_reader = TagReader(self)
        self._player.track_changed.connect(self._tag_reader.ensure)

        # Optional MPRIS2.  Failure (no D-Bus, name taken) is non-fatal.
        self._mpris2 = None
        if Mpris2Service is not None:
            try:
                self._mpris2 = Mpris2Service(
                    self._player,
                    bus_name="org.mpris.MediaPlayer2.minim",
                    identity="Minim",
                    desktop_entry="minim",
                )
            except Exception as e:
                _log.info("MPRIS2 unavailable: %s", e)

        # Lazy-created popups + overlays.
        self._eq_popup: EqPopup | None = None
        self._dsp_popup: DspPopup | None = None
        self._fullscreen_overlay: FullscreenOverlay | None = None
        self._preferences_dialog: PreferencesDialog | None = None

        # Minimal Interface state — hides transport + queue, leaves the
        # Now Playing panel as the entire window.  Toggled with Ctrl+H
        # or via the panel's right-click context menu.
        self._minimal_ui: bool = False
        self._pre_minimal_queue_visible: bool = True

        # Window-chrome state (read by paintEvent + _apply_appearance).
        self._bg_alpha: int = 255
        self._bg_color: list[int] = [30, 30, 30]
        self._bg_tint_alpha: int = 178

        self._build_ui()
        self._connect_signals()
        self._install_shortcuts()
        self._apply_playback_settings()
        self._apply_appearance()
        self._restore_state()

        # Hide menu bar — Minim uses a hamburger button (corner of transport
        # row would also work, but Ctrl+O / right-click cover the same
        # surface for now).
        self.menuBar().setVisible(False)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setObjectName("main_splitter")

        # NowPlayingPanel — wire host hooks so the right-click
        # appearance submenu (Background / Overlay Intensity / Blur /
        # Art size / etc.) round-trips through Minim's TOML config.
        # poster_reader stays at the default no-op (no scraper).
        # extra_context_actions exposes the Minimal Interface toggle on
        # the panel's right-click menu so users can exit minimal mode
        # without remembering Ctrl+H.
        self._nowplaying = NowPlayingPanel(
            load_config=load_config,
            save_config=save_config,
            extra_context_actions=self._panel_extra_actions,
        )

        # QueueWidget — _playlists stays None → Minim mode (no
        # save-as-playlist actions, no add-to-playlist submenu).
        self._queue = QueueWidget()

        self._main_splitter.addWidget(self._nowplaying)
        self._main_splitter.addWidget(self._queue)
        self._main_splitter.setStretchFactor(0, 4)
        self._main_splitter.setStretchFactor(1, 1)
        layout.addWidget(self._main_splitter, stretch=1)

        # Transport pinned to the bottom.
        self._transport = TransportWidget()
        self._transport.set_volume(self._player.get_volume())
        # Auto-DJ button doesn't make sense without a library — hide it.
        self._transport._btn_autodj.setVisible(False)
        layout.addWidget(self._transport)

    def _connect_signals(self) -> None:
        # Player → UI.
        self._player.state_changed.connect(self._on_state_changed)
        self._player.track_changed.connect(self._on_track_changed)
        self._player.queue_changed.connect(self._on_queue_changed)
        self._tag_reader.tracks_updated.connect(self._queue.refresh_tracks)
        self._player.position_updated.connect(self._transport.set_position)
        self._player.spectrum_updated.connect(
            self._transport.spectrum_widget.update_magnitudes
        )
        self._player.spectrum_updated.connect(
            self._nowplaying.spectrum_widget.update_magnitudes
        )
        self._player.shuffle_changed.connect(self._transport.set_shuffle_checked)
        self._player.repeat_changed.connect(self._transport.set_repeat_label)
        self._player.error_occurred.connect(self._on_error)

        # Transport → player + window actions.
        self._transport.play_toggled.connect(self._toggle_play)
        self._transport.stop_clicked.connect(self._player.stop)
        self._transport.prev_clicked.connect(self._player.prev_track)
        self._transport.next_clicked.connect(self._player.next_track)
        self._transport.seek_requested.connect(self._player.seek)
        self._transport.volume_changed.connect(self._player.set_volume)
        self._transport.shuffle_toggled.connect(self._player.toggle_shuffle)
        self._transport.repeat_cycled.connect(self._player.cycle_repeat)
        self._transport.eq_clicked.connect(self._show_eq)
        self._transport.dsp_clicked.connect(self._show_dsp)
        self._transport.queue_toggled.connect(self._toggle_queue_panel)
        self._transport.fullscreen_clicked.connect(self._enter_fullscreen)

        # Now Playing → window.
        self._nowplaying.fullscreen_requested.connect(self._enter_fullscreen)
        self._nowplaying.volume_change_requested.connect(self._on_np_volume_change)

        # Queue → player.
        self._queue.track_activated.connect(self._on_queue_track_activated)
        self._queue.remove_requested.connect(self._player.remove_from_queue)
        self._queue.move_requested.connect(self._player.move_in_queue)
        self._queue.move_batch_requested.connect(self._player.move_batch_in_queue)
        self._queue.clear_requested.connect(self._player.clear_queue)
        # Tracks dragged in from another Minim window's queue.
        self._queue.tracks_dropped.connect(self._on_tracks_dropped)

    def _install_shortcuts(self) -> None:
        # File → Open.
        open_action = QAction(self)
        open_action.setShortcut(QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_O))
        open_action.triggered.connect(self._open_files)
        self.addAction(open_action)

        # F11 fullscreen toggle.
        fs_action = QAction(self)
        fs_action.setShortcut(QKeySequence(Qt.Key.Key_F11))
        fs_action.triggered.connect(self._toggle_fullscreen)
        self.addAction(fs_action)

        # Ctrl+H — Minimal Interface toggle (VLC convention).
        min_action = QAction(self)
        min_action.setShortcut(QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_H))
        min_action.triggered.connect(self._toggle_minimal_ui)
        self.addAction(min_action)

        # Right-click anywhere on the window → mini menu.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_window_menu)

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    @Slot()
    def _open_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open Media", "",
            "Media Files (*.mp3 *.flac *.ogg *.opus *.m4a *.wav *.wma *.aac"
            " *.mkv *.mp4 *.m4v *.avi *.mov *.webm);;All Files (*)",
        )
        if paths:
            self.play_paths(paths)

    def play_paths(self, paths: list[str]) -> None:
        """Replace the queue with *paths* and start playback at the first."""
        tracks = tracks_from_paths(list(paths))
        if not tracks:
            self._transport.set_status_message("No playable files in selection")
            return
        self._transport.clear_status_message()
        self._tag_reader.cancel()  # the old queue is being replaced
        self._tag_reader.queue(tracks)
        self._player.play_tracks(tracks, start_index=0)

    def enqueue_paths(self, paths: list[str]) -> None:
        """Append *paths* to the queue without disturbing playback."""
        tracks = tracks_from_paths(list(paths))
        if not tracks:
            return
        self._tag_reader.queue(tracks)
        # If the queue was empty, start playback automatically — the
        # behavioural spec says "drag a folder onto the window → queue
        # populated and first plays".  Otherwise append the whole drop in
        # one queue change: one per track reloaded the queue each time,
        # freezing Minim for seconds on a big folder.
        if not self._player.queue:
            self._player.play_tracks(tracks, start_index=0)
        else:
            self._player.insert_tracks(tracks, len(self._player.queue))

    def play_paths_now(self, paths: list[str]) -> None:
        """Append *paths* to the queue and jump to the first appended track.

        If the queue is empty this is identical to :meth:`play_paths`.
        Otherwise the existing queue is preserved and playback hops
        straight to the new tracks — used for drops onto the Now Playing
        panel ("play this *now*").
        """
        tracks = tracks_from_paths(list(paths))
        if not tracks:
            self._transport.set_status_message("No playable files in selection")
            return
        self._transport.clear_status_message()
        self._tag_reader.queue(tracks)
        if not self._player.queue:
            self._player.play_tracks(tracks, start_index=0)
        else:
            start_index = len(self._player.queue)
            combined = list(self._player.queue) + tracks
            self._player.play_tracks(combined, start_index=start_index)

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if not urls:
            return super().dropEvent(event)
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if not paths:
            return
        # Route by drop location: queue → append, Now Playing → play now.
        # Anywhere else (transport, dead space) falls back to the legacy
        # behaviour: replace if idle, otherwise append.
        target = self._drop_target_at(event.position().toPoint())
        if target == "panel":
            self.play_paths_now(paths)
        elif target == "queue":
            self.enqueue_paths(paths)
        elif self._player.queue:
            self.enqueue_paths(paths)
        else:
            self.play_paths(paths)
        event.acceptProposedAction()

    def _drop_target_at(self, pos) -> str | None:
        """Return ``"panel"``, ``"queue"`` or ``None`` for *pos*
        (window-relative). Queue takes precedence only when visible —
        if it's hidden, the panel claims the whole splitter area.
        """
        w = self.childAt(pos)
        if w is None:
            return None
        if self._queue.isVisible() and (
            w is self._queue or self._queue.isAncestorOf(w)
        ):
            return "queue"
        if w is self._nowplaying or self._nowplaying.isAncestorOf(w):
            return "panel"
        return None

    # ------------------------------------------------------------------
    # Player → UI handlers
    # ------------------------------------------------------------------

    @Slot(PlaybackState)
    def _on_state_changed(self, state: PlaybackState) -> None:
        self._transport.set_play_state(state)
        if state == PlaybackState.STOPPED:
            self._transport.spectrum_widget.clear()
            self._nowplaying.spectrum_widget.clear()

    @Slot(object)
    def _on_track_changed(self, track: Track | None) -> None:
        self._transport.show_track(track)

        # Hand the engine to the panel + queue for video stream menus.
        is_video = track is not None and track.media_type in (
            "movie", "series_episode",
        )
        if is_video:
            self._nowplaying.current_engine = self._player.engine
            self._queue.set_engine(self._player.engine)
        else:
            self._nowplaying.current_engine = None
            self._queue.set_engine(None)

        self._nowplaying.update_track(track)

    @Slot()
    def _on_queue_changed(self) -> None:
        self._queue.load_queue(self._player.queue, self._player.queue_index)

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self._transport.set_status_message(f"Error: {msg}")

    # ------------------------------------------------------------------
    # Transport / Now Playing handlers
    # ------------------------------------------------------------------

    @Slot()
    def _toggle_play(self) -> None:
        self._player.toggle_play()

    @Slot(int)
    def _on_queue_track_activated(self, row: int) -> None:
        if 0 <= row < len(self._player.queue):
            self._player.play_tracks(self._player.queue, start_index=row)

    @Slot(list, int)
    def _on_tracks_dropped(self, tracks: list[Track], position: int) -> None:
        """Tracks dragged from another Minim window's queue.

        If this Minim is idle (empty queue) the receiver becomes the
        new playlist and starts at the first dropped track — same UX as
        a fresh file drop on the panel. Otherwise we just splice the
        tracks in at the requested row.
        """
        if not tracks:
            return
        if not self._player.queue:
            self._player.play_tracks(list(tracks), start_index=0)
            return
        self._player.insert_tracks(list(tracks), position)

    @Slot(float)
    def _on_np_volume_change(self, delta: float) -> None:
        new_vol = max(0.0, min(1.0, self._player.get_volume() + delta))
        self._player.set_volume(new_vol)
        self._transport.set_volume(new_vol)

    @Slot()
    def _show_eq(self) -> None:
        if self._eq_popup is None:
            config = load_config()
            self._eq_popup = EqPopup(parent=self)
            self._eq_popup.set_gains(config.eq_bands)
            self._eq_popup.bands_changed.connect(self._player.set_eq_bands)
            self._eq_popup.bands_changed.connect(self._save_eq_bands)
        self._eq_popup.show()
        self._eq_popup.raise_()

    @Slot()
    def _show_dsp(self) -> None:
        if self._dsp_popup is None:
            config = load_config()
            self._dsp_popup = DspPopup(parent=self)
            self._dsp_popup.set_compressor_state(
                config.compressor_enabled,
                config.compressor_ratio,
                config.compressor_threshold,
            )
            self._dsp_popup.set_speed(config.playback_speed)
            self._dsp_popup.compressor_changed.connect(self._player.set_compressor)
            self._dsp_popup.compressor_changed.connect(self._save_compressor)
            self._dsp_popup.speed_changed.connect(self._player.set_playback_speed)
            self._dsp_popup.speed_changed.connect(self._save_speed)
        self._dsp_popup.show()
        self._dsp_popup.raise_()

    def _save_eq_bands(self, gains: list[float]) -> None:
        config = load_config()
        config.eq_bands = list(gains)
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

    @Slot()
    def _toggle_queue_panel(self) -> None:
        if self._fullscreen_overlay is not None:
            return
        visible = not self._queue.isVisible()
        self._queue.setVisible(visible)
        self._transport.set_queue_checked(visible)

    # ------------------------------------------------------------------
    # Fullscreen
    # ------------------------------------------------------------------

    @Slot()
    def _enter_fullscreen(self) -> None:
        if self._fullscreen_overlay is not None:
            return
        overlay = FullscreenOverlay(
            nowplaying_panel=self._nowplaying,
            transport_widget=self._transport,
            queue_widget=self._queue,
            parent=self,
        )
        self._fullscreen_overlay = overlay
        overlay.closed.connect(self._on_fullscreen_closed)

    @Slot()
    def _on_fullscreen_closed(self) -> None:
        overlay = self._fullscreen_overlay
        if overlay is None:
            return
        # Reparent panel + queue back into the splitter.
        self._main_splitter.insertWidget(0, self._nowplaying)
        self._nowplaying.show()

        queue_visible = overlay._queue_visible
        self._main_splitter.addWidget(self._queue)
        self._queue.setVisible(queue_visible)
        self._transport.set_queue_checked(queue_visible)

        # Reparent transport back to the bottom of the central layout.
        central_layout = self.centralWidget().layout()
        self._transport.setParent(self.centralWidget())
        central_layout.addWidget(self._transport)
        self._transport._btn_fullscreen.setVisible(True)
        self._transport.show()

        overlay.deleteLater()
        self._fullscreen_overlay = None

    @Slot()
    def _toggle_fullscreen(self) -> None:
        if self._fullscreen_overlay is not None:
            self._fullscreen_overlay._exit_fullscreen()
        else:
            self._enter_fullscreen()

    # ------------------------------------------------------------------
    # Minimal Interface (Ctrl+H) — hides transport + queue
    # ------------------------------------------------------------------

    @Slot()
    def _toggle_minimal_ui(self) -> None:
        """Hide / show transport + queue, leaving Now Playing as the
        whole window.  Mirrors VLC's Ctrl+H "Minimal Interface" mode.
        """
        # No-op while a fullscreen overlay owns the panel/transport —
        # those widgets are reparented and toggling visibility from
        # under FullscreenOverlay would corrupt its state.
        if self._fullscreen_overlay is not None:
            return

        if not self._minimal_ui:
            # Entering: remember the queue's visibility so we can
            # restore it on exit, then hide both chrome surfaces.
            self._pre_minimal_queue_visible = self._queue.isVisible()
            self._queue.setVisible(False)
            self._transport.setVisible(False)
            self._minimal_ui = True
        else:
            # Exiting: restore the queue's prior visibility (don't just
            # blindly show it — the user may have had it hidden).
            self._queue.setVisible(self._pre_minimal_queue_visible)
            self._transport.set_queue_checked(self._pre_minimal_queue_visible)
            self._transport.setVisible(True)
            self._minimal_ui = False

    def _panel_extra_actions(self) -> list[tuple[str, object]]:
        """Build the host-injected items appended to the Now Playing
        right-click menu.  Called fresh each time the menu opens, so
        the Minimal-Interface label reflects current state.

        The panel's right-click is the only reliable way into
        Preferences — the QMainWindow-level customContextMenu only
        fires in dead space, which is rare (panel/queue/transport eat
        every right-click).
        """
        if self._minimal_ui:
            min_label = "Exit Minimal Interface\tCtrl+H"
        else:
            min_label = "Minimal Interface\tCtrl+H"
        return [
            (min_label, self._toggle_minimal_ui),
            ("Preferences…", self._show_preferences),
        ]

    # ------------------------------------------------------------------
    # Window menu (right-click)
    # ------------------------------------------------------------------

    @Slot()
    def _show_window_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("&Open File…\tCtrl+O", self._open_files)
        menu.addSeparator()
        menu.addAction("&Play/Pause", self._toggle_play)
        menu.addAction("&Stop", self._player.stop)
        menu.addAction("&Next", self._player.next_track)
        menu.addAction("Pre&vious", self._player.prev_track)
        menu.addSeparator()
        rg = menu.addMenu("&ReplayGain")
        rg.addAction("Off", lambda: self._player.set_rg_mode(RGMode.OFF))
        rg.addAction("Track", lambda: self._player.set_rg_mode(RGMode.TRACK))
        rg.addAction("Album", lambda: self._player.set_rg_mode(RGMode.ALBUM))
        menu.addSeparator()
        if self._minimal_ui:
            menu.addAction("Exit Minimal &Interface\tCtrl+H",
                           self._toggle_minimal_ui)
        else:
            menu.addAction("Minimal &Interface\tCtrl+H",
                           self._toggle_minimal_ui)
        menu.addAction("&Preferences…", self._show_preferences)
        menu.addAction("E&xit", self.close)
        menu.exec(self.mapToGlobal(pos))

    @Slot()
    def _show_preferences(self) -> None:
        if self._preferences_dialog is None:
            self._preferences_dialog = PreferencesDialog(parent=self)
            self._preferences_dialog.settings_changed.connect(self._apply_appearance)
            self._preferences_dialog.settings_changed.connect(
                self._apply_playback_settings
            )
            self._preferences_dialog.finished.connect(
                lambda _: setattr(self, "_preferences_dialog", None)
            )
        self._preferences_dialog.show()
        self._preferences_dialog.raise_()
        self._preferences_dialog.activateWindow()

    # ------------------------------------------------------------------
    # Appearance + playback settings (from minim.core.config TOML)
    # ------------------------------------------------------------------

    def _apply_appearance(self) -> None:
        """Read the TOML config and push every appearance setting into
        the panel + window chrome.  Called once at startup and again
        whenever the Preferences dialog hits Apply/OK or the Now Playing
        right-click menu mutates a setting.
        """
        config = load_config()
        self._bg_color = list(config.bg_color)
        self._bg_alpha = int(config.window_opacity * 255)
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

        # Now Playing background.
        self._nowplaying.set_bg_mode(config.nowplaying_bg_mode)
        self._nowplaying.set_tint_alpha(config.nowplaying_tint_alpha)
        self._nowplaying.set_blur_strength(config.nowplaying_blur_strength)
        self._nowplaying.set_bg_opacity(config.nowplaying_bg_opacity)
        self._nowplaying.set_art_size(config.nowplaying_art_size)
        self._nowplaying.set_show_labels(config.nowplaying_show_labels)
        self._nowplaying.set_spectrum_visible(config.nowplaying_show_spectrum)
        self._nowplaying.set_video_mode(config.youtube_nowplaying_mode)

        # Subtitles.
        self._nowplaying._subtitle_overlay.set_font_size(config.subtitle_font_size)
        self._nowplaying._subtitle_overlay.set_outline(config.subtitle_outline)
        self._nowplaying._subtitle_overlay.set_bg_bar(config.subtitle_bg)

        self.update()

    def _apply_playback_settings(self) -> None:
        """Push playback-side settings (RG mode, EQ, compressor, speed)
        from the TOML config into the engine.
        """
        config = load_config()

        rg_map = {
            "off": RGMode.OFF,
            "track": RGMode.TRACK,
            "album": RGMode.ALBUM,
        }
        self._player.set_rg_mode(rg_map.get(config.replaygain_mode, RGMode.OFF))
        self._player.set_eq_bands(list(config.eq_bands))
        self._player.set_compressor(
            config.compressor_enabled,
            config.compressor_ratio,
            config.compressor_threshold,
        )
        self._player.set_playback_speed(config.playback_speed)

    def paintEvent(self, event) -> None:
        """Paint the window background at configured color + opacity."""
        painter = QPainter(self)
        r, g, b = self._bg_color
        if self._bg_alpha < 255:
            tint_alpha = int(self._bg_tint_alpha * self._bg_alpha / 255)
            painter.fillRect(self.rect(), QColor(r, g, b, tint_alpha))
        else:
            painter.fillRect(self.rect(), QColor(r, g, b))
        painter.end()

    def _build_transparent_stylesheet(self) -> str:
        """Stylesheet used when window_opacity < 1.0 — keeps child
        widgets readable on top of the compositor blur.
        """
        return """
            QMainWindow, QWidget#central, QSplitter, QStackedWidget
            { background: transparent; }

            QListWidget, QListView, QTableWidget, QTableView
            { background: rgba(0, 0, 0, 30); border: none;
              color: rgba(255, 255, 255, 210); }
            QListWidget::item, QListView::item,
            QTableWidget::item, QTableView::item
            { background: transparent; }
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
    # Persistence
    # ------------------------------------------------------------------

    def _settings(self) -> QSettings:
        return QSettings("Minim", "Minim")

    def _save_state(self) -> None:
        """Persist *only* window chrome (geometry, splitter, queue panel
        visibility, volume).  The queue itself is intentionally not
        saved — Minim is a "play now, gone when closed" player; the
        user opens a file, plays it, walks away, and the next launch
        starts clean.  No history, no playlists.
        """
        s = self._settings()
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("splitter/main", self._main_splitter.saveState())
        s.setValue("queue/panel_visible", self._queue.isVisible())
        s.setValue("volume", self._player.get_volume())
        # Stale queue keys from earlier versions get cleared on every
        # save so a previously-persisted queue can't resurface after an
        # upgrade.
        s.remove("queue/tracks")
        s.remove("queue/index")

    def _restore_state(self) -> None:
        s = self._settings()
        geo = s.value("window/geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            size_for_first_run(self, 0.45, 0.55)
        main_state = s.value("splitter/main")
        if main_state:
            self._main_splitter.restoreState(main_state)
        panel_visible = s.value("queue/panel_visible")
        if panel_visible is not None:
            visible = panel_visible == "true" or panel_visible is True
            self._queue.setVisible(visible)
            self._transport.set_queue_checked(visible)

        vol = s.value("volume")
        if vol is not None:
            try:
                v = float(vol)
                self._player.set_volume(v)
                self._transport.set_volume(v)
            except (TypeError, ValueError):
                pass
        # Queue is intentionally not restored — see _save_state.

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._save_state()
        self._player.shutdown()
        super().closeEvent(event)
