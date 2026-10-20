#!/usr/bin/env bash
# OPT-IN CHAOS TEST: cuts this machine's network for N seconds while the player plays, and checks
# the connection-recovery behaviour. It is NEVER run by scripts/verify.sh.
#
#   scripts/chaos-network.sh --yes [--seconds 100] [--seek-minutes 10] [--poll 2]
#   scripts/chaos-network.sh --dry-run          print what it would do; touches nothing
#
# WARNING: this switches off your Wi-Fi (or disconnects your wired device) for the whole duration:
# every other program loses its connection too, and a remote session over this network would drop.
# Run it from the machine itself. It refuses to run without --yes, without nmcli and an active
# NetworkManager device it can bring back, or when nothing is playing.
#
# Expected timeline (checked and printed): playing -> buffering with "Connection lost, retrying..."
# (the status `error` field) -> playing again at about the saved position once the network is back.
# The network is ALWAYS restored: an EXIT/INT/TERM/HUP trap re-enables it. If the script is killed
# with SIGKILL nothing can run, so the exact manual command is printed before the cut.
# Needs: nmcli (NetworkManager), a running signed-in backend, a playing track (a long mix, > N s left).
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
. scripts/ui-lib.sh
seconds=100; seek_min=0; poll=2; yes=0; dry=0
while [ $# -gt 0 ]; do
  case $1 in
    --yes) yes=1 ;;
    --dry-run) dry=1 ;;
    --seconds) seconds=${2:?}; shift ;;
    --seek-minutes) seek_min=${2:?}; shift ;;
    --poll) poll=${2:?}; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
  shift
done
case $seconds$seek_min$poll in *[!0-9]*) echo "numbers must be whole and positive"; exit 2 ;; esac

# Find the active device and how to bring it back.
dev=; type=; conn=
if command -v nmcli >/dev/null 2>&1; then
  line=$(nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device 2>/dev/null | awk -F: '$3=="connected" && ($2=="wifi"||$2=="ethernet"){print; exit}')
  IFS=: read -r dev type _ conn <<<"$line"
fi
if [ "$type" = wifi ]; then
  cut_cmd="nmcli radio wifi off"; restore_cmd="nmcli radio wifi on"
else
  cut_cmd="nmcli device disconnect $dev"; restore_cmd="nmcli connection up '$conn'"
fi

if [ $dry = 1 ]; then
  echo "DRY RUN: nothing will be changed."
  echo "  would check: nmcli present and an active wifi/ethernet device (found: ${dev:-none} ${type:+($type)})"
  echo "  would check: the backend is signed in and playing"
  [ "$seek_min" -gt 0 ] && echo "  would seek to minute $seek_min with the control socket (seek:<ratio>)"
  echo "  would run:   ${cut_cmd:-nmcli ...}  (cut)   then poll the status every ${poll}s for ${seconds}s"
  echo "  would run:   ${restore_cmd:-nmcli ...}  (restore, also from a trap on EXIT INT TERM HUP)"
  echo "  would check: playbackState buffering with 'Connection lost, retrying' during the cut, playing again within 90 s of the restore, at about the saved position"
  exit 0
fi

[ $yes = 1 ] || { echo "Refusing to run: this cuts your network for ${seconds}s. Add --yes to confirm (or --dry-run to preview)."; exit 2; }
command -v nmcli >/dev/null 2>&1 || { echo "Refusing to run: nmcli is not installed, so I cannot guarantee restoring the network."; exit 2; }
[ -n "$dev" ] || { echo "Refusing to run: no active Wi-Fi or Ethernet device found in NetworkManager, so I cannot restore it afterwards."; exit 2; }
[ "$type" = wifi ] || [ -n "$conn" ] || { echo "Refusing to run: cannot find the connection name to bring $dev back."; exit 2; }
[ "$(sc field playing)" = true ] || { echo "Refusing to run: start a long track first (nothing is playing)."; exit 2; }

restored=0
restore() {
  [ $restored = 1 ] && return 0
  restored=1
  echo "restoring the network: $restore_cmd"
  eval "$restore_cmd" >/dev/null 2>&1 || eval "$restore_cmd" >/dev/null 2>&1
}
trap restore EXIT
trap 'echo; echo "interrupted"; restore; exit 130' INT TERM HUP

get() { sc status | python3 -c 'import json,sys; d=json.load(sys.stdin) or {}; print(d.get("playbackState"), "%.0f" % (d.get("position") or 0), "|", d.get("error") or "")' 2>/dev/null || echo "unreachable 0 |"; }

if [ "$seek_min" -gt 0 ]; then
  dur=$(sc field duration)
  ratio=$(python3 -c "d=float('$dur'); print('%.6f' % min(0.99, $seek_min*60/d)) if d>0 else print('')")
  [ -n "$ratio" ] && sc "seek:$ratio" >/dev/null && sleep 5
fi
read -r state0 pos0 _ <<<"$(get)"
echo "Before: state=$state0 position=${pos0}s on $dev ($type)"
notify "Chaos test: cutting the network for ${seconds}s while the player plays. It is restored automatically."
echo "If this script is ever killed hard, restore with:  $restore_cmd"
echo "== network OFF at $(date +%T) for ${seconds}s"
eval "$cut_cmd" >/dev/null 2>&1 || { echo "FAIL  could not cut the network"; exit 1; }
saw_retry=0; end=$((SECONDS + seconds)); t0=$SECONDS
while [ $SECONDS -lt $end ]; do
  sleep "$poll"
  line=$(get); printf '  +%3ds  %s\n' $((SECONDS - t0)) "$line"
  case $line in *"Connection lost, retrying"*) saw_retry=1 ;; esac
done
restore
echo "== network ON at $(date +%T)"
recovered=0; t1=$SECONDS
while [ $((SECONDS - t1)) -lt 90 ]; do
  sleep "$poll"
  line=$(get); printf '  +%3ds  %s\n' $((SECONDS - t0)) "$line"
  case $line in playing*) recovered=1; break ;; esac
done
read -r state1 pos1 _ <<<"$(get)"
echo
fails=0
[ $saw_retry = 1 ]; r=$?; [ $r = 0 ] && echo "PASS  buffering with \"Connection lost, retrying\" was shown during the cut" || { echo "FAIL  never saw \"Connection lost, retrying\" during the cut"; fails=$((fails + 1)); }
[ $recovered = 1 ]; r=$?; [ $r = 0 ] && echo "PASS  playing again $((SECONDS - t1))s after the network came back" || { echo "FAIL  not playing 90 s after the network came back (state=$state1)"; fails=$((fails + 1)); }
# The saved position is where playback stopped; the track may have moved a little, never back to the start.
python3 -c "import sys; sys.exit(0 if abs(float('$pos1') - float('$pos0')) <= 60 else 1)" && echo "PASS  position resumed near the saved one (${pos0}s -> ${pos1}s)" || { echo "FAIL  position jumped (${pos0}s -> ${pos1}s)"; fails=$((fails + 1)); }
[ $fails -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
