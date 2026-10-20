#!/usr/bin/env bash
# Run every automated check for the plugin. Works from any directory and does
# not touch a running backend or the desktop.
# Usage: scripts/verify.sh
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

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

run "python unit tests" python3 -m unittest tests/test_soundcloud_app.py
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
# "Error" or tagged [syntax] count. Every .qml file of the plugin root is checked.
printf '\n== qml syntax\n'
qmllint=$(command -v qmllint qmllint6 | head -n 1)
if [ -z "$qmllint" ] && [ -x /usr/lib/qt6/bin/qmllint ]; then qmllint=/usr/lib/qt6/bin/qmllint; fi
if [ -z "$qmllint" ]; then
  echo "qmllint not found"
  report SKIP "qml syntax" "qmllint not installed"
else
  errors=""
  for file in ./*.qml; do
    found=$("$qmllint" "$file" 2>&1 | grep -E '^Error|\[syntax\]' || true)
    [ -n "$found" ] && errors+="$found"$'\n'
  done
  if [ -n "$errors" ]; then printf '%s' "$errors"; report FAIL "qml syntax"; else report PASS "qml syntax"; fi
fi

# The Qt Quick tests run offscreen, so they need no display.
printf '\n== qml tests\n'
qmltestrunner=$(command -v qmltestrunner qmltestrunner6 | head -n 1)
if [ -z "$qmltestrunner" ] && [ -x /usr/lib/qt6/bin/qmltestrunner ]; then qmltestrunner=/usr/lib/qt6/bin/qmltestrunner; fi
if [ -z "$qmltestrunner" ]; then
  echo "qmltestrunner not found"
  report SKIP "qml tests" "qmltestrunner not installed"
elif [ ! -d tests/qml ]; then
  echo "tests/qml not found"
  report SKIP "qml tests" "no tests/qml"
else
  if QT_QPA_PLATFORM=offscreen "$qmltestrunner" -input tests/qml; then report PASS "qml tests"; else report FAIL "qml tests"; fi
fi

printf '\nSummary: %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
