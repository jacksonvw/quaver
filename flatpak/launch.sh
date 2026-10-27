#!/bin/sh
# Flatpak launcher, installed as /app/bin/quaver or /app/bin/minim.
#
# bin/quaver and bin/minim log to /tmp, which is private to the sandbox, so
# the host never sees it. This logs where the host can read it, e.g.
#   tail -n 200 ~/.var/app/com.jacksonv.Quaver/cache/quaver.log
# truncated on each launch, as the checkout's log is. The suite keeps the
# checkout's layout, so each app's __init__ finds ../media_core as it does
# there.
app=$(basename "$0")
export PYTHONPATH="/app/share/suite/$app"
exec python3 -m "$app.main" "$@" >"$XDG_CACHE_HOME/$app.log" 2>&1
