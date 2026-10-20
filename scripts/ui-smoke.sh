#!/usr/bin/env bash
# UI smoke test for the SoundCloud popup: opens the real popup, sends a few keys, checks the
# screen and the backend, closes it, prints PASS/FAIL per step. Safe to skip: on a machine that
# cannot run it (no Omarchy shell, backend not signed in, nothing playing, a missing tool) it
# prints SKIP with the reason and exits 0.
#
# Usage: scripts/ui-smoke.sh [--playback]
#   --playback  also press Space twice (pause, then resume), Right then Left (seek) and m twice
#               (mute, then unmute); each is checked through the plugin's control socket and
#               restored. Playback blips for a moment if the music was paused.
# Needs: hyprctl, grim, wtype, tesseract, omarchy-shell, a running signed-in backend with a track.
#        Optional: notify-send (announces the test).
#
# Safety rules (unchanged since the first version):
#   * keys are only sent while the popup layer is present (checked before every key batch);
#     Hyprland cannot report layer focus, so a batch that finds the layer gone is retried, never
#     sent blind
#   * never Enter, never a media key; the daemon is never started, stopped or restarted
#   * volume, mute, play state, shuffle/repeat and the track are read before and after and must
#     be unchanged; the popup is closed at the end (also on Ctrl+C)
# The test takes the keyboard for about 20 s: do not type elsewhere while it runs.
#
# Facts over pixels: playback facts come from the control socket (scripts/sc_state.py). The
# screen is only read for things the backend does not know: tabs, queue view, filter, selection.
# Regions come from the calibrated popup card (scripts/calibrate.sh), as proportions of it, so
# any screen size, scale or popup width works. Selection is found by comparing two screenshots,
# not by an accent colour, so any theme works.
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
SC_NAME="UI smoke test"
. scripts/ui-lib.sh
playback=0; [ "${1:-}" = "--playback" ] && playback=1

ui_prereqs 1 hyprctl grim wtype tesseract omarchy-shell
ui_config
tmp=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/sc-smoke.XXXXXX")
trap '[ -n "${KEEP:-}" ] || rm -rf "$tmp"; ensure_closed' EXIT
fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
check() { if [ "$1" = 0 ]; then pass "$2"; else fail "$2"; fi; }
wait_for() { # wait_for <open|closed> <seconds>
  local end=$((SECONDS + $2 + 1)) want=$1
  for _ in $(seq 1 $(($2 * 20))); do
    if panel_open; then [ "$want" = open ] && return 0; else [ "$want" = closed ] && return 0; fi
    sleep 0.05
  done
  return 1
}
# The layer stays mapped for the fade after a close; toggling during the fade would reopen it.
ensure_closed() { wait_for closed 1 && return 0; toggle; wait_for closed 2; }
card=$(sc region card); tabbar=$(sc region tabbar); list=$(sc region list); cardw=$(sc card-width)
shot() { grim -t ppm -g "$card" "$tmp/$1.ppm"; }
shot_tabs() { grim -t ppm -g "$tabbar" "$tmp/$1.ppm"; }
shot_list() { grim -t ppm -g "$list" "$tmp/$1.ppm"; }
ocr() { tesseract "$tmp/$1.ppm" - --psm 6 2>/dev/null; }
keys() { panel_open || return 1; wtype "$@"; }
rowdiff() { sc rowdiff "$tmp/$1.ppm" "$tmp/$2.ppm" "$cardw"; }

echo "SoundCloud popup smoke test"
notify "UI smoke test: opening the popup and sending a few keys (do not type for ~20 s). Music keeps playing."

# Facts before: everything the test must leave as it found it.
snapshot() { sc status | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("playbackId"), d.get("playing"), d.get("muted"), d.get("volume"), d.get("shuffle"), d.get("repeat"))'; }
facts0=$(snapshot)
title0=$(sc field title); muted0=$(sc field muted); playing0=$(sc field playing)
check 0 "backend reachable and signed in, a track is loaded"
ensure_closed
panel_open; [ $? -ne 0 ]; check $? "popup starts closed"

group() { # group <name> <function>: run inside a fresh open, retried while the popup is gone
  local name=$1 fn=$2 attempt
  for attempt in 1 2 3; do
    toggle
    wait_for open 2 || continue
    sleep 0.35  # let the focus settle; keys reach the popup from here on
    if "$fn"; then ensure_closed; return 0; fi
    ensure_closed; sleep 0.5
  done
  fail "$name (no complete run in 3 attempts)"
  return 1
}

