#!/bin/sh
# Flatpak launcher, installed as /app/bin/quaver or /app/bin/minim.
#
# bin/quaver and bin/minim suit a checkout but not the sandbox. They put the
# app in the background, and the sandbox closes as soon as its first process
# exits. They also log to /tmp, which is private to the sandbox. This runs
# the app in the foreground and logs where the host can read it, e.g.
#   tail -n 200 ~/.var/app/com.jacksonv.Quaver/cache/quaver.log
# truncated on each launch, as the checkout's log is. The suite keeps the
# checkout's layout, so each app's __init__ finds ../media_core as it does
# there.
app=$(basename "$0")
export PYTHONPATH="/app/share/suite/$app"
exec python3 -m "$app.main" "$@" >"$XDG_CACHE_HOME/$app.log" 2>&1
