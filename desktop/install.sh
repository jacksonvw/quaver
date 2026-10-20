#!/bin/bash
# Install Quaver + Minim desktop integration into the user's home directory:
#   - quaver and minim launchers on PATH, symlinked to this checkout's bin/
#     (Quaver's "Open in Minim" looks minim up on PATH)
#   - Minim's .desktop entry and icon
#   - Minim as the default player for every MIME type that entry declares
#
# Files land in:
#   ~/bin/quaver, ~/bin/minim                  (symlinks)
#   ~/.local/share/applications/minim.desktop
#   ~/.local/share/icons/hicolor/scalable/apps/minim.svg
#   ~/.config/mimeapps.list                    (default-application entries)
#
# Run from anywhere — paths resolve relative to the suite root. Safe to
# re-run; re-run it after moving the checkout. Installs no packages —
# README.md has the dnf line.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
BIN_DIR="${HOME}/bin"
APPS_DIR="${DATA_HOME}/applications"
ICON_DIR="${DATA_HOME}/icons/hicolor/scalable/apps"

mkdir -p "${BIN_DIR}" "${APPS_DIR}" "${ICON_DIR}"

# --- Launchers: symlinks, so they always run this checkout ---
for app in quaver minim; do
    ln -sfn "${PROJECT_ROOT}/bin/${app}" "${BIN_DIR}/${app}"
    echo "Linked launcher: ${BIN_DIR}/${app}"
done

# --- Icon (the desktop entry says Icon=minim) ---
cp "${PROJECT_ROOT}/minim/minim/resources/minim.svg" "${ICON_DIR}/minim.svg"
echo "Wrote icon: ${ICON_DIR}/minim.svg"

# --- .desktop entry: substitute the absolute project root into Exec ---
DESKTOP_FILE="${APPS_DIR}/minim.desktop"
sed "s|@PROJECT_ROOT@|${PROJECT_ROOT}|g" "${SCRIPT_DIR}/minim.desktop.in" > "${DESKTOP_FILE}"
echo "Wrote desktop entry: ${DESKTOP_FILE}"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APPS_DIR}"
    echo "Refreshed desktop database"
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -t -f "${DATA_HOME}/icons/hicolor" 2>/dev/null || true
fi

# --- Minim as the default for every type the entry declares ---
# gio rather than xdg-mime: under KDE 6, xdg-utils 1.2.1 shells out to an
# unversioned `qtpaths` that Fedora doesn't ship, and errors once per type.
# A re-run may prune Minim's redundant [Added Associations] lines; the
# [Default Applications] entries are what decide.
count=0
for type in $(sed -n 's/^MimeType=//p' "${DESKTOP_FILE}" | tr ';' ' '); do
    gio mime "${type}" minim.desktop >/dev/null
    count=$((count + 1))
done
echo "Set Minim as the default for ${count} MIME types"

# KDE caches associations in sycoca; rebuild it, as xdg-mime does.
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 >/dev/null 2>&1 || true
fi

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "Note: ${BIN_DIR} isn't on PATH — the quaver/minim commands and Open in Minim need it." ;;
esac

echo
echo "Done. Run 'quaver', or open any audio/video file to play it in Minim."
