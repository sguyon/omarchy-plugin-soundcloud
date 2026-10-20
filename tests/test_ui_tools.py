"""Tests of the pure helpers behind scripts/ui-smoke.sh, scripts/screenshots.sh and
scripts/calibrate.sh (scripts/sc_state.py): display fingerprint, calibration staleness, popup card
detection, proportional regions. They use fake `hyprctl` JSON and synthetic images, so they need
no display, no Hyprland and no backend. Standard library only."""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_helper():
    spec = importlib.util.spec_from_file_location("sc_state_helper", ROOT / "scripts" / "sc_state.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sc = load_helper()


def monitor(name="eDP-1", width=1366, height=768, scale=1, transform=0, x=0, y=0, **extra):
    return {"id": 0, "name": name, "width": width, "height": height, "scale": scale, "transform": transform,
            "x": x, "y": y, "focused": True, "disabled": False, **extra}


def layers(**bars):
    """Fake `hyprctl layers -j`: bars is {monitor: (x, y, w, h)}."""
    out = {}
    for name, (x, y, w, h) in bars.items():
        out[name] = {"levels": {"0": [], "2": [{"x": x, "y": y, "w": w, "h": h, "namespace": "omarchy-bar"}], "3": []}}
    return out


LAPTOP = ([monitor()], layers(**{"eDP-1": (0, 0, 1366, 26)}))
FULLHD = ([monitor("DP-1", 1920, 1080)], layers(**{"DP-1": (0, 0, 1920, 32)}))
HIDPI = ([monitor("DP-2", 2560, 1440, scale=2)], layers(**{"DP-2": (0, 0, 1280, 32)}))


class GeometryTest(unittest.TestCase):
    def test_logical_size_uses_scale_and_rotation(self):
        self.assertEqual(sc.logical_size(monitor()), (1366, 768))
        self.assertEqual(sc.logical_size(monitor(width=1920, height=1080)), (1920, 1080))
        self.assertEqual(sc.logical_size(monitor(width=2560, height=1440, scale=2)), (1280, 720))
        self.assertEqual(sc.logical_size(monitor(width=1920, height=1080, transform=1)), (1080, 1920))
        self.assertEqual(sc.logical_size(monitor(width=2880, height=1800, scale=1.5)), (1920, 1200))

    def test_bar_edge(self):
        m = monitor(width=1920, height=1080)
        self.assertEqual(sc.bar_edge({"x": 0, "y": 0, "w": 1920, "h": 32}, m), ("top", 32))
        self.assertEqual(sc.bar_edge({"x": 0, "y": 1048, "w": 1920, "h": 32}, m), ("bottom", 32))
        self.assertEqual(sc.bar_edge({"x": 0, "y": 0, "w": 40, "h": 1080}, m), ("left", 40))
        self.assertEqual(sc.bar_edge({"x": 1880, "y": 0, "w": 40, "h": 1080}, m), ("right", 40))

    def test_fingerprint_for_three_setups(self):
        for setup, size, edge in ((LAPTOP, (1366, 768), "top"), (FULLHD, (1920, 1080), "top"), (HIDPI, (2560, 1440), "top")):
            fp = sc.fingerprint(*setup, "4.0.5-1")
            self.assertEqual((fp["monitors"][0]["width"], fp["monitors"][0]["height"]), size)
            self.assertEqual(fp["bars"][0]["edge"], edge)
        hidpi = sc.fingerprint(*HIDPI, "4.0.5-1")
        self.assertEqual(hidpi["monitors"][0]["scale"], 2)
        self.assertEqual(hidpi["bars"][0]["size"], 32)  # bar height is in logical pixels on a scale-2 monitor

    def test_fingerprint_ignores_irrelevant_fields(self):
        a = sc.fingerprint([monitor(refreshRate=59.9, focused=True, activeWorkspace={"id": 1})], LAPTOP[1], "4.0.5-1")
        b = sc.fingerprint([monitor(refreshRate=144.0, focused=False, activeWorkspace={"id": 7})], LAPTOP[1], "4.0.5-1")
        self.assertEqual(a, b)
        self.assertEqual(sc.fingerprint_differences(a, b), [])

    def test_popup_region_is_proportional_to_the_card(self):
        small = {"x": 100, "y": 30, "w": 400, "h": 500}
        big = {"x": 200, "y": 60, "w": 800, "h": 1000}
        for name in ("tabbar", "list", "avatar"):
            a, b = sc.region_rect(small, name), sc.region_rect(big, name)
            for got, want in zip((b[2], b[3], b[0] - 200, b[1] - 60), (a[2] * 2, a[3] * 2, (a[0] - 100) * 2, (a[1] - 30) * 2)):
                self.assertAlmostEqual(got, want, delta=2)  # rounding only
        x, y, w, h = sc.region_rect(small, "avatar")
        self.assertGreater(x, small["x"] + small["w"] * 0.7)  # top right corner
        self.assertLess(y + h, small["y"] + small["h"] * 0.25)


class StalenessTest(unittest.TestCase):
    base = sc.fingerprint(*LAPTOP, "4.0.5-1")

    def diffs(self, monitors, lay=None, version="4.0.5-1"):
        return sc.fingerprint_differences(self.base, sc.fingerprint(monitors, lay or LAPTOP[1], version))

    def test_same_setup_matches(self):
        self.assertEqual(self.diffs(*LAPTOP), [])
        self.assertEqual(sc.check_config({"fingerprint": self.base}, sc.fingerprint(*LAPTOP, "4.0.5-1"))[0], "ok")

    def test_changed_resolution_does_not_match(self):
        self.assertTrue(any("width" in d for d in self.diffs([monitor(width=1920, height=1080)])))

    def test_changed_scale_does_not_match(self):
        self.assertTrue(any("scale" in d for d in self.diffs([monitor(scale=2)])))

    def test_changed_monitor_count_does_not_match(self):
        diffs = self.diffs([monitor(), monitor("HDMI-A-1", 1920, 1080, x=1366)])
        self.assertTrue(any("monitor count" in d for d in diffs))

    def test_changed_bar_position_does_not_match(self):
        diffs = self.diffs(LAPTOP[0], layers(**{"eDP-1": (0, 742, 1366, 26)}))
        self.assertTrue(any("bar" in d for d in diffs))

    def test_changed_rotation_and_omarchy_version_do_not_match(self):
        self.assertTrue(any("rotation" in d for d in self.diffs([monitor(transform=1)])))
        self.assertTrue(any("Omarchy version" in d for d in self.diffs(*LAPTOP, version="4.1.0")))

    def test_missing_and_stale_status(self):
        live = sc.fingerprint(*LAPTOP, "4.0.5-1")
        self.assertEqual(sc.check_config(None, live)[0], "missing")
        self.assertEqual(sc.check_config({"fingerprint": sc.fingerprint(*FULLHD, "4.0.5-1")}, live)[0], "stale")
        self.assertEqual(sc.check_config({"fingerprint": live}, None)[0], "stale")


def ppm(width, height, paint):
    """Binary PPM bytes; paint(x, y) returns an (r, g, b) tuple."""
    body = bytearray()
    for y in range(height):
        for x in range(width):
            body += bytes(paint(x, y))
    return b"P6\n%d %d\n255\n" % (width, height) + bytes(body)


class CardDetectionTest(unittest.TestCase):
    def detect(self, width, height, card, bar_h, scale_note=""):
        x0, y0, w, h = card
        before = ppm(width, height, lambda x, y: (30, 30, 40))
        after = ppm(width, height, lambda x, y: (
            (200, 200, 220) if x0 <= x < x0 + w and y0 <= y < y0 + h else
            (255, 0, 0) if y < bar_h and x < 60 else  # bar content that changed: must be ignored
            (30, 30, 40)))
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a.ppm"), Path(tmp, "b.ppm")
            a.write_bytes(before)
            b.write_bytes(after)
            wa, ha, da = sc.read_ppm(a)
            _, _, db = sc.read_ppm(b)
        return sc.diff_box(wa, ha, da, db, ignore=(0, 0, width, bar_h))

    def test_finds_the_card_at_1366x768(self):
        self.assertEqual(self.detect(683, 384, (289, 15, 221, 255), 13), (289, 15, 221, 255))  # half-size image: fast and exact

    def test_finds_the_card_at_1920x1080_logical(self):
        self.assertEqual(self.detect(960, 540, (400, 20, 300, 330), 16), (400, 20, 300, 330))

    def test_finds_the_card_on_a_scale_2_monitor_in_logical_pixels(self):
        # 2560x1440 at scale 2 is 1280x720 logical; the helper works on the scale-1 grim capture.
        self.assertEqual(self.detect(640, 360, (330, 20, 221, 255), 16), (330, 20, 221, 255))

    def test_border_only_card_with_a_toast_touching_it(self):
        # Card interior is the same colour as the desktop (only the 2 px border differs), and a
        # notification toast sits right of it, its top edge merging into the card's top edge.
        w, h = 400, 300
        card = (50, 20, 200, 250)
        def paint(x, y):
            cx, cy, cw, ch = card
            if cx <= x < cx + cw and cy <= y < cy + ch:
                edge = x < cx + 2 or x >= cx + cw - 2 or y < cy + 2 or y >= cy + ch - 2
                return (137, 180, 250) if edge else (24, 24, 36)
            if 250 <= x < 390 and 20 <= y < 70:
                return (137, 180, 250) if y < 22 or y >= 68 or x < 252 or x >= 388 else (24, 24, 36)
            return (24, 24, 36)
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a.ppm"), Path(tmp, "b.ppm")
            a.write_bytes(ppm(w, h, lambda x, y: (24, 24, 36)))
            b.write_bytes(ppm(w, h, paint))
            wa, ha, da = sc.read_ppm(a)
            _, _, db = sc.read_ppm(b)
        self.assertEqual(sc.diff_box(wa, ha, da, db), card)

    def test_no_change_gives_none(self):
        flat = ppm(40, 40, lambda x, y: (1, 2, 3))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "f.ppm")
            p.write_bytes(flat)
            w, h, d = sc.read_ppm(p)
        self.assertIsNone(sc.diff_box(w, h, d, d))

    def test_row_changed_finds_a_selected_row_whatever_the_colour(self):
        def region(selected, colour):
            return ppm(100, 200, lambda x, y: colour if selected and 60 <= y < 116 and x > 5 else (20, 20, 20))
        for colour in ((60, 90, 200), (220, 120, 30), (30, 160, 90)):  # blue, orange, green themes
            with tempfile.TemporaryDirectory() as tmp:
                a, b = Path(tmp, "a.ppm"), Path(tmp, "b.ppm")
                a.write_bytes(region(False, colour))
                b.write_bytes(region(True, colour))
                self.assertEqual(sc.row_changed(a, b, 100), 60)
                self.assertEqual(sc.row_changed(a, a, 100), None)


