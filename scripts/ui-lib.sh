#!/usr/bin/env bash
# Shared helpers for scripts/ui-smoke.sh, scripts/screenshots.sh and scripts/calibrate.sh (sourced, not run).
# Everything here is read-only: it never sends a key and never touches the daemon.
SC_SCRIPTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
sc() { python3 -I "$SC_SCRIPTS/sc_state.py" "$@"; }
panel_open() { hyprctl layers 2>/dev/null | grep -q "namespace: omarchy-keyboard-panel"; }
toggle() { omarchy-shell shell toggle brunosilveira.soundcloud >/dev/null 2>&1; }
notify() { command -v notify-send >/dev/null 2>&1 && notify-send -a "SoundCloud plugin" "SoundCloud plugin test" "$1" || true; }

# A machine that cannot run a UI test is not a failure: say why in plain English and exit 0.
skip() { printf 'SKIP  %s\n      %s\n' "$1" "$2"; exit 0; }

# ui_prereqs <needs-backend 0|1> <tool>...
ui_prereqs() {
  local need_backend=$1 missing=() tool; shift
  [ -n "${SC_SMOKE_FORCE_SKIP:-}" ] && skip "$SC_NAME" "SC_SMOKE_FORCE_SKIP is set, so this test was skipped on purpose"
  for tool in "$@"; do command -v "$tool" >/dev/null 2>&1 || missing+=("$tool"); done
  [ ${#missing[@]} -eq 0 ] || skip "$SC_NAME" "these tools are not installed: ${missing[*]} (needed to look at and drive the screen)"
  [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] || skip "$SC_NAME" "this is not a Hyprland session, so there is no popup to test"
  hyprctl layers -j 2>/dev/null | grep -q '"omarchy-bar"' || skip "$SC_NAME" "the Omarchy shell is not running (no Omarchy bar found), so the plugin popup cannot open"
  if [ "$need_backend" = 1 ]; then
    local loggedin title
    loggedin=$(sc field loggedIn 2>/dev/null)
    [ "$loggedin" = null ] || [ -z "$loggedin" ] && skip "$SC_NAME" "the SoundCloud backend is not running (control socket not answering); start the plugin from the bar first"
    [ "$loggedin" = true ] || skip "$SC_NAME" "the SoundCloud backend is not signed in; sign in from the popup first"
    title=$(sc field title 2>/dev/null)
    { [ "$title" = null ] || [ "$title" = '""' ] || [ -z "$title" ]; } && skip "$SC_NAME" "nothing is playing; start any track first so there is a player to look at"
  fi
  return 0
}

# ui_config: use the saved calibration only if it matches the live display; otherwise say so and
# measure again (opens the popup once, sends no key). Skips with the exact command if that fails.
ui_config() {
  local status rc
  status=$(sc config-check); rc=$?
  if [ $rc -eq 0 ]; then
    echo "calibration: $status"
    return 0
  fi
  case $rc in
    2) echo "calibration: none yet for this machine, measuring the popup now (it opens once, no keys are sent)." ;;
    *) echo "calibration: out of date (${status#stale: }); measuring the popup again (it opens once, no keys are sent)." ;;
  esac
  if ! sc calibrate; then
    skip "$SC_NAME" "could not calibrate this display automatically. Close other windows near the bar and run: scripts/calibrate.sh"
  fi
}