# 1. Tab bar visible; Down Down selects a row, Up moves the selection.
g_select() {
  shot open0; shot_list list0
  keys -k Down -k Down || return 1
  sleep 0.15; shot_list list_sel
  keys -k Up || return 1
  sleep 0.15; shot_list list_up
  return 0
}
group "selection" g_select && {
  tabs=$(ocr open0 | grep -o -E "Home|Feed|Likes|History" | sort -u | wc -l)
  [ "$tabs" -ge 2 ]; check $? "popup opens with the tab bar ($tabs of 4 tab names read)"
  [ "$tabs" -ge 2 ] || echo "      hint: if the popup looks right on screen, the saved geometry may be stale: run scripts/calibrate.sh"
  ocr open0 | grep -qiE "Qu[eo][uv]?[ec]|Quene|Queu"; check $? "Queue button is in the tab row"
  ocr open0 | grep -qi "library"; [ $? -ne 0 ]; check $? "no Library link in the tab row"
  y2=$(rowdiff list0 list_sel); y1=$(rowdiff list0 list_up)
  [ "$y2" != none ]; check $? "Down Down selects a row (changed row at y=$y2)"
  [ "$y1" != none ] && [ "$y2" != none ] && [ "$y2" != "$y1" ]; check $? "Up moves the selection (y=$y1)"
}

# 2. q opens the queue view, q again returns to the list; Esc closes.
g_queue() {
  keys -k q || return 1
  sleep 0.2; shot queue
  keys -k q || return 1
  sleep 0.2; shot back
  keys -k Escape || return 1
  return 0
}
group "queue" g_queue && {
  ocr queue | grep -Eqi "[0-9]+ next|empty"; check $? "q shows the queue view (toolbar count or empty text)"
  ocr back | grep -Eqi "[0-9]+ next|queue is empty"; [ $? -ne 0 ]; check $? "q again returns to the track list"
  wait_for closed 1; check $? "Esc closes the popup"
}

# 3. Shift+Tab goes to Queue (right end of the tab row), Tab goes back.
g_tabs() {
  shot_tabs t0
  keys -M shift -k Tab -m shift || return 1
  sleep 0.2; shot_tabs t1
  keys -k Tab || return 1
  sleep 0.3; shot_tabs t2
  return 0
}
group "tabs" g_tabs && {
  d01=$(sc diffcount "$tmp/t0.ppm" "$tmp/t1.ppm"); d02=$(sc diffcount "$tmp/t0.ppm" "$tmp/t2.ppm")
  [ "$d01" -gt 20 ]; check $? "Shift+Tab moves the selected tab ($d01 pixels changed)"
  [ "$d02" -lt "$d01" ]; check $? "Tab returns to the original tab ($d02 pixels differ, against $d01 after Shift+Tab)"
}

# 3b. Filter: / opens and focuses the field, letters are typed (not shortcuts); Esc clears the
#     text, Esc closes the field, Esc closes the popup.
g_filter() {
  keys -k slash -k q -k x -k j -k z -k k || return 1
  sleep 0.25; shot fil1
  keys -k Escape || return 1
  sleep 0.25; shot fil2
  panel_open && keys -k Escape || return 1
  sleep 0.25; shot fil3
  panel_open || return 1
  keys -k Escape || return 1
  return 0
}
group "filter" g_filter && {
  ocr fil1 | grep -qiE "no tracks match|[08] of|qxjz|tracks match"; check $? "/ opens the filter and typing filters ('qxjzk' matches nothing)"
  ocr fil2 | grep -qi "no tracks match"; [ $? -ne 0 ]; check $? "first Esc clears the filter text, popup stays open"
  ocr fil3 | grep -qi "filter tracks"; [ $? -ne 0 ]; check $? "second Esc closes the filter field"
  wait_for closed 1; check $? "third Esc closes the popup"
  ocr fil1 | grep -qiE "Qu[eo][uv]?[ec]|Quene|Queu"; check $? "Queue button still fits the tab row next to the magnifier"
}

# 4. Optional playback keys, checked through the control socket and restored.
if [ $playback = 1 ]; then
  g_space() { keys -k space || return 1; sleep 0.3; p1=$(sc field playing); return 0; }
  g_seek() {
    pos_a=$(sc field position)
    keys -k Right || return 1
    sleep 0.3; pos_b=$(sc field position)
    keys -k Left || return 1
    return 0
  }
  g_mute() { keys -k m || return 1; sleep 0.3; m1=$(sc field muted); return 0; }
  group "pause" g_space && { [ "$p1" != "$playing0" ]; check $? "Space toggles play/pause (playing: $playing0 -> $p1)"; }
  group "resume" g_space && { [ "$p1" = "$playing0" ]; check $? "Space again restores it (playing: $p1)"; }
  group "seek" g_seek && {
    # The backend seek snaps to the stream's segments: only "clearly moved" is checked.
    python3 -c "import sys; sys.exit(0 if abs(float('$pos_b') - float('$pos_a')) > 1.0 else 1)"; check $? "Right moves the position ($pos_a -> $pos_b); Left sent back"
  }
  group "mute" g_mute && { [ "$m1" != "$muted0" ]; check $? "m toggles mute (muted: $muted0 -> $m1)"; }
  group "unmute" g_mute && { [ "$m1" = "$muted0" ]; check $? "m again restores it (muted: $m1)"; }
fi

# 5. Nothing changed for the user.
[ "$(sc field title)" = "$title0" ]; check $? "same track still loaded"
facts1=$(snapshot)
[ "$facts0" = "$facts1" ]; check $? "playing, mute, volume, shuffle and repeat are as before"
panel_open; [ $? -ne 0 ]; check $? "popup left closed"

[ $fails -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
