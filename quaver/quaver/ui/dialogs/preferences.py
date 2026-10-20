"""Preferences dialog — application settings."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from quaver.core.config import AppConfig, load_config, save_config


class PreferencesDialog(QDialog):
    """Application preferences with tabbed categories."""

    # Emitted when settings change that need immediate application.
    settings_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumSize(450, 350)

        self._config = load_config()
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # Appearance tab.
        tabs.addTab(self._build_appearance_tab(), "Appearance")

        # Playback tab.
        tabs.addTab(self._build_playback_tab(), "Playback")

        # Podcasts tab.
        tabs.addTab(self._build_podcasts_tab(), "Podcasts")

        # YouTube tab.
        tabs.addTab(self._build_youtube_tab(), "YouTube")

        # Video tab.
        tabs.addTab(self._build_video_tab(), "Video")

        layout.addWidget(tabs)

        # Buttons.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self._on_apply
        )
        layout.addWidget(buttons)

    def _build_appearance_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Now Playing background.
        np_group = QGroupBox("Now Playing")
        np_form = QFormLayout(np_group)

        self._np_bg_combo = QComboBox()
        self._np_bg_modes = [
            ("blur", "Blurred Album Art"),
            ("gradient_natural", "Gradient — Natural"),
            ("gradient_vivid", "Gradient — Vivid"),
            ("gradient_warm", "Gradient — Warm"),
            ("gradient_cool", "Gradient — Cool"),
            ("gradient_pastel", "Gradient — Pastel"),
            ("solid", "Solid Dark"),
            ("transparent", "Transparent"),
        ]
        for _, label in self._np_bg_modes:
            self._np_bg_combo.addItem(label)
        mode_key = self._config.nowplaying_bg_mode
        mode_idx = next(
            (i for i, (k, _) in enumerate(self._np_bg_modes) if k == mode_key), 0
        )
        self._np_bg_combo.setCurrentIndex(mode_idx)
        np_form.addRow("Background:", self._np_bg_combo)

        # Overlay intensity slider.
        self._np_tint_slider = QSlider(Qt.Orientation.Horizontal)
        self._np_tint_slider.setRange(0, 255)
        self._np_tint_slider.setValue(self._config.nowplaying_tint_alpha)
        np_form.addRow("Overlay intensity:", self._np_tint_slider)

        # Blur strength slider.
        self._np_blur_slider = QSlider(Qt.Orientation.Horizontal)
        self._np_blur_slider.setRange(1, 20)
        self._np_blur_slider.setValue(self._config.nowplaying_blur_strength)
        np_form.addRow("Blur strength:", self._np_blur_slider)

        # Background opacity slider.
        self._np_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._np_opacity_slider.setRange(0, 100)
        self._np_opacity_slider.setValue(int(self._config.nowplaying_bg_opacity * 100))
        np_form.addRow("Background opacity:", self._np_opacity_slider)

        # Art size slider (10–90% of panel height).
        self._np_art_size_slider = QSlider(Qt.Orientation.Horizontal)
        self._np_art_size_slider.setRange(10, 90)
        self._np_art_size_slider.setValue(self._config.nowplaying_art_size)
        np_form.addRow("Art size (% of panel):", self._np_art_size_slider)

        layout.addWidget(np_group)

        # Window transparency & color.
        win_group = QGroupBox("Window")
        win_form = QFormLayout(win_group)

        from PySide6.QtWidgets import QHBoxLayout, QPushButton, QColorDialog
        from PySide6.QtGui import QColor

        # Background color picker.
        self._bg_color = QColor(*self._config.bg_color)
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(60, 24)
        self._update_color_btn()
        self._color_btn.clicked.connect(self._pick_bg_color)
        win_form.addRow("Background color:", self._color_btn)

        # Opacity slider.
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(0, 100)
        self._opacity_slider.setValue(int(self._config.window_opacity * 100))
        self._opacity_label = QLabel(f"{self._opacity_slider.value()}%")
        self._opacity_slider.valueChanged.connect(
            lambda v: self._opacity_label.setText(f"{v}%")
        )
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(self._opacity_slider)
        opacity_row.addWidget(self._opacity_label)
        win_form.addRow("Transparency:", opacity_row)

        # Blur behind.
        self._blur_behind_cb = QCheckBox("Enable blur behind window (KDE)")
        self._blur_behind_cb.setChecked(self._config.blur_behind)
        win_form.addRow(self._blur_behind_cb)

        # Tint strength (controls how much bg color overlays the blur).
        self._tint_slider = QSlider(Qt.Orientation.Horizontal)
        self._tint_slider.setRange(0, 100)
        self._tint_slider.setValue(self._config.bg_tint_strength)
        self._tint_label = QLabel(f"{self._tint_slider.value()}%")
        self._tint_slider.valueChanged.connect(
            lambda v: self._tint_label.setText(f"{v}%")
        )
        tint_row = QHBoxLayout()
        tint_row.addWidget(self._tint_slider)
        tint_row.addWidget(self._tint_label)
        win_form.addRow("Tint strength:", tint_row)

        layout.addWidget(win_group)

        # Library display.
        lib_group = QGroupBox("Library")
        lib_form = QFormLayout(lib_group)

        self._show_genre_cb = QCheckBox("Show genre in album/movie headers")
        self._show_genre_cb.setChecked(self._config.show_genre_in_headers)
        lib_form.addRow(self._show_genre_cb)

        layout.addWidget(lib_group)

        # Subscriptions.
        sub_group = QGroupBox("Subscriptions")
        sub_form = QFormLayout(sub_group)

        self._show_feed_urls_cb = QCheckBox("Show feed/channel URLs")
        self._show_feed_urls_cb.setChecked(self._config.show_feed_urls)
        sub_form.addRow(self._show_feed_urls_cb)

        layout.addWidget(sub_group)

        # ── Built-in Tabs ─────────────────────────────────────────────
        tabs_group = QGroupBox("Built-in Tabs")
        tabs_form = QFormLayout(tabs_group)

        self._show_playlists_cb = QCheckBox("Show Playlists tab")
        self._show_playlists_cb.setChecked(self._config.show_playlists_tab)
        tabs_form.addRow(self._show_playlists_cb)

        self._show_podcasts_cb = QCheckBox("Show Podcasts tab")
        self._show_podcasts_cb.setChecked(self._config.show_podcasts_tab)
        tabs_form.addRow(self._show_podcasts_cb)

        self._show_youtube_cb = QCheckBox("Show YouTube tab")
        self._show_youtube_cb.setChecked(self._config.show_youtube_tab)
        tabs_form.addRow(self._show_youtube_cb)

        self._show_nowplaying_cb = QCheckBox("Show Now Playing tab")
        self._show_nowplaying_cb.setChecked(self._config.show_nowplaying_tab)
        tabs_form.addRow(self._show_nowplaying_cb)

        layout.addWidget(tabs_group)

        layout.addStretch()
        return widget

    def _build_playback_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        rg_group = QGroupBox("ReplayGain")
        rg_form = QFormLayout(rg_group)

        self._rg_modes = [("off", "Off"), ("track", "Track"), ("album", "Album")]
        self._rg_combo = QComboBox()
        for _, label in self._rg_modes:
            self._rg_combo.addItem(label)
        rg_idx = next(
            (i for i, (k, _) in enumerate(self._rg_modes) if k == self._config.replaygain_mode), 0
        )
        self._rg_combo.setCurrentIndex(rg_idx)
        rg_form.addRow("Mode:", self._rg_combo)

        layout.addWidget(rg_group)

        queue_group = QGroupBox("Queue")
        queue_form = QFormLayout(queue_group)

        self._restore_queue_cb = QCheckBox("Restore queue on startup")
        self._restore_queue_cb.setChecked(self._config.restore_queue)
        queue_form.addRow(self._restore_queue_cb)

        layout.addWidget(queue_group)
        layout.addStretch()
        return widget

    def _build_podcasts_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        pod_group = QGroupBox("Defaults for New Podcasts")
        pod_form = QFormLayout(pod_group)

        self._retain_spin = QSpinBox()
        self._retain_spin.setRange(1, 100)
        self._retain_spin.setValue(self._config.podcast_default_retain)
        pod_form.addRow("Episodes to retain:", self._retain_spin)

        self._refresh_hours = [6, 12, 24, 48]
        self._refresh_combo = QComboBox()
        self._refresh_combo.addItems([f"{h} hours" for h in self._refresh_hours])
        refresh_idx = next(
            (i for i, h in enumerate(self._refresh_hours) if h == self._config.podcast_default_refresh_hours), 2
        )
        self._refresh_combo.setCurrentIndex(refresh_idx)
        pod_form.addRow("Refresh interval:", self._refresh_combo)

        layout.addWidget(pod_group)
        layout.addStretch()
        return widget

    def _build_youtube_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        auth_group = QGroupBox("Authentication")
        auth_form = QFormLayout(auth_group)

        self._yt_auth_combo = QComboBox()
        self._yt_auth_browsers = [
            ("", "Guest (no login)"),
            ("firefox", "Firefox"),
            ("chromium", "Chromium"),
            ("chrome", "Chrome"),
            ("brave", "Brave"),
        ]
        for _, label in self._yt_auth_browsers:
            self._yt_auth_combo.addItem(label)
        auth_idx = next(
            (i for i, (k, _) in enumerate(self._yt_auth_browsers)
             if k == self._config.youtube_auth_browser), 0
        )
        self._yt_auth_combo.setCurrentIndex(auth_idx)
        auth_form.addRow("Browser cookies:", self._yt_auth_combo)
        layout.addWidget(auth_group)

        video_group = QGroupBox("Video")
        video_form = QFormLayout(video_group)

        self._yt_quality_combo = QComboBox()
        self._yt_qualities = [
            ("best", "Best available"),
            ("1080p", "1080p"),
            ("720p", "720p"),
            ("480p", "480p"),
            ("audio_only", "Audio only"),
        ]
        for _, label in self._yt_qualities:
            self._yt_quality_combo.addItem(label)
        quality_idx = next(
            (i for i, (k, _) in enumerate(self._yt_qualities)
             if k == self._config.youtube_video_quality), 0
        )
        self._yt_quality_combo.setCurrentIndex(quality_idx)
        video_form.addRow("Default quality:", self._yt_quality_combo)

        self._yt_np_combo = QComboBox()
        self._yt_np_modes = [
            ("thumbnail", "Thumbnail"),
            ("video", "Video Player"),
        ]
        for _, label in self._yt_np_modes:
            self._yt_np_combo.addItem(label)
        np_idx = next(
            (i for i, (k, _) in enumerate(self._yt_np_modes)
             if k == self._config.youtube_nowplaying_mode), 0
        )
        self._yt_np_combo.setCurrentIndex(np_idx)
        video_form.addRow("Now Playing display:", self._yt_np_combo)

        layout.addWidget(video_group)

        refresh_group = QGroupBox("Refresh")
        refresh_form = QFormLayout(refresh_group)

        self._yt_refresh_hours = [6, 12, 24, 48, 168]
        self._yt_refresh_combo = QComboBox()
        self._yt_refresh_combo.addItems(
            [f"{h} hours" if h < 168 else "Weekly" for h in self._yt_refresh_hours]
        )
        yt_refresh_idx = next(
            (i for i, h in enumerate(self._yt_refresh_hours)
             if h == self._config.youtube_default_refresh_hours), 2
        )
        self._yt_refresh_combo.setCurrentIndex(yt_refresh_idx)
        refresh_form.addRow("Refresh interval:", self._yt_refresh_combo)

        layout.addWidget(refresh_group)
        layout.addStretch()
        return widget

    def _build_video_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # -- Scanning group --------------------------------------------------
        scan_group = QGroupBox("Scanning")
        scan_form = QFormLayout(scan_group)

        self._auto_enrich_cb = QCheckBox("Automatically fetch metadata after scanning")
        self._auto_enrich_cb.setChecked(self._config.auto_enrich)
        scan_form.addRow(self._auto_enrich_cb)

        layout.addWidget(scan_group)

        # -- TMDb group -------------------------------------------------------
        tmdb_group = QGroupBox("TMDb Metadata")
        tmdb_form = QFormLayout(tmdb_group)

        from PySide6.QtWidgets import QLineEdit

        self._tmdb_key_edit = QLineEdit()
        self._tmdb_key_edit.setPlaceholderText("Enter your TMDb API key (v3 auth)")
        self._tmdb_key_edit.setText(self._config.tmdb_api_key)
        self._tmdb_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        tmdb_form.addRow("API key:", self._tmdb_key_edit)

        tmdb_form.addRow(
            QLabel(
                '<a href="https://www.themoviedb.org/settings/api">'
                "Get a free API key at themoviedb.org</a>"
            )
        )
        # Make the link clickable.
        label = tmdb_form.itemAt(tmdb_form.count() - 1).widget()
        label.setOpenExternalLinks(True)

        layout.addWidget(tmdb_group)

        # -- Subtitles group --------------------------------------------------
        sub_group = QGroupBox("Subtitles")
        sub_form = QFormLayout(sub_group)

        self._sub_font_spin = QSpinBox()
        self._sub_font_spin.setRange(12, 48)
        self._sub_font_spin.setValue(self._config.subtitle_font_size)
        self._sub_font_spin.setSuffix(" pt")
        sub_form.addRow("Font size:", self._sub_font_spin)

        self._sub_outline_cb = QCheckBox("Text outline")
        self._sub_outline_cb.setChecked(self._config.subtitle_outline)
        sub_form.addRow(self._sub_outline_cb)

        self._sub_bg_cb = QCheckBox("Background bar")
        self._sub_bg_cb.setChecked(self._config.subtitle_bg)
        sub_form.addRow(self._sub_bg_cb)

        layout.addWidget(sub_group)

        layout.addStretch()
        return widget

    def _update_color_btn(self) -> None:
        self._color_btn.setStyleSheet(
            f"background-color: {self._bg_color.name()}; border: 1px solid #666;"
        )

    def _pick_bg_color(self) -> None:
        from PySide6.QtWidgets import QColorDialog
        color = QColorDialog.getColor(self._bg_color, self, "Background Color")
        if color.isValid():
            self._bg_color = color
            self._update_color_btn()

    def _apply_to_config(self) -> None:
        self._config.restore_queue = self._restore_queue_cb.isChecked()
        rg_key, _ = self._rg_modes[self._rg_combo.currentIndex()]
        self._config.replaygain_mode = rg_key
        mode_key, _ = self._np_bg_modes[self._np_bg_combo.currentIndex()]
        self._config.nowplaying_bg_mode = mode_key
        self._config.nowplaying_tint_alpha = self._np_tint_slider.value()
        self._config.nowplaying_blur_strength = self._np_blur_slider.value()
        self._config.nowplaying_bg_opacity = self._np_opacity_slider.value() / 100.0
        self._config.nowplaying_art_size = self._np_art_size_slider.value()
        self._config.window_opacity = self._opacity_slider.value() / 100.0
        self._config.blur_behind = self._blur_behind_cb.isChecked()
        self._config.bg_color = [self._bg_color.red(), self._bg_color.green(), self._bg_color.blue()]
        self._config.bg_tint_strength = self._tint_slider.value()
        self._config.show_feed_urls = self._show_feed_urls_cb.isChecked()
        self._config.show_genre_in_headers = self._show_genre_cb.isChecked()
        self._config.show_playlists_tab = self._show_playlists_cb.isChecked()
        self._config.show_podcasts_tab = self._show_podcasts_cb.isChecked()
        self._config.show_youtube_tab = self._show_youtube_cb.isChecked()
        self._config.show_nowplaying_tab = self._show_nowplaying_cb.isChecked()
        self._config.podcast_default_retain = self._retain_spin.value()
        self._config.podcast_default_refresh_hours = self._refresh_hours[self._refresh_combo.currentIndex()]
        yt_auth_key, _ = self._yt_auth_browsers[self._yt_auth_combo.currentIndex()]
        self._config.youtube_auth_browser = yt_auth_key
        yt_quality_key, _ = self._yt_qualities[self._yt_quality_combo.currentIndex()]
        self._config.youtube_video_quality = yt_quality_key
        yt_np_key, _ = self._yt_np_modes[self._yt_np_combo.currentIndex()]
        self._config.youtube_nowplaying_mode = yt_np_key
        self._config.youtube_default_refresh_hours = self._yt_refresh_hours[self._yt_refresh_combo.currentIndex()]
        self._config.tmdb_api_key = self._tmdb_key_edit.text().strip()
        self._config.auto_enrich = self._auto_enrich_cb.isChecked()
        self._config.subtitle_font_size = self._sub_font_spin.value()
        self._config.subtitle_outline = self._sub_outline_cb.isChecked()
        self._config.subtitle_bg = self._sub_bg_cb.isChecked()

    def _on_apply(self) -> None:
        self._apply_to_config()
        save_config(self._config)
        self.settings_changed.emit()

    def _on_ok(self) -> None:
        self._on_apply()
        self.accept()

    @property
    def config(self) -> AppConfig:
        return self._config
