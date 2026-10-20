#!/usr/bin/env bash
# Measure this machine's display and the popup card once and save the result OUTSIDE the repo:
#   ${XDG_CONFIG_HOME:-~/.config}/omarchy-soundcloud/test-config.json  (mode 0600, no personal data)
# ui-smoke.sh and screenshots.sh read it, and re-run this automatically when the display setup
# (monitors, resolution, scale, rotation, bar position, Omarchy version) no longer matches.
# It opens the popup once (a few times to confirm) and closes it again; it sends no keys and does not
# touch playback. Deliberately silent: a notification toast of its own would land on top of the
# popup it is measuring. If some other toast is on screen while it runs, the card can be measured
# too narrow: wait for it to disappear and run this again.
# Usage: scripts/calibrate.sh [--show]   (--show prints the saved file)
SC_NAME="calibration"
. "$(dirname "${BASH_SOURCE[0]}")/ui-lib.sh"
ui_prereqs 0 grim hyprctl omarchy-shell
if [ "${1:-}" = "--show" ]; then cat "$(sc config-path)"; exit 0; fi
sc calibrate
