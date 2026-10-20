"""Deterministic fuzz test of the control-socket command grammar.

About 600 malformed commands (huge numbers, NUL, nan/inf, path-like strings, file: and javascript:
URLs, unicode digits, over-long strings) go through every command-parsing helper that does not need
a running daemon. Each helper must either reject cleanly (None, or LinkError for the link parser)
or return a sane value, and never raise. Standard library only; the random generator is seeded, so
a failure reproduces.
"""
import importlib.util
import math
import random
import re
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "soundcloud_app.py"


def load_module():
    spec = importlib.util.spec_from_file_location("soundcloud_app", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PREFIXES = [
    "", "launch", "status", "show", "play", "play:", "play-from:", "play-from:home:", "play-from:likes:",
    "tracks:", "tracks:home", "tracks:feed", "load-more:", "load-more:home", "list:", "list:likes", "list:history",
    "queue-next:", "queue-add:", "queue-play:", "queue-remove:", "queue-move:", "queue-clear",
    "link-next:", "link-play:", "play-url:", "repeat", "repeat:", "shuffle", "shuffle:", "volume:", "volume-step:",
    "seek:", "seek-by:", "mute:", "like:", "unlike:", "notify:", "autoplay:",
]
NASTY = [
    "", " ", "\x00", "a\x00b", "\n", "\r\n", "\t", "\x1b[31m", "\u202e", "\ufeff", "\U0001f3b5", "\ud800" if False else "\ufffd",
    "0", "-0", "+0", "1", "-1", "1e999", "-1e999", "1e-999", "nan", "NaN", "inf", "-inf", "Infinity", "-Infinity",
    "0x10", "0b11", "1_000", "1,5", "1.5.5", ".", "-", "+", "--1", "9" * 40, "-" + "9" * 40, "1" * 5000,
    "\u0661\u0662\u0663", "\uff11\uff12", "\u00b2", "\u0967", "\u2460",
    "../../etc/passwd", "/etc/passwd", "..\\..\\windows", "~/.ssh/id_rsa", "C:\\x", "/proc/self/environ", "%2e%2e%2f",
    "file:///etc/passwd", "file:///home/user/.ssh/id_rsa", "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<b>x</b>",
    "ftp://soundcloud.com/a/b", "https://evil.example/soundcloud.com/a/b", "https://soundcloud.com.evil.example/a/b",
    "https://user:pw@soundcloud.com/a/b", "https://soundcloud.com:8080/a/b", "https://soundcloud.com/a/b?x=1#y",
    "https://soundcloud.com/a/b/extra/segments/here", "https://soundcloud.com/a/sets/b", "https://on.soundcloud.com/abc",
    "https://on.soundcloud.com/" + "a" * 100, "https%3A%2F%2Fsoundcloud.com%2Fa%2Fb", "soundcloud.com/a/b", "//soundcloud.com/a/b",
    "soundcloud:tracks:1", "soundcloud:tracks:0", "soundcloud:tracks:-5", "soundcloud:tracks:007", "soundcloud:tracks:" + "9" * 30,
    "soundcloud:tracks:1\n", "soundcloud:tracks:1 ", "soundcloud:playlists:1", "SOUNDCLOUD:TRACKS:1", "soundcloud:tracks:\u0661",
    "home", "likes", "history", "feed", "HOME", "home:", ":home", "none", "one", "all", "on", "off", "true", "false", "ON",
    "A" * 10000, "A" * 100000, "%" * 3000, ":" * 3000, "soundcloud:tracks:1:" * 500,
]
SEPARATORS = ["", ":", "::", ":+", ":-", ":1", ":-1", ":99999", ":1.5", ":nan"]


def build_commands(count=640, seed=20261019):
    rng = random.Random(seed)
    commands = []
    seen = set()

    def add(command):
        if command not in seen:
            seen.add(command)
            commands.append(command)

    for prefix in PREFIXES:
        add(prefix)
        for bad in NASTY:
            add(prefix + bad)
    pid = "soundcloud:tracks:12345"
    for prefix in ("queue-move:", "play-from:home:", "play-from:likes:", "like:", "unlike:", "queue-next:", "queue-play:"):
        for sep in SEPARATORS:
            add(prefix + pid + sep)
            add(prefix + sep.lstrip(":") + ":" + pid)
    alphabet = ":+-.0123456789eEnaNifx/%\\\x00 \n\u0661abc"
    while len(commands) < count + 60:
        prefix = rng.choice(PREFIXES)
        body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        add(prefix + body)
    return commands


class CommandFuzzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_module()
        cls.commands = build_commands()

    def test_enough_commands(self):
        self.assertGreaterEqual(len(self.commands), 600)
        self.assertEqual(self.commands, build_commands(), "the corpus must be deterministic")

    def each(self, name, function, check):
        """Run `function` over every command; it must not raise and `check` must accept the result."""
        for command in self.commands:
            try:
                result = function(command)
            except Exception as error:  # a parser must reject, never raise
                self.fail(f"{name}({command[:80]!r}) raised {type(error).__name__}: {error}")
            try:
                check(result)
            except AssertionError as error:
                self.fail(f"{name}({command[:80]!r}) returned {result!r}: {error}")

    def assertPlaybackId(self, value):
        self.assertRegex(value, r"\Asoundcloud:tracks:[1-9][0-9]*\Z")  # \Z: a trailing newline is not accepted

    def test_normalize_backend_command(self):
        self.each("normalize_backend_command", self.app.normalize_backend_command,
                  lambda r: self.assertIsInstance(r, str))
        self.assertEqual(self.app.normalize_backend_command("launch"), "show")

    def test_source_helpers(self):
        for name, check in (
            ("track_source_from_command", lambda r: self.assertIn(r, (None, "home", "feed"))),
            ("load_more_source_from_command", lambda r: self.assertIn(r, (None, "home", "feed"))),
        ):
            self.each(name, getattr(self.app, name), check)
        for prefix in ("list:", "tracks:", "api:", ""):
            self.each(f"api_list_source_from_command[{prefix!r}]",
                      lambda c, p=prefix: self.app.api_list_source_from_command(c, p),
                      lambda r: self.assertTrue(r is None or r in self.app.API_TRACK_LISTS))

    def test_playback_id_commands(self):
        for name in ("playback_id_from_command", "queue_next_from_command", "queue_add_from_command",
                     "queue_play_from_command", "queue_remove_from_command"):
            self.each(name, getattr(self.app, name), lambda r: r is None or self.assertPlaybackId(r))

    def test_play_from_command(self):
        def check(result):
            if result is not None:
                tab, track = result
                self.assertIn(tab, self.app.QUEUE_SOURCES)
                self.assertPlaybackId(track)
        self.each("play_from_command", self.app.play_from_command, check)

    def test_queue_move_command(self):
        def check(result):
            if result is not None:
                track, offset = result
                self.assertPlaybackId(track)
                self.assertIsInstance(offset, int)
                self.assertLessEqual(abs(offset), self.app.MAX_TRACKS)
        self.each("queue_move_from_command", self.app.queue_move_from_command, check)

    def test_track_like_command(self):
        def check(result):
            if result is not None:
                self.assertPlaybackId(result[0])
                self.assertIsInstance(result[1], bool)
        self.each("track_like_from_command", self.app.track_like_from_command, check)

    def test_link_commands(self):
        def check(result):
            if result is not None:
                mode, text = result
                self.assertIn(mode, ("next", "play"))
                self.assertIsInstance(text, str)
        self.each("link_from_command", self.app.link_from_command, check)

        def parse(command):  # the second stage: a clean LinkError is the expected rejection
            link = self.app.link_from_command(command)
            text = link[1] if link else command
            try:
                return self.app.parse_track_link(text)
            except self.app.LinkError:
                return None

        def check_link(result):
            if result is not None:
                kind, url = result
                self.assertIn(kind, ("track", "short"))
                self.assertRegex(url, r"\Ahttps://(soundcloud\.com|www\.soundcloud\.com|m\.soundcloud\.com|on\.soundcloud\.com)/")
                self.assertNotIn("\x00", url)
                self.assertLessEqual(len(url.encode()), self.app.MAX_URL_BYTES)
        self.each("parse_track_link", parse, check_link)

    def test_mode_commands(self):
        for current in self.app.REPEAT_MODES:
            self.each("repeat_from_command", lambda c, cur=current: self.app.repeat_from_command(c, cur),
                      lambda r: self.assertTrue(r is None or r in self.app.REPEAT_MODES))
        for current in (True, False):
            self.each("shuffle_from_command", lambda c, cur=current: self.app.shuffle_from_command(c, cur),
                      lambda r: self.assertTrue(r is None or isinstance(r, bool)))
        for name in ("autoplay", "notify", "mute", "multi-press"):
            self.each("on_off_from_command", lambda c, n=name: self.app.on_off_from_command(c, n),
                      lambda r: self.assertTrue(r is None or isinstance(r, bool)))

    def test_number_commands(self):
        def in_range(low, high):
            def check(result):
                if result is not None:
                    self.assertTrue(math.isfinite(result) and low <= result <= high)
            return check
        self.each("volume_from_command", self.app.volume_from_command, in_range(0.0, 1.0))
        self.each("volume_step_from_command", self.app.volume_step_from_command, in_range(-1.0, 1.0))
        self.each("seek_by_from_command", self.app.seek_by_from_command, in_range(-86400, 86400))

    def test_seek_target_is_always_sane(self):
        rng = random.Random(7)
        for _ in range(200):
            position, delta = rng.uniform(-1e6, 1e6), rng.uniform(-86400, 86400)
            duration = rng.choice([0.0, rng.uniform(0, 20000)])
            target = self.app.seek_by_target(position, delta, duration)
            self.assertTrue(math.isfinite(target) and target >= 0)
            if duration > 0:
                self.assertLessEqual(target, max(0.0, duration - self.app.SEEK_END_MARGIN))

    def test_known_good_commands_still_accepted(self):
        app = self.app
        self.assertEqual(app.play_from_command("play-from:likes:soundcloud:tracks:42"), ("likes", "soundcloud:tracks:42"))
        self.assertEqual(app.queue_move_from_command("queue-move:soundcloud:tracks:42:-3"), ("soundcloud:tracks:42", -3))
        self.assertEqual(app.volume_from_command("volume:0.5"), 0.5)
        self.assertEqual(app.volume_from_command("volume:7"), 1.0)
        self.assertIsNone(app.volume_from_command("volume:nan"))
        self.assertIsNone(app.seek_by_from_command("seek-by:inf"))
        self.assertEqual(app.seek_by_from_command("seek-by:-5"), -5.0)


if __name__ == "__main__":
    unittest.main()
