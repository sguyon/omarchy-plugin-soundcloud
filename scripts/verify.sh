#!/usr/bin/env bash
# Run the plugin's automated checks. Works from any directory.
# Usage: scripts/verify.sh [--ui] [--live]
#   (default)  python tests, node tests, manifest validation, QML syntax and the Qt Quick tests.
#              Safe: no desktop interaction, no network, never touches a running backend.
#   --ui       also run scripts/ui-smoke.sh (opens the real popup and takes the keyboard for about
#              20 s; SKIPs when the machine cannot run it)
#   --live     also run scripts/api-contract.py (read-only calls to SoundCloud's private API with
#              your signed-in session; SKIPs when not signed in)
# Prints PASS, FAIL or SKIP for every step and a summary; exits non-zero if any step failed.
# A missing optional tool is a SKIP with the reason, never a failure.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

ui=0 live=0
for arg in "$@"; do
  case $arg in
    --ui) ui=1 ;;
    --live) live=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)"; exit 2 ;;
  esac
done

pass=0 fail=0 skip=0

report() { # status name [detail]
  printf '%s  %s%s\n' "$1" "$2" "${3:+ ($3)}"
  case $1 in PASS) pass=$((pass + 1)) ;; FAIL) fail=$((fail + 1)) ;; *) skip=$((skip + 1)) ;; esac
}

run() { # name command...
  local name=$1; shift
  printf '\n== %s\n' "$name"
  if "$@"; then report PASS "$name"; else report FAIL "$name"; fi
}

# A script that prints a line starting with SKIP and exits 0 counts as skipped, not passed.
run_optional() { # name command...
  local name=$1 out rc; shift
  printf '\n== %s\n' "$name"
  out=$("$@" 2>&1); rc=$?
  printf '%s\n' "$out"
  if [ $rc -ne 0 ]; then report FAIL "$name"
  elif printf '%s\n' "$out" | grep -q '^SKIP'; then report SKIP "$name" "$(printf '%s\n' "$out" | sed -n 's/^ *//;2p')"
  else report PASS "$name"; fi
}

tests=(tests/test_soundcloud_app.py)
for extra in tests/test_command_fuzz.py tests/test_ui_tools.py; do [ -f "$extra" ] && tests+=("$extra"); done
run "python unit tests" python3 -m unittest "${tests[@]}"
run "javascript model tests" node tests/test_soundcloud_model.js

printf '\n== plugin manifest\n'
if command -v omarchy >/dev/null 2>&1; then
  if omarchy plugin validate .; then report PASS "plugin manifest"; else report FAIL "plugin manifest"; fi
else
  echo "omarchy command not found"
  report SKIP "plugin manifest" "omarchy not installed"
fi

# qmllint cannot resolve the Quickshell imports and prints many warnings, and it
# reports syntax errors as "Warning: ... [syntax]", so only lines starting with
# "Error" or tagged [syntax] count.
qtbin() { # tool name: on PATH (also the -qt6 / 6 spellings) or in /usr/lib/qt6/bin
  local name candidate
  for name in "$1" "${1}6" "${1}-qt6"; do
    candidate=$(command -v "$name" 2>/dev/null) && { echo "$candidate"; return 0; }
  done
  [ -x "/usr/lib/qt6/bin/$1" ] && { echo "/usr/lib/qt6/bin/$1"; return 0; }
  return 1
}

printf '\n== qml syntax\n'
if qmllint=$(qtbin qmllint); then
  errors=$("$qmllint" BarWidget.qml 2>&1 | grep -E '^Error|\[syntax\]' || true)
  if [ -n "$errors" ]; then echo "$errors"; report FAIL "qml syntax"; else report PASS "qml syntax"; fi
else
  echo "qmllint not found"
  report SKIP "qml syntax" "qmllint not installed"
fi

printf '\n== qt quick tests\n'
if [ ! -d tests/qml ]; then
  echo "tests/qml not found"
  report SKIP "qt quick tests" "no tests/qml directory"
elif ! qmltestrunner=$(qtbin qmltestrunner); then
  echo "qmltestrunner not found"
  report SKIP "qt quick tests" "qmltestrunner not installed"
else
  if QT_QPA_PLATFORM=offscreen "$qmltestrunner" -input tests/qml 2>&1 | grep -E '^(FAIL|Totals)|^XFAIL|Error' ; [ "${PIPESTATUS[0]}" -eq 0 ]; then
    report PASS "qt quick tests"
  else
    report FAIL "qt quick tests"
  fi
fi

[ $ui = 1 ] && run_optional "ui smoke test" scripts/ui-smoke.sh
[ $live = 1 ] && run_optional "api contract (live)" python3 -I scripts/api-contract.py

printf '\nSummary: %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
