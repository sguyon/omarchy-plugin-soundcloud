import contextlib
import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "soundcloud_app.py"


def load_module():
    spec = importlib.util.spec_from_file_location("soundcloud_app", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProfilePathsTest(unittest.TestCase):
    def test_uses_xdg_locations_when_present(self):
        app = load_module()
        env = {
            "HOME": "/home/tester",
            "XDG_DATA_HOME": "/custom/data",
            "XDG_CACHE_HOME": "/custom/cache",
        }

        paths = app.profile_paths(env)

        self.assertEqual(paths.data_dir, Path("/custom/data/omarchy-soundcloud"))
        self.assertEqual(paths.cache_dir, Path("/custom/cache/omarchy-soundcloud"))
        self.assertEqual(paths.cookie_db, paths.data_dir / "cookies.sqlite")

    def test_falls_back_to_home_xdg_defaults(self):
        app = load_module()

        paths = app.profile_paths({"HOME": "/home/tester"})

        self.assertEqual(paths.data_dir, Path("/home/tester/.local/share/omarchy-soundcloud"))
        self.assertEqual(paths.cache_dir, Path("/home/tester/.cache/omarchy-soundcloud"))

    def test_private_directory_rejects_symlink_and_uses_owner_only_mode(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaises(PermissionError):
                app.ensure_private_directory(link / "child")

            private = root / "private"
            app.ensure_private_directory(private)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)

    def test_runtime_profile_ignores_environment_path_overrides(self):
        app = load_module()
        trusted_home = Path(app.pwd.getpwuid(os.geteuid()).pw_dir)
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/untrusted-home",
            "XDG_DATA_HOME": "/tmp/untrusted-data",
            "XDG_CACHE_HOME": "/tmp/untrusted-cache",
        }):
            paths = app.profile_paths()

        self.assertEqual(paths.data_dir, trusted_home / ".local/share/omarchy-soundcloud")
        self.assertEqual(paths.cache_dir, trusted_home / ".cache/omarchy-soundcloud")

    def test_cookie_store_is_descriptor_anchored_and_owner_only(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                store = app.CookieStore(directory_fd)
                record = {
                    "name": "session",
                    "value": "test-value",
                    "domain": ".soundcloud.com",
                    "path": "/",
                    "expires": None,
                    "secure": True,
                    "httpOnly": True,
                }
                store.save([record])
                self.assertEqual(store.load(), [record])
                cookie_path = Path(directory) / "cookies.json"
                self.assertEqual(stat.S_IMODE(cookie_path.stat().st_mode), 0o600)
                cookie_path.unlink()
                target = Path(directory) / "target"
                target.write_text("[]")
                cookie_path.symlink_to(target)
                with self.assertRaises(OSError):
                    store.load()
            finally:
                os.close(directory_fd)

    def assert_returns_despite_fifo(self, fifo_path, call):
        # A same-user process can plant a FIFO at a fixed state path. Opening it
        # without O_NONBLOCK blocks until a writer appears, hanging the daemon at
        # startup before any file-type check runs.
        os.mkfifo(fifo_path, 0o600)
        outcome = {}

        def run():
            try:
                outcome["value"] = call()
            except Exception as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(2)
        if worker.is_alive():
            for flags in (os.O_WRONLY | os.O_NONBLOCK, os.O_RDONLY | os.O_NONBLOCK):
                with contextlib.suppress(OSError):
                    os.close(os.open(fifo_path, flags))
            worker.join(2)
            self.fail(f"blocked opening planted FIFO at {fifo_path.name}")
        return outcome

    def test_private_stores_refuse_planted_fifo_without_blocking(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                store = app.CookieStore(directory_fd)
                outcome = self.assert_returns_despite_fifo(Path(directory) / "cookies.json", store.load)
                self.assertIsInstance(outcome.get("error"), PermissionError)

                outcome = self.assert_returns_despite_fifo(
                    Path(directory) / "cookies.sqlite", store.migrate_legacy_sqlite
                )
                self.assertIsInstance(outcome.get("error"), PermissionError)

                cache = app.PrivateJsonStore(directory_fd, "tracks.json", app.MAX_TRACK_CACHE_BYTES)
                outcome = self.assert_returns_despite_fifo(Path(directory) / "tracks.json", cache.load)
                self.assertEqual(outcome, {"value": None})
            finally:
                os.close(directory_fd)

    def test_runtime_event_log_refuses_planted_fifo_without_blocking(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            runtime = Path(directory) / "omarchy-soundcloud"
            runtime.mkdir(mode=0o700)
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                outcome = self.assert_returns_despite_fifo(
                    runtime / "pagination.log", app.open_runtime_event_log
                )
            self.assertIsInstance(outcome.get("error"), OSError)

    def test_track_cache_survives_backend_restart_in_owner_only_store(self):
        app = load_module()
        payload = {
            "home": [{
                "title": "Cached track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/cached-track",
                "artUrl": "https://i1.sndcdn.com/artworks-test-large.jpg",
                "playCount": 12,
                "durationMs": 3456,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                store = app.PrivateJsonStore(directory_fd, "tracks.json", app.MAX_TRACK_CACHE_BYTES)
                store.save(payload)
                self.assertEqual(store.load(), payload)
                self.assertEqual(
                    stat.S_IMODE((Path(directory) / "tracks.json").stat().st_mode),
                    0o600,
                )
            finally:
                os.close(directory_fd)

    def test_track_cache_is_sanitized_but_keeps_artwork_for_refresh(self):
        app = load_module()
        cache = app.sanitize_track_cache({
            "home": [{
                "playbackId": "soundcloud:tracks:123",
                "title": "Track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/track",
                "artUrl": "https://i1.sndcdn.com/artworks-test-large.jpg",
                "playCount": 12,
                "durationMs": 3456,
                "privatePayload": {"secret": True},
            }],
            "evil": [{"title": "ignored"}],
        })

        self.assertEqual(set(cache), {"home"})
        self.assertEqual(set(cache["home"][0]), {
            "playbackId", "title", "artist", "url", "artUrl", "playCount", "durationMs",
        })
        self.assertEqual(
            cache["home"][0]["artUrl"],
            "https://i1.sndcdn.com/artworks-test-large.jpg",
        )


class UriPolicyTest(unittest.TestCase):
    def test_soundcloud_domains_stay_in_the_app(self):
        app = load_module()

        self.assertTrue(app.is_soundcloud_uri("https://soundcloud.com/you/likes"))
        self.assertTrue(app.is_soundcloud_uri("https://secure.soundcloud.com/sign-in"))
        self.assertTrue(app.is_soundcloud_uri("https://m.soundcloud.com/discover"))

    def test_lookalike_domains_are_rejected(self):
        app = load_module()

        self.assertFalse(app.is_soundcloud_uri("https://soundcloud.com.example.org/login"))
        self.assertFalse(app.is_soundcloud_uri("javascript:alert(1)"))
        self.assertFalse(app.is_soundcloud_uri("not a url"))
        self.assertFalse(app.is_soundcloud_uri("http://soundcloud.com/you/likes"))
        self.assertFalse(app.is_soundcloud_uri("https://user:pass@soundcloud.com/you/likes"))
        self.assertFalse(app.is_soundcloud_uri("https://soundcloud.com:8443/you/likes"))

    def test_bot_check_captcha_can_load_but_nothing_else_off_soundcloud(self):
        app = load_module()

        # Blocking this iframe left the sign-in window blank when liking a track.
        self.assertTrue(app.is_bot_check_uri("https://geo.captcha-delivery.com/captcha/?cid=x"))
        self.assertFalse(app.is_bot_check_uri("http://geo.captcha-delivery.com/captcha/"))
        self.assertFalse(app.is_bot_check_uri("https://geo.captcha-delivery.com.example.org/"))
        self.assertFalse(app.is_bot_check_uri("https://user@geo.captcha-delivery.com/"))
        self.assertFalse(app.is_bot_check_uri("https://example.org/captcha/"))


class GraphicsWorkaroundTest(unittest.TestCase):
    def test_disables_webkit_dmabuf_on_hyprland(self):
        app = load_module()
        env = {"HYPRLAND_INSTANCE_SIGNATURE": "instance", "XDG_SESSION_TYPE": "wayland"}

        changed = app.apply_graphics_workarounds(env)

        self.assertTrue(changed)
        self.assertEqual(env["WEBKIT_DISABLE_DMABUF_RENDERER"], "1")

    def test_preserves_an_explicit_renderer_setting(self):
        app = load_module()
        env = {
            "HYPRLAND_INSTANCE_SIGNATURE": "instance",
            "WEBKIT_DISABLE_DMABUF_RENDERER": "0",
        }

        changed = app.apply_graphics_workarounds(env)

        self.assertFalse(changed)
        self.assertEqual(env["WEBKIT_DISABLE_DMABUF_RENDERER"], "0")


class BackendCommandTest(unittest.TestCase):
    def test_pagination_event_log_is_dedicated_sanitized_and_rotated(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pagination.log"
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            setattr(app, "_event_log_fd", fd)
            setattr(app, "MAX_EVENT_LOG_BYTES", 320)
            try:
                app.log_event("untrusted-event", secret="must-not-appear")
                for count in range(12):
                    app.log_event(
                        "track-page",
                        source="feed",
                        addedCount=count,
                        hasMore=True,
                        secret="must-not-appear",
                    )
            finally:
                app.close_runtime_event_log()

            contents = path.read_text()
            self.assertLessEqual(path.stat().st_size, app.MAX_EVENT_LOG_BYTES)
            self.assertNotIn("must-not-appear", contents)
            for line in contents.splitlines():
                record = json.loads(line)
                self.assertEqual(record["event"], "track-page")
                self.assertEqual(record["source"], "feed")

        source = MODULE_PATH.read_text()
        self.assertIn("stderr=subprocess.DEVNULL", source)
        self.assertIn("pass_fds=(event_log_fd,)", source)

    def test_runtime_socket_uses_xdg_runtime_dir(self):
        app = load_module()

        path = app.runtime_socket_path({"XDG_RUNTIME_DIR": "/run/user/123"})

        self.assertEqual(path, Path("/run/user/123/omarchy-soundcloud/control.sock"))

    def test_runtime_socket_fails_closed_without_xdg_runtime_dir(self):
        app = load_module()

        with self.assertRaises(RuntimeError):
            app.runtime_socket_path({})

    def test_control_socket_handles_multiple_correlated_requests_per_connection(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "control.sock"

            def dispatch(command, connection, request_id):
                app.send_response(
                    connection,
                    {"ok": True, "command": command},
                    request_id=request_id,
                )

            server = app.ControlServer(socket_path, dispatch)
            server.start()
            try:
                for _ in range(100):
                    if socket_path.exists():
                        break
                    time.sleep(0.01)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(1)
                client.connect(str(socket_path))
                stream = client.makefile("rwb", buffering=0)
                stream.write(b'{"id":1,"command":"status"}\n')
                stream.write(b'{"id":2,"command":"tracks"}\n')

                first = json.loads(stream.readline())
                second = json.loads(stream.readline())

                self.assertEqual(first, {"type": "response", "id": 1, "ok": True, "command": "status"})
                self.assertEqual(second, {"type": "response", "id": 2, "ok": True, "command": "tracks"})
                client.close()
            finally:
                server.close()
                server.join(timeout=1)

    def test_control_socket_rejects_oversized_frames(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "control.sock"
            server = app.ControlServer(socket_path, lambda *_args: None)
            server.start()
            try:
                for _ in range(100):
                    if socket_path.exists():
                        break
                    time.sleep(0.01)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(1)
                client.connect(str(socket_path))
                client.sendall(b"x" * (app.MAX_REQUEST_BYTES + 1))
                self.assertEqual(client.recv(1), b"")
                client.close()
            finally:
                server.close()
                server.join(timeout=1)

    def test_status_subscribers_receive_events_and_failed_connections_are_removed(self):
        app = load_module()

        class FakeConnection:
            def __init__(self, succeeds):
                self.succeeds = succeeds
                self.messages = []

            def send(self, payload):
                self.messages.append(payload)
                return self.succeeds

        subscribers = app.StatusSubscribers()
        active = FakeConnection(True)
        failed = FakeConnection(False)
        subscribers.add(active)
        subscribers.add(failed)

        subscribers.publish({"playing": True, "title": "Track"})
        subscribers.publish({"playing": False, "title": "Track"})

        self.assertEqual(active.messages[0]["type"], "status")
        self.assertEqual(active.messages[0]["state"]["title"], "Track")
        self.assertEqual(len(active.messages), 2)
        self.assertEqual(len(failed.messages), 1)

    def test_subscribers_can_receive_completed_track_events(self):
        app = load_module()

        class FakeConnection:
            def __init__(self):
                self.messages = []

            def send(self, payload):
                self.messages.append(payload)
                return True

        subscribers = app.StatusSubscribers()
        connection = FakeConnection()
        subscribers.add(connection)
        subscribers.publish_message({"type": "tracks", "source": "feed", "tracks": []})

        self.assertEqual(
            connection.messages,
            [{"type": "tracks", "source": "feed", "tracks": []}],
        )

    def test_only_known_player_actions_have_scripts(self):
        app = load_module()

        self.assertIn("playControls__play", app.command_script("play-pause"))
        # next and previous follow the queue; they no longer click the page's skip buttons
        self.assertIsNone(app.command_script("next"))
        self.assertIsNone(app.command_script("previous"))
        self.assertIsNone(app.command_script("arbitrary-javascript"))

    def test_socket_launch_command_is_backward_compatible_with_show(self):
        app = load_module()

        self.assertEqual(app.normalize_backend_command("launch"), "show")
        self.assertEqual(app.normalize_backend_command("play-pause"), "play-pause")

    def test_seek_script_clamps_ratio_and_targets_soundcloud_timeline(self):
        app = load_module()

        script = app.seek_script(1.5)

        self.assertIn("const ratio = 1.0", script)
        self.assertIn("playbackTimeline__progressWrapper", script)
        self.assertIn("currentTime", script)

    def test_status_script_returns_required_fields(self):
        app = load_module()

        script = app.status_script()

        for field in ("playerPresent", "playing", "title", "artist", "artUrl", "loggedIn", "url"):
            self.assertIn(field, script)
        self.assertIn("playControls__play", script)
        self.assertIn("Pause", script)
        self.assertIn("playbackTimeline__timePassed", script)
        self.assertIn("playbackTimeline__duration", script)
        self.assertIn("getComputedStyle(art).backgroundImage", script)
        self.assertIn(".playbackSoundBadge__avatar [style*=\"background-image\"]", script)
        self.assertIn("signin|register", script)

    def test_status_script_clears_stale_badge_without_an_audio_player(self):
        app = load_module()
        script = app.status_script()

        self.assertIn("const playerPresent = !!audio", script)
        self.assertIn("playing: playerPresent ? !audio.paused : false", script)
        self.assertIn("artUrl: playerPresent ? artUrl : ''", script)
        validated = app.validate_status_payload({
            "playerPresent": False,
            "playing": False,
            "title": "",
            "artist": "",
            "artUrl": "",
            "loggedIn": True,
            "url": "https://soundcloud.com/feed",
            "duration": 0,
            "position": 0,
        })
        self.assertIsNotNone(validated)
        self.assertFalse(validated["playerPresent"])

    def test_status_validation_preserves_only_explicit_playback_states(self):
        app = load_module()
        base = {
            "playerPresent": False,
            "playing": False,
            "title": "",
            "artist": "",
            "artUrl": "",
            "loggedIn": True,
            "url": "https://soundcloud.com/feed",
            "duration": 0,
            "position": 0,
        }

        for state in ("idle", "resolving", "buffering", "playing", "paused", "error"):
            validated = app.validate_status_payload({**base, "playbackState": state})
            self.assertEqual(validated["playbackState"], state)
        self.assertIsNone(app.validate_status_payload({**base, "playbackState": "unknown"}))

    def test_failed_selection_keeps_its_track_id_so_the_error_shows_on_its_row(self):
        app = load_module()
        player = object.__new__(app.GstPlayback)
        player.generation = 0
        player.on_change = lambda: None
        player._stop_current = lambda: None
        player.begin({"title": "Broken", "playbackId": "soundcloud:tracks:5"})

        player.stop("SoundCloud could not resolve this track")
        failed = app.validate_status_payload({**player.status(), "loggedIn": True})
        self.assertEqual(failed["playbackState"], "error")
        self.assertEqual(failed["playbackId"], "soundcloud:tracks:5")

        player.stop()
        self.assertEqual(player.status()["playbackId"], "")

    def test_status_identifies_the_selected_track_so_the_list_can_mark_it(self):
        app = load_module()
        player = object.__new__(app.GstPlayback)
        player.generation = 0
        player.on_change = lambda: None
        player._stop_current = lambda: None
        player.begin({"title": "Selected", "playbackId": "soundcloud:tracks:9"})

        validated = app.validate_status_payload({**player.status(), "loggedIn": True})

        self.assertEqual(validated["playbackId"], "soundcloud:tracks:9")
        forged = app.validate_status_payload({**player.status(), "playbackId": "javascript:1"})
        self.assertEqual(forged["playbackId"], "")

    def test_native_player_begins_in_resolving_state_before_media_exists(self):
        app = load_module()
        player = object.__new__(app.GstPlayback)
        player.active = True
        player.playing = True
        player.metadata = {"title": "Old"}
        player.error = "old error"
        player.state = "playing"
        player.generation = 7
        player.on_change = lambda: None
        player._stop_current = lambda: setattr(player, "generation", player.generation + 1)

        generation = player.begin({"title": "Selected", "playbackId": "soundcloud:tracks:9"})
        status = player.status()

        self.assertEqual(generation, 8)
        self.assertEqual(status["playbackState"], "resolving")
        self.assertFalse(status["playerPresent"])
        self.assertFalse(status["playing"])
        self.assertEqual(player.metadata["title"], "Selected")
        # Resolution can take seconds; the popup names what is loading.
        self.assertEqual(status["title"], "Selected")

    def test_private_api_playback_resolution_stays_inside_webkit(self):
        app = load_module()
        script = app.api_request_capture_script()

        self.assertIn("const playbackTracks = new Map()", script)
        self.assertIn("window.__omarchyPlayApiTrack", script)
        self.assertIn("headers: new Headers(context.headers)", script)
        self.assertIn("messageHandlers.omarchyPlayback.postMessage", script)
        self.assertIn("const generation = ++playbackGeneration", script)
        self.assertIn("if (generation !== playbackGeneration) return", script)
        self.assertIn("if (pageAudio && !pageAudio.paused) pageAudio.pause()", script)
        self.assertNotIn("window.playbackTracks", script)

    def test_stream_urls_are_strictly_limited_to_soundcloud_media_hosts(self):
        app = load_module()

        self.assertEqual(
            app.validate_stream_uri("https://cf-hls-media.sndcdn.com/media/test.m3u8?token=private"),
            "https://cf-hls-media.sndcdn.com/media/test.m3u8?token=private",
        )
        self.assertEqual(
            app.validate_stream_uri("https://playback.media-streaming.soundcloud.cloud/test"),
            "https://playback.media-streaming.soundcloud.cloud/test",
        )
        for value in (
            "http://cf-hls-media.sndcdn.com/test",
            "https://sndcdn.com.evil.example/test",
            "https://user:pass@cf-hls-media.sndcdn.com/test",
            "https://example.com/test",
        ):
            self.assertIsNone(app.validate_stream_uri(value))

    def test_source_routes_are_fixed_soundcloud_pages(self):
        app = load_module()

        self.assertEqual(app.source_uri("home"), "https://soundcloud.com/discover")
        self.assertEqual(app.source_uri("likes"), "https://soundcloud.com/you/likes")
        self.assertEqual(app.source_uri("feed"), "https://soundcloud.com/feed")
        self.assertIsNone(app.source_uri("https://evil.example"))


    def test_api_tracks_script_reads_only_intercepted_route_state(self):
        app = load_module()

        feed_script = app.api_tracks_script("feed")
        home_script = app.api_tracks_script("home")

        self.assertIn('const source = "feed"', feed_script)
        self.assertNotIn("document.querySelector", feed_script)
        self.assertNotIn("fetch(", feed_script)
        self.assertIn('const source = "home"', home_script)
        self.assertIn("window.__omarchyApiTrackLists", home_script)
        self.assertIn("state.completed", feed_script)
        self.assertIn("state.error", feed_script)

    def test_track_command_allows_only_route_bound_api_sources(self):
        app = load_module()

        self.assertEqual(app.track_source_from_command("tracks:home"), "home")
        self.assertEqual(app.track_source_from_command("tracks:feed"), "feed")
        self.assertIsNone(app.track_source_from_command("tracks"))
        self.assertIsNone(app.track_source_from_command("tracks:https://evil.example"))

    def test_cli_does_not_offer_bare_tracks_command(self):
        # The backend only answers tracks:home and tracks:feed, so a bare
        # `tracks` CLI command always failed with "unknown command".
        app = load_module()

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            app.parse_args(["tracks"])

    def test_load_more_command_allows_only_route_bound_sources(self):
        app = load_module()

        self.assertEqual(app.load_more_source_from_command("load-more:home"), "home")
        self.assertEqual(app.load_more_source_from_command("load-more:feed"), "feed")
        self.assertIsNone(app.load_more_source_from_command("load-more"))
        self.assertIsNone(app.load_more_source_from_command("load-more:likes"))

    def test_play_command_accepts_only_stable_soundcloud_track_ids(self):
        app = load_module()

        self.assertEqual(
            app.playback_id_from_command("play:soundcloud:tracks:123"),
            "soundcloud:tracks:123",
        )
        self.assertIsNone(app.playback_id_from_command("play:soundcloud:tracks:0"))
        self.assertIsNone(app.playback_id_from_command("play-url:https://soundcloud.com/a/b"))
        self.assertIsNone(app.playback_id_from_command("play:soundcloud:users:123"))

    def test_track_pages_append_unique_tracks_and_new_navigation_resets(self):
        app = load_module()
        first = [
            {"url": "https://soundcloud.com/artist/one", "title": "One"},
            {"url": "https://soundcloud.com/artist/two", "title": "Two"},
        ]
        second = [
            {"url": "https://soundcloud.com/artist/two", "title": "Two updated"},
            {"url": "https://soundcloud.com/artist/three", "title": "Three"},
        ]

        self.assertEqual(
            [track["title"] for track in app.merge_track_pages(first, second, reset=False, limit=10)],
            ["One", "Two updated", "Three"],
        )
        self.assertEqual(
            app.merge_track_pages(first, second, reset=True, limit=10),
            second,
        )

    def test_load_more_script_scrolls_only_the_selected_route(self):
        app = load_module()

        home_script = app.load_more_script("home")
        feed_script = app.load_more_script("feed")

        self.assertIn('__omarchyLoadMore("home")', home_script)
        self.assertIn('__omarchyLoadMore("feed")', feed_script)
        self.assertNotIn("window.scrollTo", home_script)
        self.assertIsNone(app.load_more_script("likes"))

    def test_load_more_fetches_the_validated_next_api_page_without_dom_scrolling(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const captureScript = {json.dumps(capture_script)};
const posted = [];
const calls = [];
const pages = [
  {{ collection: [{{ permalink_url: 'https://soundcloud.com/a/one', title: 'One', duration: 1000, user: {{ username: 'A' }} }}], next_href: 'https://api-v2.soundcloud.com/stream?offset=10' }},
  {{ collection: [{{ permalink_url: 'https://soundcloud.com/b/two', title: 'Two', duration: 1000, user: {{ username: 'B' }} }}], next_href: null }}
];
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.window = global;
window.webkit = {{ messageHandlers: {{ omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }} }} }};
window.fetch = async function(input, init) {{
  calls.push(typeof input === 'string' ? input : input.url);
  const body = JSON.stringify(pages.shift());
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval(captureScript);
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?offset=0', {{
    headers: {{ authorization: 'OAuth private-test-value' }}
  }}));
  await new Promise(setImmediate);
  const started = window.__omarchyLoadMore('feed');
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  const exhausted = window.__omarchyLoadMore('feed');
  process.stdout.write(JSON.stringify({{ started, exhausted, calls, posted, globals: Object.keys(window) }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertTrue(result["started"])
        self.assertEqual(result["calls"], [
            "https://api-v2.soundcloud.com/stream?offset=0",
            "https://api-v2.soundcloud.com/stream?offset=10",
        ])
        self.assertEqual([message["tracks"][0]["title"] for message in result["posted"]], ["One", "Two"])
        self.assertFalse(result["posted"][1]["hasMore"])
        self.assertEqual(result["exhausted"], {"started": False, "hasMore": False})
        self.assertNotIn("private-test-value", completed.stdout)

    def test_failed_load_more_emits_a_retryable_completion_event(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const posted = [];
let callCount = 0;
global.location = {{ href: 'https://soundcloud.com/feed' }};
global.window = global;
window.webkit = {{ messageHandlers: {{ omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }} }} }};
window.fetch = async function() {{
  callCount += 1;
  if (callCount > 1) return {{ ok: false, status: 503 }};
  const body = JSON.stringify({{
    collection: [{{ permalink_url: 'https://soundcloud.com/a/one', title: 'One', duration: 1000 }}],
    next_href: 'https://api-v2.soundcloud.com/stream?offset=10'
  }});
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?offset=0'));
  await new Promise(setImmediate);
  const started = window.__omarchyLoadMore('feed');
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  const retried = window.__omarchyLoadMore('feed');
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({{ started, retried, callCount, posted }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["started"], {"started": True, "hasMore": True})
        self.assertEqual(result["retried"], {"started": True, "hasMore": True})
        self.assertEqual(result["callCount"], 3)
        self.assertEqual(len(result["posted"]), 3)
        self.assertEqual(result["posted"][1]["tracks"], [])
        self.assertTrue(result["posted"][1]["hasMore"])
        self.assertEqual(result["posted"][1]["addedCount"], 0)
        self.assertEqual(result["posted"][1]["error"], "Could not load more tracks")

    def test_load_more_stops_before_requesting_a_cursor_cycle(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const calls = [];
const pages = [
  {{ collection: [{{ permalink_url: 'https://soundcloud.com/a/one', title: 'One', duration: 1 }}], next_href: 'https://api-v2.soundcloud.com/stream?offset=10' }},
  {{ collection: [{{ permalink_url: 'https://soundcloud.com/a/two', title: 'Two', duration: 1 }}], next_href: 'https://api-v2.soundcloud.com/stream?offset=0' }}
];
global.location = {{ href: 'https://soundcloud.com/feed' }};
global.window = global;
window.webkit = {{ messageHandlers: {{ omarchyTracks: {{ postMessage() {{}} }} }} }};
window.fetch = async function(input) {{
  calls.push(typeof input === 'string' ? input : input.url);
  const body = JSON.stringify(pages.shift());
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?offset=0'));
  await new Promise(setImmediate);
  window.__omarchyLoadMore('feed');
  await new Promise(setImmediate); await new Promise(setImmediate);
  const cycle = window.__omarchyLoadMore('feed');
  process.stdout.write(JSON.stringify({{ calls, cycle }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["calls"], [
            "https://api-v2.soundcloud.com/stream?offset=0",
            "https://api-v2.soundcloud.com/stream?offset=10",
        ])
        self.assertEqual(result["cycle"], {"started": False, "hasMore": False})

    def test_api_observation_script_exposes_only_bounded_resource_paths(self):
        app = load_module()

        script = app.api_observation_script()

        self.assertIn("api-v2.soundcloud.com", script)
        self.assertIn("candidate.pathname", script)
        self.assertNotIn("candidate.href", script)
        self.assertIn("slice(0, 32)", script)

    def test_api_request_capture_keeps_frontend_credentials_out_of_page_global_state(self):
        app = load_module()

        capture_script = app.api_request_capture_script()
        tracks_script = app.api_tracks_script("feed")

        self.assertIn("window.fetch", capture_script)
        self.assertIn("/stream", capture_script)
        self.assertNotIn("__omarchyApiRequests", capture_script)
        self.assertIn("const pagination = Object.create(null)", capture_script)
        self.assertIn("const privateHeaders = new Headers()", capture_script)
        self.assertIn("url.protocol !== 'https:'", capture_script)
        self.assertIn("url.username || url.password", capture_script)
        self.assertIn("const context = path ? requestContext(input, init) : null", capture_script)
        self.assertIn(
            "JSON.stringify({ source, tracks, reset, hasMore, addedCount })",
            capture_script,
        )
        self.assertNotIn("console.", capture_script)
        self.assertNotIn("fetch(", tracks_script)
        self.assertNotIn("performance.getEntriesByType", tracks_script)

    def test_private_transcoding_endpoints_reject_userinfo_and_nonstandard_ports(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const posted = [];
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.window = global;
window.webkit = {{ messageHandlers: {{ omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }} }} }};
const encode = (value) => new TextEncoder().encode(JSON.stringify(value));
const response = (value) => {{
  const bytes = encode(value); let sent = false; let cancelled = false;
  return {{ ok: true, headers: new Headers({{'content-length': String(bytes.length)}}), clone() {{ return response(value); }},
    body: {{ getReader() {{ return {{ async read() {{ if (sent) return {{done:true}}; sent=true; return {{done:false,value:bytes}}; }}, async cancel() {{ cancelled=true; }} }}; }} }} }};
}};
window.fetch = async () => response({{ collection: [
  {{kind:'track',id:1,title:'Userinfo',duration:1,permalink_url:'https://soundcloud.com/a/one',media:{{transcodings:[{{url:'https://user:pass@api-v2.soundcloud.com/media/one',format:{{protocol:'progressive',mime_type:'audio/mpeg'}}}}]}}}},
  {{kind:'track',id:2,title:'Port',duration:1,permalink_url:'https://soundcloud.com/a/two',media:{{transcodings:[{{url:'https://api-v2.soundcloud.com:8443/media/two',format:{{protocol:'progressive',mime_type:'audio/mpeg'}}}}]}}}}
], next_href:null}});
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}}; XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}}; XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream', {{headers:{{authorization:'secret'}}}}));
  for (let i=0;i<5;i++) await new Promise(setImmediate);
  process.stdout.write(JSON.stringify(posted));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
        tracks = json.loads(completed.stdout)[0]["tracks"]
        self.assertEqual([track.get("playbackId", "") for track in tracks], ["", ""])
        self.assertNotIn("secret", completed.stdout)

    def test_broker_response_reader_cancels_an_over_limit_stream(self):
        app = load_module()
        script = app.api_request_capture_script()
        harness = f"""
let cancelled = false; let reads = 0;
global.location = {{href:'https://soundcloud.com/feed',hostname:'soundcloud.com',pathname:'/feed'}};
global.window = global;
window.webkit = {{messageHandlers:{{omarchyTracks:{{postMessage(){{}}}},omarchyPlayback:{{postMessage(){{}}}}}}}};
const chunk = new Uint8Array(1024 * 1024);
const makeResponse = () => ({{ok:true,headers:new Headers(),clone(){{return makeResponse();}},body:{{getReader(){{return {{async read(){{reads++;return {{done:false,value:chunk}};}},async cancel(){{cancelled=true;}}}};}}}}}});
window.fetch = async () => makeResponse();
global.XMLHttpRequest=function(){{}}; XMLHttpRequest.prototype.open=function(){{}};
XMLHttpRequest.prototype.setRequestHeader=function(){{}}; XMLHttpRequest.prototype.send=function(){{}};
XMLHttpRequest.prototype.addEventListener=function(){{}};
eval({json.dumps(script)});
(async()=>{{await window.fetch('https://api-v2.soundcloud.com/stream'); for(let i=0;i<6;i++) await new Promise(setImmediate); process.stdout.write(JSON.stringify({{cancelled,reads}}));}})();
"""
        result = json.loads(subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        ).stdout)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["reads"], 3)

    def test_newer_home_request_wins_and_embedded_tracks_are_combined_with_expanded_ids(self):
        app = load_module()
        script = app.api_request_capture_script()
        harness = f"""
const posted=[]; let finishExpansion;
global.location={{href:'https://soundcloud.com/discover',hostname:'soundcloud.com',pathname:'/discover'}}; global.window=global;
window.webkit={{messageHandlers:{{omarchyTracks:{{postMessage(v){{posted.push(JSON.parse(v));}}}},omarchyPlayback:{{postMessage(){{}}}}}}}};
const resp=(value)=>{{const bytes=new TextEncoder().encode(JSON.stringify(value));return {{ok:true,headers:new Headers(),clone(){{return resp(value);}},body:{{getReader(){{let sent=false;return {{async read(){{if(sent)return {{done:true}};sent=true;return {{done:false,value:bytes}};}},async cancel(){{}}}};}}}}}};}};
const old={{collection:[{{kind:'track',id:1,title:'Embedded',duration:1,permalink_url:'https://soundcloud.com/a/embedded'}},{{items:{{collection:[{{tracks:[{{id:2}}]}}]}}}}],next_href:null}};
const newer={{collection:[{{kind:'track',id:3,title:'Newer',duration:1,permalink_url:'https://soundcloud.com/a/newer'}}],next_href:null}};
window.fetch=async(input)=>{{const url=typeof input==='string'?input:input.url;if(url.includes('/tracks?'))return new Promise(r=>{{finishExpansion=()=>r(resp([{{kind:'track',id:2,title:'Expanded',duration:1,permalink_url:'https://soundcloud.com/a/expanded'}}]));}});if(url.includes('old'))return resp(old);return resp(newer);}};
global.XMLHttpRequest=function(){{}}; XMLHttpRequest.prototype.open=function(){{}}; XMLHttpRequest.prototype.setRequestHeader=function(){{}}; XMLHttpRequest.prototype.send=function(){{}}; XMLHttpRequest.prototype.addEventListener=function(){{}};
eval({json.dumps(script)});
(async()=>{{await window.fetch('https://api-v2.soundcloud.com/mixed-selections?old=1');for(let i=0;i<3;i++)await new Promise(setImmediate);await window.fetch('https://api-v2.soundcloud.com/mixed-selections?new=1');for(let i=0;i<5;i++)await new Promise(setImmediate);finishExpansion();for(let i=0;i<8;i++)await new Promise(setImmediate);process.stdout.write(JSON.stringify(posted));}})();
"""
        posted = json.loads(subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        ).stdout)
        self.assertEqual([[track["title"] for track in event["tracks"]] for event in posted], [["Newer"]])

    def test_superseded_home_expansion_cannot_replace_newer_playback_metadata(self):
        app = load_module()
        script = app.api_request_capture_script()
        harness = f"""
const posted=[]; const playback=[]; const mediaCalls=[]; let finishOldExpansion;
global.location={{href:'https://soundcloud.com/discover',hostname:'soundcloud.com',pathname:'/discover'}};
global.document={{querySelector(){{return null;}}}}; global.window=global;
window.webkit={{messageHandlers:{{omarchyTracks:{{postMessage(v){{posted.push(JSON.parse(v));}}}},omarchyPlayback:{{postMessage(v){{const {{streamUrl,...safe}}=JSON.parse(v);playback.push(safe);}}}}}}}};
const resp=(value)=>new Response(JSON.stringify(value),{{status:200}});
const track=(title,endpoint)=>({{kind:'track',id:7,urn:'soundcloud:tracks:7',title,duration:1,permalink_url:'https://soundcloud.com/a/'+title.toLowerCase(),media:{{transcodings:[{{url:'https://api-v2.soundcloud.com/media/'+endpoint,format:{{protocol:'progressive',mime_type:'audio/mpeg'}}}}]}}}});
const old={{collection:[{{items:{{collection:[{{tracks:[{{id:7}}]}}]}}}}],next_href:null}};
const newer={{collection:[track('New','new')],next_href:null}};
window.fetch=async(input)=>{{
  const url=typeof input==='string'?input:input.url;
  if(url.includes('/tracks?'))return new Promise(resolve=>{{finishOldExpansion=()=>resolve(resp([track('Old','old')]));}});
  if(url.includes('/media/')){{mediaCalls.push(new URL(url).pathname);return resp({{url:'https://cf-media.sndcdn.com/audio.mp3'}});}}
  return resp(url.includes('old=1')?old:newer);
}};
global.XMLHttpRequest=function(){{}}; XMLHttpRequest.prototype.open=function(){{}}; XMLHttpRequest.prototype.setRequestHeader=function(){{}}; XMLHttpRequest.prototype.send=function(){{}}; XMLHttpRequest.prototype.addEventListener=function(){{}};
eval({json.dumps(script)});
(async()=>{{
  await window.fetch('https://api-v2.soundcloud.com/mixed-selections?old=1');
  for(let i=0;i<3;i++)await new Promise(setImmediate);
  await window.fetch('https://api-v2.soundcloud.com/mixed-selections?new=1');
  for(let i=0;i<5;i++)await new Promise(setImmediate);
  finishOldExpansion();
  for(let i=0;i<8;i++)await new Promise(setImmediate);
  window.__omarchyPlayApiTrack('soundcloud:tracks:7',9);
  for(let i=0;i<8;i++)await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({{posted,playback,mediaCalls}}));
}})().catch((error)=>{{console.error(error);process.exit(1);}});
"""
        result = json.loads(subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        ).stdout)

        self.assertEqual([track["title"] for track in result["posted"][0]["tracks"]], ["New"])
        self.assertEqual([message["track"]["title"] for message in result["playback"]], ["New"])
        self.assertEqual(result["mediaCalls"], ["/media/new"])
        self.assertNotIn("Old", json.dumps(result))

    def test_cached_selection_waits_for_request_context(self):
        app = load_module()
        script = app.api_request_capture_script()
        harness = f"""
const playback=[]; const calls=[];
global.location={{href:'https://soundcloud.com/feed',hostname:'soundcloud.com',pathname:'/feed'}};global.document={{querySelector(){{return null;}}}};global.window=global;
window.webkit={{messageHandlers:{{omarchyTracks:{{postMessage(){{}}}},omarchyPlayback:{{postMessage(v){{playback.push(JSON.parse(v));}}}}}}}};
const resp=(v)=>{{const b=new TextEncoder().encode(JSON.stringify(v));return {{ok:true,headers:new Headers(),clone(){{return resp(v);}},body:{{getReader(){{let s=false;return {{async read(){{if(s)return {{done:true}};s=true;return {{done:false,value:b}};}},async cancel(){{}}}};}}}}}};}};
window.fetch=async(input)=>{{const u=typeof input==='string'?input:input.url;calls.push(u);if(u.includes('/tracks?'))return resp([{{kind:'track',id:9,title:'Cached',duration:1,permalink_url:'https://soundcloud.com/a/cached',media:{{transcodings:[{{url:'https://api-v2.soundcloud.com/media/cached',format:{{protocol:'progressive',mime_type:'audio/mpeg'}}}}]}}}}]);if(u.includes('/media/'))return resp({{url:'https://cf-media.sndcdn.com/cached.mp3?token=private'}});return resp({{collection:[],next_href:null}});}};
global.XMLHttpRequest=function(){{}};XMLHttpRequest.prototype.open=function(){{}};XMLHttpRequest.prototype.setRequestHeader=function(){{}};XMLHttpRequest.prototype.send=function(){{}};XMLHttpRequest.prototype.addEventListener=function(){{}};
eval({json.dumps(script)});
(async()=>{{const accepted=window.__omarchyPlayApiTrack('soundcloud:tracks:9',7);await new Promise(r=>setTimeout(r,20));await window.fetch('https://api-v2.soundcloud.com/stream?client_id=test');for(let i=0;i<12;i++)await new Promise(setImmediate);process.stdout.write(JSON.stringify({{accepted,calls,playback:playback.map(({{streamUrl,...x}})=>x)}}));}})();
"""
        result = json.loads(subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        ).stdout)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["playback"][0]["requestId"], 7)
        self.assertNotIn("token=private", json.dumps(result))

    def test_backend_resolution_timeout_invalidates_the_waiting_webkit_generation(self):
        app = load_module()
        script = app.api_request_capture_script()
        source = MODULE_PATH.read_text()

        self.assertIn("const waitDeadline = Date.now() + 30000", script)
        timeout_method = source[source.index("def _resolution_timed_out"):source.index(
            "def _respond", source.index("def _resolution_timed_out")
        )]
        self.assertIn("self.selection_generation += 1", timeout_method)

    def test_gst_selection_uses_a_fresh_pipeline_and_ignores_retired_bus_messages(self):
        app = load_module()
        class Bus:
            def __init__(self): self.handler = None; self.disconnected = False
            def add_signal_watch(self): pass
            def connect(self, _name, callback, pipeline, generation):
                self.handler = (callback, pipeline, generation); return 1
            def disconnect(self, _handler): self.disconnected = True
            def remove_signal_watch(self): pass
        class Pipeline:
            def __init__(self, name): self.name=name; self.bus=Bus(); self.states=[]
            def get_bus(self): return self.bus
            def set_property(self, *_args): pass
            def set_state(self, state): self.states.append(state); return Gst.StateChangeReturn.SUCCESS
        class Factory:
            made=[]
            @classmethod
            def make(cls, _kind, name): p=Pipeline(name); cls.made.append(p); return p
        class Gst:
            ElementFactory=Factory
            class State: NULL='null'; PLAYING='playing'; PAUSED='paused'
            class StateChangeReturn: FAILURE='failure'; SUCCESS='success'
            class MessageType: ERROR='error'; EOS='eos'; STATE_CHANGED='state'
            @staticmethod
            def init(_value): pass
        player = app.GstPlayback(Gst, lambda: None)
        self.assertTrue(player.hls_slots.acquire(blocking=False))
        self.assertTrue(player.hls_slots.acquire(blocking=False))
        self.assertFalse(player.hls_slots.acquire(blocking=False))
        player.hls_slots.release()
        player.hls_slots.release()
        player.begin({"title":"A"}); self.assertTrue(player.play("https://x", {"title":"A"}))
        first = player.current
        player.begin({"title":"B"}); self.assertTrue(first.bus.disconnected)
        self.assertTrue(player.play("https://y", {"title":"B"}))
        second = player.current
        self.assertIsNot(first, second)
        message = type("Message", (), {"type": Gst.MessageType.ERROR, "src": first})()
        player._message(None, message, first, player.generation - 1)
        self.assertIs(player.current, second)
        self.assertNotEqual(player.state, "error")

    def test_failed_gstreamer_pipelines_are_disconnected_and_set_to_null(self):
        app = load_module()

        class Bus:
            def __init__(self): self.disconnected = False; self.watch_removed = False
            def add_signal_watch(self): pass
            def connect(self, *_args): return 1
            def disconnect(self, _handler): self.disconnected = True
            def remove_signal_watch(self): self.watch_removed = True

        class Pipeline:
            def __init__(self): self.bus = Bus(); self.states = []; self.source = object()
            def get_bus(self): return self.bus
            def get_by_name(self, _name): return self.source
            def set_property(self, *_args): pass
            def set_state(self, state):
                self.states.append(state)
                return Gst.StateChangeReturn.FAILURE if state == Gst.State.PLAYING else Gst.StateChangeReturn.SUCCESS

        class Factory:
            made = []
            @classmethod
            def make(cls, *_args):
                pipeline = Pipeline(); cls.made.append(pipeline); return pipeline

        class Gst:
            ElementFactory = Factory
            class State: NULL = "null"; PLAYING = "playing"; PAUSED = "paused"
            class StateChangeReturn: FAILURE = "failure"; SUCCESS = "success"
            @staticmethod
            def init(_value): pass
            @staticmethod
            def parse_launch(_description):
                pipeline = Pipeline(); Factory.made.append(pipeline); return pipeline

        for protocol, mime_type in (("", ""), ("hls", "audio/mpeg")):
            with self.subTest(protocol=protocol or "progressive"):
                player = app.GstPlayback(Gst, lambda: None)
                self.assertFalse(player.play(
                    "https://example.invalid/audio", {"title": "Track"}, protocol, mime_type
                ))
                failed = Factory.made[-1]
                self.assertIsNone(player.current)
                self.assertTrue(failed.bus.disconnected)
                self.assertTrue(failed.bus.watch_removed)
                self.assertEqual(failed.states, [Gst.State.PLAYING, Gst.State.NULL])
                player.hls_executor.shutdown(wait=False, cancel_futures=True)

    def test_hls_fetch_has_deadline_cancellation_and_proxy_isolation(self):
        app = load_module()
        self.assertEqual(app.HLS_MAX_WORKERS, 2)
        self.assertIn("ProxyHandler({})", MODULE_PATH.read_text())
        with self.assertRaises(TimeoutError):
            app.fetch_private_media(
                "https://cf-media.sndcdn.com/a", 10, deadline=time.monotonic() - 1
            )
        with self.assertRaises(app.concurrent.futures.CancelledError):
            app.fetch_private_media(
                "https://cf-media.sndcdn.com/a", 10, cancelled=lambda: True
            )

    def test_hls_fetch_enforces_deadline_during_slow_drip_reads(self):
        app = load_module()

        class SlowDripResponse:
            def __init__(self):
                self.headers = {}
                self.read_sizes = []
                self.read1_sizes = []

            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def geturl(self): return "https://cf-media.sndcdn.com/slow"

            def read(self, size):
                self.read_sizes.append(size)
                time.sleep(0.2)
                return b"x"

            def read1(self, size):
                self.read1_sizes.append(size)
                time.sleep(0.01)
                return b"x"

        response = SlowDripResponse()
        deadline = time.monotonic() + 0.03
        started = time.monotonic()
        with mock.patch.object(app._media_opener, "open", return_value=response):
            with self.assertRaises(TimeoutError):
                app.fetch_private_media(
                    "https://cf-media.sndcdn.com/slow",
                    1024 * 1024,
                    deadline=deadline,
                )

        self.assertLess(time.monotonic() - started, 0.12)
        self.assertEqual(response.read_sizes, [])
        self.assertGreaterEqual(len(response.read1_sizes), 2)
        self.assertTrue(all(size <= 64 * 1024 for size in response.read1_sizes))

    def test_api_request_capture_pushes_the_frontends_completed_response(self):
        app = load_module()

        script = app.api_request_capture_script()

        self.assertIn("boundedText(response.clone(), 2 * 1024 * 1024)", script)
        self.assertIn("addEventListener('load'", script)
        self.assertIn("messageHandlers.omarchyTracks.postMessage", script)
        self.assertIn("await homeTracks(payload, context, stagedPlayback)", script)
        self.assertIn("reset", script)

    def test_home_mixed_selections_expand_playlist_track_ids_into_real_tracks(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const posted = [];
const calls = [];
global.location = {{ href: 'https://soundcloud.com/discover', hostname: 'soundcloud.com', pathname: '/discover' }};
global.window = global;
window.webkit = {{ messageHandlers: {{
  omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }},
  omarchyPlayback: {{ postMessage() {{}} }}
}} }};
window.fetch = async function(input, init) {{
  const url = typeof input === 'string' ? input : input.url;
  calls.push(url);
  const payload = url.includes('/tracks?') ? [
    {{ kind: 'track', id: 11, urn: 'soundcloud:tracks:11', permalink_url: 'https://soundcloud.com/a/one', title: 'One', duration: 1000, user: {{ username: 'A' }}, media: {{ transcodings: [{{ url: 'https://api-v2.soundcloud.com/media/one', format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }} }}] }} }},
    {{ kind: 'track', id: 22, urn: 'soundcloud:tracks:22', permalink_url: 'https://soundcloud.com/b/two', title: 'Two', duration: 2000, user: {{ username: 'B' }}, media: {{ transcodings: [{{ url: 'https://api-v2.soundcloud.com/media/two', format: {{ protocol: 'hls', mime_type: 'audio/mp4' }} }}] }} }}
  ] : {{ collection: [
    {{ kind: 'track', id: 33, urn: 'soundcloud:tracks:33', permalink_url: 'https://soundcloud.com/c/embedded', title: 'Embedded', duration: 3000, user: {{ username: 'C' }}, media: {{ transcodings: [{{ url: 'https://api-v2.soundcloud.com/media/embedded', format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }} }}] }} }},
    {{ kind: 'selection', items: {{ collection: [{{ kind: 'system-playlist', tracks: [{{ id: 11 }}, {{ id: 22 }}] }}] }} }}
  ], next_href: null }};
  const body = JSON.stringify(payload);
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/mixed-selections?client_id=public-test', {{ headers: {{ authorization: 'OAuth private-test-value' }} }}));
  for (let index = 0; index < 6; index++) await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({{ calls, posted }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(len(result["posted"]), 1)
        self.assertEqual(
            [track["title"] for track in result["posted"][0]["tracks"]],
            ["Embedded", "One", "Two"],
        )
        self.assertEqual(
            [track["playbackId"] for track in result["posted"][0]["tracks"]],
            ["soundcloud:tracks:33", "soundcloud:tracks:11", "soundcloud:tracks:22"],
        )
        self.assertIn("/tracks?", result["calls"][1])
        self.assertNotIn("private-test-value", completed.stdout)

    def test_home_rows_with_only_encrypted_streams_carry_the_encrypted_flag(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const posted = [];
global.location = {{ href: 'https://soundcloud.com/discover', hostname: 'soundcloud.com', pathname: '/discover' }};
global.window = global;
window.webkit = {{ messageHandlers: {{
  omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }},
  omarchyPlayback: {{ postMessage() {{}} }}
}} }};
const stream = (protocol, name) => ({{ url: 'https://api-v2.soundcloud.com/media/' + name, format: {{ protocol, mime_type: 'audio/mpeg' }} }});
const track = (id, name, transcodings) => ({{ kind: 'track', id, urn: 'soundcloud:tracks:' + id,
  permalink_url: 'https://soundcloud.com/a/' + name, title: name, duration: 1000, user: {{ username: 'A' }},
  media: transcodings === undefined ? undefined : {{ transcodings }} }});
window.fetch = async function() {{
  return new Response(JSON.stringify({{ collection: [
    track(1, 'locked', [stream('cbc-encrypted-hls', 'a'), stream('ctr-encrypted-hls', 'b')]),
    track(2, 'plain', [stream('hls', 'c'), stream('ctr-encrypted-hls', 'd')]),
    track(3, 'progressive', [stream('progressive', 'e')]),
    track(4, 'empty', []),
    track(5, 'nomedia', undefined),
    track(6, 'junk', [null, 'x', {{ url: 'https://api-v2.soundcloud.com/media/f' }}])
  ], next_href: null }}), {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/mixed-selections?client_id=public-test', {{ headers: {{ authorization: 'OAuth private-test-value' }} }}));
  for (let index = 0; index < 6; index++) await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({{ posted }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
        posted = json.loads(completed.stdout)["posted"]

        self.assertEqual(len(posted), 1)
        by_title = {track["title"]: track for track in posted[0]["tracks"]}
        self.assertIs(by_title["locked"].get("encrypted"), True)
        for title in ("plain", "progressive", "empty", "nomedia"):
            self.assertNotIn("encrypted", by_title[title], title)
        # junk entries offer no playable stream either, as track_from_entry decides
        self.assertIs(by_title["junk"].get("encrypted"), True)
        # the flag passes the same validation the likes rows go through, and only when true
        checked = {row["title"]: row for row in app.validate_tracks_payload(posted[0])["tracks"]}
        self.assertIs(checked["locked"]["encrypted"], True)
        self.assertNotIn("encrypted", checked["plain"])
        self.assertLessEqual(len(completed.stdout), 20_000)
        self.assertNotIn("private-test-value", completed.stdout)

    def test_playback_resolves_unknown_track_id_directly_without_dom_or_list_map(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const playback = [];
const calls = [];
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.document = {{ querySelector() {{ return null; }} }};
global.window = global;
window.webkit = {{ messageHandlers: {{
  omarchyTracks: {{ postMessage() {{}} }},
  omarchyPlayback: {{ postMessage(value) {{ playback.push(JSON.parse(value)); }} }}
}} }};
window.fetch = async function(input) {{
  const url = typeof input === 'string' ? input : input.url;
  calls.push(url);
  let payload;
  if (url.includes('/tracks?')) payload = [{{
    kind: 'track', id: 777, urn: 'soundcloud:tracks:777',
    permalink_url: 'https://soundcloud.com/a/direct', title: 'Direct', duration: 3000,
    user: {{ username: 'A' }}, media: {{ transcodings: [{{
      url: 'https://api-v2.soundcloud.com/media/direct',
      format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }}
    }}] }}
  }}];
  else if (url.includes('/media/direct')) payload = {{ url: 'https://cf-media.sndcdn.com/direct.mp3?token=private' }};
  else payload = {{ collection: [], next_href: null }};
  const body = JSON.stringify(payload);
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?client_id=public-test', {{ headers: {{ authorization: 'OAuth private-test-value' }} }}));
  await new Promise(setImmediate);
  const accepted = window.__omarchyPlayApiTrack('soundcloud:tracks:777', 42);
  for (let index = 0; index < 8; index++) await new Promise(setImmediate);
  const safePlayback = playback.map(({{ streamUrl, ...message }}) => message);
  process.stdout.write(JSON.stringify({{ accepted, calls, playback: safePlayback }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        )
        result = json.loads(completed.stdout)

        self.assertTrue(result["accepted"])
        self.assertIn("/tracks?", result["calls"][1])
        self.assertEqual(result["playback"][0]["track"]["playbackId"], "soundcloud:tracks:777")
        self.assertEqual(result["playback"][0]["requestId"], 42)
        self.assertTrue(result["playback"][0]["ok"])
        self.assertNotIn("private-test-value", completed.stdout)

    def test_playback_carries_the_real_waveform_from_soundcloud_only(self):
        app = load_module()
        capture_script = app.api_request_capture_script()

        def run(waveform_url, samples="[...Array(76).fill(140), ...Array(76).fill(35)]"):
            harness = f"""
const playback = [];
const calls = [];
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.document = {{ querySelector() {{ return null; }} }};
global.window = global;
window.webkit = {{ messageHandlers: {{
  omarchyTracks: {{ postMessage() {{}} }},
  omarchyPlayback: {{ postMessage(value) {{ playback.push(JSON.parse(value)); }} }}
}} }};
window.fetch = async function(input) {{
  const url = typeof input === 'string' ? input : input.url;
  calls.push(url);
  let payload;
  if (url.includes('/tracks?')) payload = [{{
    kind: 'track', id: 777, urn: 'soundcloud:tracks:777',
    permalink_url: 'https://soundcloud.com/a/direct', title: 'Direct', duration: 3000,
    waveform_url: {json.dumps(waveform_url)},
    user: {{ username: 'A' }}, media: {{ transcodings: [{{
      url: 'https://api-v2.soundcloud.com/media/direct',
      format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }}
    }}] }}
  }}];
  else if (url.includes('/media/direct')) payload = {{ url: 'https://cf-media.sndcdn.com/direct.mp3' }};
  else if (url.includes('_m.json')) payload = {{
    width: 152, height: 140, samples: {samples}
  }};
  else payload = {{ collection: [], next_href: null }};
  return new Response(JSON.stringify(payload), {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?client_id=public-test'));
  await new Promise(setImmediate);
  window.__omarchyPlayApiTrack('soundcloud:tracks:777', 42);
  for (let index = 0; index < 12; index++) await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({{ calls, playback: playback.map(({{ streamUrl, ...x }}) => x) }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
            completed = subprocess.run(
                ["node", "-e", harness], check=True, capture_output=True, text=True
            )
            return json.loads(completed.stdout)

        # SoundCloud's track JSON points at a PNG; the same path serves JSON samples.
        real = run("https://wave.sndcdn.com/AbC123_m.png")
        waveform = real["playback"][0]["waveform"]
        self.assertEqual(len(waveform), 76)
        self.assertEqual(waveform[0], 100)
        self.assertEqual(waveform[-1], 20)
        self.assertIn("https://wave.sndcdn.com/AbC123_m.json", real["calls"])

        # A dense mix (levels 72-100%) must still span the full height.
        dense = run(
            "https://wave.sndcdn.com/AbC123_m.json",
            "Array.from({ length: 76 }, (_, index) => 101 + (index % 2) * 39)",
        )["playback"][0]["waveform"]
        self.assertEqual((min(dense), max(dense)), (20, 100))

        # A near-constant signal is not amplified into noise.
        steady = run(
            "https://wave.sndcdn.com/AbC123_m.json",
            "Array.from({ length: 76 }, (_, index) => 138 + (index % 2) * 2)",
        )["playback"][0]["waveform"]
        self.assertGreater(min(steady), 95)

        foreign = run("https://attacker.example/AbC123_m.json")
        self.assertTrue(foreign["playback"][0]["ok"])
        self.assertEqual(foreign["playback"][0]["waveform"], [])
        self.assertFalse(any("attacker" in call for call in foreign["calls"]))

    def test_waveform_reaches_status_only_as_bounded_levels(self):
        app = load_module()
        self.assertEqual(app.validate_waveform([0, 50, 100]), [0, 50, 100])
        for invalid in ([101], [-1], [True], [1.5], ["9"], [0] * 201, "0,1", None):
            self.assertEqual(app.validate_waveform(invalid), [])

        player = object.__new__(app.GstPlayback)
        player.active = True
        player.playing = True
        player.state = "playing"
        player.error = ""
        player.metadata = {"title": "T", "waveform": [10, 90]}
        player._query_time = lambda _method: 1.0
        validated = app.validate_status_payload({**player.status(), "loggedIn": True})
        self.assertEqual(validated["waveform"], [10, 90])

    def test_late_resolution_cannot_replace_a_newer_track_selection(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const playback = [];
let finishA;
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.document = {{ querySelector() {{ return null; }} }};
global.window = global;
window.webkit = {{ messageHandlers: {{
  omarchyTracks: {{ postMessage() {{}} }},
  omarchyPlayback: {{ postMessage(value) {{ playback.push(JSON.parse(value)); }} }}
}} }};
const response = (payload) => {{
  const body = JSON.stringify(payload);
  return new Response(body, {{ status: 200 }});
}};
const track = (id, name) => ({{
  kind: 'track', id, urn: 'soundcloud:tracks:' + id,
  permalink_url: 'https://soundcloud.com/a/' + name.toLowerCase(), title: name,
  duration: 1000, user: {{ username: 'A' }}, media: {{ transcodings: [{{
    url: 'https://api-v2.soundcloud.com/media/' + name.toLowerCase(),
    format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }}
  }}] }}
}});
window.fetch = async function(input) {{
  const url = typeof input === 'string' ? input : input.url;
  if (url.includes('/media/a')) return await new Promise((resolve) => {{
    finishA = () => resolve(response({{ url: 'https://cf-media.sndcdn.com/a.mp3?token=private' }}));
  }});
  if (url.includes('/media/b')) return response({{ url: 'https://cf-media.sndcdn.com/b.mp3?token=private' }});
  return response({{ collection: [track(1, 'A'), track(2, 'B')], next_href: null }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?client_id=public-test'));
  await new Promise(setImmediate);
  window.__omarchyPlayApiTrack('soundcloud:tracks:1', 1);
  await new Promise(setImmediate);
  window.__omarchyPlayApiTrack('soundcloud:tracks:2', 2);
  for (let index = 0; index < 4; index++) await new Promise(setImmediate);
  finishA();
  for (let index = 0; index < 4; index++) await new Promise(setImmediate);
  process.stdout.write(JSON.stringify(playback.map(({{ streamUrl, ...message }}) => message)));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        )
        result = json.loads(completed.stdout)

        self.assertEqual([message["requestId"] for message in result], [2])
        self.assertEqual(result[0]["track"]["title"], "B")
        self.assertNotIn("token=private", completed.stdout)

    def test_injected_track_scripts_do_not_extract_or_replay_frontend_credentials(self):
        source = MODULE_PATH.read_text()

        self.assertNotIn("def tracks_script", source)
        self.assertNotIn("__omarchyMetadataResources", source)
        self.assertNotIn("window.latestRequestContext", source)
        self.assertNotIn("api-v2.soundcloud.com/resolve", source)

    def test_track_metadata_counts_and_duration_are_bounded(self):
        app = load_module()
        payload = {
            "tracks": [{
                "playbackId": "soundcloud:tracks:123",
                "title": "Track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/track",
                "artUrl": "",
                "playCount": 123456,
                "durationMs": 245000,
            }]
        }

        track = app.validate_tracks_payload(payload)["tracks"][0]

        self.assertEqual("soundcloud:tracks:123", track["playbackId"])
        self.assertEqual(123456, track["playCount"])
        self.assertEqual(245000, track["durationMs"])
        payload["tracks"][0]["playCount"] = "123456"
        payload["tracks"][0]["durationMs"] = -1
        track = app.validate_tracks_payload(payload)["tracks"][0]
        self.assertEqual(0, track["playCount"])
        self.assertEqual(0, track["durationMs"])

    def test_track_artwork_urls_are_validated_before_tokenization(self):
        app = load_module()
        payload = {
            "tracks": [{
                "title": "Track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/track",
                "artUrl": "https://i1.sndcdn.com/artworks-test-t120x120.jpg",
            }]
        }
        validated = app.validate_tracks_payload(payload)

        self.assertEqual(validated["tracks"][0]["artUrl"], payload["tracks"][0]["artUrl"])
        payload["tracks"][0]["artUrl"] = "http://127.0.0.1/private.png"
        self.assertEqual(app.validate_tracks_payload(payload)["tracks"][0]["artUrl"], "")

    def test_track_artwork_is_rewritten_to_a_small_cdn_variant(self):
        app = load_module()

        self.assertEqual(
            app.normalize_list_artwork_url(
                "https://i1.sndcdn.com/artworks-example-t500x500.jpg"
            ),
            "https://i1.sndcdn.com/artworks-example-t120x120.jpg",
        )
        self.assertEqual(
            app.normalize_list_artwork_url("https://evil.example/art-large.jpg"),
            "",
        )

    def test_chunked_artwork_body_is_decoded_with_a_strict_size_limit(self):
        app = load_module()

        self.assertEqual(
            b"Wikipedia",
            app._decode_chunked_body(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n", 16),
        )
        self.assertIsNone(app._decode_chunked_body(b"11\r\ntoo-large-for-cap\r\n0\r\n\r\n", 8))
        self.assertIsNone(app._decode_chunked_body(b"4\r\nWiki", 16))

    def test_play_track_script_uses_only_stable_playback_id(self):
        app = load_module()

        script = app.play_track_script("soundcloud:tracks:123", 17)

        self.assertIn("soundcloud:tracks:123", script)
        self.assertIn("__omarchyPlayApiTrack(playbackId, 17)", script)
        self.assertIn("__omarchyPlayApiTrack", script)
        self.assertNotIn("document.querySelector", script)
        self.assertNotIn("button.click()", script)

    def test_play_track_script_rejects_invalid_playback_id(self):
        app = load_module()

        self.assertIsNone(app.play_track_script("https://soundcloud.com/artist/track", 1))
        self.assertIsNone(app.play_track_script("soundcloud:tracks:not-a-number", 1))
        self.assertIsNone(app.play_track_script("soundcloud:tracks:123", -1))

    def test_remote_payloads_are_schema_bounded_before_ipc(self):
        app = load_module()
        payload = {
            "title": "t" * 600,
            "artist": "artist",
            "playing": True,
            "loggedIn": True,
            "duration": 123,
            "position": 12,
            "url": "https://soundcloud.com/artist/track",
            "artUrl": "https://evil.example/image.png",
        }

        self.assertIsNone(app.validate_status_payload(payload))
        self.assertIsNone(app.validate_tracks_payload({"tracks": [{"title": "x" * 600}]}))

    def test_artwork_urls_are_restricted_to_soundcloud_cdn(self):
        app = load_module()

        self.assertTrue(app.is_allowed_artwork_uri("https://i1.sndcdn.com/artworks-test-t120x120.png"))
        self.assertFalse(app.is_allowed_artwork_uri("http://i1.sndcdn.com/image.png"))
        self.assertFalse(app.is_allowed_artwork_uri("https://127.0.0.1/image.png"))
        self.assertFalse(app.is_allowed_artwork_uri("file:///etc/passwd"))

    def test_artwork_cache_caps_pending_fetches(self):
        app = load_module()
        release = threading.Event()
        cache = app.ArtworkCache(maximum_entries=2, maximum_pending=3)
        try:
            with mock.patch.object(
                app,
                "fetch_artwork_data_url",
                side_effect=lambda _uri: release.wait(1) or "",
            ):
                for index in range(20):
                    cache.get(f"https://i1.sndcdn.com/artworks-{index}.jpg", lambda: None)
                self.assertLessEqual(len(cache.inflight), 3)
        finally:
            release.set()
            cache.close()


class FakeSoundCloudApi:
    def __init__(self, user_id=42, liked_ids=(7,), like_items=(), history_items=()):
        self.user_id = user_id
        self.liked_ids = list(liked_ids)
        self.like_items = list(like_items)
        self.history_items = list(history_items)
        self.calls = []
        self.fail_writes = False

    def __call__(self, method, path, token, maximum=0):
        self.calls.append((method, path, token))
        if method == "GET" and path == "/me":
            return {"id": self.user_id}
        if method == "GET" and path.startswith("/me/track_likes/ids"):
            offset = int(path.partition("offset=")[2] or 0)
            page = self.liked_ids[offset:offset + 2]
            next_href = (
                f"https://api-v2.soundcloud.com/me/track_likes/ids?limit=2&offset={offset + 2}"
                if offset + 2 < len(self.liked_ids) else None
            )
            return {"collection": page, "next_href": next_href}
        if method == "GET" and path.startswith(f"/users/{self.user_id}/track_likes?"):
            offset = int(path.partition("offset=")[2] or 0)
            page = self.like_items[offset:offset + 2]
            next_href = (
                f"https://api-v2.soundcloud.com/users/{self.user_id}/track_likes?limit=2&offset={offset + 2}"
                if offset + 2 < len(self.like_items) else None
            )
            return {"collection": page, "next_href": next_href}
        if method == "GET" and path.startswith("/me/play-history/tracks?"):
            offset = int(path.partition("offset=")[2] or 0)
            page = self.history_items[offset:offset + 2]
            next_href = (
                f"https://api-v2.soundcloud.com/me/play-history/tracks?limit=2&offset={offset + 2}"
                if offset + 2 < len(self.history_items) else None
            )
            return {"collection": page, "next_href": next_href}
        raise AssertionError(f"unexpected API read {method} {path}")

    def write(self, user_id, track_id, liked, token):
        self.calls.append(("PUT" if liked else "DELETE", f"/users/{user_id}/track_likes/{track_id}", token))
        if self.fail_writes:
            raise OSError("like request failed with HTTP 403")


class StatusSplitTest(unittest.TestCase):
    PAGE = {
        "playerPresent": True, "playing": False, "title": "Page title", "artist": "",
        "artUrl": "", "loggedIn": True, "url": "https://soundcloud.com/feed",
        "duration": 0, "position": 0,
    }

    def gst_status(self, **overrides):
        return {
            "playerPresent": True, "playbackState": "playing", "playing": True,
            "playbackId": "soundcloud:tracks:5", "title": "Song", "artist": "Band",
            "artUrl": "", "duration": 200.0, "position": 12.5, "waveform": [10, 20],
            **overrides,
        }

    def cache(self, app, **payload):
        cache = app.PageStatusCache(lambda script, callback: None, "script")
        cache.update({**self.PAGE, **payload})
        return cache

    def test_merged_status_keeps_the_keys_the_widget_receives(self):
        app = load_module()
        gst = self.gst_status()

        merged = app.validate_status_payload(app.merge_status(self.cache(app).values, gst))
        before = app.validate_status_payload({**self.PAGE, **gst})

        self.assertEqual(set(merged), set(before))
        self.assertEqual(merged["title"], "Song")
        self.assertEqual(merged["position"], 12.5)
        self.assertEqual(merged["waveform"], [10, 20])
        self.assertTrue(merged["loggedIn"])
        self.assertEqual(merged["url"], "https://soundcloud.com/feed")

    def test_idle_status_comes_from_the_cache_without_page_metadata(self):
        app = load_module()
        idle = self.gst_status(
            playerPresent=False, playbackState="idle", playing=False, playbackId="",
            title="", artist="", duration=0.0, position=0.0, waveform=[],
        )

        merged = app.validate_status_payload(app.merge_status(self.cache(app).values, idle))

        self.assertEqual(merged["playbackState"], "idle")
        self.assertEqual(merged["title"], "")
        self.assertTrue(merged["loggedIn"])
        self.assertTrue(merged["playerPresent"])
        self.assertNotIn("error", merged)

    def test_page_error_keeps_the_previous_cache_values(self):
        app = load_module()
        cache = self.cache(app)

        changed = cache.update({"error": "refusing untrusted page"})
        merged = app.validate_status_payload(app.merge_status(cache.values, self.gst_status()))

        self.assertFalse(changed)
        self.assertTrue(merged["loggedIn"])
        self.assertEqual(merged["url"], "https://soundcloud.com/feed")
        self.assertEqual(merged["error"], "refusing untrusted page")
        self.assertEqual(merged["title"], "Song")

    def test_playback_error_wins_over_the_page_error(self):
        app = load_module()
        cache = self.cache(app)
        cache.update({"error": "refusing untrusted page"})

        merged = app.merge_status(cache.values, self.gst_status(error="Stream failed"))

        self.assertEqual(merged["error"], "Stream failed")

    def test_update_reports_only_login_and_url_changes(self):
        app = load_module()
        cache = self.cache(app)

        self.assertFalse(cache.update({**self.PAGE, "position": 30, "title": "Other"}))
        self.assertTrue(cache.update({**self.PAGE, "url": "https://soundcloud.com/discover"}))
        self.assertTrue(cache.update({**self.PAGE, "loggedIn": False}))
        self.assertEqual(cache.values["error"], "")

    def test_slow_page_never_blocks_the_fast_status(self):
        app = load_module()
        asked = []
        cache = app.PageStatusCache(lambda script, callback: asked.append(callback), "script")
        positions = iter([1.0, 1.5, 2.0])

        cache.poll()
        cache.poll()
        statuses = [
            app.merge_status(cache.values, self.gst_status(position=next(positions)))
            for _ in range(3)
        ]

        self.assertEqual(len(asked), 1)
        self.assertEqual([status["position"] for status in statuses], [1.0, 1.5, 2.0])
        self.assertFalse(statuses[0]["loggedIn"])
        asked[0](self.PAGE)
        cache.poll()
        self.assertEqual(len(asked), 2)

    def test_first_publish_carries_login_once_the_cache_is_seeded(self):
        app = load_module()
        seen = []
        cache = app.PageStatusCache(lambda script, callback: callback(self.PAGE), "script")
        self.assertFalse(cache.seeded)

        cache.poll(lambda changed: seen.append(
            (changed, app.merge_status(cache.values, self.gst_status())["loggedIn"])
        ))

        self.assertTrue(cache.seeded)
        self.assertEqual(seen, [(True, True)])

    def test_every_caller_waiting_on_a_pending_poll_is_told(self):
        app = load_module()
        asked = []
        cache = app.PageStatusCache(lambda script, callback: asked.append(callback), "script")
        seen = []

        cache.poll(seen.append)
        cache.poll(seen.append)
        asked[0](self.PAGE)

        self.assertEqual(seen, [True, True])


def like_item(track_id, **overrides):
    track = {
        "id": track_id,
        "title": f"Song {track_id}",
        "duration": 180000,
        "permalink_url": f"https://soundcloud.com/artist/song-{track_id}",
        "artwork_url": "https://i1.sndcdn.com/artworks-abc-large.jpg",
        "playback_count": 12,
        "user": {"username": "Artist"},
    }
    track.update(overrides)
    return {"kind": "like", "created_at": "2026-10-01T00:00:00Z", "track": track}


class LikedTracksTest(unittest.TestCase):
    def test_like_entries_become_playable_track_rows(self):
        app = load_module()

        row = app.track_from_entry(like_item(123))

        self.assertEqual(row, {
            "playbackId": "soundcloud:tracks:123",
            "title": "Song 123",
            "artist": "Artist",
            "url": "https://soundcloud.com/artist/song-123",
            "artUrl": "https://i1.sndcdn.com/artworks-abc-large.jpg",
            "playCount": 12,
            "durationMs": 180000,
        })
        # The row must survive the same validation the Home and Feed lists get.
        self.assertEqual(len(app.validate_tracks_payload({"tracks": [row]})["tracks"]), 1)

    def test_like_rows_fall_back_to_the_uploader_avatar_and_publisher_artist(self):
        app = load_module()
        item = like_item(5, artwork_url=None, user={"avatar_url": "https://i1.sndcdn.com/avatar.jpg"},
                         publisher_metadata={"artist": "Credited"})

        row = app.track_from_entry(item)

        self.assertEqual(row["artUrl"], "https://i1.sndcdn.com/avatar.jpg")
        self.assertEqual(row["artist"], "Credited")

    def test_unusable_like_entries_are_skipped(self):
        app = load_module()

        self.assertIsNone(app.track_from_entry(None))
        self.assertIsNone(app.track_from_entry({"kind": "like", "track": None}))
        self.assertIsNone(app.track_from_entry(like_item(0)))
        self.assertIsNone(app.track_from_entry(like_item(True)))
        self.assertIsNone(app.track_from_entry(like_item(7, title="  ")))
        self.assertIsNone(app.track_from_entry(like_item(7, duration=None)))
        self.assertIsNone(app.track_from_entry(like_item(7, permalink_url="")))

    def test_oversized_like_fields_are_truncated_instead_of_rejecting_the_page(self):
        app = load_module()

        row = app.track_from_entry(like_item(9, title="t" * 2000, user={"username": "a" * 2000}))

        self.assertEqual(len(row["title"]), 512)
        self.assertEqual(len(row["artist"]), 256)
        self.assertEqual(len(app.validate_tracks_payload({"tracks": [row]})["tracks"]), 1)

    def test_a_long_multibyte_title_is_cut_to_the_byte_limit_so_the_page_validates(self):
        app = load_module()

        row = app.track_from_entry(like_item(9, title="\u6f22" * 300, user={"username": "\u5b57" * 300}))

        self.assertLessEqual(len(row["title"].encode()), app.MAX_TITLE_BYTES)
        self.assertLessEqual(len(row["artist"].encode()), app.MAX_ARTIST_BYTES)
        self.assertEqual((len(row["title"]), len(row["artist"])), (170, 85))  # whole characters only
        page = [row, app.track_from_entry(like_item(10))]
        self.assertEqual(len(app.validate_tracks_payload({"tracks": page})["tracks"]), 2)
        # the validator itself stays strict
        too_long = {**page[1], "title": "\u6f22" * 300}
        self.assertIsNone(app.validate_tracks_payload({"tracks": [too_long]}))

    def test_liked_tracks_load_newest_first_page_by_page(self):
        app = load_module()
        api = FakeSoundCloudApi(user_id=42, like_items=[like_item(n) for n in (1, 2, 3)])
        likes = app.TrackLikes(lambda: "token", api.write, request=api)

        first, cursor = likes.tracks_page("likes")
        second, last = likes.tracks_page("likes", cursor)

        self.assertEqual([t["playbackId"] for t in first], ["soundcloud:tracks:1", "soundcloud:tracks:2"])
        self.assertEqual([t["playbackId"] for t in second], ["soundcloud:tracks:3"])
        self.assertIsNone(last)
        self.assertEqual(
            [path for _method, path, _token in api.calls if "track_likes" in path],
            [
                "/users/42/track_likes?limit=24",
                "/users/42/track_likes?limit=2&offset=2",
            ],
        )

    def test_liked_tracks_do_not_load_every_liked_id_first(self):
        app = load_module()
        api = FakeSoundCloudApi(like_items=[like_item(1)])
        likes = app.TrackLikes(lambda: "token", api.write, request=api)

        likes.tracks_page("likes")

        self.assertFalse(any("track_likes/ids" in path for _method, path, _token in api.calls))

    def test_liked_tracks_skip_unusable_entries_but_keep_paging(self):
        app = load_module()
        api = FakeSoundCloudApi(like_items=[{"kind": "like", "track": None}, like_item(2)])
        likes = app.TrackLikes(lambda: "token", api.write, request=api)

        tracks, cursor = likes.tracks_page("likes")

        self.assertEqual([t["playbackId"] for t in tracks], ["soundcloud:tracks:2"])
        self.assertIsNone(cursor)

    def test_liked_tracks_never_follow_a_cursor_off_soundcloud_or_to_another_path(self):
        app = load_module()
        for next_href in (
            "https://evil.example/users/1/track_likes?offset=2",
            "http://api-v2.soundcloud.com/users/1/track_likes?offset=2",
            "https://api-v2.soundcloud.com/users/2/track_likes?offset=2",
            "https://api-v2.soundcloud.com/me/track_likes/ids?offset=2",
            None,
        ):
            def request(method, path, token, maximum=0, next_href=next_href):
                if path == "/me":
                    return {"id": 1}
                return {"collection": [like_item(3)], "next_href": next_href}

            likes = app.TrackLikes(lambda: "token", None, request=request)

            _tracks, cursor = likes.tracks_page("likes")

            self.assertIsNone(cursor, next_href)

    def test_signed_out_liked_tracks_never_call_the_api(self):
        app = load_module()
        api = FakeSoundCloudApi(like_items=[like_item(1)])
        likes = app.TrackLikes(lambda: None, api.write, request=api)

        with self.assertRaises(PermissionError):
            likes.tracks_page("likes")

        self.assertEqual(api.calls, [])

    def test_invalid_likes_response_is_an_error(self):
        app = load_module()

        def request(method, path, token, maximum=0):
            return {"id": 1} if path == "/me" else {"collection": "nope"}

        likes = app.TrackLikes(lambda: "token", None, request=request)

        with self.assertRaises(ValueError):
            likes.tracks_page("likes")

    def test_play_history_entries_load_like_liked_tracks_without_needing_the_user_id(self):
        app = load_module()
        entries = [{"played_at": 1, "track_id": n, "track": like_item(n)["track"]} for n in (1, 2, 3)]
        api = FakeSoundCloudApi(history_items=entries)
        likes = app.TrackLikes(lambda: "token", api.write, request=api)

        first, cursor = likes.tracks_page("history")
        second, last = likes.tracks_page("history", cursor)

        self.assertEqual([t["playbackId"] for t in first], ["soundcloud:tracks:1", "soundcloud:tracks:2"])
        self.assertEqual([t["playbackId"] for t in second], ["soundcloud:tracks:3"])
        self.assertIsNone(last)
        self.assertEqual(
            [path for _method, path, _token in api.calls if "play-history" in path],
            ["/me/play-history/tracks?limit=24", "/me/play-history/tracks?limit=2&offset=2"],
        )

    def test_history_never_follows_a_cursor_to_the_likes_path_or_off_soundcloud(self):
        app = load_module()
        for next_href in (
            "https://api-v2.soundcloud.com/users/1/track_likes?offset=2",
            "https://evil.example/me/play-history/tracks?offset=2",
        ):
            def request(method, path, token, maximum=0, next_href=next_href):
                if path == "/me":
                    return {"id": 1}
                return {"collection": [like_item(3)], "next_href": next_href}

            _tracks, cursor = app.TrackLikes(lambda: "token", None, request=request).tracks_page("history")

            self.assertIsNone(cursor, next_href)

    def test_account_list_commands_accept_only_known_sources(self):
        app = load_module()

        self.assertEqual(app.api_list_source_from_command("tracks:likes", "tracks:"), "likes")
        self.assertEqual(app.api_list_source_from_command("tracks:history", "tracks:"), "history")
        self.assertEqual(app.api_list_source_from_command("load-more:history", "load-more:"), "history")
        self.assertIsNone(app.api_list_source_from_command("tracks:feed", "tracks:"))
        self.assertIsNone(app.api_list_source_from_command("load-more:likes", "tracks:"))
        self.assertIsNone(app.api_list_source_from_command("tracks:../me", "tracks:"))
        self.assertIsNone(app.api_list_source_from_command("likes", "tracks:"))

    def test_only_home_and_feed_pages_give_playback_a_request_context(self):
        app = load_module()

        self.assertTrue(app.is_track_source_page("https://soundcloud.com/discover"))
        self.assertTrue(app.is_track_source_page("https://soundcloud.com/feed?x=1"))
        self.assertFalse(app.is_track_source_page("https://soundcloud.com/you/likes"))
        self.assertFalse(app.is_track_source_page("https://soundcloud.com/discovery"))
        self.assertFalse(app.is_track_source_page("https://evil.example/https://soundcloud.com/feed"))
        self.assertFalse(app.is_track_source_page(""))

    def test_likes_survive_a_restart_through_the_track_cache(self):
        app = load_module()
        row = app.track_from_entry(like_item(4))

        cache = app.sanitize_track_cache({"likes": [row], "history": [row], "home": []})

        self.assertEqual(cache, {"likes": [row], "history": [row]})


def make_resolver(responses, user_id=1):
    """A fake SoundCloud API request function that answers /me and the given paths."""
    calls = []

    def request(method, path, token, maximum=0):
        calls.append((method, path))
        if path == "/me":
            return {"id": user_id}
        result = responses[path]
        if isinstance(result, Exception):
            raise result
        return result

    return request, calls


def track_payload(track_id=7, **overrides):
    return {**like_item(track_id)["track"], "kind": "track", **overrides}


class ParseTrackLinkTest(unittest.TestCase):
    def test_track_links_are_normalised_to_the_bare_track_page(self):
        app = load_module()
        track = ("track", "https://soundcloud.com/artist/song")

        for text in (
            "https://soundcloud.com/artist/song",
            "  https://soundcloud.com/artist/song\n",
            "soundcloud.com/artist/song",
            "http://soundcloud.com/artist/song",
            "https://www.soundcloud.com/artist/song",
            "https://m.soundcloud.com/artist/song/",
            "https://soundcloud.com/artist/song?si=abc123&utm_source=clipboard",
            "https://soundcloud.com/artist/song?in=artist/sets/mix#t=1:30",
        ):
            self.assertEqual(app.parse_track_link(text), track, text)

    def test_private_track_secret_tokens_are_kept_and_nothing_else(self):
        app = load_module()

        self.assertEqual(
            app.parse_track_link("https://soundcloud.com/artist/song/s-AbC123"),
            ("track", "https://soundcloud.com/artist/song/s-AbC123"),
        )
        self.assertEqual(
            app.parse_track_link("https://soundcloud.com/artist/song?secret_token=s-AbC123&si=x"),
            ("track", "https://soundcloud.com/artist/song?secret_token=s-AbC123"),
        )
        self.assertEqual(
            app.parse_track_link("https://soundcloud.com/artist/song?secret_token=../../me"),
            ("track", "https://soundcloud.com/artist/song"),
        )

    def test_short_links_are_recognised_but_not_followed_here(self):
        app = load_module()

        self.assertEqual(
            app.parse_track_link("https://on.soundcloud.com/AbC123"),
            ("short", "https://on.soundcloud.com/AbC123"),
        )
        self.assertEqual(
            app.parse_track_link("on.soundcloud.com/AbC123/"),
            ("short", "https://on.soundcloud.com/AbC123"),
        )
        with self.assertRaisesRegex(app.LinkError, "valid SoundCloud short link"):
            app.parse_track_link("https://on.soundcloud.com/a/b")

    def test_other_soundcloud_pages_say_what_they_are(self):
        app = load_module()
        cases = {
            "https://soundcloud.com": "page, not a track",
            "https://soundcloud.com/discover": "page, not a track",
            "https://soundcloud.com/you/likes": "page, not a track",
            "https://soundcloud.com/artist": "artist page",
            "https://soundcloud.com/artist/likes": "artist page",
            "https://soundcloud.com/artist/tracks": "artist page",
            "https://soundcloud.com/artist/sets/mix": "Playlists",
            "https://soundcloud.com/artist/sets": "Playlists",
            "https://soundcloud.com/artist/song/extra": "isn't a track link",
            "https://soundcloud.com/artist/song/recommended/more": "isn't a track link",
            "https://soundcloud.com/artist/..": "isn't a track link",
        }

        for text, message in cases.items():
            with self.assertRaisesRegex(app.LinkError, message, msg=text):
                app.parse_track_link(text)

    def test_anything_else_is_rejected_before_any_network_call(self):
        app = load_module()
        cases = (
            None,
            "",
            "   ",
            "just some words",
            "line one\nhttps://soundcloud.com/artist/song",
            "https://example.com/artist/song",
            "https://soundcloud.com.evil.example/artist/song",
            "https://evilsoundcloud.com/artist/song",
            "https://soundcloud.com@evil.example/artist/song",
            "https://user:pass@soundcloud.com/artist/song",
            "https://soundcloud.com:8443/artist/song",
            "ftp://soundcloud.com/artist/song",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://soundcloud.com/artist/song" + "a" * 3000,
        )

        for text in cases:
            with self.assertRaises(app.LinkError, msg=repr(text)[:60]):
                app.parse_track_link(text)


class FollowShortLinkTest(unittest.TestCase):
    class Opener:
        def __init__(self, error=None):
            self.error = error
            self.requests = []

        def open(self, request, timeout=0):
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            return contextlib.nullcontext()

    def redirect(self, location, code=302):
        from urllib.error import HTTPError
        error = HTTPError("https://on.soundcloud.com/AbC123", code, "Found", {"Location": location}, None)
        self.addCleanup(error.close)
        return error

    def test_a_redirect_is_followed_without_sending_any_credentials(self):
        app = load_module()
        opener = self.Opener(self.redirect("https://soundcloud.com/artist/song?si=x"))

        result = app.follow_short_link("https://on.soundcloud.com/AbC123", opener)

        self.assertEqual(result, "https://soundcloud.com/artist/song?si=x")
        headers = {name.lower() for name in opener.requests[0].headers}
        self.assertNotIn("authorization", headers)

    def test_relative_redirects_are_resolved_against_the_short_link(self):
        app = load_module()
        opener = self.Opener(self.redirect("/artist/song", code=301))

        self.assertEqual(
            app.follow_short_link("https://on.soundcloud.com/AbC123", opener),
            "https://on.soundcloud.com/artist/song",
        )

    def test_a_short_link_that_does_not_redirect_is_an_error(self):
        app = load_module()

        with self.assertRaisesRegex(app.LinkError, "short link"):
            app.follow_short_link("https://on.soundcloud.com/AbC123", self.Opener())
        from urllib.error import HTTPError
        gone = HTTPError("https://on.soundcloud.com/AbC123", 404, "Not Found", {}, None)
        self.addCleanup(gone.close)
        with self.assertRaisesRegex(app.LinkError, "short link"):
            app.follow_short_link("https://on.soundcloud.com/AbC123", self.Opener(gone))

    def test_network_failures_are_not_hidden_as_link_errors(self):
        app = load_module()

        with self.assertRaises(OSError):
            app.follow_short_link("https://on.soundcloud.com/AbC123", self.Opener(OSError("offline")))


class ResolveLinkTest(unittest.TestCase):
    RESOLVE_SONG = "/resolve?url=https%3A%2F%2Fsoundcloud.com%2Fartist%2Fsong"

    def likes(self, responses):
        request, calls = make_resolver(responses)
        return load_module(), request, calls

    def test_a_track_link_resolves_to_a_playable_row(self):
        app = load_module()
        request, calls = make_resolver({self.RESOLVE_SONG: track_payload(7)})

        row = app.TrackLikes(lambda: "token", None, request=request).resolve_link(
            "https://soundcloud.com/artist/song?si=tracking"
        )

        self.assertEqual(row["playbackId"], "soundcloud:tracks:7")
        self.assertEqual(row["title"], "Song 7")
        self.assertIn(("GET", self.RESOLVE_SONG), calls)

    def test_short_links_are_followed_before_resolving(self):
        app = load_module()
        request, calls = make_resolver({self.RESOLVE_SONG: track_payload(7)})
        opener = FollowShortLinkTest.Opener(
            FollowShortLinkTest().redirect("https://soundcloud.com/artist/song?utm=x")
        )

        row = app.TrackLikes(lambda: "token", None, request=request).resolve_link(
            "https://on.soundcloud.com/AbC123", opener
        )

        self.assertEqual(row["playbackId"], "soundcloud:tracks:7")
        self.assertIn(("GET", self.RESOLVE_SONG), calls)

    def test_a_short_link_to_something_else_is_still_rejected(self):
        app = load_module()
        request, calls = make_resolver({})
        opener = FollowShortLinkTest.Opener(FollowShortLinkTest().redirect("https://evil.example/artist/song"))

        with self.assertRaisesRegex(app.LinkError, "doesn't lead to SoundCloud"):
            app.TrackLikes(lambda: "token", None, request=request).resolve_link(
                "https://on.soundcloud.com/AbC123", opener
            )
        self.assertEqual(calls, [])

    def test_a_short_link_to_a_soundcloud_page_that_is_not_a_track_says_so(self):
        app = load_module()
        request, calls = make_resolver({})
        opener = FollowShortLinkTest.Opener(FollowShortLinkTest().redirect("https://soundcloud.com/some-artist"))

        with self.assertRaisesRegex(app.LinkError, "artist page"):
            app.TrackLikes(lambda: "token", None, request=request).resolve_link(
                "https://on.soundcloud.com/AbC123", opener
            )
        self.assertEqual(calls, [])

    def test_playlists_artists_and_unknown_kinds_are_explained(self):
        app = load_module()
        cases = {
            "playlist": "Playlists",
            "system-playlist": "Playlists",
            "user": "artist page",
            "something-new": "isn't a track link",
        }

        for kind, message in cases.items():
            request, _calls = make_resolver({self.RESOLVE_SONG: {"kind": kind, "id": 1}})
            with self.assertRaisesRegex(app.LinkError, message, msg=kind):
                app.TrackLikes(lambda: "token", None, request=request).resolve_link(
                    "https://soundcloud.com/artist/song"
                )

    def test_blocked_and_unplayable_tracks_are_explained(self):
        app = load_module()
        cases = {
            "doesn't allow playing that track": track_payload(7, policy="BLOCK"),
            "can't play that track": track_payload(7, duration=None),
        }

        for message, payload in cases.items():
            request, _calls = make_resolver({self.RESOLVE_SONG: payload})
            with self.assertRaisesRegex(app.LinkError, message):
                app.TrackLikes(lambda: "token", None, request=request).resolve_link(
                    "https://soundcloud.com/artist/song"
                )

    def test_http_errors_become_readable_messages(self):
        from urllib.error import HTTPError
        app = load_module()
        cases = {404: "not found", 401: "refused", 403: "refused", 429: "busy", 500: "couldn't open"}

        for code, message in cases.items():
            error = HTTPError("https://api-v2.soundcloud.com/resolve", code, "x", {}, None)
            self.addCleanup(error.close)
            request, _calls = make_resolver({self.RESOLVE_SONG: error})
            with self.assertRaisesRegex(app.LinkError, message, msg=str(code)):
                app.TrackLikes(lambda: "token", None, request=request).resolve_link(
                    "https://soundcloud.com/artist/song"
                )

    def test_network_failures_are_not_reported_as_bad_links(self):
        app = load_module()
        request, _calls = make_resolver({self.RESOLVE_SONG: OSError("offline")})

        with self.assertRaises(OSError) as raised:
            app.TrackLikes(lambda: "token", None, request=request).resolve_link(
                "https://soundcloud.com/artist/song"
            )
        self.assertNotIsInstance(raised.exception, app.LinkError)

    def test_bad_links_and_signed_out_users_never_reach_the_api(self):
        app = load_module()
        request, calls = make_resolver({})

        with self.assertRaises(app.LinkError):
            app.TrackLikes(lambda: "token", None, request=request).resolve_link("https://example.com/a/b")
        with self.assertRaises(PermissionError):
            app.TrackLikes(lambda: None, None, request=request).resolve_link("https://soundcloud.com/artist/song")

        self.assertEqual(calls, [])


class PlayUrlCommandTest(unittest.TestCase):
    def run_cli(self, argv, *, backend_running=True, responses=None, playback=None):
        app = load_module()
        sent, notices, popups = [], [], []

        def request_backend(command, timeout=4.0):
            if not backend_running:
                return None
            if command == "status":
                return {"ok": True, "title": "Song 7", "artist": "Artist"}
            sent.append(command)
            return responses or {"ok": True, "running": True}

        app.launch_backend = lambda show: {"running": backend_running}
        app.wait_for_backend = lambda timeout=20.0: backend_running
        app.request_backend = request_backend
        app.send_desktop_notification = notices.append
        app.open_popup = lambda: popups.append(True)
        app.wait_for_playback = lambda timeout=12.0: playback or {
            "playing": True, "playbackState": "playing", "title": "Song 7", "artist": "Artist"
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = app.main(argv)
        return code, json.loads(output.getvalue()), sent, notices, popups

    def test_a_good_link_is_normalised_and_sent_to_the_backend(self):
        code, result, sent, notices, popups = self.run_cli(
            ["play-url", "https://m.soundcloud.com/artist/song?si=abc"]
        )

        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(sent, ["play-url:https://soundcloud.com/artist/song"])
        self.assertEqual((notices, popups), ([], []))

    def test_playback_outcome_decides_the_announcement(self):
        with self.subTest('notify names the track that started playing'):
            code, _result, _sent, notices, _popups = self.run_cli(
                ["play-url", "--notify", "https://soundcloud.com/artist/song"]
            )

            self.assertEqual(code, 0)
            self.assertEqual(notices, ["Playing: Song 7 — Artist"])
        with self.subTest('a track that fails to start is reported instead of announced'):
            code, result, _sent, notices, popups = self.run_cli(
                ["play-url", "--notify", "--open", "https://soundcloud.com/artist/song"],
                playback={"playing": False, "playbackState": "error", "title": "Song 7", "artist": "Artist"},
            )

            self.assertEqual(code, 1)
            self.assertEqual(result["error"], "SoundCloud can't play that track")
            self.assertEqual(notices, ["SoundCloud can't play that track"])
            self.assertEqual(popups, [])
        with self.subTest('a track that is still loading is not announced as playing'):
            code, _result, _sent, notices, _popups = self.run_cli(
                ["play-url", "--notify", "https://soundcloud.com/artist/song"],
                playback={"playing": False, "playbackState": "resolving", "title": "Song 7", "artist": "Artist"},
            )

            self.assertEqual(code, 0)
            self.assertEqual(notices, ["Loading: Song 7 — Artist"])

    def test_waiting_for_playback_stops_at_playing_error_or_timeout(self):
        app = load_module()
        states = iter([{"playbackState": "resolving"}, {"playbackState": "resolving"}, {"playing": True}])
        app.request_backend = lambda command, timeout=4.0: next(states)
        with mock.patch.object(app.time, "sleep"):
            self.assertEqual(app.wait_for_playback(), {"playing": True})

        app.request_backend = lambda command, timeout=4.0: {"playbackState": "error"}
        self.assertEqual(app.wait_for_playback(), {"playbackState": "error"})

        app.request_backend = lambda command, timeout=4.0: {"playbackState": "resolving"}
        with mock.patch.object(app.time, "sleep"):
            self.assertEqual(app.wait_for_playback(timeout=0.0), {"playbackState": "resolving"})

    def test_open_shows_the_popup_only_after_the_track_starts(self):
        *_rest, popups = self.run_cli(["play-url", "--open", "https://soundcloud.com/artist/song"])
        self.assertEqual(popups, [True])

        *_rest, popups = self.run_cli(
            ["play-url", "--open", "https://soundcloud.com/artist/song"],
            responses={"ok": False, "error": "Track not found"},
        )
        self.assertEqual(popups, [])

    def test_failures_are_reported_and_notify_only_when_asked(self):
        with self.subTest('a bad link fails without starting or contacting the backend'):
            code, result, sent, notices, _popups = self.run_cli(
                ["play-url", "--notify", "https://example.com/a/b"], backend_running=False
            )

            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(sent, [])
            self.assertEqual(notices, ["That isn't a SoundCloud link"])
        with self.subTest('failures notify only when asked'):
            quiet = self.run_cli(["play-url", "not a link"])
            loud = self.run_cli(["play-url", "--notify", "not a link"])

            self.assertEqual(quiet[3], [])
            self.assertEqual(loud[3], ["That isn't a SoundCloud link"])
            # A good link stays silent without --notify, too.
            self.assertEqual(self.run_cli(["play-url", "https://soundcloud.com/artist/song"])[3], [])
        with self.subTest('backend errors are reported'):
            code, result, _sent, notices, _popups = self.run_cli(
                ["play-url", "--notify", "https://soundcloud.com/artist/song"],
                responses={"ok": False, "running": True, "error": "Track not found. It may be private or deleted"},
            )

            self.assertEqual(code, 1)
            self.assertEqual(result["error"], "Track not found. It may be private or deleted")
            self.assertEqual(notices, ["Track not found. It may be private or deleted"])
        with self.subTest('a backend that will not start is reported'):
            code, result, sent, notices, _popups = self.run_cli(
                ["play-url", "--notify", "https://soundcloud.com/artist/song"], backend_running=False
            )

            self.assertEqual(code, 1)
            self.assertEqual(sent, [])
            # A cold start is announced so the wait is not silent, then the failure is reported.
            self.assertEqual(notices, ["Starting SoundCloud…", "The SoundCloud backend didn't start"])

    def test_every_cannot_play_path_says_the_same_thing(self):
        app = load_module()
        no_text = self.run_cli(
            ["play-url", "https://soundcloud.com/artist/song"], responses={"ok": False, "running": True}
        )
        errored = self.run_cli(
            ["play-url", "https://soundcloud.com/artist/song"],
            playback={"playing": False, "playbackState": "error"},
        )
        request, _calls = make_resolver({ResolveLinkTest.RESOLVE_SONG: track_payload(7, duration=None)})
        with self.assertRaises(app.LinkError) as unusable:
            app.TrackLikes(lambda: "token", None, request=request).resolve_link(
                "https://soundcloud.com/artist/song"
            )

        self.assertEqual(
            {no_text[1]["error"], errored[1]["error"], str(unusable.exception)}, {app.CANT_PLAY_MESSAGE}
        )

    def test_repeated_messages_come_from_one_definition(self):
        app = load_module()

        for text, message in (
            ("https://soundcloud.com/artist/sets/mix", app.PLAYLIST_MESSAGE),
            ("https://soundcloud.com/artist", app.ARTIST_PAGE_MESSAGE),
            ("https://soundcloud.com/artist/song/extra", app.NOT_A_TRACK_MESSAGE),
            ("https://example.com/a/b", app.NOT_SOUNDCLOUD_MESSAGE),
        ):
            with self.assertRaises(app.LinkError) as raised:
                app.parse_track_link(text)
            self.assertEqual(str(raised.exception), message)
        for kind, message in (("playlist", app.PLAYLIST_MESSAGE), ("user", app.ARTIST_PAGE_MESSAGE),
                              ("other", app.NOT_A_TRACK_MESSAGE)):
            request, _calls = make_resolver({ResolveLinkTest.RESOLVE_SONG: {"kind": kind, "id": 1}})
            with self.assertRaises(app.LinkError) as raised:
                app.TrackLikes(lambda: "token", None, request=request).resolve_link(
                    "https://soundcloud.com/artist/song"
                )
            self.assertEqual(str(raised.exception), message)


class DesktopNotificationTest(unittest.TestCase):
    def test_desktop_notification_separates_options_times_out_and_cleans_the_text(self):
        app = load_module()
        calls = []
        with mock.patch.object(app.os, "access", return_value=True), \
                mock.patch.object(app.subprocess, "run", lambda *a, **k: calls.append((a, k))):
            app.send_desktop_notification("-h <b>&\x1b[2J\x07\u202etxt")

        (args,), options = calls[0]
        self.assertEqual(args[-3:], ["--", "SoundCloud", "-h &lt;b&gt;&amp;[2Jtxt"])
        self.assertEqual(options["timeout"], 3.0)
        self.assertNotIn("shell", options)

    def test_desktop_notification_ignores_a_notifier_that_fails_or_hangs(self):
        app = load_module()
        for error in (OSError("gone"), subprocess.TimeoutExpired("notify-send", 3)):
            def run(*_args, _error=error, **_kwargs):
                raise _error
            with mock.patch.object(app.os, "access", return_value=True), \
                    mock.patch.object(app.subprocess, "run", run):
                app.send_desktop_notification("hello")


class LocalPlayHistoryTest(unittest.TestCase):
    def row(self, number):
        return {"url": f"https://soundcloud.com/artist/song-{number}", "title": f"Song {number}"}

    def test_a_played_track_goes_to_the_front_once(self):
        app = load_module()
        tracks = [self.row(1), self.row(2), self.row(3)]

        result = app.with_play_first(tracks, self.row(3))

        self.assertEqual([t["title"] for t in result], ["Song 3", "Song 1", "Song 2"])
        self.assertEqual([t["title"] for t in app.with_play_first(tracks, self.row(9))],
                         ["Song 9", "Song 1", "Song 2", "Song 3"])

    def test_the_play_list_is_bounded(self):
        app = load_module()

        result = app.with_play_first([self.row(n) for n in range(1, 6)], self.row(9), limit=3)

        self.assertEqual([t["title"] for t in result], ["Song 9", "Song 1", "Song 2"])

    def test_local_plays_are_merged_with_a_server_page_by_play_time(self):
        app = load_module()
        at = lambda number, when: {**self.row(number), "playedAt": when}
        server = [at(1, 900), at(2, 500), at(3, 100)]

        result = app.with_local_plays(server, [at(5, 700), at(2, 800)])

        # Song 5 and Song 2 played here fall between the server's own plays,
        # and Song 2 appears once, with its latest time.
        self.assertEqual([t["title"] for t in result], ["Song 1", "Song 2", "Song 5", "Song 3"])
        self.assertEqual(result[1]["playedAt"], 800)
        self.assertEqual(app.with_local_plays(server, []), server)

    def test_a_server_play_keeps_its_time_and_a_bad_time_is_dropped(self):
        app = load_module()

        row = app.track_from_entry({**like_item(4), "played_at": 1_700_000_000_000})
        bad = app.track_from_entry({**like_item(4), "played_at": "yesterday"})

        self.assertEqual(row["playedAt"], 1_700_000_000_000)
        self.assertNotIn("playedAt", bad)

    def test_local_plays_survive_a_restart_through_the_track_cache(self):
        app = load_module()
        row = app.track_from_entry({**like_item(4), "played_at": 1_700_000_000_000})

        cache = app.sanitize_track_cache({"plays": [row], "history": [row]})

        self.assertEqual(cache, {"plays": [row], "history": [row]})

    def test_history_refreshes_sooner_than_likes(self):
        app = load_module()

        self.assertLess(app.API_LIST_REFRESH_SECONDS["history"], app.API_LIST_REFRESH_SECONDS["likes"])
        self.assertEqual(set(app.API_LIST_REFRESH_SECONDS), set(app.API_TRACK_LISTS))


class PlayQueueTest(unittest.TestCase):
    def row(self, number):
        return {
            "playbackId": f"soundcloud:tracks:{number}",
            "title": f"Song {number}",
            "artist": "Artist",
            "url": f"https://soundcloud.com/artist/song-{number}",
            "artUrl": "",
            "playCount": 0,
            "durationMs": 1000,
        }

    def queue(self, count=5, index=0, **options):
        app = load_module()
        queue = app.PlayQueue(**options)
        queue.replace([self.row(n) for n in range(1, count + 1)], index, "home")
        return queue

    def titles(self, rows):
        return [row["title"] for row in rows]

    def test_replace_sanitizes_clamps_and_caps_the_list(self):
        with self.subTest('replace puts the cursor on the clicked track'):
            queue = self.queue(5, index=2)

            self.assertEqual(queue.current["title"], "Song 3")
            self.assertEqual((queue.cursor, queue.length, queue.source), (2, 5, "home"))
        with self.subTest('a bad index is clamped and an empty list empties the queue'):
            queue = self.queue(3, index=99)
            self.assertEqual(queue.current["title"], "Song 3")
            queue.replace([self.row(1)], -4, "feed")
            self.assertEqual(queue.current["title"], "Song 1")
            queue.replace([], 0, "feed")
            self.assertIsNone(queue.current)
            self.assertEqual(queue.length, 0)
            self.assertIsNone(queue.advance(True))
            self.assertIsNone(queue.previous(0.0))
        with self.subTest('replace drops duplicate urls and keeps the clicked track'):
            app = load_module()
            queue = app.PlayQueue()
            rows = [self.row(1), self.row(2), self.row(1), self.row(3), self.row(3)]

            queue.replace(rows, 3, "home")

            self.assertEqual(self.titles(queue.items), ["Song 1", "Song 2", "Song 3"])
            self.assertEqual(queue.current["title"], "Song 3")
        with self.subTest('rows without a url are ignored and rows are copied'):
            app = load_module()
            queue = app.PlayQueue()
            mine = self.row(1)

            queue.replace([mine, {"title": "No url"}, "junk", None, {"url": 5}], 0, "home")
            mine["title"] = "Changed"

            self.assertEqual(self.titles(queue.items), ["Song 1"])
        with self.subTest('the queue is capped and keeps the clicked track in view'):
            app = load_module()
            queue = app.PlayQueue()

            queue.replace([self.row(n) for n in range(1, 301)], 250, "home")

            self.assertEqual(queue.length, app.MAX_TRACKS)
            self.assertEqual(queue.current["title"], "Song 251")

    def test_next_walks_the_list_and_stops_at_the_end(self):
        queue = self.queue(3)

        self.assertEqual(queue.advance(True)["title"], "Song 2")
        self.assertEqual(queue.advance(False)["title"], "Song 3")
        self.assertIsNone(queue.advance(True))
        self.assertIsNone(queue.advance(False))
        self.assertEqual(queue.current["title"], "Song 3")
        self.assertFalse(queue.can_next)

    def test_play_next_rules(self):
        with self.subTest('play next items go after the cursor in the order added'):
            queue = self.queue(3)

            queue.play_next(self.row(10))
            queue.play_next(self.row(11))
            order = [queue.advance(True)["title"] for _ in range(4)]

            self.assertEqual(order, ["Song 10", "Song 11", "Song 2", "Song 3"])
        with self.subTest('play next works on an empty queue and ignores bad rows'):
            app = load_module()
            queue = app.PlayQueue()

            queue.play_next({"title": "No url"})
            self.assertFalse(queue.can_next)
            queue.play_next(self.row(7))

            self.assertTrue(queue.can_next)
            self.assertEqual(queue.advance(True)["title"], "Song 7")
            self.assertEqual(queue.current["title"], "Song 7")
        with self.subTest('a repeated play next keeps one copy in its first place'):
            queue = self.queue(2)

            for number in (10, 11, 10):
                queue.play_next(self.row(number))

            self.assertEqual(self.titles(queue.explicit), ["Song 10", "Song 11"])
        with self.subTest('a played explicit item is not repeated in the list'):
            queue = self.queue(4)
            queue.play_next(self.row(3))

            order = [queue.advance(True)["title"] for _ in range(3)]

            self.assertEqual(order, ["Song 3", "Song 2", "Song 4"])
            self.assertEqual(self.titles(queue.items), ["Song 1", "Song 3", "Song 2", "Song 4"])
        with self.subTest('replace keeps the explicit items'):
            queue = self.queue(3)
            queue.play_next(self.row(10))

            queue.replace([self.row(20), self.row(21)], 0, "feed")

            self.assertEqual(queue.current["title"], "Song 20")
            self.assertEqual(self.titles(queue.explicit), ["Song 10"])
            self.assertEqual(queue.advance(True)["title"], "Song 10")
            self.assertEqual(queue.advance(True)["title"], "Song 21")

    def test_previous_rules(self):
        with self.subTest('previous restarts after the threshold and goes back before it'):
            app = load_module()
            queue = self.queue(3, index=2)

            self.assertEqual(app.PREVIOUS_RESTART_SECONDS, 3.0)
            self.assertEqual(queue.previous(app.PREVIOUS_RESTART_SECONDS), "restart")
            self.assertEqual(queue.previous(10.0), "restart")
            self.assertEqual(queue.current["title"], "Song 3")
            self.assertEqual(queue.previous(2.9)["title"], "Song 2")
            self.assertEqual(queue.current["title"], "Song 2")
        with self.subTest('previous restarts when there is nothing before'):
            queue = self.queue(3)

            self.assertEqual(queue.previous(0.0), "restart")
            self.assertEqual(queue.current["title"], "Song 1")
            self.assertTrue(queue.can_previous)
        with self.subTest('previous treats a hostile position as the start'):
            queue = self.queue(3, index=1)

            for position in (True, "9", None, float("nan"), -5.0):
                queue.replace([self.row(n) for n in range(1, 4)], 1, "home")
                self.assertEqual(queue.previous(position)["title"], "Song 1")
            queue.replace([self.row(n) for n in range(1, 4)], 1, "home")
            self.assertEqual(queue.previous(float("inf")), "restart")
        with self.subTest('previous after a play next item returns to the list track'):
            queue = self.queue(3)
            queue.play_next(self.row(10))
            queue.advance(True)

            self.assertEqual(queue.previous(0.0)["title"], "Song 1")

    def test_repeat_modes(self):
        with self.subTest('repeat one repeats on an automatic end but not on a user next'):
            queue = self.queue(3, repeat="one")

            self.assertEqual(queue.advance(False)["title"], "Song 1")
            self.assertEqual(queue.advance(False)["title"], "Song 1")
            self.assertEqual(queue.advance(True)["title"], "Song 2")
        with self.subTest('repeat all wraps at the end'):
            queue = self.queue(2, index=1, repeat="all")

            self.assertTrue(queue.can_next)
            self.assertEqual(queue.advance(False)["title"], "Song 1")
            self.assertEqual(queue.advance(True)["title"], "Song 2")
            self.assertEqual(queue.advance(True)["title"], "Song 1")
        with self.subTest('repeat none ends and an unknown mode is refused'):
            queue = self.queue(1)

            self.assertEqual(queue.repeat, "none")
            self.assertIsNone(queue.advance(False))
            queue.repeat = "all"
            queue.repeat = "bogus"
            self.assertEqual(queue.repeat, "all")

    def test_shuffle_rules(self):
        with self.subTest('shuffle permutes only the part after the cursor'):
            import random
            app = load_module()
            queue = app.PlayQueue(rng=random.Random(4))
            queue.replace([self.row(n) for n in range(1, 21)], 5, "home")
            before = self.titles(queue.items)

            queue.shuffle = True

            after = self.titles(queue.items)
            self.assertEqual(after[:6], before[:6])
            self.assertEqual(sorted(after[6:]), sorted(before[6:]))
            self.assertNotEqual(after[6:], before[6:])
            self.assertEqual(queue.current["title"], "Song 6")
        with self.subTest('shuffle uses the injected random source and survives a replace'):
            app = load_module()

            class Reverse:
                def shuffle(self, items):
                    items.reverse()

            queue = app.PlayQueue(rng=Reverse(), shuffle=True)
            queue.replace([self.row(n) for n in range(1, 6)], 1, "home")

            self.assertEqual(self.titles(queue.items), ["Song 1", "Song 2", "Song 5", "Song 4", "Song 3"])
            queue.shuffle = False
            self.assertEqual(queue.length, 5)
            self.assertFalse(queue.shuffle)

    def test_dead_tracks_are_removed_and_playback_moves_on(self):
        with self.subTest('a dead track after the cursor is removed'):
            queue = self.queue(4)

            queue.mark_dead("soundcloud:tracks:3")

            self.assertEqual(self.titles(queue.items), ["Song 1", "Song 2", "Song 4"])
            self.assertEqual(queue.current["title"], "Song 1")
        with self.subTest('a dead track before the cursor keeps the current one'):
            queue = self.queue(4, index=2)

            queue.mark_dead("https://soundcloud.com/artist/song-1")

            self.assertEqual(queue.current["title"], "Song 3")
            self.assertEqual(queue.cursor, 1)
        with self.subTest('a dead current track hands over to the next one'):
            queue = self.queue(3, index=1)
            queue.play_next(self.row(10))

            self.assertEqual(queue.mark_dead("soundcloud:tracks:2")["title"], "Song 10")
            self.assertEqual(queue.advance(True)["title"], "Song 3")
        with self.subTest('a dead last track ends the queue or wraps under repeat all'):
            queue = self.queue(2, index=1)
            self.assertIsNone(queue.mark_dead("soundcloud:tracks:2"))
            self.assertEqual(queue.length, 1)

            queue = self.queue(2, index=1, repeat="all")
            self.assertEqual(queue.mark_dead("soundcloud:tracks:2")["title"], "Song 1")
        with self.subTest('a dead explicit item is removed and the last dead track empties the queue'):
            queue = self.queue(1)
            queue.play_next(self.row(10))

            queue.mark_dead("soundcloud:tracks:10")
            self.assertEqual(queue.explicit, [])
            self.assertIsNone(queue.mark_dead("soundcloud:tracks:1"))
            self.assertIsNone(queue.current)
            self.assertEqual((queue.length, queue.cursor), (0, 0))
            self.assertIsNone(queue.mark_dead("soundcloud:tracks:1"))
            self.assertIsNone(queue.mark_dead(None))

    def test_the_queue_round_trips_through_a_dict(self):
        app = load_module()
        queue = self.queue(4, index=2, repeat="all")
        queue.play_next(self.row(10))

        data = queue.to_dict(position_ms=4321, now_ms=1_700_000_000_000)
        again = app.PlayQueue.from_dict(data)

        self.assertEqual(data["v"], 1)
        self.assertEqual((data["cursor"], data["repeat"], data["shuffle"], data["source"]),
                         (2, "all", False, "home"))
        self.assertEqual((data["positionMs"], data["savedAt"]), (4321, 1_700_000_000_000))
        self.assertEqual(again.to_dict(4321, 1_700_000_000_000), data)
        self.assertEqual(again.current["title"], "Song 3")
        self.assertEqual(self.titles(again.explicit), ["Song 10"])


    def ids(self, rows):
        return [row["playbackId"].rpartition(":")[2] for row in rows]

    def test_add_last_rules(self):
        with self.subTest('add last plays after the whole list and after play next items'):
            queue = self.queue(3)

            self.assertTrue(queue.add_last(self.row(20)))
            self.assertTrue(queue.add_last(self.row(21)))
            queue.play_next(self.row(10))
            order = [queue.advance(True)["title"] for _ in range(5)]

            self.assertEqual(order, ["Song 10", "Song 2", "Song 3", "Song 20", "Song 21"])
            self.assertEqual(self.titles(queue.items), ["Song 1", "Song 10", "Song 2", "Song 3", "Song 20", "Song 21"])
            self.assertIsNone(queue.advance(True))
            self.assertEqual(queue.current["title"], "Song 21")
        with self.subTest('add last marks its rows and keeps one copy by url'):
            queue = self.queue(3)
            queue.play_next(self.row(10))

            self.assertTrue(queue.add_last(self.row(20)))
            self.assertFalse(queue.add_last(self.row(20)))   # already added
            self.assertFalse(queue.add_last(self.row(10)))   # waiting as play next
            self.assertFalse(queue.add_last(self.row(3)))    # coming up in the list
            self.assertFalse(queue.add_last({"title": "No url"}))
            self.assertFalse(queue.add_last(None))

            self.assertEqual(self.titles(queue.added), ["Song 20"])
            self.assertEqual((queue.added[0]["added"], queue.added[0]["explicit"]), (True, True))
        with self.subTest('add last allows a track that was already played'):
            queue = self.queue(3, index=1)

            self.assertTrue(queue.add_last(self.row(1)))
            queue.advance(True)

            self.assertEqual([queue.advance(True)["title"]], ["Song 1"])
        with self.subTest('add last makes next possible and plays on an empty queue'):
            app = load_module()
            queue = app.PlayQueue()

            queue.add_last(self.row(7))

            self.assertTrue(queue.can_next)
            self.assertEqual(queue.advance(True)["title"], "Song 7")
            self.assertFalse(queue.can_next)

    def test_added_rows_interact_with_the_list_and_other_queue_operations(self):
        with self.subTest('added rows survive a new list and come after it'):
            queue = self.queue(3)
            queue.add_last(self.row(20))

            queue.replace([self.row(30), self.row(31)], 0, "feed")

            self.assertEqual(self.titles(queue.added), ["Song 20"])
            self.assertEqual([queue.advance(True)["title"] for _ in range(2)], ["Song 31", "Song 20"])
        with self.subTest('added rows play before repeat all wraps'):
            queue = self.queue(2, repeat="all")
            queue.add_last(self.row(20))

            order = [queue.advance(True)["title"] for _ in range(3)]

            self.assertEqual(order, ["Song 2", "Song 20", "Song 1"])
        with self.subTest('play next takes over a row that was added'):
            queue = self.queue(2)
            queue.add_last(self.row(20))

            queue.play_next(self.row(20))

            self.assertEqual((self.titles(queue.explicit), queue.added), (["Song 20"], []))
        with self.subTest('remove clear and mark dead also reach added rows'):
            queue = self.queue(3)
            for number in (20, 21, 22):
                queue.add_last(self.row(number))

            self.assertTrue(queue.remove("soundcloud:tracks:20"))
            queue.mark_dead("https://soundcloud.com/artist/song-21")
            self.assertEqual(self.titles(queue.added), ["Song 22"])
            queue.clear()

            self.assertEqual((queue.added, self.titles(queue.items)), ([], ["Song 1"]))
        with self.subTest('view lists added rows last and marks them'):
            queue = self.queue(3)
            queue.play_next(self.row(10))
            queue.add_last(self.row(20))

            rows, cursor, explicit = queue.view()

            self.assertEqual(self.ids(rows), ["1", "10", "2", "3", "20"])
            self.assertEqual((cursor, explicit), (0, 1))
            self.assertEqual([(row["explicit"], row["added"]) for row in rows],
                             [(False, False), (True, False), (False, False), (False, False), (True, True)])

    def test_jump_to_rules(self):
        with self.subTest('jump to moves the cursor inside the list without reordering'):
            queue = self.queue(4)

            self.assertEqual(queue.jump_to("soundcloud:tracks:3")["title"], "Song 3")
            self.assertEqual(queue.jump_to("https://soundcloud.com/artist/song-1")["title"], "Song 1")

            self.assertEqual((self.ids(queue.items), queue.cursor), (["1", "2", "3", "4"], 0))
            self.assertIsNone(queue.jump_to("soundcloud:tracks:99"))
            self.assertIsNone(queue.jump_to(""))
            self.assertIsNone(queue.jump_to(None))
            self.assertIsNone(queue.jump_to(7))
        with self.subTest('jump to a waiting row enters the list after the cursor'):
            queue = self.queue(4, index=1)
            queue.play_next(self.row(10))
            queue.add_last(self.row(20))

            self.assertEqual(queue.jump_to("soundcloud:tracks:20")["title"], "Song 20")
            self.assertEqual(self.ids(queue.items), ["1", "2", "20", "3", "4"])
            self.assertEqual((queue.cursor, self.titles(queue.explicit), queue.added), (2, ["Song 10"], []))
        with self.subTest('jump to a row also waiting in the list does not play it twice'):
            queue = self.queue(4)
            queue.play_next(self.row(3))

            queue.jump_to("soundcloud:tracks:3")

            self.assertEqual((self.ids(queue.items), queue.cursor, queue.explicit), (["1", "3", "2", "4"], 1, []))

    def test_move_rules(self):
        with self.subTest('move reorders inside a group and clamps'):
            queue = self.queue(5)
            for number in (10, 11, 12):
                queue.play_next(self.row(number))
            for number in (20, 21):
                queue.add_last(self.row(number))

            self.assertTrue(queue.move("soundcloud:tracks:12", -1))
            self.assertTrue(queue.move("soundcloud:tracks:10", 99))
            self.assertTrue(queue.move("soundcloud:tracks:21", -99))
            self.assertTrue(queue.move("soundcloud:tracks:3", 1))
            self.assertTrue(queue.move("soundcloud:tracks:5", -99))
            self.assertTrue(queue.move("soundcloud:tracks:2", 0))

            self.assertEqual(self.ids(queue.explicit), ["12", "11", "10"])
            self.assertEqual(self.ids(queue.added), ["21", "20"])
            self.assertEqual(self.ids(queue.items), ["1", "5", "2", "4", "3"])
            self.assertEqual(queue.cursor, 0)
        with self.subTest('move counts places the way the view shows them'):
            queue = self.queue(5)
            queue.play_next(self.row(3))  # hidden from the list part of the view
            rows, _cursor, _count = queue.view()
            self.assertEqual(self.ids(rows), ["1", "3", "2", "4", "5"])

            queue.move("soundcloud:tracks:2", 1)

            rows, _cursor, _count = queue.view()
            self.assertEqual(self.ids(rows), ["1", "3", "4", "2", "5"])
        with self.subTest('move refuses the playing row played rows and bad input'):
            queue = self.queue(4, index=1)
            before = self.ids(queue.items)

            self.assertFalse(queue.move("soundcloud:tracks:2", 1))   # playing
            self.assertFalse(queue.move("soundcloud:tracks:1", 1))   # played
            self.assertFalse(queue.move("soundcloud:tracks:99", 1))
            self.assertFalse(queue.move("", 1))
            self.assertFalse(queue.move(None, 1))
            self.assertFalse(queue.move("soundcloud:tracks:3", "1"))
            self.assertFalse(queue.move("soundcloud:tracks:3", 1.5))
            self.assertFalse(queue.move("soundcloud:tracks:3", True))

            self.assertEqual((self.ids(queue.items), queue.cursor), (before, 1))

    def test_added_rows_are_saved_loaded_and_older_saves_still_load(self):
        app = load_module()
        queue = self.queue(3)
        queue.add_last(self.row(20))

        data = queue.to_dict(position_ms=5, now_ms=1_700_000_000_000)
        again = app.PlayQueue.from_dict(data)

        self.assertEqual(self.ids(again.added), ["20"])
        self.assertEqual((again.added[0]["added"], again.added[0]["explicit"]), (True, True))
        self.assertEqual(again.to_dict(5, 1_700_000_000_000), data)
        old = {key: value for key, value in data.items() if key != "added"}
        self.assertEqual(self.ids(app.PlayQueue.from_dict(old).items), ["1", "2", "3"])
        self.assertEqual(app.PlayQueue.from_dict(old).added, [])
        self.assertEqual(app.sanitize_queue({**data, "added": [{"title": "bad"}]})["items"], [])


class QueueStoreTest(unittest.TestCase):
    def row(self, number):
        return PlayQueueTest.row(None, number)

    def with_store(self, check, maximum=None):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                store = app.QueueStore(directory_fd, **({"maximum_bytes": maximum} if maximum else {}))
                check(app, store, Path(directory) / "queue.json")
            finally:
                os.close(directory_fd)

    def test_the_queue_survives_a_restart_in_an_owner_only_file(self):
        def check(app, store, path):
            queue = app.PlayQueue(repeat="one", shuffle=False)
            queue.replace([self.row(n) for n in range(1, 4)], 1, "likes")
            queue.play_next(self.row(10))

            store.save(queue, position_ms=61_000)
            loaded, position_ms = store.load()

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(position_ms, 61_000)
            self.assertEqual((loaded.cursor, loaded.repeat, loaded.source), (1, "one", "likes"))
            self.assertEqual(loaded.current["title"], "Song 2")
            self.assertEqual([r["title"] for r in loaded.explicit], ["Song 10"])
            self.assertEqual(app.MAX_QUEUE_BYTES, 256 * 1024)

        self.with_store(check)

    def test_a_missing_or_corrupt_file_gives_an_empty_queue(self):
        def check(app, store, path):
            loaded, position_ms = store.load()
            self.assertEqual((loaded.length, loaded.current, position_ms), (0, None, 0))
            for content in (b"{not json", b"[]", b"\xff\xfe", b"null", b"{}", b'{"v":2,"items":[]}'):
                path.write_bytes(content)
                os.chmod(path, 0o600)
                loaded, position_ms = store.load()
                self.assertEqual((loaded.length, position_ms), (0, 0))

        self.with_store(check)

    def test_an_oversize_file_is_ignored(self):
        def check(app, store, path):
            queue = app.PlayQueue()
            queue.replace([self.row(n) for n in range(1, 4)], 0, "home")
            store.save(queue)
            self.assertEqual(store.load()[0].length, 3)
            path.write_bytes(b" " * (app.MAX_QUEUE_BYTES + 1))
            os.chmod(path, 0o600)
            self.assertEqual(store.load()[0].length, 0)

        self.with_store(check)

    def test_a_queue_that_does_not_fit_is_not_written(self):
        def check(app, store, path):
            queue = app.PlayQueue()
            queue.replace([self.row(n) for n in range(1, 4)], 0, "home")

            store.save(queue)

            self.assertFalse(path.exists())

        self.with_store(check, maximum=100)

    def sanitized(self, **changes):
        app = load_module()
        payload = {
            "v": 1, "items": [self.row(1), self.row(2), self.row(3)], "cursor": 1,
            "explicit": [self.row(10)], "repeat": "all", "shuffle": True,
            "source": "home", "positionMs": 500, "savedAt": 1_700_000_000_000,
        }
        return app.sanitize_queue({**payload, **changes})

    def test_sanitize_keeps_a_good_payload_and_clamps_or_resets_bad_fields(self):
        with self.subTest('sanitize keeps a good payload'):
            result = self.sanitized()

            self.assertEqual(result["cursor"], 1)
            self.assertEqual((result["repeat"], result["shuffle"], result["source"]), ("all", True, "home"))
            self.assertEqual((result["positionMs"], result["savedAt"]), (500, 1_700_000_000_000))
            self.assertEqual(len(result["items"]), 3)
        with self.subTest('sanitize clamps the cursor and drops hostile numbers'):
            for cursor, expected in ((99, 2), (-3, 0), (10**30, 2), (True, 0), ("1", 0), (1.5, 0), (None, 0)):
                self.assertEqual(self.sanitized(cursor=cursor)["cursor"], expected, cursor)
            for value in (True, -1, 10**30, "5", 2.5, float("nan")):
                result = self.sanitized(positionMs=value, savedAt=value)
                self.assertEqual((result["positionMs"], result["savedAt"]), (0, 0), value)
        with self.subTest('sanitize resets unknown modes and non boolean shuffle'):
            result = self.sanitized(repeat="forever", shuffle=1, source=["x"])

            self.assertEqual((result["repeat"], result["shuffle"], result["source"]), ("none", False, ""))

    def test_sanitize_empties_everything_on_a_bad_row_or_too_many_rows(self):
        app = load_module()
        bad = {**self.row(2), "url": "https://example.com/x"}
        many = [self.row(n) for n in range(1, app.MAX_TRACKS + 2)]

        for changes in ({"items": [self.row(1), bad]}, {"items": many}, {"explicit": [bad]},
                        {"items": "nope"}, {"explicit": {"a": 1}}):
            result = self.sanitized(**changes)
            self.assertEqual((result["items"], result["explicit"], result["cursor"]), ([], [], 0), changes)

    def test_sanitize_drops_duplicate_urls_and_bad_payloads(self):
        app = load_module()
        result = self.sanitized(items=[self.row(1), self.row(1), self.row(2)], explicit=[self.row(5), self.row(5)])

        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(len(result["explicit"]), 1)
        for payload in (None, 5, "x", [], True):
            self.assertEqual(app.sanitize_queue(payload)["items"], [])
        self.assertEqual(app.sanitize_queue({"v": True, "items": [self.row(1)]})["items"], [])


class QueueCommandParsingTest(unittest.TestCase):
    def test_play_from_names_a_tab_and_a_playback_id(self):
        app = load_module()

        self.assertEqual(
            app.play_from_command("play-from:likes:soundcloud:tracks:42"),
            ("likes", "soundcloud:tracks:42"),
        )
        for bad in (
            "play-from:library:soundcloud:tracks:42", "play-from:home:42", "play-from:home:",
            "play-from:soundcloud:tracks:42", "play-from:home:soundcloud:tracks:0", "play:x",
        ):
            self.assertIsNone(app.play_from_command(bad), bad)

    def test_queue_next_needs_a_playback_id(self):
        app = load_module()

        self.assertEqual(app.queue_next_from_command("queue-next:soundcloud:tracks:7"), "soundcloud:tracks:7")
        self.assertIsNone(app.queue_next_from_command("queue-next:7"))
        self.assertIsNone(app.queue_next_from_command("queue-next:"))

    def test_repeat_cycles_or_takes_a_mode_and_shuffle_toggles_or_takes_a_state(self):
        app = load_module()

        self.assertEqual([app.repeat_from_command("repeat", m) for m in ("none", "one", "all")],
                         ["one", "all", "none"])
        self.assertEqual(app.repeat_from_command("repeat:all", "none"), "all")
        self.assertIsNone(app.repeat_from_command("repeat:twice", "none"))
        self.assertIs(app.shuffle_from_command("shuffle", False), True)
        self.assertIs(app.shuffle_from_command("shuffle", True), False)
        self.assertIs(app.shuffle_from_command("shuffle:on", True), True)
        self.assertIs(app.shuffle_from_command("shuffle:off", True), False)
        self.assertIsNone(app.shuffle_from_command("shuffle:maybe", True))

class QueueStatusTest(unittest.TestCase):
    BASE = {
        "playerPresent": True, "playbackState": "playing", "playing": True,
        "playbackId": "soundcloud:tracks:5", "title": "T", "artist": "A", "artUrl": "",
        "loggedIn": True, "url": "https://soundcloud.com/feed", "duration": 10, "position": 1,
    }

    def test_the_old_keys_are_unchanged_and_the_queue_keys_are_optional(self):
        app = load_module()
        plain = app.validate_status_payload(self.BASE)
        queue = app.PlayQueue()
        queue.replace([PlayQueueTest.row(None, n) for n in (1, 2, 3)], 1, "home")

        with_queue = app.validate_status_payload({**self.BASE, **app.queue_status(queue)})

        self.assertEqual(
            set(plain),
            {"playerPresent", "playbackState", "playing", "playbackId", "title", "artist",
             "artUrl", "loggedIn", "url", "duration", "position", "waveform"},
        )
        self.assertEqual({key: with_queue[key] for key in plain}, plain)
        self.assertEqual(
            {key: with_queue[key] for key in set(with_queue) - set(plain)},
            {"canNext": True, "canPrevious": True, "cursor": 1, "length": 3,
             "repeat": "none", "shuffle": False},
        )

    def test_invalid_queue_values_are_left_out(self):
        app = load_module()
        forged = {**self.BASE, "canNext": "yes", "canPrevious": 1, "cursor": -1, "length": True,
                  "repeat": "twice", "shuffle": None}

        self.assertEqual(
            set(app.validate_status_payload(forged)), set(app.validate_status_payload(self.BASE))
        )
        self.assertNotIn("cursor", app.validate_status_payload({**self.BASE, "cursor": 10**6}))

    def test_an_empty_queue_cannot_go_next_or_previous(self):
        app = load_module()

        self.assertEqual(
            app.queue_status(app.PlayQueue()),
            {"canNext": False, "canPrevious": False, "cursor": 0, "length": 0,
             "repeat": "none", "shuffle": False},
        )

    def test_a_restored_track_is_paused_at_its_saved_position_with_its_metadata(self):
        app = load_module()
        row = {**PlayQueueTest.row(None, 4), "durationMs": 200_000}

        status = app.validate_status_payload(
            {**app.restored_status(row, 61_000), "loggedIn": True}
        )

        self.assertEqual(
            (status["playbackState"], status["playing"], status["position"], status["duration"]),
            ("paused", False, 61.0, 200.0),
        )
        self.assertEqual((status["title"], status["playbackId"]), ("Song 4", "soundcloud:tracks:4"))
        self.assertEqual(app.restored_status(row, 900_000)["position"], 200.0)


class AutoSkipTest(unittest.TestCase):
    def test_only_a_fast_failure_of_an_automatic_start_is_skipped(self):
        app = load_module()
        skip = app.AutoSkip()
        skip.begin_chain(5)

        skip.started(1, automatic=False, now=10.0)
        self.assertFalse(skip.should_skip(1, 10.5))      # a track the user picked
        skip.started(2, automatic=True, now=20.0)
        self.assertFalse(skip.should_skip(2, 22.5))      # failed after the window
        self.assertFalse(skip.should_skip(1, 20.5))      # a stale generation
        self.assertTrue(skip.should_skip(2, 21.9))

    def test_the_chain_is_capped_at_the_queue_length_when_it_began(self):
        app = load_module()
        skip = app.AutoSkip()
        skip.begin_chain(3)

        results = []
        for generation in range(1, 6):
            skip.started(generation, automatic=True, now=0.0)
            results.append(skip.should_skip(generation, 0.1))

        self.assertEqual(results, [True, True, True, False, False])
        self.assertEqual(skip.notice(), "Skipped 3 tracks that can't play")

    def test_a_user_start_ends_the_chain_and_one_skip_reads_in_the_singular(self):
        app = load_module()
        skip = app.AutoSkip()
        skip.begin_chain(2)
        skip.started(1, automatic=True, now=0.0)
        skip.should_skip(1, 0.1)
        self.assertEqual(skip.notice(), "Skipped 1 track that can't play")

        skip.started(2, automatic=False, now=1.0)

        self.assertEqual(skip.skipped, 0)


class TrackLikesTest(unittest.TestCase):
    def test_like_command_accepts_only_stable_soundcloud_track_ids(self):
        app = load_module()

        self.assertEqual(
            app.track_like_from_command("like:soundcloud:tracks:123"),
            ("soundcloud:tracks:123", True),
        )
        self.assertEqual(
            app.track_like_from_command("unlike:soundcloud:tracks:123"),
            ("soundcloud:tracks:123", False),
        )
        self.assertIsNone(app.track_like_from_command("like:soundcloud:tracks:0"))
        self.assertIsNone(app.track_like_from_command("like:soundcloud:users:123"))
        self.assertIsNone(app.track_like_from_command("like:soundcloud:tracks:1/../../me"))

    def test_session_token_comes_only_from_the_soundcloud_oauth_cookie(self):
        app = load_module()
        cookie = {"name": "oauth_token", "value": "2-1-abc", "domain": "soundcloud.com", "path": "/"}

        self.assertEqual(app.oauth_token_from_cookies([cookie]), "2-1-abc")
        self.assertIsNone(app.oauth_token_from_cookies([{**cookie, "domain": "api-auth.soundcloud.com"}]))
        self.assertIsNone(app.oauth_token_from_cookies([{**cookie, "name": "connect_session"}]))
        # Current SoundCloud tokens are far longer than the original 512 character cap.
        long_token = "2-" + "a" * 850
        self.assertEqual(app.oauth_token_from_cookies([{**cookie, "value": long_token}]), long_token)
        self.assertIsNone(app.oauth_token_from_cookies([{**cookie, "value": "a" * 2049}]))
        # A header-injecting value must never reach the Authorization header.
        self.assertIsNone(app.oauth_token_from_cookies([{**cookie, "value": "abc\r\nX-Evil: 1"}]))

    def test_liking_writes_to_the_signed_in_users_likes_and_updates_state(self):
        app = load_module()
        api = FakeSoundCloudApi(user_id=42, liked_ids=[7])
        likes = app.TrackLikes(lambda: "token", api.write, request=api)

        likes.set_liked(99, True)

        self.assertIn(("PUT", "/users/42/track_likes/99", "token"), api.calls)
        self.assertTrue(likes.is_liked(99))
        self.assertTrue(likes.is_liked(7))

        likes.set_liked(7, False)

        self.assertIn(("DELETE", "/users/42/track_likes/7", "token"), api.calls)
        self.assertFalse(likes.is_liked(7))

    def test_likes_beyond_the_first_page_show_as_liked(self):
        app = load_module()
        api = FakeSoundCloudApi(liked_ids=[1, 2, 3, 4, 5])
        likes = app.TrackLikes(lambda: "token", api.write, request=api, spawn=lambda target: target())

        likes.load_in_background(lambda: None)

        self.assertTrue(likes.is_liked(5))
        self.assertFalse(likes.is_liked(6))

    def test_likes_paging_never_follows_a_cursor_off_soundcloud(self):
        app = load_module()
        calls = []

        def request(method, path, token, maximum=0):
            calls.append(path)
            if path == "/me":
                return {"id": 1}
            return {"collection": [7], "next_href": "https://evil.example/me/track_likes/ids?offset=1"}

        likes = app.TrackLikes(lambda: "token", None, request=request, spawn=lambda target: target())
        likes.load_in_background(lambda: None)

        self.assertEqual(len(calls), 2)
        self.assertTrue(likes.is_liked(7))

    def test_failed_write_keeps_the_previous_liked_state(self):
        app = load_module()
        api = FakeSoundCloudApi(liked_ids=[7])
        likes = app.TrackLikes(lambda: "token", api.write, request=api)
        api.fail_writes = True

        with self.assertRaises(OSError):
            likes.set_liked(7, False)

        self.assertTrue(likes.is_liked(7))

    def test_likes_are_unknown_until_loaded_and_signed_out_never_calls_the_api(self):
        app = load_module()
        api = FakeSoundCloudApi()
        likes = app.TrackLikes(lambda: None, api.write, request=api, spawn=lambda target: target())

        self.assertIsNone(likes.is_liked(7))
        with self.assertRaises(PermissionError):
            likes.set_liked(7, True)
        likes.load_in_background(lambda: None)

        self.assertEqual(api.calls, [])
        self.assertIsNone(likes.is_liked(7))

    def test_background_load_retries_are_throttled(self):
        app = load_module()
        api = FakeSoundCloudApi()
        api.user_id = 0  # invalid account response
        likes = app.TrackLikes(lambda: "token", api.write, request=api, spawn=lambda target: target())

        likes.load_in_background(lambda: None)
        likes.load_in_background(lambda: None)

        self.assertEqual(len(api.calls), 1)

    def test_switching_accounts_drops_the_previous_accounts_likes(self):
        app = load_module()
        api = FakeSoundCloudApi(user_id=1, liked_ids=[7])
        token = ["first"]
        likes = app.TrackLikes(lambda: token[0], api.write, request=api)
        likes.set_liked(8, True)

        token[0] = "second"
        api.user_id, api.liked_ids = 2, []
        likes.set_liked(9, True)

        self.assertIn(("PUT", "/users/2/track_likes/9", "second"), api.calls)
        self.assertFalse(likes.is_liked(7))

    def run_like_function(self, responses, method="PUT"):
        app = load_module()
        harness = f"""
const responses = {json.dumps(responses)};
const requests = [];
global.fetch = async (url, init) => {{
  requests.push({{ url, method: init.method, auth: init.headers.Authorization }});
  const next = responses.shift();
  return new Response(next.body, {{ status: next.status }});
}};
(async () => {{
  const outcomes = [];
  for (let index = 0; index < {len(responses)}; index++) {{
    outcomes.push(await (async (userId, trackId, method, token) => {{
      {app.LIKE_TRACK_FUNCTION}
    }})(42, 99, {json.dumps(method)}, 'token'));
  }}
  process.stdout.write(JSON.stringify({{ outcomes, requests }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
        return json.loads(completed.stdout)

    def test_page_like_reports_a_captcha_so_the_user_can_be_sent_to_solve_it(self):
        captcha = json.dumps({"url": "https://geo.captcha-delivery.com/captcha/?cid=x"})
        lookalike = json.dumps({"url": "https://geo.captcha-delivery.com.example.org/captcha/"})

        result = self.run_like_function([
            {"status": 200, "body": ""},
            {"status": 403, "body": captcha},
            {"status": 403, "body": lookalike},
            {"status": 403, "body": "{}"},
            {"status": 500, "body": captcha},
        ])

        self.assertEqual(result["outcomes"], ["ok", "bot-check", "failed", "failed", "failed"])
        self.assertEqual(result["requests"][0], {
            "url": "https://api-v2.soundcloud.com/users/42/track_likes/99",
            "method": "PUT",
            "auth": "OAuth token",
        })

    def test_page_like_refuses_methods_other_than_like_and_unlike(self):
        result = self.run_like_function([{"status": 200, "body": ""}], method="POST")

        self.assertEqual(result["outcomes"], ["failed"])
        self.assertEqual(result["requests"], [])

    def test_api_requests_go_only_to_soundcloud_without_proxies_or_redirects(self):
        app = load_module()
        seen = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b'{"id": 1}'

        def fake_open(request, timeout):
            seen.append(request)
            return Response()

        with mock.patch.object(app._api_opener, "open", side_effect=fake_open):
            self.assertEqual(app.soundcloud_api_request("GET", "/me", "token"), {"id": 1})
            with self.assertRaises(ValueError):
                app.soundcloud_api_request("GET", "@evil.example/me", "token")

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].full_url, "https://api-v2.soundcloud.com/me")
        self.assertEqual(seen[0].get_header("Authorization"), "OAuth token")
        handler_types = {type(handler) for handler in app._api_opener.handlers}
        self.assertIn(app._NoMediaRedirect, handler_types)


def fake_gst():
    """A GStreamer stand-in with pipelines whose bus messages the test delivers by hand."""
    class Bus:
        def __init__(self): self.handler = None; self.disconnected = False
        def add_signal_watch(self): pass
        def connect(self, _name, callback, pipeline, generation):
            self.handler = (callback, pipeline, generation); return 1
        def disconnect(self, _handler): self.disconnected = True
        def remove_signal_watch(self): pass

    class Source:
        def __init__(self): self.emitted = []; self.on_emit = None
        def emit(self, name, *_args):
            self.emitted.append(name)
            if self.on_emit: self.on_emit(name)
            return Gst.FlowReturn.OK

    class Volume:
        def __init__(self): self.properties = {}
        def set_property(self, name, value): self.properties[name] = value

    class Pipeline:
        def __init__(self, name=""):
            self.name = name; self.bus = Bus(); self.states = []; self.seeks = []
            self.position = 0; self.seek_result = True; self.source = Source()
            self.volume = Volume(); self.properties = self.volume.properties
        def get_bus(self): return self.bus
        def get_by_name(self, name): return self.volume if name == "volume" else self.source
        def set_property(self, name, value): self.properties[name] = value
        def set_state(self, state): self.states.append(state); return Gst.StateChangeReturn.SUCCESS
        def seek_simple(self, _format, flags, nanoseconds):
            self.seeks.append(nanoseconds); self.seek_flags = flags; return self.seek_result
        def query_position(self, _format): return True, self.position
        def query_duration(self, _format): return True, 200 * Gst.SECOND

    class Factory:
        made = []
        @classmethod
        def make(cls, _kind, name):
            pipeline = Pipeline(name); cls.made.append(pipeline); return pipeline

    class Chunk:
        @staticmethod
        def new_allocate(*_args): return Chunk()
        def fill(self, *_args): pass

    class Gst:
        SECOND = 10**9
        ElementFactory = Factory
        Buffer = Chunk
        class State: NULL = "null"; PLAYING = "playing"; PAUSED = "paused"
        class StateChangeReturn: FAILURE = "failure"; SUCCESS = "success"
        class MessageType: ERROR = "error"; EOS = "eos"; STATE_CHANGED = "state"
        class Format: TIME = "time"
        class SeekFlags: FLUSH = 1; KEY_UNIT = 2; ACCURATE = 4
        class FlowReturn: OK = "ok"
        @staticmethod
        def init(_value): pass
        @staticmethod
        def parse_launch(_description):
            pipeline = Pipeline(); Factory.made.append(pipeline); return pipeline

    return Gst


def deliver(player, kind, state=None, error=None):
    """Send the current pipeline's bus a message, as GStreamer would.

    `error` is (domain, code, message, debug) for an ERROR message."""
    pipeline = player.current
    callback, _pipeline, generation = pipeline.bus.handler

    class Message:
        type = kind
        src = pipeline
        def parse_state_changed(self): return (None, state, None)
        def parse_error(self):
            if error is None:
                raise TypeError("no details")
            domain, code, text, debug = error
            return type("GError", (), {"domain": domain, "code": code, "message": text})(), debug

    callback(None, Message(), pipeline, generation)


class GstPlaybackEndTest(unittest.TestCase):
    def player(self, **hooks):
        app = load_module()
        Gst = fake_gst()
        queued = []
        player = app.GstPlayback(
            Gst, lambda: None, lambda callback, *args: queued.append((callback, args)), **hooks
        )
        self.addCleanup(player.hls_executor.shutdown, False)
        player.run_scheduled = lambda: [callback(*args) for callback, args in
                                        [queued.pop(0) for _ in range(len(queued))]]
        player.Gst_fake = Gst
        return app, player

    def start(self, player, protocol="", mime_type=""):
        player.begin({"title": "A", "playbackId": "soundcloud:tracks:1"})
        self.assertTrue(player.play("https://x.sndcdn.com/a", {"title": "A"}, protocol, mime_type))

    def test_end_and_error_hooks_run_once_on_the_scheduler(self):
        with self.subTest('a natural end runs the hook once on the scheduler'):
            ended = []
            _app, player = self.player(on_end=lambda: ended.append(1))
            self.start(player)

            deliver(player, "eos")
            self.assertEqual(ended, [])  # scheduled, not called from the bus callback
            player.run_scheduled()

            self.assertEqual(ended, [1])
            self.assertEqual(player.state, "paused")
        with self.subTest('the end of a hls stream runs the hook too'):
            ended = []
            app, player = self.player(on_end=lambda: ended.append(1))
            self.start(player, "hls", "audio/mpeg")
            source = player.current.source
            source.on_emit = lambda name: deliver(player, "eos") if name == "end-of-stream" else None

            with mock.patch.object(app, "hls_segment_list", return_value=[("https://x.sndcdn.com/s", 10.0)]), \
                    mock.patch.object(app, "fetch_private_media", return_value=b"data"):
                player._feed_hls(player.generation, source, "https://x.sndcdn.com/a", time.monotonic() + 5)
            player.run_scheduled()

            self.assertEqual(source.emitted, ["push-buffer", "end-of-stream"])
            self.assertEqual(ended, [1])
        with self.subTest('a failure runs the error hook but not the end hook'):
            ended, failed = [], []
            _app, player = self.player(on_end=lambda: ended.append(1), on_error=lambda: failed.append(1))
            self.start(player)

            deliver(player, "error")
            player.run_scheduled()

            self.assertEqual((ended, failed), ([], [1]))
            self.assertEqual(player.state, "error")

    def test_stale_ends_and_failures_are_dropped(self):
        with self.subTest('a user stop does not run the hook'):
            ended, failed = [], []
            _app, player = self.player(on_end=lambda: ended.append(1), on_error=lambda: failed.append(1))
            self.start(player)
            pipeline = player.current

            player.stop()
            player.run_scheduled()

            self.assertEqual((ended, failed), ([], []))
            # a late end from the stopped pipeline is ignored as well
            player._message(None, type("M", (), {"type": "eos", "src": pipeline})(), pipeline, 0)
            player.run_scheduled()
            self.assertEqual(ended, [])
        with self.subTest('an end reported before a new track began is dropped'):
            ended = []
            _app, player = self.player(on_end=lambda: ended.append(1))
            self.start(player)
            deliver(player, "eos")

            player.begin({"title": "B"})  # the user picked another track first
            player.run_scheduled()

            self.assertEqual(ended, [])
        with self.subTest('a failure overtaken by a new track is dropped'):
            failed = []
            _app, player = self.player(on_error=lambda: failed.append(1))
            self.start(player)
            deliver(player, "error")

            player.begin({"title": "B"})
            player.run_scheduled()

            self.assertEqual(failed, [])

    def test_resume_seek_rules(self):
        with self.subTest('a resume position is sought once the pipeline plays'):
            _app, player = self.player()
            self.start(player)
            player.seek_when_playing(61.0)

            deliver(player, "state", "paused")
            self.assertEqual(player.current.seeks, [])
            deliver(player, "state", "playing")

            self.assertEqual(player.current.seeks, [61 * 10**9])
            self.assertEqual(player.pending_seek, 0.0)
        with self.subTest('a refused resume seek is retried a bounded number of times'):
            _app, player = self.player()
            self.start(player)
            player.current.seek_result = False
            player.seek_when_playing(61.0)
            deliver(player, "state", "playing")

            for _ in range(40):
                player.status()

            self.assertEqual(len(player.current.seeks), 20)
            self.assertEqual(player.pending_seek, 0.0)
        with self.subTest('a new selection cancels a pending resume seek'):
            _app, player = self.player()
            self.start(player)
            player.seek_when_playing(61.0)

            player.begin({"title": "B"})

            self.assertEqual(player.pending_seek, 0.0)

    def test_a_seek_is_accurate_not_snapped_to_a_key_unit(self):
        _app, player = self.player()
        self.start(player)

        player.seek_seconds(61.0)

        flags = player.current.seek_flags
        self.assertTrue(flags & 1 and flags & 4)  # FLUSH | ACCURATE
        self.assertFalse(flags & 2)  # not KEY_UNIT: hlsdemux would snap to the 10 s fragment

    def test_seek_by_rules(self):
        with self.subTest('seek by moves relative to the position'):
            _app, player = self.player()
            self.start(player)
            deliver(player, "state", "playing")
            player.current.position = 30 * 10**9

            self.assertTrue(player.seek_by(5))
            player.last_seek = None  # the pipeline has caught up
            player.current.position = 35 * 10**9
            self.assertTrue(player.seek_by(-5))

            self.assertEqual(player.current.seeks[-2:], [35 * 10**9, 30 * 10**9])
        with self.subTest('seek by works while paused'):
            _app, player = self.player()
            self.start(player)
            deliver(player, "state", "playing")
            deliver(player, "state", "paused")
            player.current.position = 30 * 10**9

            self.assertTrue(player.seek_by(5))

            self.assertEqual(player.current.seeks, [35 * 10**9])
        with self.subTest('seek by is clamped to the track'):
            _app, player = self.player()
            self.start(player)
            deliver(player, "state", "playing")
            player.current.position = 3 * 10**9
            player.seek_by(-5)
            player.last_seek = None
            player.current.position = 199 * 10**9
            player.seek_by(5)

            self.assertEqual(player.current.seeks, [0, 199 * 10**9])  # duration 200 s, margin 1 s
        with self.subTest('seek by with nothing loaded is refused'):
            _app, player = self.player()

            self.assertFalse(player.seek_by(5))
        with self.subTest('a refused seek by reports failure without reloading'):
            _app, player = self.player()
            self.start(player)
            deliver(player, "state", "playing")
            player.current.seek_result = False
            pipeline = player.current

            self.assertFalse(player.seek_by(5))
            self.assertIs(player.current, pipeline)
            self.assertEqual(player.state, "playing")

    def test_quick_repeated_seek_by_builds_on_the_last_target(self):
        app, player = self.player()
        self.start(player)
        deliver(player, "state", "playing")
        player.current.position = 30 * 10**9
        clock = [100.0]
        with mock.patch.object(app.time, "monotonic", lambda: clock[0]):
            player.seek_by(5)
            clock[0] += 0.2  # the pipeline position has not caught up yet
            player.seek_by(5)
            clock[0] += 5.0  # long after: the real position wins again
            player.current.position = 50 * 10**9
            player.seek_by(5)

        seeks = [s / 10**9 for s in player.current.seeks]
        self.assertAlmostEqual(seeks[0], 35)
        self.assertAlmostEqual(seeks[1], 40.2)
        self.assertEqual(seeks[2], 55)

    def test_a_seek_does_not_flash_paused_while_playing(self):
        _app, player = self.player()
        self.start(player)
        deliver(player, "state", "playing")

        player.seek_by(5)
        deliver(player, "state", "paused")  # playbin3 goes PAUSED while it flushes

        self.assertTrue(player.playing)
        self.assertEqual(player.state, "playing")
        deliver(player, "state", "playing")
        self.assertEqual(player.state, "playing")
        deliver(player, "state", "paused")  # a real pause afterwards still counts
        self.assertEqual(player.state, "paused")

    def test_the_position_does_not_flash_zero_while_a_seek_settles(self):
        app, player = self.player()
        self.start(player)
        deliver(player, "state", "playing")
        clock = [100.0]
        with mock.patch.object(app.time, "monotonic", lambda: clock[0]):
            player.seek_seconds(61.0)
            player.current.position = 0  # the demuxer reports 0 while it flushes
            self.assertAlmostEqual(player.status()["position"], 61.0)
            clock[0] += 0.5
            self.assertAlmostEqual(player.status()["position"], 61.5)
            player.current.position = 61.6 * 10**9  # settled: the real position wins
            self.assertAlmostEqual(player.status()["position"], 61.6)
            clock[0] += 3.0
            player.current.position = 70 * 10**9
            self.assertAlmostEqual(player.status()["position"], 70.0)

    def test_toggle_during_a_seek_still_pauses(self):
        _app, player = self.player()
        self.start(player)
        deliver(player, "state", "playing")
        player.seek_by(5)

        player.toggle()
        deliver(player, "state", "paused")

        self.assertEqual(player.state, "paused")

    def hls_segments(self, app, count=6):
        return [(f"https://x.sndcdn.com/s{i}", 10.0) for i in range(count)]

    def test_a_hls_mp3_seek_restarts_the_feed_at_the_segment_and_skips_ahead(self):
        app, player = self.player()
        self.start(player, "hls", "audio/mpeg")
        first = player.current
        player.hls_cache = ("https://x.sndcdn.com/a", self.hls_segments(app))
        pushed = []
        with mock.patch.object(app, "fetch_private_media", side_effect=lambda url, *a, **k: pushed.append(url) or b"x" * 160000), \
                mock.patch.object(player.hls_executor, "submit", lambda fn, *a: fn(*a)):
            self.assertTrue(player.seek_seconds(34.0))

        self.assertIsNot(player.current, first)
        self.assertEqual(pushed[0], "https://x.sndcdn.com/s3")  # the segment holding 34 s
        self.assertEqual(pushed[-1], "https://x.sndcdn.com/s5")
        self.assertEqual(player.hls_base, 30.0)
        self.assertEqual(player.hls_skip, 4.0)
        player.current.position = 2 * 10**9
        self.assertAlmostEqual(player.status()["position"], 36.0)  # base + skip + pipeline position

    def test_rapid_hls_seeks_wait_for_a_stuck_feeder_and_report_a_failure(self):
        app, player = self.player()
        errors = []
        player.on_error = lambda: errors.append(player.error)
        stuck = threading.Event()
        release = threading.Event()

        def slow(*_args, **_kwargs):  # a network read that does not notice the cancel
            stuck.set()
            release.wait(5)
            raise OSError("cancelled")

        self.addCleanup(release.set)
        segments = self.hls_segments(app)
        with mock.patch.object(app, "fetch_private_media", slow), \
                mock.patch.object(app, "hls_segment_list", return_value=segments):
            self.start(player, "hls", "audio/mpeg")  # feeder 1 is stuck in a read
            self.assertTrue(stuck.wait(2))
            player.hls_cache = ("https://x.sndcdn.com/a", segments)
            self.assertTrue(player.seek_seconds(34.0))  # feeder 2 takes the last slot, also stuck
            with mock.patch.object(app, "HLS_SEEK_WAIT_SECONDS", 0.05):
                self.assertFalse(player.seek_seconds(44.0))  # no slot frees up in time
            self.assertEqual((player.state, player.error), ("error", "SoundCloud playback is busy"))
            player.run_scheduled()
            self.assertEqual(errors, ["SoundCloud playback is busy"])  # reported, not silent
            release.set()

    def test_a_hls_seek_waits_briefly_for_the_old_feeder_to_stop(self):
        app, player = self.player()
        self.start(player, "hls", "audio/mpeg")
        player.hls_cache = ("https://x.sndcdn.com/a", self.hls_segments(app))
        self.assertTrue(player.hls_slots.acquire(blocking=False))  # a second feeder, still finishing
        threading.Timer(0.1, player.hls_slots.release).start()
        with mock.patch.object(player.hls_executor, "submit", lambda fn, *a: None):
            self.assertTrue(player.seek_seconds(34.0))

    def test_a_hls_mp3_seek_before_the_playlist_is_known_is_refused(self):
        _app, player = self.player()
        self.start(player, "hls", "audio/mpeg")

        self.assertFalse(player.seek_seconds(34.0))


class SeekByCommandTest(unittest.TestCase):
    def test_target_is_clamped_to_zero_and_the_end_margin(self):
        app = load_module()
        self.assertEqual(app.seek_by_target(30, 5, 200), 35)
        self.assertEqual(app.seek_by_target(3, -5, 200), 0)
        self.assertEqual(app.seek_by_target(198, 5, 200), 199)
        self.assertEqual(app.seek_by_target(0.2, 5, 0.5), 0)  # shorter than the margin
        self.assertEqual(app.seek_by_target(30, 5, 0), 35)  # unknown duration

    def test_the_cli_refuses_a_non_finite_seek_without_asking_the_backend(self):
        app = load_module()
        for value in ("nan", "inf", "1e400"):
            with self.subTest(value=value):
                stdout = io.StringIO()
                with mock.patch.object(app, "request_backend") as request, \
                        contextlib.redirect_stdout(stdout):
                    code = app.main(["seek", value])

                request.assert_not_called()
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(stdout.getvalue())["ok"])
        with mock.patch.object(app, "request_backend", return_value={"ok": True}) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(["seek", "0.5"]), 0)
        request.assert_called_once_with("seek:0.5")

    def test_command_parsing(self):
        app = load_module()
        self.assertEqual(app.seek_by_from_command("seek-by:5"), 5.0)
        self.assertEqual(app.seek_by_from_command("seek-by:-5"), -5.0)
        self.assertEqual(app.seek_by_from_command("seek-by:+2.5"), 2.5)
        for bad in ("seek-by:", "seek-by:abc", "seek-by:nan", "seek-by:inf", "seek-by:99999999"):
            self.assertIsNone(app.seek_by_from_command(bad), bad)

    def test_hls_playlist_segments_carry_their_durations(self):
        app = load_module()
        playlist = (b"#EXTM3U\n#EXTINF:10.0,\nseg0.mp3\n#EXTINF:9.5,\nseg1.mp3\n#EXT-X-ENDLIST\n")
        with mock.patch.object(app, "fetch_private_media", return_value=playlist):
            segments = app.hls_segment_list("https://x.sndcdn.com/dir/p.m3u8")
        self.assertEqual(segments, [("https://x.sndcdn.com/dir/seg0.mp3", 10.0),
                                    ("https://x.sndcdn.com/dir/seg1.mp3", 9.5)])


class FakeGLib:
    def __init__(self): self.idle = []; self.timers = []; self.delays = []
    def set_application_name(self, _name): pass
    def set_prgname(self, _name): pass
    def idle_add(self, callback, *args): self.idle.append((callback, args)); return 1
    def timeout_add(self, interval, callback, *args):
        self.timers.append((callback, args)); self.delays.append(interval); return 1
    timeout_add_seconds = timeout_add
    def run_idle(self):
        while self.idle:
            callback, args = self.idle.pop(0)
            callback(*args)


class FakeConnection:
    def __init__(self): self.sent = []
    def send(self, payload): self.sent.append(payload); return True
    @property
    def reply(self): return self.sent[-1]


class FakeWebview:
    def __init__(self, uri="https://soundcloud.com/you/likes"):
        self.uri = uri; self.loading = False; self.loaded = []
    def get_uri(self): return self.uri
    def is_loading(self): return self.loading
    def load_uri(self, uri): self.loaded.append(uri); self.uri = uri


class PageMessage:
    def __init__(self, payload): self.payload = payload
    def get_js_value(self): return self
    def to_json(self, _indent): return json.dumps(json.dumps(self.payload))


class QueuePlaybackTest(unittest.TestCase):
    """Queue commands, auto-advance and resume, through the real application class.

    GTK, WebKit and GStreamer are fakes and the profile is a temporary directory.
    """

    def row(self, number, **overrides):
        return {**PlayQueueTest.row(None, number), **overrides}

    def build(self, saved=None, saved_file=None, settings=None):
        self.app = load_module()
        self.glib, self.gst = FakeGLib(), fake_gst()
        self.evaluated = []
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = directory / "data" / "omarchy-soundcloud"
        data.mkdir(parents=True, mode=0o700)
        self.queue_file = data / "queue.json"
        if saved is not None:
            queue, position_ms = saved
            fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY)
            self.app.QueueStore(fd).save(queue, position_ms)
            os.close(fd)
        if saved_file is not None:
            self.queue_file.write_bytes(saved_file)
            os.chmod(self.queue_file, 0o600)
        Gtk = type("Gtk", (), {"Application": type("Application", (), {
            "__init__": lambda self, **_kw: None, "hold": lambda self: None,
            "quit": lambda self: None, "do_shutdown": lambda self: None,
        })})
        Gio = type("Gio", (), {"ApplicationFlags": type("F", (), {"NON_UNIQUE": 0})})
        patches = (
            mock.patch.object(self.app, "_load_gi",
                              lambda: (Gio, self.glib, self.gst, Gtk, None, None)),
            mock.patch.object(self.app, "profile_paths", lambda env=None: self.app.ProfilePaths(
                data, directory / "cache", data / "cookies.sqlite")),
            mock.patch.object(self.app, "runtime_socket_path",
                              lambda env=None: directory / "run" / "omarchy-soundcloud" / "control.sock"),
            mock.patch.object(self.app, "prepare_runtime_socket_path", lambda path: None),
        )
        for patch in patches:
            self.enterContext(patch)
        if settings is not None:
            fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY)
            self.app.SettingsStore(fd).save(settings)
            os.close(fd)
        self.application = self.app.build_application()
        self.application.run_in_thread = lambda target: target()
        self.addCleanup(self.application.playback.close)
        self.addCleanup(self.application.artwork_cache.close)
        self.addCleanup(self.application.list_artwork_cache.close)
        self.application.webview = FakeWebview()
        self.application._evaluate = lambda script, callback: self.evaluated.append((script, callback))
        self.application.page_status.seeded = True
        return self.application

    def command(self, text):
        connection = FakeConnection()
        self.application.dispatch_slots.acquire()
        self.application._handle_command(text, connection, 7)
        return connection

    def fill(self, tab="home", count=4):
        rows = [self.row(n) for n in range(1, count + 1)]
        self.application.track_cache[tab] = rows
        for row in rows:
            self.application.playback_tracks[row["playbackId"]] = row
        return rows

    def started_id(self):
        script = self.evaluated[-1][0]
        return script.split("soundcloud:tracks:")[1].split('"')[0]

    def resolve(self, ok=True):
        """Finish the page's work for the track that began last."""
        application = self.application
        _script, callback = self.evaluated[-1]
        callback(True)
        payload = {"requestId": application.selection_generation, "ok": ok,
                   "streamUrl": "https://cf-media.sndcdn.com/a.mp3", "protocol": "progressive",
                   "mimeType": "audio/mpeg", "waveform": [],
                   "track": {key: value for key, value in application.playback.metadata.items()
                             if key != "waveform"}, **({} if ok else {"error": "gone"})}
        application._playback_message_received(None, PageMessage(payload))
        if ok:
            deliver(application.playback, "state", self.gst.State.PLAYING)

    def end_track(self):
        deliver(self.application.playback, "eos")
        self.glib.run_idle()

    def queue_ids(self):
        return [row["playbackId"].rpartition(":")[2] for row in self.application.queue.items]

    # commands

    def test_play_from_replaces_the_queue_with_a_snapshot_of_the_tab(self):
        self.build()
        self.fill("home", 4)
        self.fill("likes", 3)

        reply = self.command("play-from:home:soundcloud:tracks:3")
        self.resolve()

        self.assertEqual(self.queue_ids(), ["1", "2", "3", "4"])
        self.assertEqual((self.application.queue.cursor, self.application.queue.source), (2, "home"))
        self.assertEqual(self.started_id(), "3")
        self.assertTrue(reply.reply["ok"])
        self.assertEqual(self.application.playback.state, "playing")

    def test_play_from_an_unknown_track_changes_nothing(self):
        self.build()
        self.fill("home", 2)

        reply = self.command("play-from:likes:soundcloud:tracks:1")

        self.assertFalse(reply.reply["ok"])
        self.assertEqual((self.application.queue.length, self.evaluated), (0, []))

    def test_a_bare_play_is_a_queue_of_one(self):
        self.build()
        self.fill("home", 4)

        self.command("play:soundcloud:tracks:2")

        self.assertEqual(self.queue_ids(), ["2"])
        self.assertEqual(self.started_id(), "2")

    def test_next_and_previous_move_through_the_queue_without_touching_the_page_player(self):
        self.build()
        self.fill("home", 4)
        self.command("play-from:home:soundcloud:tracks:2")
        self.resolve()
        stopped = []
        self.application.playback.stop = lambda *a: stopped.append(a)

        self.command("next")
        self.assertEqual(self.started_id(), "3")
        self.resolve()
        self.command("previous")  # right after the start, so it goes back a track
        self.assertEqual(self.started_id(), "2")

        self.assertEqual(stopped, [])
        self.assertFalse(any("skipControl" in script for script, _callback in self.evaluated))

    def test_previous_restarts_the_track_after_the_threshold(self):
        self.build()
        self.fill("home", 3)
        self.command("play-from:home:soundcloud:tracks:2")
        self.resolve()
        pipeline = self.application.playback.current
        started = len(self.evaluated)

        pipeline.position = 10 * 10**9
        reply = self.command("previous")

        self.assertTrue(reply.reply["ok"])
        self.assertEqual(pipeline.seeks, [0])
        self.assertEqual(len(self.evaluated), started)
        self.assertEqual(self.application.queue.cursor, 1)

    def test_next_and_previous_with_nothing_to_go_to_return_not_ok(self):
        self.build()
        self.assertFalse(self.command("next").reply["ok"])
        self.assertFalse(self.command("previous").reply["ok"])

        self.fill("home", 2)
        self.command("play-from:home:soundcloud:tracks:2")
        self.resolve()
        before = (len(self.evaluated), self.application.playback.state)
        self.assertFalse(self.command("next").reply["ok"])  # the last track
        self.assertEqual((len(self.evaluated), self.application.playback.state), before)
        self.assertEqual(self.application.queue.cursor, 1)

    def test_repeat_shuffle_and_queue_next_change_the_queue_and_show_in_the_status(self):
        self.build()
        self.fill("home", 4)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()

        self.assertEqual(self.command("repeat").reply["repeat"], "one")
        self.assertEqual(self.command("repeat:all").reply["repeat"], "all")
        self.assertFalse(self.command("repeat:sometimes").reply["ok"])
        self.assertTrue(self.command("shuffle").reply["shuffle"])
        self.assertFalse(self.command("shuffle:off").reply["shuffle"])
        self.assertTrue(self.command("queue-next:soundcloud:tracks:4").reply["ok"])
        self.assertFalse(self.command("queue-next:soundcloud:tracks:99").reply["ok"])

        status = self.application.build_status()
        self.assertEqual((status["repeat"], status["shuffle"], status["length"]), ("all", False, 4))
        self.command("next")
        self.assertEqual(self.started_id(), "4")  # "play next" jumps the list order

    def test_a_queue_start_keeps_the_hidden_page_on_home_or_feed(self):
        self.build()
        self.fill("likes", 3)
        self.application.webview.loading = True

        self.command("play-from:likes:soundcloud:tracks:1")
        self.assertEqual(self.application.webview.loaded, ["https://soundcloud.com/discover"])
        self.assertEqual(self.evaluated, [])  # waits for the page

        self.application.webview.loading = False
        self.glib.timers[-1][0]()
        self.assertEqual(self.started_id(), "1")

    # auto-advance

    def test_a_track_that_ends_starts_the_next_one_and_records_it_in_history(self):
        self.build()
        self.fill("home", 3)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()

        self.end_track()
        self.assertEqual(self.started_id(), "2")
        self.assertEqual(self.application.queue.cursor, 1)
        self.resolve()

        self.assertEqual([row["playbackId"] for row in self.application.track_cache["plays"]],
                         ["soundcloud:tracks:2", "soundcloud:tracks:1"])

    def test_auto_advance_brings_the_page_back_to_home_or_feed(self):
        self.build()
        self.fill("home", 2)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()
        self.application.webview.uri = "https://soundcloud.com/you/likes"

        self.end_track()

        self.assertEqual(self.application.webview.loaded[-1], "https://soundcloud.com/discover")
        self.assertEqual(self.started_id(), "2")

    def test_the_end_of_the_queue_starts_nothing(self):
        self.build()
        self.fill("home", 2)
        self.command("play-from:home:soundcloud:tracks:2")
        self.resolve()
        started = len(self.evaluated)

        self.end_track()

        self.assertEqual(len(self.evaluated), started)

    def test_repeat_one_replays_the_track_that_ended_and_repeat_all_wraps(self):
        self.build()
        self.fill("home", 2)
        self.command("play-from:home:soundcloud:tracks:2")
        self.resolve()
        self.command("repeat:one")
        self.end_track()
        self.assertEqual(self.started_id(), "2")
        self.resolve()

        self.command("repeat:all")
        self.end_track()

        self.assertEqual(self.started_id(), "1")

    def test_next_pressed_right_after_an_end_does_not_advance_twice(self):
        self.build()
        self.fill("home", 5)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()

        deliver(self.application.playback, "eos")  # reported, not yet handled
        self.command("next")                       # the user beat it to the main loop
        self.glib.run_idle()

        self.assertEqual(self.application.queue.cursor, 1)
        self.assertEqual(self.started_id(), "2")

    def test_an_advance_that_is_overtaken_by_a_newer_selection_never_starts(self):
        self.build()
        self.fill("home", 5)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()
        self.application.webview.loading = True  # the advance has to wait for the page
        self.end_track()
        started = len(self.evaluated)
        waiting = self.glib.timers[-1][0]

        self.application.webview.loading = False
        self.command("play-from:home:soundcloud:tracks:4")
        waiting()                                  # the old advance wakes up late

        self.assertEqual((len(self.evaluated), self.started_id()), (started + 1, "4"))
        self.application.selection_generation += 1  # as a resolve timeout does
        queued = self.application.queue.cursor
        self.application._track_ended()
        self.assertEqual((len(self.evaluated), self.application.queue.cursor), (started + 1, queued))

    def test_next_during_a_resolve_moves_one_step_from_where_the_queue_is(self):
        self.build()
        self.fill("home", 5)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()
        self.end_track()  # track 2 is resolving

        self.command("next")
        self.glib.run_idle()

        self.assertEqual(self.application.queue.cursor, 2)
        self.assertEqual(self.started_id(), "3")

    # dead tracks

    def test_an_automatic_start_that_fails_at_once_is_skipped_with_a_notice(self):
        self.build()
        self.fill("home", 5)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()
        self.end_track()                      # track 2 starts by itself

        self.resolve(ok=False)                # and fails at once
        self.glib.run_idle()

        self.assertEqual(self.started_id(), "3")
        self.assertEqual(self.queue_ids(), ["1", "3", "4", "5"])
        self.assertEqual(self.application.build_status()["error"], "Skipped 1 track that can't play")

    def test_a_queue_where_nothing_plays_stops_instead_of_looping(self):
        self.build()
        self.fill("home", 3)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()
        self.command("repeat:all")
        self.end_track()

        for _ in range(20):
            self.resolve(ok=False)
            self.glib.run_idle()
            if self.application.playback.state == "error":
                break

        self.assertEqual(self.application.playback.state, "error")
        self.assertLessEqual(len(self.evaluated), 1 + 3 + 1)

    def test_a_track_the_user_picked_keeps_its_error_and_stays_in_the_queue(self):
        self.build()
        self.fill("home", 3)
        started = self.command("play-from:home:soundcloud:tracks:2")
        self.resolve(ok=False)
        self.glib.run_idle()

        self.assertEqual(self.application.playback.state, "error")
        self.assertEqual(self.queue_ids(), ["1", "2", "3"])
        self.assertEqual(len(self.evaluated), 1)
        self.assertNotIn("Skipped", self.application.build_status().get("error", ""))
        self.assertTrue(started.reply["ok"])

    def test_a_late_error_cannot_skip_a_track_the_user_started_since(self):
        self.build()
        self.fill("home", 5)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()
        self.end_track()                      # track 2 starts by itself
        self.resolve(ok=False)                # fails; the skip is queued, not yet run

        self.command("play-from:home:soundcloud:tracks:5")
        self.glib.run_idle()

        self.assertEqual(self.started_id(), "5")
        self.assertEqual(self.queue_ids(), ["1", "2", "3", "4", "5"])

    # save and resume

    def test_structural_changes_are_saved_once_the_loop_is_idle(self):
        self.build()
        self.fill("home", 3)

        self.command("play-from:home:soundcloud:tracks:2")
        self.command("repeat:one")
        self.assertFalse(self.queue_file.exists())  # debounced to the main loop
        self.glib.run_idle()

        saved, position_ms = self.app.QueueStore(os.open(self.queue_file.parent, os.O_RDONLY)).load()
        self.assertEqual((saved.cursor, saved.repeat, saved.length), (1, "one", 3))
        self.assertEqual(stat.S_IMODE(self.queue_file.stat().st_mode), 0o600)

    def test_the_position_is_saved_while_playing_and_when_pausing_and_on_shutdown(self):
        self.build()
        self.fill("home", 3)
        self.command("play-from:home:soundcloud:tracks:2")
        self.resolve()
        pipeline = self.application.playback.current
        directory = os.open(self.queue_file.parent, os.O_RDONLY)
        self.addCleanup(os.close, directory)

        def saved_position():
            return self.app.QueueStore(directory).load()[1]

        pipeline.position = 42 * 10**9
        self.application._save_position_tick()
        self.assertEqual(saved_position(), 42_000)

        pipeline.position = 50 * 10**9
        self.command("play-pause")
        self.assertEqual(saved_position(), 50_000)

        pipeline.position = 55 * 10**9
        self.application._save_queue()  # what do_shutdown does first
        self.assertEqual(saved_position(), 55_000)

    def test_a_saved_queue_comes_back_paused_with_nothing_playing(self):
        queue = load_module().PlayQueue(repeat="all")
        queue.replace([self.row(n, durationMs=200_000) for n in (1, 2, 3)], 1, "likes")
        self.build(saved=(queue, 61_000))

        status = self.application.build_status()

        self.assertEqual((status["playbackState"], status["playing"], status["position"]),
                         ("paused", False, 61.0))
        self.assertEqual((status["title"], status["duration"]), ("Song 2", 200.0))
        self.assertEqual((status["cursor"], status["length"], status["repeat"]), (1, 3, "all"))
        self.assertEqual((self.gst.ElementFactory.made, self.evaluated), ([], []))

    def test_play_pause_on_a_restored_track_starts_it_and_seeks_to_the_saved_position(self):
        queue = load_module().PlayQueue()
        queue.replace([self.row(n, durationMs=200_000) for n in (1, 2, 3)], 1, "likes")
        self.build(saved=(queue, 61_000))

        reply = self.command("play-pause")
        self.assertEqual(self.started_id(), "2")
        self.resolve()

        self.assertEqual(self.application.playback.current.seeks, [61 * 10**9])
        self.assertIsNone(self.application.restored_position_ms)
        self.assertTrue(reply.reply["ok"])

    def test_next_from_a_restored_state_plays_the_following_track(self):
        queue = load_module().PlayQueue()
        queue.replace([self.row(n) for n in (1, 2, 3)], 0, "feed")
        self.build(saved=(queue, 10_000))

        self.command("next")

        self.assertEqual(self.started_id(), "2")
        self.assertIsNone(self.application.restored_position_ms)

    def test_a_corrupt_or_missing_queue_file_gives_an_empty_idle_queue(self):
        for content in (None, b"{broken", b'{"v":1,"items":"x"}'):
            with self.subTest(content=content):
                self.build(saved_file=content)
                status = self.application.build_status()
                self.assertEqual((status["length"], status["playbackState"], status["canNext"]),
                                 (0, "idle", False))
                self.assertEqual(status["title"], "")


    # volume, settings

    def play_last_of_two(self):
        self.fill("home", 2)
        self.command("play-from:home:soundcloud:tracks:2")
        self.resolve()

    def test_an_automatic_start_that_cannot_begin_is_skipped_not_stalled(self):
        self.build()
        rows = self.fill("home", 4)
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()
        self.application.queue.items[1]["title"] = ""  # fails validate_tracks_payload at start
        self.application.playback_tracks.pop(rows[1]["playbackId"])

        self.end_track()
        self.glib.run_idle()

        self.assertEqual(self.started_id(), "3")
        self.assertEqual(self.queue_ids(), ["1", "3", "4"])
        self.assertEqual(self.application.build_status()["error"], "Skipped 1 track that can't play")

    # an API reply cut short mid-body must not wedge the worker threads

    def test_picking_an_encrypted_only_track_fails_at_once_with_the_real_reason(self):
        self.build()
        self.fill("home", 3)[1]["encrypted"] = True
        message = "This track is only offered as an encrypted stream the plugin can't play"

        reply = self.command("play-from:home:soundcloud:tracks:2")

        self.assertEqual((reply.reply["ok"], reply.reply["error"]), (False, message))
        self.assertEqual(self.evaluated, [])  # the page was not asked
        self.assertEqual(self.application.playback.state, "error")
        status = self.application.build_status()
        self.assertEqual((status["error"], status["playbackId"]), (message, "soundcloud:tracks:2"))
        self.glib.run_idle()
        self.assertEqual(self.queue_ids(), ["1", "2", "3"])  # a user pick keeps its row

    def test_auto_advance_skips_an_encrypted_only_track(self):
        self.build()
        rows = self.fill("home", 4)
        rows[1]["encrypted"] = True
        self.command("play-from:home:soundcloud:tracks:1")
        self.resolve()

        self.end_track()
        self.glib.run_idle()

        self.assertEqual(self.started_id(), "3")
        self.assertEqual(self.queue_ids(), ["1", "3", "4"])
        self.assertEqual(self.application.build_status()["error"], "Skipped 1 track that can't play")

    # user next and previous skip tracks that can't play

    def play_track(self, number, rows):
        self.command(f"play-from:home:soundcloud:tracks:{number}")
        self.resolve()

    def test_next_skips_encrypted_only_tracks_with_a_notice(self):
        self.build()
        rows = self.fill("home", 5)
        rows[1]["encrypted"] = rows[2]["encrypted"] = True
        self.play_track(1, rows)

        reply = self.command("next")
        self.glib.run_idle()
        self.resolve()

        self.assertEqual((self.started_id(), self.application.queue.cursor), ("4", 3))
        self.assertEqual(self.queue_ids(), ["1", "2", "3", "4", "5"])  # a user step keeps the rows
        self.assertEqual(self.application.build_status()["error"], "Skipped 2 tracks that can't play")
        self.assertEqual(reply.reply["ok"], True)

    def test_previous_skips_encrypted_only_tracks(self):
        self.build()
        rows = self.fill("home", 5)
        rows[2]["encrypted"] = rows[3]["encrypted"] = True
        self.play_track(5, rows)

        self.command("previous")
        self.glib.run_idle()

        self.assertEqual((self.started_id(), self.application.queue.cursor), ("2", 1))
        self.assertEqual(self.application.build_status()["error"], "Skipped 2 tracks that can't play")

    def test_next_with_nothing_playable_left_stays_on_the_current_track_and_says_why(self):
        self.build()
        rows = self.fill("home", 3)
        rows[1]["encrypted"] = rows[2]["encrypted"] = True
        self.play_track(1, rows)
        started = len(self.evaluated)

        reply = self.command("next")
        self.glib.run_idle()

        self.assertEqual((reply.reply["ok"], reply.reply["error"]),
                         (False, "No other track in the queue can play"))
        self.assertEqual((len(self.evaluated), self.application.queue.cursor), (started, 0))
        self.assertEqual(self.application.playback.state, "playing")
        self.assertEqual(self.application.build_status()["error"], "No other track in the queue can play")

    def test_previous_with_nothing_playable_before_stays_on_the_current_track(self):
        self.build()
        rows = self.fill("home", 3)
        rows[0]["encrypted"] = rows[1]["encrypted"] = True
        self.play_track(3, rows)
        started = len(self.evaluated)

        reply = self.command("previous")

        self.assertEqual((reply.reply["ok"], self.application.queue.cursor, len(self.evaluated)),
                         (False, 2, started))
        self.assertEqual(self.application.playback.state, "playing")

    def test_next_onto_a_track_that_fails_at_once_moves_on_in_the_same_direction(self):
        self.build()
        self.fill("home", 4)
        self.play_track(1, None)

        self.command("next")
        self.resolve(ok=False)                # track 2 has a track problem
        self.glib.run_idle()

        self.assertEqual(self.started_id(), "3")
        self.assertEqual(self.queue_ids(), ["1", "2", "3", "4"])
        self.assertEqual(self.application.build_status()["error"], "Skipped 1 track that can't play")

    def test_previous_onto_a_track_that_fails_at_once_keeps_going_back(self):
        self.build()
        self.fill("home", 4)
        self.play_track(4, None)

        self.command("previous")
        self.resolve(ok=False)
        self.glib.run_idle()

        self.assertEqual(self.started_id(), "2")

    def test_a_queue_of_failing_tracks_ends_the_next_chain_instead_of_looping(self):
        self.build()
        self.fill("home", 4)
        self.play_track(1, None)
        self.command("next")
        for _ in range(10):
            before = len(self.evaluated)
            self.resolve(ok=False)
            self.glib.run_idle()
            if len(self.evaluated) == before:
                break

        self.assertLessEqual(len(self.evaluated), 5)
        self.assertEqual(self.application.playback.state, "error")
        self.assertEqual(self.application.notice, "No other track in the queue can play")

    def test_a_track_picked_by_hand_is_not_skipped_when_it_fails(self):
        self.build()
        self.fill("home", 4)
        self.command("play-from:home:soundcloud:tracks:2")
        started = len(self.evaluated)

        self.resolve(ok=False)
        self.glib.run_idle()

        self.assertEqual((len(self.evaluated), self.application.playback.state), (started, "error"))
        self.assertEqual(self.queue_ids(), ["1", "2", "3", "4"])

    def test_the_status_carries_the_playing_tracks_page_link(self):
        self.build()
        self.fill("home", 2)
        self.assertEqual(self.application.build_status()["trackUrl"], "")

        self.command("play-from:home:soundcloud:tracks:2")
        self.resolve()

        self.assertEqual(self.application.build_status()["trackUrl"], "https://soundcloud.com/artist/song-2")

    def test_a_restored_track_has_its_link_too(self):
        queue = load_module().PlayQueue()
        queue.replace([self.row(n) for n in (1, 2)], 1, "likes")
        self.build(saved=(queue, 5_000))

        self.assertEqual(self.application.build_status()["trackUrl"], "https://soundcloud.com/artist/song-2")

    # MPRIS wiring

    # earbud multi-press

    class Invocation:
        def __init__(self): self.result = "pending"
        def return_value(self, _value): self.result = "ok"
        def return_dbus_error(self, name, _message): self.result = name


class SeekCommandTest(unittest.TestCase):
    """The seek and seek-by socket commands, through the real application class."""

    def build(self, settings=None):
        self.app = load_module()
        self.glib, self.gst = FakeGLib(), fake_gst()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = self.data = directory / "data" / "omarchy-soundcloud"
        data.mkdir(parents=True, mode=0o700)
        if settings is not None:
            (data / "settings.json").write_text(json.dumps(settings))
            os.chmod(data / "settings.json", 0o600)
        Gtk = type("Gtk", (), {"Application": type("Application", (), {
            "__init__": lambda self, **_kw: None, "hold": lambda self: None,
            "quit": lambda self: None, "do_shutdown": lambda self: None,
        })})
        Gio = type("Gio", (), {"ApplicationFlags": type("F", (), {"NON_UNIQUE": 0})})
        patches = (
            mock.patch.object(self.app, "_load_gi",
                              lambda: (Gio, self.glib, self.gst, Gtk, None, None)),
            mock.patch.object(self.app, "profile_paths", lambda env=None: self.app.ProfilePaths(
                data, directory / "cache", data / "cookies.sqlite")),
            mock.patch.object(self.app, "runtime_socket_path",
                              lambda env=None: directory / "run" / "omarchy-soundcloud" / "control.sock"),
            mock.patch.object(self.app, "prepare_runtime_socket_path", lambda path: None),
        )
        for patch in patches:
            self.enterContext(patch)
        self.application = self.app.build_application()
        self.addCleanup(self.application.playback.close)
        self.addCleanup(self.application.artwork_cache.close)
        self.addCleanup(self.application.list_artwork_cache.close)
        self.evaluated = []
        self.application._evaluate = lambda script, callback: self.evaluated.append((script, callback))
        return self.application

    def command(self, text):
        connection = FakeConnection()
        self.application.dispatch_slots.acquire()
        self.application._handle_command(text, connection, 7)
        return connection

    def start(self):
        playback = self.application.playback
        playback.begin({"title": "A", "playbackId": "soundcloud:tracks:1"})
        self.assertTrue(playback.play("https://x.sndcdn.com/a", {"title": "A"}))
        return playback.current

    def test_seek_by_command_seeks_relative_to_the_position(self):
        self.build()
        pipeline = self.start()
        pipeline.position = 40 * 10**9

        reply = self.command("seek-by:5")

        self.assertTrue(reply.reply["ok"])
        self.assertEqual(pipeline.seeks[-1], 45 * 10**9)
        self.assertFalse(self.command("seek-by:nope").reply["ok"])
        self.assertFalse(self.command("seek-by:").reply["ok"])

    def test_seek_by_command_with_nothing_loaded_is_not_ok(self):
        self.build()

        self.assertFalse(self.command("seek-by:5").reply["ok"])
        self.assertEqual(self.evaluated, [])

    def test_a_seek_to_a_non_finite_ratio_is_refused(self):
        self.build()
        seeks = self.start().seeks

        for value in ("nan", "inf", "-inf", "1e400"):
            with self.subTest(value=value):
                reply = self.command("seek:" + value).reply
                self.assertEqual((reply["ok"], reply["error"]), (False, "invalid seek position"))
        self.assertEqual(seeks, [])
        self.assertTrue(self.command("seek:0.5").reply["ok"])


class PlayLinkCommandTest(unittest.TestCase):
    """The play-url socket command, through the real application class."""

    build = SeekCommandTest.build
    command = SeekCommandTest.command
    LINK = "https://soundcloud.com/artist/song-50"

    def setUp(self):
        self.build()
        self.application.run_in_thread = lambda target: target()

    def stub_links(self, track="row", error=None):
        """Make the link lookup answer without the network; returns the texts it was asked for."""
        calls = []

        def resolve_link(text):
            calls.append(text)
            if error is not None:
                raise error
            return self.app.track_from_entry(like_item(50)) if track == "row" else track

        self.application.track_likes.resolve_link = resolve_link
        return calls

    def run_link(self, command):
        connection = self.command(command)
        self.glib.run_idle()  # the lookup finished off the main loop and now reports back
        return connection

    def test_a_link_plays_once_the_page_is_on_a_track_source(self):
        calls = self.stub_links()
        page = type("Page", (), {"uri": "https://soundcloud.com/you/likes", "get_uri": lambda s: s.uri,
                                 "is_loading": lambda s: False, "load_uri": lambda s, uri: None})()
        self.application.webview = page

        self.run_link("play-url:" + self.LINK)

        self.assertEqual(calls, [self.LINK])
        self.assertIn("soundcloud:tracks:50", self.application.playback_tracks)
        self.assertEqual(self.evaluated, [])  # waits for the page: nothing is started yet
        self.assertEqual(len(self.glib.timers), 1)
        callback, args = self.glib.timers.pop()
        page.uri = "https://soundcloud.com/discover"
        self.assertFalse(callback(*args))
        self.assertEqual(len(self.evaluated), 1)  # the stream lookup for the track

    def test_the_link_lookup_runs_off_the_main_loop(self):
        self.stub_links()
        started = []
        self.application.run_in_thread = started.append

        connection = self.command("play-url:" + self.LINK)

        self.assertEqual((len(started), connection.sent), (1, []))  # handed to a thread, no reply yet
        started[0]()
        self.glib.run_idle()
        self.assertIn("soundcloud:tracks:50", self.application.playback_tracks)

    def test_link_errors_keep_the_user_facing_messages_in_the_reply(self):
        calls = self.stub_links()
        app = self.app

        for text, message in (
            ("not a link", app.NOT_SOUNDCLOUD_MESSAGE),
            ("https://example.com/a/b", app.NOT_SOUNDCLOUD_MESSAGE),
            ("https://soundcloud.com/artist", app.ARTIST_PAGE_MESSAGE),
            ("https://soundcloud.com/artist/sets/mix", app.PLAYLIST_MESSAGE),
            ("https://soundcloud.com/artist/song/extra", app.NOT_A_TRACK_MESSAGE),
            ("", "Copy a single SoundCloud track link first"),
        ):
            reply = self.run_link("play-url:" + text).reply
            self.assertEqual((reply["ok"], reply["error"]), (False, message), text)
        self.assertEqual(calls, [])  # nothing was looked up

    def test_link_lookup_errors_reach_the_reply(self):
        app = self.app

        for error, message in (
            (app.LinkError(app.PLAYLIST_MESSAGE), app.PLAYLIST_MESSAGE),
            (app.LinkError(app.CANT_PLAY_MESSAGE), app.CANT_PLAY_MESSAGE),
            (PermissionError("no"), "Sign in to SoundCloud first"),
            (OSError("down"), "Couldn't reach SoundCloud. Try again"),
        ):
            self.stub_links(error=error)
            reply = self.run_link("play-url:" + self.LINK).reply
            self.assertEqual((reply["ok"], reply["error"]), (False, message))
        self.stub_links(track=None)
        self.assertEqual(self.run_link("play-url:" + self.LINK).reply["error"], app.CANT_PLAY_MESSAGE)
        self.assertEqual(self.application.playback_tracks, {})

    def test_hostile_links_are_refused_before_any_lookup(self):
        calls = self.stub_links()

        for text in (
            "https%3A%2F%2Fsoundcloud.com%2Fa%2Fb%0Ahttps%3A%2F%2Fevil.example%2Fx",
            "https://soundcloud.com/a/b%0D%0AHost:%20evil",
            "https://user:pw@soundcloud.com/a/b",
            "https://soundcloud.com:8443/a/b",
            "https://soundcloud.com.evil.example/a/b",
            "https://evil.example/https://soundcloud.com/a/b",
            "ftp://soundcloud.com/a/b",
            "javascript:alert(1)",
            "https://soundcloud.com/a/b\x00",
            "https://soundcloud.com/../../etc/passwd",
            "https://on.soundcloud.com/a/b",
            "https://soundcloud.com/" + "a" * 300 + "/b",
        ):
            reply = self.run_link("play-url:" + text).reply
            self.assertFalse(reply["ok"], text)
            self.assertTrue(reply["error"], text)
        self.assertEqual(calls, [])

    def test_an_oversize_link_is_refused(self):
        calls = self.stub_links()

        reply = self.run_link("play-url:https://soundcloud.com/a/" + "b" * 3000).reply

        self.assertEqual((reply["ok"], reply["error"]), (False, "That link is too long"))
        self.assertEqual(calls, [])


class ApiReplyHardeningTest(unittest.TestCase):
    """A cut or garbled API reply is a network error, and a like always gets its answer.

    The application class runs with fake GTK and GStreamer and a temporary profile.
    """

    def build(self):
        self.app = load_module()
        self.glib = FakeGLib()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = directory / "data" / "omarchy-soundcloud"
        data.mkdir(parents=True, mode=0o700)
        Gtk = type("Gtk", (), {"Application": type("Application", (), {
            "__init__": lambda self, **_kw: None, "hold": lambda self: None,
            "quit": lambda self: None, "do_shutdown": lambda self: None,
        })})
        Gio = type("Gio", (), {"ApplicationFlags": type("F", (), {"NON_UNIQUE": 0})})
        Gst = type("Gst", (), {"init": staticmethod(lambda _value: None)})
        patches = (
            mock.patch.object(self.app, "_load_gi", lambda: (Gio, self.glib, Gst, Gtk, None, None)),
            mock.patch.object(self.app, "profile_paths", lambda env=None: self.app.ProfilePaths(
                data, directory / "cache", data / "cookies.sqlite")),
            mock.patch.object(self.app, "runtime_socket_path",
                              lambda env=None: directory / "run" / "omarchy-soundcloud" / "control.sock"),
            mock.patch.object(self.app, "prepare_runtime_socket_path", lambda path: None),
        )
        for patch in patches:
            self.enterContext(patch)
        self.application = self.app.build_application()
        self.application.run_in_thread = lambda target: target()
        self.addCleanup(self.application.playback.close)
        self.addCleanup(self.application.artwork_cache.close)
        self.addCleanup(self.application.list_artwork_cache.close)
        return self.application

    def command(self, text):
        connection = FakeConnection()
        self.application.dispatch_slots.acquire()
        self.application._handle_command(text, connection, 7)
        return connection

    LINK = "https://soundcloud.com/artist/song-50"

    def run_link(self, command):
        connection = self.command(command)
        self.glib.run_idle()  # the lookup finished off the main loop and now reports back
        return connection

    def truncated_api(self):
        """Make every API call except /me fail the way a cut connection does."""
        import http.client

        class Response:
            def __init__(self, body, fail): self.body, self.fail = body, fail
            def __enter__(self): return self
            def __exit__(self, *_exc): return False
            def read(self, _size=-1):
                if self.fail:
                    raise http.client.IncompleteRead(b"{", 100)
                return self.body

        class Opener:
            def open(self, request, timeout=None):
                is_me = request.full_url.endswith("/me")
                return Response(b'{"id": 42}', fail=not is_me)

        self.enterContext(mock.patch.object(self.app, "_api_opener", Opener()))
        self.application.track_likes.load_token = lambda: "token"
        self.application.track_likes.request = self.app.soundcloud_api_request

    def test_a_cut_api_reply_is_an_oserror(self):
        app = load_module()
        import http.client

        class Cut:
            def open(self, _request, timeout=None): raise http.client.CannotSendRequest()

        with mock.patch.object(app, "_api_opener", Cut()), self.assertRaises(OSError):
            app.soundcloud_api_request("GET", "/me", "token")

    def test_a_cut_reply_still_answers_a_like_and_frees_the_slot(self):
        self.build()
        self.truncated_api()
        free = self.application.dispatch_slots._value

        reply = self.command("like:soundcloud:tracks:5")
        self.glib.run_idle()

        self.assertEqual((reply.reply["ok"], reply.reply["error"]),
                         (False, "SoundCloud could not update this like"))
        self.assertEqual(self.application.dispatch_slots._value, free)

    def test_a_cut_reply_still_answers_a_pasted_link_and_frees_the_slot(self):
        self.build()
        self.truncated_api()
        free = self.application.dispatch_slots._value

        reply = self.run_link("play-url:" + self.LINK).reply

        self.assertEqual((reply["ok"], reply["error"]), (False, "Couldn't reach SoundCloud. Try again"))
        self.assertEqual(self.application.dispatch_slots._value, free)

    def test_an_unexpected_error_in_a_like_or_link_thread_still_replies_and_frees_the_slot(self):
        self.build()
        free = self.application.dispatch_slots._value
        self.application.track_likes.set_liked = mock.Mock(side_effect=RuntimeError("boom"))
        self.application.track_likes.resolve_link = mock.Mock(side_effect=RuntimeError("boom"))

        like = self.command("like:soundcloud:tracks:5")
        link = self.run_link("play-url:" + self.LINK)
        self.glib.run_idle()

        self.assertFalse(like.reply["ok"])
        self.assertFalse(link.reply["ok"])
        self.assertEqual(self.application.dispatch_slots._value, free)


class CheckResultTest(unittest.TestCase):
    def test_dependency_check_is_machine_readable(self):
        app = load_module()

        result = app.dependency_check()

        self.assertEqual(result["python_gobject"], True)
        self.assertEqual(result["gtk"], "3.0")
        self.assertEqual(result["webkit2"], "4.1")
        self.assertEqual(result["gstreamer_autoaudiosink"], True)
        self.assertEqual(result["gstreamer_aac_decoder"], True)
        self.assertEqual(result["gstreamer_playback_elements"], True)
        self.assertIn("ok", result)

    def test_missing_gstreamer_elements_name_every_package_to_install(self):
        app = load_module()
        stock_omarchy = {"playbin3", "appsrc", "audioconvert", "audioresample"}

        packages = app.missing_gstreamer_packages(lambda name: name in stock_omarchy)

        self.assertEqual(packages, ["gst-plugins-good", "gst-libav"])

    def test_missing_aac_decoder_alone_names_only_gst_libav(self):
        app = load_module()

        packages = app.missing_gstreamer_packages(lambda name: name != "avdec_aac")

        self.assertEqual(packages, ["gst-libav"])

    def test_install_hint_is_a_command_the_user_can_run(self):
        app = load_module()

        hint = app.install_hint(["gst-plugins-good", "gst-libav"])

        self.assertIn("omarchy pkg add gst-plugins-good gst-libav", hint)


class LaunchBackendTest(unittest.TestCase):
    missing = {"ok": False, "error": "Missing gst-libav. Run: omarchy pkg add gst-libav"}

    def test_missing_dependencies_are_reported_instead_of_starting_a_doomed_backend(self):
        app = load_module()
        with mock.patch.object(app, "request_backend", return_value=None), \
                mock.patch.object(app, "dependency_check", return_value=self.missing), \
                mock.patch.object(app.subprocess, "Popen") as popen:
            result = app.launch_backend(show=False)

        popen.assert_not_called()
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"], self.missing["error"])

    def test_running_backend_is_reused_without_checking_dependencies(self):
        app = load_module()
        with mock.patch.object(app, "request_backend", return_value={"ok": True, "running": True}), \
                mock.patch.object(app, "dependency_check") as check:
            result = app.launch_backend(show=False)

        check.assert_not_called()
        self.assertEqual(result["running"], True)

    def test_ensure_exits_nonzero_with_the_error_on_stdout(self):
        app = load_module()
        stdout = io.StringIO()
        with mock.patch.object(app, "request_backend", return_value=None), \
                mock.patch.object(app, "dependency_check", return_value=self.missing), \
                contextlib.redirect_stdout(stdout):
            code = app.main(["ensure"])

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"], self.missing["error"])


class SettingsCommandParsingTest(unittest.TestCase):
    def test_volume_commands_accept_finite_numbers_and_clamp(self):
        app = load_module()

        self.assertEqual(app.volume_from_command("volume:0.4"), 0.4)
        self.assertEqual(app.volume_from_command("volume:7"), 1.0)
        self.assertEqual(app.volume_from_command("volume:-2"), 0.0)
        for bad in ("volume:", "volume:loud", "volume:nan", "volume:inf", "volume-step:0.1", "mute"):
            with self.subTest(bad=bad):
                self.assertIsNone(app.volume_from_command(bad))
        self.assertEqual(app.volume_step_from_command("volume-step:-0.05"), -0.05)
        self.assertEqual(app.volume_step_from_command("volume-step:5"), 1.0)
        for bad in ("volume-step:", "volume-step:x", "volume-step:inf", "volume:0.5"):
            with self.subTest(bad=bad):
                self.assertIsNone(app.volume_step_from_command(bad))


class SettingsStoreTest(unittest.TestCase):
    DEFAULTS = {"v": 1, "volume": 1.0, "muted": False}

    def store(self, content=None):
        app = load_module()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        path = directory / "settings.json"
        if content is not None:
            path.write_bytes(content)
            os.chmod(path, 0o600)
        return app, app.SettingsStore(fd), path

    def test_bad_settings_files_and_values_fall_back_to_the_defaults(self):
        with self.subTest('missing or garbage files give the defaults'):
            for content in (None, b"{broken", b"[1,2]", b'{"v":2,"volume":0.2}', b"\xff\xfe"):
                with self.subTest(content=content):
                    _app, store, _path = self.store(content)
                    self.assertEqual(store.load(), self.DEFAULTS)
        with self.subTest('each bad value falls back on its own'):
            _app, store, _path = self.store(json.dumps({
                "v": 1, "volume": 3, "muted": True}).encode())

            self.assertEqual(store.load(), {**self.DEFAULTS, "muted": True})
            _app, store, _path = self.store(json.dumps({
                "v": 1, "volume": 0.2, "muted": "yes"}).encode())

            self.assertEqual(store.load(), {**self.DEFAULTS, "volume": 0.2})
        with self.subTest('an oversized file is ignored'):
            app, store, _path = self.store(b'{"v":1,"volume":0.2,"pad":"' + b"x" * 5000 + b'"}')

            self.assertGreater(5000, app.MAX_SETTINGS_BYTES)
            self.assertEqual(store.load(), self.DEFAULTS)

    def test_settings_round_trip_privately(self):
        _app, store, path = self.store()

        store.save({"v": 1, "volume": 0.35, "muted": True})

        self.assertEqual(store.load(), {"v": 1, "volume": 0.35, "muted": True})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_only_the_known_keys_are_kept_so_a_new_setting_is_added_on_purpose(self):
        app, store, _path = self.store()

        store.save({"v": 1, "volume": 0.5, "muted": False, "somethingNew": True})

        self.assertEqual(store.load(), {"v": 1, "volume": 0.5, "muted": False})
        self.assertEqual(set(app.DEFAULT_SETTINGS), {"v", "volume", "muted"})


class GstVolumeTest(unittest.TestCase):
    def player(self, Gst=None, **options):
        player = load_module().GstPlayback(Gst or fake_gst(), lambda: None, **options)
        self.addCleanup(player.hls_executor.shutdown, False)
        return player

    def start(self, player, protocol="", mime_type=""):
        player.begin({"title": "A", "playbackId": "soundcloud:tracks:1"})
        self.assertTrue(player.play("https://x.sndcdn.com/a", {"title": "A"}, protocol, mime_type))

    def test_the_volume_reaches_a_playbin_and_the_hls_volume_element(self):
        player = self.player(volume=0.5)
        self.start(player)
        self.assertEqual(player.current.properties["volume"], 0.5)
        player.set_volume(0.25)
        self.assertEqual(player.current.properties["volume"], 0.25)

        hls = self.player(volume=0.5)
        self.start(hls, "hls", "audio/mpeg")
        self.assertEqual(hls.current.volume.properties["volume"], 0.5)
        hls.set_volume(0.75)
        self.assertEqual(hls.current.volume.properties["volume"], 0.75)

    def test_the_hls_pipeline_has_a_volume_element_before_the_sink(self):
        Gst = fake_gst()
        described = []
        parse = Gst.parse_launch
        Gst.parse_launch = staticmethod(lambda text: described.append(text) or parse(text))
        player = self.player(Gst)

        self.start(player, "hls", "audio/mpeg")

        self.assertIn("audioresample ! volume name=volume ! autoaudiosink", described[0])

    def test_volume_zero_means_muted_and_mute_keeps_the_volume(self):
        player = self.player()
        self.start(player)

        player.set_volume(0.6)
        player.toggle_mute()
        self.assertEqual(player.volume_status(), {"volume": 0.6, "muted": True})
        self.assertEqual(player.current.properties["volume"], 0.0)
        player.toggle_mute()
        self.assertEqual(player.volume_status(), {"volume": 0.6, "muted": False})
        self.assertEqual(player.current.properties["volume"], 0.6)

        player.set_volume(0)
        self.assertEqual(player.volume_status(), {"volume": 0.0, "muted": True})
        player.toggle_mute()  # unmuting from 0 returns to the last audible level
        self.assertEqual(player.volume_status(), {"volume": 0.6, "muted": False})
        player.set_volume(0)
        player.set_volume(0.3)  # raising the volume unmutes
        self.assertEqual(player.volume_status(), {"volume": 0.3, "muted": False})

    def test_steps_adjust_and_clamp(self):
        player = self.player(volume=0.5)

        player.adjust_volume(0.1)
        self.assertEqual(player.volume, 0.6)
        player.adjust_volume(0.7)
        self.assertEqual(player.volume, 1.0)
        player.adjust_volume(-2)
        self.assertEqual(player.volume_status(), {"volume": 0.0, "muted": True})

    def test_a_new_track_starts_at_the_current_volume_and_mute(self):
        player = self.player(volume=0.4, muted=True)

        self.start(player)

        self.assertEqual(player.current.properties["volume"], 0.0)
        player.toggle_mute()
        self.assertEqual(player.current.properties["volume"], 0.4)
        self.start(player)
        self.assertEqual(player.current.properties["volume"], 0.4)

    def test_changing_the_volume_with_nothing_playing_is_fine(self):
        player = self.player()

        player.set_volume(0.2)
        player.toggle_mute()

        self.assertEqual(player.volume_status(), {"volume": 0.2, "muted": True})


class TrackFlagsTest(unittest.TestCase):
    def transcodings(self, *protocols):
        return {"transcodings": [
            {"url": f"https://api-v2.soundcloud.com/media/{n}", "format": {"protocol": protocol}}
            for n, protocol in enumerate(protocols)]}

    def test_a_track_offering_only_encrypted_streams_is_marked(self):
        app = load_module()

        row = app.track_from_entry(like_item(
            1, media=self.transcodings("cbc-encrypted-hls", "ctr-encrypted-hls")))

        self.assertIs(row["encrypted"], True)

    def test_a_playable_stream_or_no_media_information_leaves_the_row_unchanged(self):
        app = load_module()
        plain = app.track_from_entry(like_item(1))
        for media in (self.transcodings("hls", "ctr-encrypted-hls"), self.transcodings("progressive"),
                      self.transcodings(), {"transcodings": "x"}, "x"):
            with self.subTest(media=media):
                row = app.track_from_entry(like_item(1, media=media))
                self.assertEqual(row, plain)
                self.assertNotIn("encrypted", row)

    def test_malformed_transcodings_count_as_no_plain_stream(self):
        app = load_module()

        row = app.track_from_entry(like_item(1, media={"transcodings": [None, {"format": "hls"}]}))

        self.assertIs(row["encrypted"], True)

    def test_the_row_validator_keeps_the_flags_only_when_true(self):
        app = load_module()
        base = PlayQueueTest.row(None, 1)

        kept = app.validate_tracks_payload({"tracks": [
            {**base, "encrypted": True, "auto": True, "explicit": True, "added": True, "current": True}]})["tracks"][0]
        dropped = app.validate_tracks_payload({"tracks": [
            {**base, "encrypted": "yes", "auto": 1, "explicit": False, "added": 0, "current": None}]})["tracks"][0]

        for flag in ("encrypted", "auto", "explicit", "added", "current"):
            self.assertIs(kept[flag], True)
            self.assertNotIn(flag, dropped)
        self.assertEqual(dropped, app.validate_tracks_payload({"tracks": [base]})["tracks"][0])


class SettingsStatusFieldsTest(unittest.TestCase):
    BASE = {"playerPresent": False, "playing": False, "title": "", "artist": "", "artUrl": "",
            "loggedIn": True, "url": "", "duration": 0, "position": 0}

    def test_the_new_keys_pass_when_valid(self):
        app = load_module()

        status = app.validate_status_payload({
            **self.BASE, "volume": 0.5, "muted": True, "trackUrl": "https://soundcloud.com/artist/song"})

        self.assertEqual((status["volume"], status["muted"]), (0.5, True))
        self.assertEqual(status["trackUrl"], "https://soundcloud.com/artist/song")

    def test_invalid_values_are_left_out_and_a_foreign_track_url_is_emptied(self):
        app = load_module()

        status = app.validate_status_payload({
            **self.BASE, "volume": 2, "muted": "yes", "trackUrl": "https://evil.example/x"})

        for key in ("volume", "muted"):
            self.assertNotIn(key, status)
        self.assertEqual(status["trackUrl"], "")
        self.assertNotIn("volume", app.validate_status_payload({**self.BASE, "volume": float("nan")}))

    def test_a_payload_without_the_new_keys_is_unchanged(self):
        app = load_module()

        status = app.validate_status_payload(self.BASE)

        for key in ("volume", "muted", "trackUrl"):
            self.assertNotIn(key, status)


class VolumeCommandTest(unittest.TestCase):
    """The volume socket commands, through the real application class."""

    build = SeekCommandTest.build
    command = SeekCommandTest.command
    start = SeekCommandTest.start

    def saved_settings(self):
        fd = os.open(self.data, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return self.app.SettingsStore(fd).load()

    def test_volume_commands_change_the_status_and_are_saved_privately(self):
        self.build()

        reply = self.command("volume:0.4")
        self.assertEqual((reply.reply["ok"], reply.reply["volume"], reply.reply["muted"]), (True, 0.4, False))
        self.assertEqual(self.command("mute").reply["muted"], True)
        status = self.application.build_status()
        self.assertEqual((status["volume"], status["muted"]), (0.4, True))  # mute keeps the volume
        self.assertEqual(self.command("volume-step:-0.1").reply["volume"], 0.3)
        self.assertFalse(self.application.build_status()["muted"])
        self.assertFalse(self.command("volume:loud").reply["ok"])
        self.assertFalse(self.command("volume-step:inf").reply["ok"])
        self.assertEqual(self.application.playback.volume, 0.3)
        self.command("volume:0")
        self.assertTrue(self.application.build_status()["muted"])

        self.assertFalse((self.data / "settings.json").exists())  # debounced
        self.glib.run_idle()
        self.assertEqual(self.saved_settings()["volume"], 0.0)
        self.assertTrue(self.saved_settings()["muted"])
        self.assertEqual(stat.S_IMODE((self.data / "settings.json").stat().st_mode), 0o600)

    def test_a_volume_set_before_a_track_applies_to_the_next_one(self):
        self.build()
        self.command("volume:0.25")

        pipeline = self.start()

        self.assertEqual(pipeline.properties["volume"], 0.25)

    def test_saved_settings_are_restored_at_startup(self):
        self.build(settings={"v": 1, "volume": 0.3, "muted": True})

        status = self.application.build_status()

        self.assertEqual((status["volume"], status["muted"]), (0.3, True))

    def test_defaults_without_a_settings_file(self):
        self.build()

        status = self.application.build_status()

        self.assertEqual((status["volume"], status["muted"]), (1.0, False))


class BarWidgetTest(unittest.TestCase):
    def test_runtime_commands_use_one_persistent_socket(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("Socket {", qml)
        self.assertIn("SplitParser {", qml)
        self.assertIn("socket.write(JSON.stringify(payload)", qml)
        self.assertNotIn("id: statusProcess", qml)
        self.assertNotIn("id: tracksProcess", qml)
        self.assertNotIn("id: actionProcess", qml)
        self.assertIn('splitMarker: ""', qml)
        self.assertIn("maxSocketFrameChars", qml)

    def test_connected_backend_never_sends_unsupported_launch_command(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn('if (action === "launch") return "show"', qml)
        self.assertIn("function initializeConnection()", qml)
        self.assertIn('sendCommand("subscribe", "subscribe")', qml)
        self.assertIn('sendCommand("status", "status")', qml)
        self.assertIn("onBackendConnectedChanged: initializeConnection()", qml)

    def test_track_label_is_tooltip_only(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn('visible: !root.bar.vertical && root.title !== ""', qml)
        self.assertIn("showTooltip(root, root.safeTooltipLabel)", qml)
        self.assertIn('replace(/[<>&', qml)

    def test_bar_remains_icon_only_while_popup_uses_artwork(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn("id: barArtwork", qml)
        self.assertIn('text: ""', qml)
        self.assertIn("source: root.artDataUrl", qml)
        self.assertNotIn("source: track.artUrl", qml)
        self.assertNotIn("source: root.artUrl", qml)

    def test_track_list_requests_bounded_backend_artwork_by_token(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()
        source = MODULE_PATH.read_text()

        self.assertIn('"artwork:" + artworkId', qml)
        self.assertIn("source: trackArtDataUrl", qml)
        self.assertIn("visible: trackArtDataUrl !== \"\"", qml)
        self.assertNotIn("source: track.artUrl", qml)
        self.assertIn('command.startswith("artwork:")', source)
        self.assertIn("LIST_ARTWORK_BYTES", source)

    def test_track_list_displays_play_count_and_duration(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("formatCompactCount", qml)
        self.assertIn("track.playCount", qml)
        self.assertIn("track.durationMs", qml)
        self.assertIn('"—:—"', qml)

    def test_theme_colors_follow_bar_palette(self):
        import re

        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()
        self.assertIn("readonly property color dim: Qt.darker(bar.foreground, 1.5)", qml)
        self.assertIn("function onForegroundChanged() { waveform.requestPaint() }", qml)
        self.assertEqual(re.findall(r"#[0-9A-Fa-f]{6}", qml), [])

    def test_every_qml_text_sink_is_plain_text(self):
        import re

        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()
        missing = []
        for match in re.finditer(r"\bText\s*\{", qml):
            index, depth = match.end(), 1
            while index < len(qml) and depth:
                depth += (qml[index] == "{") - (qml[index] == "}")
                index += 1
            if "textFormat: Text.PlainText" not in qml[match.end():index]:
                missing.append(qml[:match.start()].count("\n") + 1)
        self.assertEqual(missing, [])

    def test_launcher_is_isolated_and_does_not_collect_unbounded_stderr(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn('["/usr/bin/python3", "-I", helperPath', qml)
        self.assertNotIn("StdioCollector", qml)
        self.assertNotIn("Component.onCompleted: startBackend", qml)

    def test_webkit_profile_is_ephemeral_and_backend_watches_removal(self):
        source = MODULE_PATH.read_text()

        self.assertIn("WebsiteDataManager.new_ephemeral()", source)
        self.assertNotIn("set_persistent_storage", source)
        self.assertIn("def _check_installation", source)

    def test_popup_uses_seekable_waveform_instead_of_feed_buttons(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn('text: "Following"', qml)
        self.assertIn("Canvas {", qml)
        self.assertIn('root.runAction("seek"', qml)

    def test_likes_tab_sits_next_to_feed_and_reads_likes_without_navigating_the_page(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertLess(qml.index('text: "Feed"'), qml.index('text: "Likes"'))
        self.assertIn('onClicked: root.selectTab("likes")', qml)
        self.assertIn("if (!isAccountTab(tab)) runAction(tab)", qml)

    def test_library_link_opens_the_soundcloud_library_in_the_browser_apart_from_the_tabs(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertLess(qml.index('text: "History"'), qml.index('text: "Library'))
        self.assertIn(
            'Quickshell.execDetached(["omarchy", "launch", "browser", "https://soundcloud.com/you/library"])',
            qml,
        )
        self.assertNotIn("selectTab(\"library\")", qml)

    def test_history_tab_follows_likes_and_reads_from_the_account_api(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertLess(qml.index('text: "Likes"'), qml.index('text: "History"'))
        self.assertIn('onClicked: root.selectTab("history")', qml)
        self.assertIn('tab === "likes" || tab === "history"', qml)

    def test_popup_has_larger_scrollable_home_and_feed_browser(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("Style.space(408)", qml)
        self.assertIn("Style.space(480)", qml)
        self.assertIn('text: "Home"', qml)
        self.assertIn('text: "Feed"', qml)
        self.assertIn("ListView {", qml)
        self.assertIn('root.runAction("play"', qml)
        self.assertIn("modelData.playbackId", qml)
        self.assertNotIn('root.runAction("play-url"', qml)

    def test_track_selection_avoids_transient_signed_out_and_optimistic_metadata(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("reportsLoggedOut", qml)
        self.assertIn("root.loggedIn && !reportsLoggedOut", qml)
        self.assertNotIn('root.title = track.title || ""', qml)
        self.assertIn("(!root.actionBusy || root.selectionBusy)", qml)
        self.assertIn('selectionBusy = action === "play"', qml)
        self.assertIn("activeSelectionRequestId = sentId", qml)
        self.assertIn("isCurrentSelectionResponse(activeSelectionRequestId, message.id)", qml)
        self.assertIn("SoundCloudModel.selectionIsPending(playbackState)", qml)
        self.assertIn('kind === "selection"', qml)

    def test_tab_loading_preserves_now_playing_state_and_each_tab_cache(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("property var homeTracks", qml)
        self.assertIn("property var feedTracks", qml)
        self.assertNotIn("    tracks = []\n    tracksLoading = true", qml)
        self.assertIn("if (!preserveMetadata)", qml)

    def test_track_refresh_requests_the_selected_route_bound_api_source(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn(
            'sendCommand("tracks:" + selectedTab, "tracks:" + selectedTab)',
            qml,
        )

    def test_pending_track_fetch_preserves_the_tab_cache(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("SoundCloudModel.applyTrackResult(cached, result)", qml)

    def test_track_refresh_has_no_legacy_navigation_delay(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn("interval: 1400", qml)
        self.assertIn("interval: 100", qml)
        self.assertIn("runAction(tab)\n    refreshTracks()", qml)
        self.assertIn("message.pending === true", qml)
        self.assertIn('if (message.type === "tracks")', qml)

    def test_track_lists_request_more_before_reaching_the_exact_end(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn("onContentYChanged: root.loadMoreTracks", qml)
        self.assertNotIn("onCountChanged: root.loadMoreTracks", qml)
        self.assertIn("onMovementEnded: root.loadMoreTracks", qml)
        self.assertIn("userInitiated: userInitiated", qml)
        self.assertIn("yPosition: yPosition", qml)
        self.assertIn("heightRatio: heightRatio", qml)
        self.assertIn('sendCommand("load-more:" + selectedTab', qml)

    def test_lazy_loading_has_bounded_observability_events(self):
        source = MODULE_PATH.read_text()

        self.assertIn('"lazy-load-request"', source)
        self.assertIn('"track-page"', source)

    def test_coalesced_socket_frames_are_limited_after_complete_lines_are_parsed(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()
        function = qml[qml.index("function handleSocketChunk"):qml.index(
            "onBackendConnectedChanged"
        )]

        self.assertLess(function.index("while ((newline"), function.index(
            "if (socketBuffer.length > maxSocketFrameChars)"
        ))
        self.assertIn("if (line.length > maxSocketFrameChars)", function)

    def test_disconnect_clears_partial_frames_and_inflight_request_state(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("function resetConnectionState()", qml)
        self.assertIn('socketBuffer = ""', qml)
        self.assertIn("pendingRequests = ({})", qml)
        self.assertIn("tracksRequestPending = false", qml)
        self.assertIn("trackArtworkPending = ({})", qml)
        self.assertIn("root.resetConnectionState()", qml)

    def test_empty_navigation_status_updates_timeline_without_erasing_metadata(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("var preserveMetadata", qml)
        self.assertIn("SoundCloudModel.shouldPreservePlaybackMetadata(state, root.title)", qml)
        self.assertLess(qml.index("playing = state.playing === true"), qml.index(
            "if (!preserveMetadata)"
        ))
        self.assertLess(qml.index("duration = Number(state.duration || 0)"), qml.index(
            "if (!preserveMetadata)"
        ))
        self.assertNotIn('if (!hasIncomingTrack && root.title !== "") return', qml)

    def test_now_playing_sections_keep_their_layout_while_data_is_missing(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("property bool hasTrack", qml)
        self.assertIn("opacity: root.hasTrack ? 1 : 0.45", qml)
        self.assertNotIn('visible: root.running && root.loggedIn && root.title !== ""', qml)

    def test_popup_has_no_bottom_shortcut_help_text(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn("Middle-click: play/pause", qml)

    def test_track_browser_fills_remaining_popup_height(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn(
            "height: Math.max(0, parent.height - y - (errorText.visible",
            qml,
        )
        self.assertIn("id: errorText", qml)
        self.assertNotIn("height: Style.space(178)", qml)

    def test_readme_documents_safe_removal_and_persistent_state(self):
        readme = (MODULE_PATH.parent / "README.md").read_text()

        self.assertIn("## Removing", readme)
        self.assertIn("omarchy plugin remove brunosilveira.soundcloud", readme)
        self.assertIn("ephemeral in-memory profile and cache", readme)
        self.assertIn("python3 soundcloud_app.py stop", readme)


if __name__ == "__main__":
    unittest.main()
