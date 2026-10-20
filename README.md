# Quaver

A media library for the Linux desktop — music, TV series, movies, podcasts
and YouTube, all through one queue — and Minim, its lightweight sibling:
open a file and it plays, with no library and no scanning. Both are built
with PySide6 and GStreamer on a shared engine.

| Directory | What it is |
|---|---|
| `quaver/` | The library app |
| `minim/` | The click-to-play player |
| `media_core/` | Shared engine, transport, queue, Now Playing and MPRIS2 |

## Highlights

- Gapless playback, ReplayGain, a 10-band equalizer and a compressor
- Album-centric music browsing; series and movies matched against TVmaze or TMDB
- Podcasts with auto-download and resume, and YouTube channels
- Tag editor with MusicBrainz and AcoustID lookup
- Now Playing view with a spectrum visualizer
- MPRIS2 media controls

## Install (Fedora)

The apps run on the system Python against distribution packages, with no
virtualenv: the GStreamer bindings and PySide6 come from RPMs built for
`/usr/bin/python3`, which is why the launchers pin it.

```bash
sudo dnf install -y \
    python3-pyside6 qt6-qtwayland python3-gobject-base python3-dbus \
    python3-numpy python3-mutagen python3-platformdirs python3-tomli-w \
    python3-httpx python3-feedparser python3-watchdog python3-musicbrainzngs \
    python3-pip python3-pytest yt-dlp chromaprint-tools libass \
    gstreamer1-plugins-base gstreamer1-plugins-good \
    gstreamer1-plugins-bad-free gstreamer1-plugins-ugly-free \
    gstreamer1-plugin-libav gstreamer1-plugin-openh264 gstreamer1-plugin-dav1d
/usr/bin/python3 -m pip install --user pyacoustid    # not packaged for Fedora

git clone https://github.com/jacksonvw/quaver.git
cd quaver
desktop/install.sh
```

`desktop/install.sh` links `bin/quaver` and `bin/minim` into `~/bin`,
installs Minim's desktop entry and icon, and makes Minim the default player
for the audio and video types it declares. It needs no root and is safe to
re-run, including after moving the checkout.

Every package above comes from Fedora's own repositories, which decode
H.264, AAC, AC-3 and AV1 but not HEVC or E-AC-3; RPM Fusion's full `ffmpeg`
adds those. Other distributions need the equivalent packages.

## Usage

```bash
quaver                  # the library
minim video.mkv         # play files, or open them from the file manager
```

Logs go to `/tmp/quaver.log` and `/tmp/minim.log`. Settings live in
`~/.config/quaver/` and `~/.config/minim/`, and Quaver's library database in
`~/.local/share/quaver/`. The TMDB scraper needs your own API key, entered
in Preferences.

## Tests

```bash
/usr/bin/python3 -m pytest quaver/quaver/tests/ minim/minim/tests/
```

The suite is headless (Qt's offscreen platform) and takes about a second.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
