#!/bin/sh
# Chromium launcher for browser_task (F100). Playwright runs this as root in place of the browser binary
# (executable_path), with Chromium's arguments; it runs the real browser as the unprivileged nous-browser
# user. NOUS_BROWSER_CHROMIUM names the real binary.
#
# exec keeps the pid (Playwright's process-group kill still reaches the browser), and setpriv keeps
# file descriptors 3 and 4 open: they are Playwright's CDP pipe.
set -eu

real="${NOUS_BROWSER_CHROMIUM:?NOUS_BROWSER_CHROMIUM is not set}"

for arg in "$@"; do
  case "$arg" in
    --user-data-dir=*)
      # The fresh, empty profile directory Playwright just created as root. Chromium writes only here
      # (downloads are refused), so this is the one directory the browser user is given.
      dir="${arg#--user-data-dir=}"
      if [ ! -d "$dir" ] || [ -L "$dir" ]; then
        echo "chromium-as-user: the profile directory is missing or a link" >&2
        exit 1
      fi
      chown -R nous-browser:nous-browser -- "$dir"
      ;;
  esac
done

# --reset-env: Chromium gets HOME, PATH and the user's names only, never the agent's environment (API keys,
# database credentials). --no-new-privs: no setuid binary can raise the browser user's privileges again.
exec /usr/bin/setpriv --reuid=nous-browser --regid=nous-browser --clear-groups --reset-env --no-new-privs \
  -- "$real" "$@"
