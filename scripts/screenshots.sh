#!/usr/bin/env bash
# Take real screenshots of the popup (for the README and PRs).
# Usage: scripts/screenshots.sh [outdir] [--blur-avatar]      (default ./screenshots-out, not tracked)
#   --blur-avatar  blur the account avatar so shared screenshots do not show personal content
#                  (needs ImageMagick; the avatar region is a proportion of the popup card)
# Needs: hyprctl, grim, wtype, omarchy-shell, and a calibration (made automatically; see
# scripts/calibrate.sh). Prints SKIP and exits 0 when the machine cannot do it.
# Opens the real popup and sends harmless keys only (Tab, q, Ctrl+F, letters, Esc); never Enter,
# never a media key. Track titles and artwork in the shots are whatever is playing: check them
# before sharing.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
SC_NAME="screenshots"
. scripts/ui-lib.sh
out=./screenshots-out; blur=0
for arg in "$@"; do case $arg in --blur-avatar) blur=1 ;; *) out=$arg ;; esac; done
need=(hyprctl grim wtype omarchy-shell); [ $blur = 1 ] && need+=(magick)
ui_prereqs 0 "${need[@]}"
ui_config
mkdir -p "$out" || exit 1
card=$(sc region card)
trap 'ensure_closed' EXIT
wait_for() { local end=$((SECONDS + $2 + 1)); for _ in $(seq 1 $(($2 * 20))); do if panel_open; then [ "$1" = open ] && return 0; else [ "$1" = closed ] && return 0; fi; sleep 0.05; done; return 1; }
ensure_closed() { wait_for closed 1 && return 0; toggle; wait_for closed 2; }
keys() { panel_open || { echo "popup not open, not sending keys"; return 1; }; wtype "$@"; }
shot() {
  sleep 0.6
  grim -g "$card" "$out/$1.png" || return 1
  if [ $blur = 1 ]; then
    box=$(sc avatar-box "$(magick identify -format %w "$out/$1.png")")
    magick "$out/$1.png" -region "$box" -blur 0x30 "$out/$1.png"
  fi
  echo "saved $out/$1.png"
}
open_popup() { toggle; wait_for open 3 || { echo "popup did not open"; exit 1; }; sleep 1.0; }

notify "Taking popup screenshots: do not type for ~15 s."
ensure_closed || { echo "could not close the popup first"; exit 1; }
open_popup
shot home
keys -k Tab; sleep 0.4; keys -k Tab; sleep 0.4; shot likes-or-feed
keys -k Tab -k Tab -k Tab -k Tab; shot tab-cycle
ensure_closed; sleep 0.5
open_popup
keys q; shot queue
keys q; sleep 0.4
keys -M ctrl -k f -m ctrl; sleep 0.5; keys d j; shot filter
ensure_closed && echo "popup closed" || echo "WARNING: popup still open"