class ConfigFileTest(unittest.TestCase):
    def test_saved_file_is_private_and_holds_no_personal_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "sub", "test-config.json")
            config = {"version": sc.CONFIG_VERSION, "fingerprint": sc.fingerprint(*LAPTOP, "4.0.5-1"), "card": {"x": 1, "y": 2, "w": 3, "h": 4}}
            sc.save_config(config, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(sc.load_config(path), config)
            self.assertIsNone(sc.load_config(Path(tmp, "missing.json")))
            path.write_text("{not json")
            self.assertIsNone(sc.load_config(path))

    def test_default_path_is_under_xdg_config_home(self):
        old = {k: os.environ.get(k) for k in ("SC_TEST_CONFIG", "XDG_CONFIG_HOME")}
        try:
            os.environ.pop("SC_TEST_CONFIG", None)
            os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-test"
            self.assertEqual(sc.config_path(), Path("/tmp/xdg-test/omarchy-soundcloud/test-config.json"))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class SkipTest(unittest.TestCase):
    def run_script(self, env_extra):
        env = {**os.environ, **env_extra}
        return subprocess.run(["bash", str(ROOT / "scripts" / "ui-smoke.sh")], capture_output=True, text=True, env=env, timeout=60)

    def test_forced_skip_exits_zero_with_a_reason(self):
        done = self.run_script({"SC_SMOKE_FORCE_SKIP": "1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("SKIP"), done.stdout)
        self.assertIn("on purpose", done.stdout)

    def test_missing_prerequisites_skip_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as tmp:  # an empty runtime dir: no control socket, no Hyprland
            done = self.run_script({"XDG_RUNTIME_DIR": tmp, "HYPRLAND_INSTANCE_SIGNATURE": ""})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.startswith("SKIP"), done.stdout)


if __name__ == "__main__":
    unittest.main()
