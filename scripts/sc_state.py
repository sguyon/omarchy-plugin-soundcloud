#!/usr/bin/env python3
"""Helpers shared by the plugin's UI test scripts (standard library only).

Never starts or stops the daemon, never sends a key, never prints the session token.

  sc_state.py status | <any control command>   print the backend answer as JSON
  sc_state.py field <name>                      print one field of the status as JSON
  sc_state.py fingerprint                       print the live display fingerprint (JSON)
  sc_state.py config-check                      exit 0 if the saved calibration matches the live
                                                setup; 1 (stale) or 2 (missing), reason on stdout
  sc_state.py calibrate [--no-defaults]         measure the popup once (opens and closes it, sends
                                                no key) and save the per-machine test config
  sc_state.py region <name>                     grim geometry "x,y WxH" (logical pixels) of
                                                card|tabbar|list|avatar|monitor from the config
  sc_state.py avatar-box <crop-width-px>        ImageMagick WxH+X+Y of the avatar inside a card crop
  sc_state.py rowdiff <before.ppm> <after.ppm>  y (card-relative, logical px) of the row that
                                                changed in the list area, or "none"
  sc_state.py diffcount <a.ppm> <b.ppm>         number of clearly different pixels

The per-machine config lives OUTSIDE the repo: ${XDG_CONFIG_HOME:-~/.config}/omarchy-soundcloud/
test-config.json (mode 0600; override with SC_TEST_CONFIG). It holds only display facts and tool
availability, never account names or track titles.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANEL_NAMESPACE = "omarchy-keyboard-panel"
BAR_NAMESPACE = "omarchy-bar"
CONFIG_VERSION = 1
OPTIONAL_TOOLS = ("grim", "wtype", "tesseract", "hyprctl", "magick", "notify-send", "omarchy-shell", "nmcli")

# Built-in defaults for the plugin author's laptop (1366x768, scale 1, bar on top). They are only a
# seed: used when the popup cannot be measured AND the live display is exactly this one.
DEFAULT_SETUP = {"width": 1366, "height": 768, "scale": 1.0, "bar": "top"}
DEFAULT_CARD = {"x": 578, "y": 31, "w": 456, "h": 512}  # monitor-relative, logical px


# ----------------------------------------------------------------------------- backend

def backend(command):
    spec = importlib.util.spec_from_file_location("soundcloud_app", ROOT / "soundcloud_app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["soundcloud_app"] = mod
    spec.loader.exec_module(mod)
    return mod.request_backend(command)


# ----------------------------------------------------------------------------- display facts

def logical_size(monitor):
    """Size of a monitor in logical (layout) pixels: mode size / scale, swapped when rotated."""
    scale = float(monitor.get("scale") or 1) or 1.0
    width, height = monitor["width"] / scale, monitor["height"] / scale
    if int(monitor.get("transform") or 0) % 2 == 1:
        width, height = height, width
    return round(width), round(height)


def bar_edge(layer, monitor):
    """Which screen edge a bar layer (monitor-relative x, y, w, h) sits on, and its thickness."""
    mw, mh = logical_size(monitor)
    x, y, w, h = layer["x"], layer["y"], layer["w"], layer["h"]
    if w >= mw * 0.8 and h < mh * 0.5:
        return ("top", h) if y < mh / 2 else ("bottom", h)
    if h >= mh * 0.8:
        return ("left", w) if x < mw / 2 else ("right", w)
    return ("unknown", min(w, h))


def iter_layers(layers_json):
    for monitor_name, body in (layers_json or {}).items():
        for level in (body.get("levels") or {}).values():
            for layer in level:
                yield monitor_name, layer


def fingerprint(monitors, layers_json, omarchy_version="unknown"):
    """The display facts a calibration depends on: every enabled monitor's mode, scale, transform
    and position, where the bar sits, and the Omarchy version."""
    mons = sorted(
        ({"name": m["name"], "width": m["width"], "height": m["height"], "scale": round(float(m.get("scale") or 1), 3),
          "transform": int(m.get("transform") or 0), "x": m.get("x", 0), "y": m.get("y", 0)}
         for m in monitors if not m.get("disabled")),
        key=lambda m: m["name"])
    by_name = {m["name"]: m for m in monitors}
    bars = sorted(
        ({"monitor": name, "edge": bar_edge(layer, by_name[name])[0], "size": bar_edge(layer, by_name[name])[1]}
         for name, layer in iter_layers(layers_json)
         if layer.get("namespace") == BAR_NAMESPACE and name in by_name),
        key=lambda b: b["monitor"])
    return {"monitors": mons, "bars": bars, "omarchy": str(omarchy_version).strip()}


def fingerprint_hash(fp):
    return hashlib.sha256(json.dumps(fp, sort_keys=True).encode()).hexdigest()[:16]


def fingerprint_differences(saved, live):
    """Plain-English list of what changed between two fingerprints; empty when they match."""
    out = []
    old = {m["name"]: m for m in saved.get("monitors", [])}
    new = {m["name"]: m for m in live.get("monitors", [])}
    if len(old) != len(new):
        out.append(f"monitor count changed ({len(old)} -> {len(new)})")
    for name in sorted(set(old) | set(new)):
        if name not in old:
            out.append(f"monitor {name} was added")
        elif name not in new:
            out.append(f"monitor {name} was removed")
        else:
            for key, label in (("width", "width"), ("height", "height"), ("scale", "scale"),
                               ("transform", "rotation"), ("x", "x position"), ("y", "y position")):
                if old[name].get(key) != new[name].get(key):
                    out.append(f"{name} {label} changed ({old[name].get(key)} -> {new[name].get(key)})")
    if saved.get("bars") != live.get("bars"):
        out.append(f"bar position or size changed ({saved.get('bars')} -> {live.get('bars')})")
    if saved.get("omarchy") != live.get("omarchy"):
        out.append(f"Omarchy version changed ({saved.get('omarchy')} -> {live.get('omarchy')})")
    return out


def run(*args, timeout=10):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def hypr_json(what):
    raw = run("hyprctl", what, "-j")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def live_fingerprint():
    monitors, layers = hypr_json("monitors"), hypr_json("layers")
    if not monitors or layers is None:
        return None
    version = (run("omarchy", "version") or "unknown").strip()
    return fingerprint(monitors, layers, version)


# ----------------------------------------------------------------------------- config file

def config_path():
    override = os.environ.get("SC_TEST_CONFIG")
    if override:
        return pathlib.Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return pathlib.Path(base) / "omarchy-soundcloud" / "test-config.json"


def load_config(path=None):
    try:
        data = json.loads((path or config_path()).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == CONFIG_VERSION else None


def save_config(config, path=None):
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_config(config, live):
    """(status, reason): "ok", "missing" or "stale"."""
    if config is None:
        return "missing", "no calibration file yet"
    if live is None:
        return "stale", "cannot read the live display setup (is Hyprland running?)"
    diffs = fingerprint_differences(config.get("fingerprint", {}), live)
    return ("stale", "; ".join(diffs)) if diffs else ("ok", "calibration matches this display setup")


# ----------------------------------------------------------------------------- pixels (PPM)

def read_ppm(path):
    data = pathlib.Path(path).read_bytes()
    parts, pos = [], 0
    while len(parts) < 4:  # magic, width, height, maxval
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            while data[pos:pos + 1] != b"\n":
                pos += 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        parts.append(data[pos:end])
        pos = end
    return int(parts[1]), int(parts[2]), data[pos + 1:]


def _differs(a, b, fuzz):
    return abs(a[0] - b[0]) > fuzz or abs(a[1] - b[1]) > fuzz or abs(a[2] - b[2]) > fuzz


def diff_box(width, height, before, after, ignore=None, fuzz=3, min_run=150):
    """The rectangle of the popup card appearing between two same-size RGB buffers.

    The card has a solid border, so its top and bottom edges are long unbroken runs of changed
    pixels starting at the same x (inside, the card may match the desktop colour and only show
    where text lies underneath). Rows are grouped by the start of their longest changed run; the
    group spanning the tallest stretch is the card (a notification toast beside it makes a short
    group, and may merge into the card's top edge, so the right edge is the shorter of the top and
    bottom runs). `ignore` is a rectangle (x, y, w, h) left out, such as the bar.
    Returns (x, y, w, h) or None."""
    ix, iy, iw, ih = ignore or (0, 0, 0, 0)
    groups = {}
    for y in range(height):
        ra, rb = before[y * width * 3:(y + 1) * width * 3], after[y * width * 3:(y + 1) * width * 3]
        if ra == rb:
            continue
        inside_ignore = iy <= y < iy + ih
        best, start, last = None, None, None
        for x in range(width):
            i = x * 3
            changed = _differs(ra[i:i + 3], rb[i:i + 3], fuzz) and not (inside_ignore and ix <= x < ix + iw)
            if changed:
                if start is None or x - last > 2:  # tolerate a stray unchanged pixel
                    start = x
                last = x
                if best is None or last - start > best[1] - best[0]:
                    best = (start, last)
        if best and best[1] - best[0] + 1 >= min_run:
            groups.setdefault(best[0], []).append((y, best[1]))
    if not groups:
        return None
    x0, rows = max(groups.items(), key=lambda item: (item[1][-1][0] - item[1][0][0], -item[0]))
    x1 = min(rows[0][1], rows[-1][1])  # the top and bottom edge rows; a toast can only lengthen one of them
    return x0, rows[0][0], x1 - x0 + 1, rows[-1][0] - rows[0][0] + 1


def list_region(card):
    """Fractions of the card (proportional, so any popup size works)."""
    return {"tabbar": (0.0, 0.447, 1.0, 0.065), "list": (0.0, 0.51, 1.0, 0.49), "avatar": (0.84, 0.0, 0.16, 0.14)}


def region_rect(card, name):
    if name == "card":
        return card["x"], card["y"], card["w"], card["h"]
    fx, fy, fw, fh = list_region(card)[name]
    return (card["x"] + round(card["w"] * fx), card["y"] + round(card["h"] * fy),
            round(card["w"] * fw), round(card["h"] * fh))


def row_changed(path_a, path_b, card_logical_w):
    """Top y (logical px from the image top) of the bounding band that changed between two crops of
    the list region, if it is as tall as one list row (30..90 logical px); else None. Works with
    any theme: it compares pixels instead of looking for an accent colour."""
    wa, ha, a = read_ppm(path_a)
    wb, hb, b = read_ppm(path_b)
    if (wa, ha) != (wb, hb):
        return None
    px = wa / float(card_logical_w)  # image pixels per logical pixel
    rows = [y for y in range(ha) if a[y * wa * 3:(y + 1) * wa * 3] != b[y * wa * 3:(y + 1) * wa * 3]
            and any(_differs(a[(y * wa + x) * 3:(y * wa + x) * 3 + 3], b[(y * wa + x) * 3:(y * wa + x) * 3 + 3], 12)
                    for x in range(0, wa, 2))]
    if not rows:
        return None
    height = (rows[-1] - rows[0] + 1) / px
    return round(rows[0] / px) if 30 <= height <= 90 else None


def diff_count(path_a, path_b, fuzz=25):
    wa, ha, a = read_ppm(path_a)
    wb, hb, b = read_ppm(path_b)
    if (wa, ha) != (wb, hb):
        return wa * ha
    return sum(1 for i in range(0, len(a) - 2, 3) if _differs(a[i:i + 3], b[i:i + 3], fuzz))


# ----------------------------------------------------------------------------- calibration

def panel_open():
    layers = hypr_json("layers")
    return any(layer.get("namespace") == PANEL_NAMESPACE for _, layer in iter_layers(layers))


def panel_monitor():
    for name, layer in iter_layers(hypr_json("layers")):
        if layer.get("namespace") == PANEL_NAMESPACE:
            return name
    return None


def toggle_popup():
    subprocess.run(["omarchy-shell", "shell", "toggle", "brunosilveira.soundcloud"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def wait_panel(want_open, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if panel_open() == want_open:
            return True
        time.sleep(0.05)
    return False


def tools_available():
    return {tool: shutil.which(tool) is not None for tool in OPTIONAL_TOOLS}


def measure_card(monitors, tmp):
    """One measurement: screenshot every monitor, open the popup, screenshot the monitor it opened
    on, close it, and find the card by difference. Pixels are logical (grim -s 1). No key is sent.
    Returns (monitor name, card dict), None when no card was recognised, or an error string."""
    for name in monitors:
        if subprocess.run(["grim", "-s", "1", "-o", name, "-t", "ppm", str(tmp / f"{name}.before.ppm")], timeout=15).returncode:
            return "grim could not take a screenshot"
    toggle_popup()
    if not wait_panel(True, 3):
        return "the popup did not open (is the plugin enabled?)"
    name = panel_monitor() or next(iter(monitors))
    time.sleep(0.7)  # let the open animation finish
    after = tmp / "after.ppm"
    subprocess.run(["grim", "-s", "1", "-o", name, "-t", "ppm", str(after)], timeout=15)
    toggle_popup()
    wait_panel(False, 2) or (toggle_popup(), wait_panel(False, 2))
    w, h, a = read_ppm(tmp / f"{name}.before.ppm")
    w2, h2, b = read_ppm(after)
    bar = next((l for n, l in iter_layers(hypr_json("layers")) if n == name and l.get("namespace") == BAR_NAMESPACE), None)
    if (w, h) != (w2, h2):
        return None
    box = diff_box(w, h, a, b, ignore=(bar["x"], bar["y"], bar["w"], bar["h"]) if bar else None)
    if box and box[2] >= 300 and box[3] >= 300 and 0.6 <= box[3] / box[2] <= 2.0:  # a popup card, not a stray window
        return name, {"x": box[0], "y": box[1], "w": box[2], "h": box[3]}
    return None


def calibrate(allow_defaults=True):
    """Measure the popup card once and save the per-machine config. Returns (ok, message)."""
    tools = tools_available()
    for needed in ("hyprctl", "grim", "omarchy-shell"):
        if not tools[needed]:
            return False, f"{needed} is not installed"
    live = live_fingerprint()
    if not live or not live["bars"]:
        return False, "no Omarchy bar found in Hyprland (is the Omarchy shell running?)"
    monitors = {m["name"]: m for m in hypr_json("monitors")}
    if panel_open():
        toggle_popup()
        if not wait_panel(False, 2):
            return False, "the popup is open and would not close; close it and retry"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sc-cal-"))
    card = monitor_name = None
    try:
        # Measure until two runs in a row agree: a notification toast or a repainting window can
        # disturb a single run.
        previous = None
        for _ in range(4):
            result = measure_card(monitors, tmp)
            if isinstance(result, str):
                return False, result
            if result is not None and result == previous:
                card, monitor_name = result[1], result[0]
                break
            previous = result
            time.sleep(0.5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if panel_open():
            toggle_popup()
            wait_panel(False, 2)
    source = "measured"
    if card is None:
        if (allow_defaults and len(live["monitors"]) == 1 and live["bars"][0]["edge"] == DEFAULT_SETUP["bar"]
                and (live["monitors"][0]["width"], live["monitors"][0]["height"], live["monitors"][0]["scale"])
                == (DEFAULT_SETUP["width"], DEFAULT_SETUP["height"], DEFAULT_SETUP["scale"])):
            card, source, monitor_name = dict(DEFAULT_CARD), "defaults", live["monitors"][0]["name"]
        else:
            return False, "could not measure the popup card on screen (a stray window over it?). Retry with nothing moving near the bar"
    mon = monitors[monitor_name]
    config = {
        "version": CONFIG_VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": source,  # "measured", or "defaults" = the built-in 1366x768 seed, not measured
        "fingerprint": live,
        "fingerprint_hash": fingerprint_hash(live),
        "monitor": monitor_name,
        "scale": float(mon.get("scale") or 1),
        "card_relative": card,
        "card": {"x": card["x"] + mon.get("x", 0), "y": card["y"] + mon.get("y", 0), "w": card["w"], "h": card["h"]},
        "tools": tools,
    }
    save_config(config)
    note = " (built-in defaults for 1366x768, not measured)" if source == "defaults" else ""
    return True, (f"calibrated: monitor {monitor_name}, scale {config['scale']:g}, popup card "
                  f"{card['w']}x{card['h']} at {card['x']},{card['y']}{note}; saved to {config_path()}")


# ----------------------------------------------------------------------------- command line

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "field":
        print(json.dumps((backend("status") or {}).get(argv[2])))
    elif cmd == "fingerprint":
        print(json.dumps(live_fingerprint(), indent=2))
    elif cmd == "config-check":
        status, reason = check_config(load_config(), live_fingerprint())
        print(f"{status}: {reason}")
        return {"ok": 0, "stale": 1, "missing": 2}[status]
    elif cmd == "calibrate":
        ok, message = calibrate(allow_defaults="--no-defaults" not in argv)
        print(("OK  " if ok else "FAILED  ") + message)
        return 0 if ok else 1
    elif cmd == "region":
        config = load_config()
        if config is None:
            print("no calibration", file=sys.stderr)
            return 2
        x, y, w, h = region_rect(config["card"], argv[2]) if argv[2] != "monitor" else (0, 0, 0, 0)
        if argv[2] == "monitor":
            print(config["monitor"])
        else:
            print(f"{x},{y} {w}x{h}")
    elif cmd == "avatar-box":  # ImageMagick geometry of the avatar inside a card crop of the given pixel width
        card = (load_config() or {})["card"]
        scale = int(argv[2]) / float(card["w"])
        fx, fy, fw, fh = list_region(card)["avatar"]
        print(f"{round(card['w'] * fw * scale)}x{round(card['h'] * fh * scale)}+{round(card['w'] * fx * scale)}+{round(card['h'] * fy * scale)}")
    elif cmd == "config-path":
        print(config_path())
    elif cmd == "card-width":
        print((load_config() or {}).get("card", {}).get("w", 0))
    elif cmd == "rowdiff":
        top = row_changed(argv[2], argv[3], float(argv[4]))
        print("none" if top is None else top)
    elif cmd == "diffcount":
        print(diff_count(argv[2], argv[3]))
    else:
        print(json.dumps(backend(cmd), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
