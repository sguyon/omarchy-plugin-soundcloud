#!/usr/bin/env python3
"""Hidden SoundCloud session/playback backend for an Omarchy bar plugin.

The only normal window is the one-time sign-in surface. Closing it hides the
WebKit view while preserving the session and audio. The bar talks to this
process over a user-only Unix socket.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import fcntl
import hashlib
import http.client
import ipaddress
import json
import math
import os
import pwd
import random
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import stat
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

APP_ID = "com.github.brunosilveira.OmarchySoundCloud"
APP_NAME = "SoundCloud"
START_URI = "https://soundcloud.com/you/likes"
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 524288
MAX_TITLE_BYTES = 512
MAX_ARTIST_BYTES = 256
MAX_URL_BYTES = 2048
MAX_TRACKS = 100
API_LIST_REFRESH_SECONDS = {"likes": 60.0, "history": 15.0}
MAX_LOCAL_PLAYS = 50
MAX_PLAYED_AT_MS = 4_102_444_800_000  # year 2100
# Track lists read straight from the account API, with each list's first-page path.
API_TRACK_LISTS = {
    "likes": "/users/{user_id}/track_likes",
    "history": "/me/play-history/tracks",
}
MAX_PLAY_COUNT = 10**12
# Optional boolean keys of a track row: only present when true.
TRACK_FLAGS = ("encrypted", "auto", "explicit", "added", "current")
MAX_TRACK_DURATION_MS = 7 * 24 * 60 * 60 * 1000
MAX_ARTWORK_BYTES = 131072
MAX_ARTWORK_PIXELS = 1024 * 1024
LIST_ARTWORK_BYTES = 32768
LIST_ARTWORK_PIXELS = 512 * 512
LIST_ARTWORK_DIMENSION = 512
MAX_COOKIE_STORE_BYTES = 512 * 1024
MAX_TRACK_CACHE_BYTES = 1024 * 1024
MAX_QUEUE_BYTES = 256 * 1024
MAX_QUEUE_SOURCE_BYTES = 64
MAX_SETTINGS_BYTES = 4096
RELATED_TRACKS_LIMIT = 10
NOTIFICATION_TIMEOUT_SECONDS = 3.0
NO_PLAYABLE_MESSAGE = "No other track in the queue can play"
ENCRYPTED_MESSAGE = "This track is only offered as an encrypted stream the plugin can't play"
# Previous restarts the track from this position on. The website's real value is unverified.
PREVIOUS_RESTART_SECONDS = 3.0
REPEAT_MODES = ("none", "one", "all")
QUEUE_SOURCES = ("home", "feed", "likes", "history")
# A track that fails this soon after an automatic start is skipped instead of stopping the queue.
AUTO_SKIP_WINDOW_SECONDS = 2.0
SKIP_NOTICE_SECONDS = 8.0
QUEUE_POSITION_SAVE_SECONDS = 5
MAX_COOKIES = 256
MAX_CLIENTS = 8
MAX_PENDING_DISPATCHES = 32
MAX_EVENT_LOG_BYTES = 512 * 1024
CLIENT_TIMEOUT_SECONDS = 5.0
CLIENT_IDLE_SECONDS = 60.0
SEND_TIMEOUT_SECONDS = 0.1
HLS_MAX_WORKERS = 2
HLS_OPERATION_SECONDS = 45.0
# A seek waits this long for the feeder it replaces to let go of its slot.
HLS_SEEK_WAIT_SECONDS = 1.0
ARTWORK_HOSTS = {"i1.sndcdn.com"}
MAX_WAVEFORM_BARS = 200
STATUS_INTERVAL_MS = 500
PAGE_STATUS_INTERVAL_SECONDS = 3
# SoundCloud's bot protection (DataDome) shows its captcha in an iframe from here.
BOT_CHECK_HOSTS = {"geo.captcha-delivery.com"}
SOUNDCLOUD_WEB_HOSTS = {
    "soundcloud.com",
    "www.soundcloud.com",
    "secure.soundcloud.com",
    "m.soundcloud.com",
}
_event_log_fd: int | None = None
_event_log_lock = threading.Lock()


@dataclass(frozen=True)
class ProfilePaths:
    data_dir: Path
    cache_dir: Path
    cookie_db: Path


def profile_paths(env: Mapping[str, str] | None = None) -> ProfilePaths:
    values: Mapping[str, str] = {} if env is None else env
    trusted_home = pwd.getpwuid(os.geteuid()).pw_dir if env is None else values.get("HOME", str(Path.home()))
    home = Path(trusted_home)
    data_home = Path(values.get("XDG_DATA_HOME", home / ".local" / "share"))
    cache_home = Path(values.get("XDG_CACHE_HOME", home / ".cache"))
    data_dir = data_home / "omarchy-soundcloud"
    return ProfilePaths(
        data_dir=data_dir,
        cache_dir=cache_home / "omarchy-soundcloud",
        cookie_db=data_dir / "cookies.sqlite",
    )


def runtime_socket_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    runtime_dir = values.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise RuntimeError("XDG_RUNTIME_DIR is required")
    root = Path(runtime_dir)
    if not root.is_absolute():
        raise RuntimeError("XDG_RUNTIME_DIR must be absolute")
    return root / "omarchy-soundcloud" / "control.sock"


def log_event(event: str, **fields: object) -> None:
    """Write bounded, credential-free pagination diagnostics."""
    allowed_fields = {
        "track-page": {
            "source", "reset", "incomingCount", "previousCount",
            "resultCount", "addedCount", "hasMore",
        },
        "lazy-load-request": {"source", "started", "hasMore"},
    }
    if _event_log_fd is None or event not in allowed_fields:
        return
    record = {"event": event, "time": round(time.time(), 3)}
    record.update({key: value for key, value in fields.items() if key in allowed_fields[event]})
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > 2048:
        return
    with _event_log_lock:
        try:
            details = os.fstat(_event_log_fd)
            if details.st_size + len(encoded) > MAX_EVENT_LOG_BYTES:
                os.ftruncate(_event_log_fd, 0)
            os.write(_event_log_fd, encoded)
        except OSError:
            pass


def configure_runtime_event_log() -> None:
    global _event_log_fd
    raw_fd = os.environ.pop("OMARCHY_SOUNDCLOUD_EVENT_LOG_FD", "")
    if not raw_fd.isdecimal():
        return
    fd = int(raw_fd)
    try:
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_mode & 0o077
        ):
            raise PermissionError("refusing unsafe pagination log")
        os.set_inheritable(fd, False)
        _event_log_fd = fd
    except (OSError, ValueError):
        try:
            os.close(fd)
        except OSError:
            pass


def close_runtime_event_log() -> None:
    global _event_log_fd
    if _event_log_fd is None:
        return
    try:
        os.close(_event_log_fd)
    except OSError:
        pass
    _event_log_fd = None


def open_runtime_event_log():
    directory_fd = ensure_private_directory(runtime_socket_path().parent)
    try:
        fd = os.open(
            "pagination.log",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise PermissionError("refusing unsafe pagination log")
        os.set_blocking(fd, True)
        os.fchmod(fd, 0o600)
        if details.st_size > MAX_EVENT_LOG_BYTES:
            os.ftruncate(fd, 0)
        return os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def ensure_private_directory(path: Path) -> int:
    """Create and open an owner-only directory without following symlinks."""
    path = path.absolute()
    parts = path.parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts[1:]):
            is_leaf = index == len(parts) - 2
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o700 if is_leaf else 0o755, dir_fd=fd)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise PermissionError(f"refusing symlink or invalid directory: {path}") from exc
            os.close(fd)
            fd = next_fd
            details = os.fstat(fd)
            if not stat.S_ISDIR(details.st_mode):
                raise PermissionError(f"refusing non-directory path component: {path}")
            if is_leaf:
                if details.st_uid != os.geteuid():
                    raise PermissionError(f"refusing directory owned by another user: {path}")
                os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def validate_private_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_mode & 0o077
        ):
            raise PermissionError(f"refusing unsafe private file: {path}")
    finally:
        os.close(fd)


class CookieStore:
    """Descriptor-anchored storage for the small persistent cookie jar."""

    def __init__(self, directory_fd: int):
        self.directory_fd = directory_fd
        self.filename = "cookies.json"

    @staticmethod
    def _valid_record(record: object) -> bool:
        if not isinstance(record, dict):
            return False
        required_strings = ("name", "value", "domain", "path")
        if any(not isinstance(record.get(key), str) for key in required_strings):
            return False
        if any(len(record[key].encode("utf-8")) > 4096 for key in required_strings):
            return False
        domain = record["domain"].lower().lstrip(".").rstrip(".")
        if domain != "soundcloud.com" and not domain.endswith(".soundcloud.com"):
            return False
        expires = record.get("expires")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, int)):
            return False
        return isinstance(record.get("secure"), bool) and isinstance(record.get("httpOnly"), bool)

    def load(self) -> list[dict[str, object]]:
        try:
            fd = os.open(
                self.filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return []
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or details.st_mode & 0o077
                or details.st_size > MAX_COOKIE_STORE_BYTES
            ):
                raise PermissionError("refusing unsafe cookie store")
            os.set_blocking(fd, True)
            data = bytearray()
            while len(data) <= MAX_COOKIE_STORE_BYTES:
                chunk = os.read(fd, min(65536, MAX_COOKIE_STORE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_COOKIE_STORE_BYTES:
                raise PermissionError("cookie store exceeds security limit")
            payload = json.loads(data.decode("utf-8"))
            if (
                not isinstance(payload, list)
                or len(payload) > MAX_COOKIES
                or not all(self._valid_record(record) for record in payload)
            ):
                raise PermissionError("invalid cookie store")
            return payload
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermissionError("invalid cookie store") from exc
        finally:
            os.close(fd)

    def save(self, records: list[dict[str, object]]) -> None:
        if len(records) > MAX_COOKIES or not all(self._valid_record(record) for record in records):
            raise ValueError("invalid cookie records")
        encoded = json.dumps(records, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_COOKIE_STORE_BYTES:
            raise ValueError("cookie store exceeds security limit")
        temporary = f".cookies-{secrets.token_hex(12)}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(
                temporary,
                self.filename,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            os.fsync(self.directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass

    def migrate_legacy_sqlite(self) -> list[dict[str, object]]:
        try:
            fd = os.open(
                "cookies.sqlite",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return []
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or details.st_mode & 0o077
                or details.st_size > MAX_COOKIE_STORE_BYTES
            ):
                raise PermissionError("refusing unsafe legacy cookie store")
            os.set_blocking(fd, True)
            chunks = []
            remaining = details.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise PermissionError("truncated legacy cookie store")
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
        database = sqlite3.connect(":memory:")
        try:
            database.deserialize(b"".join(chunks))
            rows = database.execute(
                "SELECT name, value, host, path, expiry, isSecure, isHttpOnly "
                "FROM moz_cookies LIMIT ?",
                (MAX_COOKIES + 1,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise PermissionError("invalid legacy cookie store") from exc
        finally:
            database.close()
        if len(rows) > MAX_COOKIES:
            raise PermissionError("too many legacy cookies")
        records = []
        for name, value, domain, path, expiry, secure, http_only in rows:
            normalized = str(domain).lower().lstrip(".").rstrip(".")
            if normalized != "soundcloud.com" and not normalized.endswith(".soundcloud.com"):
                continue
            record = {
                "name": str(name),
                "value": str(value),
                "domain": str(domain),
                "path": str(path),
                "expires": int(expiry) if expiry else None,
                "secure": bool(secure),
                "httpOnly": bool(http_only),
            }
            if not self._valid_record(record):
                raise PermissionError("invalid legacy cookies")
            records.append(record)
        self.save(records)
        os.unlink("cookies.sqlite", dir_fd=self.directory_fd)
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.unlink("cookies.sqlite" + suffix, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass
        return records


class PrivateJsonStore:
    """Descriptor-anchored storage for bounded non-secret cached state."""

    def __init__(self, directory_fd: int, filename: str, maximum_bytes: int):
        self.directory_fd = directory_fd
        self.filename = filename
        self.maximum_bytes = maximum_bytes

    def load(self) -> object | None:
        try:
            fd = os.open(
                self.filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return None
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or details.st_mode & 0o077
                or details.st_size > self.maximum_bytes
            ):
                return None
            os.set_blocking(fd, True)
            data = bytearray()
            while len(data) <= self.maximum_bytes:
                chunk = os.read(fd, min(65536, self.maximum_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > self.maximum_bytes:
                return None
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        finally:
            os.close(fd)

    def save(self, payload: object) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.maximum_bytes:
            return
        temporary = f".{self.filename}-{secrets.token_hex(12)}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            view = memoryview(encoded)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(
                temporary,
                self.filename,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            os.fsync(self.directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass


def prepare_profile_paths(paths: ProfilePaths) -> None:
    os.close(ensure_private_directory(paths.data_dir))
    for suffix in ("", "-journal", "-wal", "-shm"):
        validate_private_file(Path(str(paths.cookie_db) + suffix))


def prepare_runtime_socket_path(path: Path) -> None:
    runtime_root = path.parent.parent
    details = runtime_root.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_mode & 0o077
    ):
        raise PermissionError("XDG_RUNTIME_DIR must be an owner-only directory")
    os.close(ensure_private_directory(path.parent))


def is_soundcloud_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    try:
        if parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return host in SOUNDCLOUD_WEB_HOSTS


def is_bot_check_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") in BOT_CHECK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def is_playback_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"soundcloud:tracks:[1-9][0-9]*", value) is not None


def is_allowed_artwork_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") in ARTWORK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and len(uri.encode("utf-8")) <= MAX_URL_BYTES
        and not any(ord(character) < 32 for character in uri)
    )


def normalize_list_artwork_url(uri: object) -> str:
    if not isinstance(uri, str) or not is_allowed_artwork_uri(uri):
        return ""
    parsed = urlparse(uri)
    path = re.sub(
        r"-(?:large|original|t\d+x\d+)(\.(?:jpe?g|png))$",
        r"-t120x120\1",
        parsed.path,
        flags=re.IGNORECASE,
    )
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def apply_graphics_workarounds(env: dict[str, str] | None = None) -> bool:
    """Avoid WebKitGTK's invalid explicit-sync commits on Hyprland."""
    values = os.environ if env is None else env
    if "WEBKIT_DISABLE_DMABUF_RENDERER" in values:
        return False
    if values.get("HYPRLAND_INSTANCE_SIGNATURE"):
        values["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
        return True
    return False


def source_uri(source: str) -> str | None:
    return {
        "home": "https://soundcloud.com/discover",
        "likes": "https://soundcloud.com/you/likes",
        "feed": "https://soundcloud.com/feed",
    }.get(source)


def is_track_source_page(uri: str) -> bool:
    """Whether the page is one whose API calls give playback its request context."""
    return re.match(r"https://soundcloud\.com/(discover|feed)([/?#]|$)", uri) is not None


def normalize_backend_command(command: str) -> str:
    return "show" if command == "launch" else command


def track_source_from_command(command: str) -> str | None:
    return {
        "tracks:home": "home",
        "tracks:feed": "feed",
    }.get(command)


def api_list_source_from_command(command: str, prefix: str) -> str | None:
    source = command.removeprefix(prefix) if command.startswith(prefix) else None
    return source if source in API_TRACK_LISTS else None


def load_more_source_from_command(command: str) -> str | None:
    return {
        "load-more:home": "home",
        "load-more:feed": "feed",
    }.get(command)


def playback_id_from_command(command: str) -> str | None:
    if not command.startswith("play:"):
        return None
    value = command.removeprefix("play:")
    return value if is_playback_id(value) else None


def play_from_command(command: str) -> tuple[str, str] | None:
    """`play-from:<tab>:<playback id>` as (tab, playback id)."""
    if not command.startswith("play-from:"):
        return None
    tab, _, value = command.removeprefix("play-from:").partition(":")
    return (tab, value) if tab in QUEUE_SOURCES and is_playback_id(value) else None


def queue_next_from_command(command: str) -> str | None:
    value = command.removeprefix("queue-next:") if command.startswith("queue-next:") else None
    return value if is_playback_id(value) else None


def queue_add_from_command(command: str) -> str | None:
    value = command.removeprefix("queue-add:") if command.startswith("queue-add:") else None
    return value if is_playback_id(value) else None


def queue_play_from_command(command: str) -> str | None:
    value = command.removeprefix("queue-play:") if command.startswith("queue-play:") else None
    return value if is_playback_id(value) else None


def queue_move_from_command(command: str) -> tuple[str, int] | None:
    """`queue-move:<playback id>:<signed offset>` as (playback id, offset), the offset clamped
    to the queue size. Offsets are whole numbers with an optional sign, at most four digits."""
    if not command.startswith("queue-move:"):
        return None
    track_id, _, offset = command.removeprefix("queue-move:").rpartition(":")
    if not is_playback_id(track_id) or re.fullmatch(r"[+-]?[0-9]{1,4}", offset) is None:
        return None
    return track_id, max(-MAX_TRACKS, min(MAX_TRACKS, int(offset)))


def link_from_command(command: str) -> tuple[str, str] | None:
    """`link-next:<url>` or `link-play:<url>` as (mode, link text), mode "next" or "play".

    The URL may be percent-encoded as a whole (no slash left in it) or raw. Whether it is a
    SoundCloud track link is decided later by `parse_track_link`, as the CLI does.
    """
    for prefix, mode in (("link-next:", "next"), ("link-play:", "play")):
        if command.startswith(prefix):
            text = command.removeprefix(prefix)
            if "/" not in text and re.search(r"%2f", text, re.IGNORECASE):
                text = unquote(text)
            return mode, text
    return None


def repeat_from_command(command: str, current: str) -> str | None:
    """The mode a `repeat` command asks for: bare `repeat` cycles none, one, all."""
    if command == "repeat":
        return REPEAT_MODES[(REPEAT_MODES.index(current) + 1) % len(REPEAT_MODES)]
    mode = command.removeprefix("repeat:") if command.startswith("repeat:") else None
    return mode if mode in REPEAT_MODES else None


def shuffle_from_command(command: str, current: bool) -> bool | None:
    """The state a `shuffle` command asks for: bare `shuffle` toggles."""
    if command == "shuffle":
        return not current
    return {"shuffle:on": True, "shuffle:off": False}.get(command)


def volume_from_command(command: str) -> float | None:
    """The level a `volume:<0..1>` command sets, clamped to 0..1."""
    return _command_number(command, "volume:", 0.0, 1.0)


def volume_step_from_command(command: str) -> float | None:
    """The signed change a `volume-step:<float>` command asks for."""
    return _command_number(command, "volume-step:", -1.0, 1.0)


def _command_number(command: str, prefix: str, minimum: float, maximum: float) -> float | None:
    if not command.startswith(prefix):
        return None
    try:
        number = float(command.removeprefix(prefix))
    except ValueError:
        return None
    return max(minimum, min(maximum, number)) if math.isfinite(number) else None


def on_off_from_command(command: str, name: str) -> bool | None:
    """`<name>:on` or `<name>:off` as a bool."""
    return {f"{name}:on": True, f"{name}:off": False}.get(command)


def queue_remove_from_command(command: str) -> str | None:
    value = command.removeprefix("queue-remove:") if command.startswith("queue-remove:") else None
    return value if is_playback_id(value) else None


def track_like_from_command(command: str) -> tuple[str, bool] | None:
    for prefix, liked in (("like:", True), ("unlike:", False)):
        if command.startswith(prefix):
            value = command.removeprefix(prefix)
            return (value, liked) if is_playback_id(value) else None
    return None


def oauth_token_from_cookies(records: list[dict[str, object]]) -> str | None:
    for record in records:
        domain = str(record.get("domain", "")).lower().lstrip(".").rstrip(".")
        value = record.get("value")
        if (record.get("name") == "oauth_token" and domain == "soundcloud.com"
                and isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z._-]{1,2048}", value)):
            return value
    return None


def merge_track_pages(
    existing: list[dict[str, object]],
    incoming: list[dict[str, object]],
    *,
    reset: bool,
    limit: int = MAX_TRACKS,
) -> list[dict[str, object]]:
    merged: OrderedDict[str, dict[str, object]] = OrderedDict()
    if not reset:
        for track in existing:
            url = track.get("url") if isinstance(track, dict) else None
            if isinstance(url, str) and url:
                merged[url] = track
    for track in incoming:
        url = track.get("url") if isinstance(track, dict) else None
        if isinstance(url, str) and url:
            merged[url] = track
    return list(merged.values())[:max(0, limit)]


def with_play_first(tracks: list[dict[str, object]], track: dict[str, object],
                    limit: int = MAX_TRACKS) -> list[dict[str, object]]:
    """The tracks with `track` moved or added at the front, one row per track URL."""
    rest = [item for item in tracks if item.get("url") != track.get("url")]
    return ([track] + rest)[:max(0, limit)]


def with_local_plays(server_tracks: list[dict[str, object]],
                     plays: list[dict[str, object]]) -> list[dict[str, object]]:
    """A page of SoundCloud's play history merged with this player's own plays, newest first.

    SoundCloud does not record plays from this player, so they would never appear otherwise.
    A track in both lists appears once, with its latest play time.
    """
    def played_at(track):
        return track.get("playedAt") or 0

    latest: dict[object, dict[str, object]] = {}
    for track in [*plays, *server_tracks]:
        known = latest.get(track.get("url"))
        if known is None or played_at(track) > played_at(known):
            latest[track.get("url")] = track
    return sorted(latest.values(), key=played_at, reverse=True)


def _queue_rows(rows: object) -> list[dict[str, object]]:
    """Copies of the rows that have a URL, one per URL, in order."""
    seen: set[str] = set()
    clean = []
    for row in rows if isinstance(rows, list) else []:
        url = row.get("url") if isinstance(row, dict) else None
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            # "current" is computed for the queue view, never stored
            clean.append({key: value for key, value in row.items() if key != "current"})
    return clean


class PlayQueue:
    """A list with a cursor, plus "play next" items kept apart from the list.

    Like the website: starting a track from a list replaces the list and keeps the
    explicit items. Explicit items play before the next list item, in the order added,
    and join the list at the cursor when they start, so Previous can come back through them.
    Rows have the shape `validate_tracks_payload` produces.

    Two kinds of user-queued rows differ in where they play. "Play next" goes right
    after the current track, ahead of the rest of the list. "Add to queue" (`add_last`) goes
    after every list item still to come, so the whole list plays first; among themselves,
    both kinds play in the order added. The website's "Add to Next up" is closer to play next;
    this plugin keeps the two apart so a long list can be queued up without being jumped.
    """

    def __init__(self, repeat: str = "none", shuffle: bool = False, rng: object | None = None):
        self._items: list[dict[str, object]] = []
        self._explicit: list[dict[str, object]] = []
        self._added: list[dict[str, object]] = []
        self._cursor = 0
        self._repeat = repeat if repeat in REPEAT_MODES else "none"
        self._shuffle = shuffle is True
        self._rng = rng if rng is not None else random.Random()
        self.source = ""

    @property
    def items(self) -> list[dict[str, object]]:
        return list(self._items)

    @property
    def explicit(self) -> list[dict[str, object]]:
        return list(self._explicit)

    @property
    def added(self) -> list[dict[str, object]]:
        return list(self._added)

    @property
    def current(self) -> dict[str, object] | None:
        return self._items[self._cursor] if 0 <= self._cursor < len(self._items) else None

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def length(self) -> int:
        return len(self._items)

    @property
    def can_next(self) -> bool:
        return bool(self._explicit) or bool(self._added) or self._cursor + 1 < len(self._items) or (
            self._repeat == "all" and bool(self._items)
        )

    @property
    def can_previous(self) -> bool:
        # With nothing before the current track, Previous restarts it.
        return self.current is not None

    @property
    def repeat(self) -> str:
        return self._repeat

    @repeat.setter
    def repeat(self, mode: str) -> None:
        if mode in REPEAT_MODES:
            self._repeat = mode

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @shuffle.setter
    def shuffle(self, enabled: bool) -> None:
        enabled = enabled is True
        if enabled and not self._shuffle:
            self._permute_after_cursor()
        self._shuffle = enabled

    def _permute_after_cursor(self) -> None:
        # Only what is still to come moves; what was played and the current track stay.
        tail = self._items[self._cursor + 1:]
        self._rng.shuffle(tail)
        self._items[self._cursor + 1:] = tail

    def replace(self, rows: list[dict[str, object]], index: int, source: str = "") -> None:
        """Start from `rows` with the cursor on `rows[index]`. Explicit items stay."""
        clean = _queue_rows(rows)
        wanted = None
        if isinstance(index, int) and not isinstance(index, bool) and isinstance(rows, list) \
                and 0 <= index < len(rows) and isinstance(rows[index], dict):
            wanted = rows[index].get("url")
        position = next((n for n, row in enumerate(clean) if row["url"] == wanted), None)
        if position is None:
            position = index if isinstance(index, int) and not isinstance(index, bool) else 0
            position = max(0, min(position, len(clean) - 1))
        start = max(0, min(position, len(clean) - MAX_TRACKS))
        self._items = clean[start:start + MAX_TRACKS]
        self._cursor = position - start
        self.source = _bounded_string(source, MAX_QUEUE_SOURCE_BYTES) or ""
        if self._shuffle:
            self._permute_after_cursor()

    def play_next(self, row: dict[str, object]) -> None:
        """Queue `row` after the current track and after earlier "play next" items."""
        clean = _queue_rows([row])
        if clean and len(self._explicit) < MAX_TRACKS and all(
            known["url"] != clean[0]["url"] for known in self._explicit
        ):
            # A row already waiting at the end moves up instead of playing twice.
            self._added = [known for known in self._added if known["url"] != clean[0]["url"]]
            self._explicit.append({**clean[0], "explicit": True})

    def add_last(self, row: dict[str, object]) -> bool:
        """Queue `row` after every list item still to come ("Add to queue"), in the order added.

        Returns False, and changes nothing, for a row that is unusable, already waiting as
        a user-queued row, or already coming up in the list (no duplicates by URL)."""
        clean = _queue_rows([row])
        if not clean or len(self._added) >= MAX_TRACKS:
            return False
        url = clean[0]["url"]
        waiting = [*self._explicit, *self._added, *self._items[self._cursor + 1:]]
        if any(known["url"] == url for known in waiting):
            return False
        self._added.append({**clean[0], "explicit": True, "added": True})
        return True

    def _upcoming_list_positions(self) -> list[int]:
        """Positions in the list of the rows shown after the "play next" ones."""
        pending = {row["url"] for row in self._explicit}
        return [n for n in range(self._cursor + 1, len(self._items))
                if self._items[n]["url"] not in pending]

    def jump_to(self, track_id: object) -> dict[str, object] | None:
        """Put the cursor on a queued track, by playback id or URL, and return it to play.

        A row from the list, played or not, just takes the cursor. A "play next" or "add to
        queue" row joins the list right after the cursor, as it does when it comes up."""
        if not isinstance(track_id, str) or not track_id:
            return None

        def wanted(row):
            return track_id in (row.get("playbackId"), row.get("url"))

        for waiting in (self._explicit, self._added):
            for position, row in enumerate(waiting):
                if wanted(row):
                    del waiting[position]
                    self._enter(row)
                    return self.current
        for position, row in enumerate(self._items):
            if wanted(row):
                self._cursor = position
                return self.current
        return None

    def move(self, track_id: object, offset: object) -> bool:
        """Move a queued row up (negative) or down by `offset` places inside its group, stopping
        at the group's ends. The groups are the "play next" rows, the list rows still to come
        and the "add to queue" rows. The playing and already played rows can't move."""
        if (not isinstance(track_id, str) or not track_id
                or isinstance(offset, bool) or not isinstance(offset, int)):
            return False

        def wanted(row):
            return track_id in (row.get("playbackId"), row.get("url"))

        for group in (self._explicit, self._added):
            for position, row in enumerate(group):
                if wanted(row):
                    group.insert(max(0, min(position + offset, len(group) - 1)), group.pop(position))
                    return True
        slots = self._upcoming_list_positions()
        for place, position in enumerate(slots):
            if wanted(self._items[position]):
                target = slots[max(0, min(place + offset, len(slots) - 1))]
                self._items.insert(target, self._items.pop(position))
                return True
        return False

    def extend_auto(self, rows: list[dict[str, object]]) -> int:
        """Add autoplay rows at the end as implicit items marked `auto`. Returns how many were new."""
        known = {row["url"] for row in self._items} | {row["url"] for row in self._explicit}
        added = 0
        for row in _queue_rows(rows):
            if row["url"] in known:
                continue
            known.add(row["url"])
            self._items.append({**row, "auto": True})
            added += 1
            if len(self._items) > MAX_TRACKS:
                del self._items[0]
                self._cursor -= 1
        return added

    def view(self, limit: int = MAX_TRACKS) -> tuple[list[dict[str, object]], int, int]:
        """The rows to show as (rows, cursor, explicit count): what was played and the current
        track, then the "play next" items, then the rest of the list. Each row says if it is
        `explicit`, `auto` or `current`; the cursor is the current row's index (-1 if none)."""
        current = self._cursor if 0 <= self._cursor < len(self._items) else -1
        pending = {row["url"] for row in self._explicit}
        rows = [dict(row) for row in self._items[:current + 1]]
        for row in rows:
            row["explicit"] = row.get("explicit") is True
            row["auto"] = row.get("auto") is True
            row["current"] = False
        if rows:
            rows[-1]["current"] = True
        rows += [{**row, "explicit": True, "auto": False, "current": False} for row in self._explicit]
        rows += [
            {**row, "explicit": row.get("explicit") is True, "auto": row.get("auto") is True,
             "current": False}
            for row in self._items[current + 1:] if row["url"] not in pending
        ]
        rows += [{**row, "explicit": True, "added": True, "auto": False, "current": False}
                 for row in self._added]
        for row in rows:
            row["added"] = row.get("added") is True
        if len(rows) > limit:
            start = max(0, min(current - 10, len(rows) - limit))
            rows, current = rows[start:start + limit], current - start
        return rows, current if current >= 0 else -1, len(self._explicit)

    def remove(self, track_id: object) -> bool:
        """Take a queued track out, by playback id. The playing track can't be removed."""
        if not isinstance(track_id, str) or not track_id:
            return False

        def wanted(row):
            return track_id in (row.get("playbackId"), row.get("url"))

        removed = len(self._explicit) + len(self._added)
        self._explicit = [row for row in self._explicit if not wanted(row)]
        self._added = [row for row in self._added if not wanted(row)]
        removed -= len(self._explicit) + len(self._added)
        keep = []
        cursor = self._cursor
        for position, row in enumerate(self._items):
            if position != cursor and wanted(row):
                removed += 1
                if position < cursor:
                    self._cursor -= 1
            else:
                keep.append(row)
        self._items = keep
        return removed > 0

    def clear(self) -> None:
        """Keep only the current track, as the website's clear does."""
        current = self.current
        self._items = [current] if current is not None else []
        self._explicit = []
        self._added = []
        self._cursor = 0

    def _step(self, automatic: bool) -> dict[str, object] | None:
        """Move to the next track and return it, or return None and stay."""
        if automatic and self._repeat == "one" and self.current is not None:
            return self.current
        if self._explicit:
            self._enter(self._explicit.pop(0))
        elif self._cursor + 1 < len(self._items):
            self._cursor += 1
        elif self._added:
            self._enter(self._added.pop(0))
        elif self._repeat == "all" and self._items:
            self._cursor = 0
            if self._shuffle:
                self._permute_after_cursor()
        else:
            return None
        return self.current

    def _enter(self, row: dict[str, object]) -> None:
        """Make a user-queued row the current one, placed right after the cursor in the list."""
        for position, item in enumerate(self._items):
            if item["url"] == row["url"]:
                del self._items[position]
                if position <= self._cursor:
                    self._cursor -= 1
                break
        if not self._items:
            self._cursor = -1
        self._cursor += 1
        self._items.insert(self._cursor, row)
        if len(self._items) > MAX_TRACKS:
            del self._items[0]
            self._cursor -= 1

    def advance(self, user_initiated: bool) -> dict[str, object] | None:
        """The next track to play, or None at the end. An automatic end repeats under repeat one."""
        return self._step(automatic=not user_initiated)

    def previous(self, position_seconds: object) -> dict[str, object] | str | None:
        """"restart" when the track is past the threshold or nothing is before it, else the previous track."""
        if not self._items:
            return None
        position = 0.0
        if isinstance(position_seconds, (int, float)) and not isinstance(position_seconds, bool) \
                and not math.isnan(position_seconds):
            position = float(position_seconds)
        if position >= PREVIOUS_RESTART_SECONDS or self._cursor <= 0:
            return "restart"
        self._cursor -= 1
        return self.current

    def snapshot(self) -> tuple:
        """The list, the user-queued rows and the cursor, to put back with `restore`."""
        return list(self._items), list(self._explicit), list(self._added), self._cursor

    def restore(self, snapshot: tuple) -> None:
        items, explicit, added, cursor = snapshot
        self._items, self._explicit, self._added, self._cursor = list(items), list(explicit), list(added), cursor

    def mark_dead(self, track_id: object) -> dict[str, object] | None:
        """Drop a track that can't play, by playback id or URL. Returns what to play now, if any."""
        if not isinstance(track_id, str) or not track_id:
            return self.current

        def alive(row):
            return track_id not in (row.get("playbackId"), row.get("url"))

        self._explicit = [row for row in self._explicit if alive(row)]
        self._added = [row for row in self._added if alive(row)]
        removed = [n for n, row in enumerate(self._items) if not alive(row)]
        if not removed:
            return self.current
        current_died = self._cursor in removed
        self._cursor -= sum(1 for n in removed if n < self._cursor)
        self._items = [row for row in self._items if alive(row)]
        if not current_died:
            return self.current
        # The cursor now points at the next list item; step from just before it.
        self._cursor -= 1
        replacement = self._step(automatic=False)
        if replacement is None:
            self._cursor = max(0, min(self._cursor, len(self._items) - 1))
        return replacement

    def to_dict(self, position_ms: int = 0, now_ms: int | None = None) -> dict[str, object]:
        return {
            "v": 1,
            "items": [dict(row) for row in self._items],
            "cursor": self._cursor,
            "explicit": [dict(row) for row in self._explicit],
            "added": [dict(row) for row in self._added],
            "repeat": self._repeat,
            "shuffle": self._shuffle,
            "source": self.source,
            "positionMs": position_ms,
            "savedAt": int(time.time() * 1000) if now_ms is None else now_ms,
        }

    @classmethod
    def from_dict(cls, payload: object, rng: object | None = None) -> "PlayQueue":
        """A queue from saved data. Anything invalid gives an empty queue."""
        data = sanitize_queue(payload)
        queue = cls(repeat=data["repeat"], shuffle=data["shuffle"], rng=rng)
        queue._items = data["items"]
        queue._explicit = data["explicit"]
        queue._added = data["added"]
        queue._cursor = data["cursor"]
        queue.source = data["source"]
        return queue


def load_more_script(source: str) -> str | None:
    if source not in {"home", "feed"}:
        return None
    return rf"""(() => {{
      if (typeof window.__omarchyLoadMore !== 'function') {{
        return {{ started: false, hasMore: false }};
      }}
      return window.__omarchyLoadMore({json.dumps(source)});
    }})()"""


def api_tracks_script(source: str) -> str | None:
    if source not in {"home", "feed"}:
        return None
    return rf"""(() => {{
      const source = {json.dumps(source)};
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      window.__omarchyApiTrackLists = cache;
      const state = cache[source];
      if (!state || typeof state !== 'object') {{
        return {{ tracks: [], pending: true, error: '' }};
      }}
      const tracks = Array.isArray(state.tracks) ? state.tracks.slice(0, 100) : [];
      return {{
        tracks,
        pending: state.loading === true || state.completed !== true,
        error: typeof state.error === 'string' ? state.error.slice(0, 128) : ''
      }};
    }})()"""


def api_request_capture_script() -> str:
    return r"""(() => {
      const allowedPaths = new Set(['/stream', '/mixed-selections']);
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      window.__omarchyApiTrackLists = cache;
      const publishedSources = new Set();
      const pagination = Object.create(null);
      const xhrRequests = new WeakMap();
      const playbackTracks = new Map();
      let playbackGeneration = 0;
      let latestRequestContext = null;
      const routeGenerations = Object.create(null);
      const nextRouteGeneration = (path) => {
        const source = path === '/stream' ? 'feed' : 'home';
        routeGenerations[source] = (routeGenerations[source] || 0) + 1;
        return routeGenerations[source];
      };
      const boundedText = async (response, maximum) => {
        const declared = Number(response.headers && response.headers.get('content-length'));
        if (Number.isFinite(declared) && declared > maximum) throw new Error('response too large');
        if (!response.body || typeof response.body.getReader !== 'function')
          throw new Error('response body unavailable');
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8', { fatal: true });
        let total = 0;
        let text = '';
        try {
          while (true) {
            const part = await reader.read();
            if (part.done) break;
            if (!(part.value instanceof Uint8Array)) throw new Error('invalid response body');
            total += part.value.byteLength;
            if (total > maximum) {
              await reader.cancel();
              throw new Error('response too large');
            }
            text += decoder.decode(part.value, { stream: true });
          }
          text += decoder.decode();
          return text;
        } catch (error) {
          try { await reader.cancel(); } catch (_) {}
          throw error;
        }
      };
      const normalise = (payload, context, playbackTarget = playbackTracks) => {
        const queue = [[payload, 0]];
        const seenObjects = new Set();
        const seenTracks = new Set();
        const tracks = [];
        let visited = 0;
        while (queue.length && visited++ < 3000 && tracks.length < 50) {
          const current = queue.shift();
          const value = current[0];
          const depth = current[1];
          if (!value || typeof value !== 'object' || seenObjects.has(value)) continue;
          seenObjects.add(value);
          const permalink = typeof value.permalink_url === 'string' ? value.permalink_url : '';
          const title = typeof value.title === 'string' ? value.title.trim().slice(0, 512) : '';
          if ((value.kind === undefined || value.kind === 'track') && permalink && title
              && typeof value.duration === 'number' && !seenTracks.has(permalink)) {
            seenTracks.add(permalink);
            const user = value.user && typeof value.user === 'object' ? value.user : {};
            const publisher = value.publisher_metadata && typeof value.publisher_metadata === 'object'
              ? value.publisher_metadata : {};
            const track = {
              title,
              artist: String(user.username || user.full_name || publisher.artist || '').slice(0, 256),
              url: permalink.slice(0, 2048),
              artUrl: String(value.artwork_url || user.avatar_url || '').slice(0, 2048),
              playCount: Number.isSafeInteger(value.playback_count) ? value.playback_count : 0,
              durationMs: Number.isSafeInteger(value.duration) ? value.duration : 0
            };
            tracks.push(track);
            const transcodings = value.media && Array.isArray(value.media.transcodings)
              ? value.media.transcodings : [];
            // Offered streams but none the plugin can play: only encrypted HLS.
            if (transcodings.length > 0 && !transcodings.some((item) => item && item.format
                && ['hls', 'progressive'].includes(item.format.protocol))) {
              track.encrypted = true;
            }
            const playable = transcodings
              .filter((item) => item && item.format && typeof item.url === 'string'
                && item.url.length <= 4096
                && ['hls', 'progressive'].includes(item.format.protocol))
              .sort((left, right) => {
                const score = (item) => item.format.protocol === 'progressive' ? 0
                  : item.format.mime_type === 'audio/mpeg' ? 1 : 2;
                return score(left) - score(right);
              })[0];
            if (context && context.headers instanceof Headers) {
              try {
                const urn = typeof value.urn === 'string'
                    && /^soundcloud:tracks:[0-9]+$/.test(value.urn)
                  ? value.urn
                  : (Number.isSafeInteger(value.id) && value.id > 0
                    ? 'soundcloud:tracks:' + value.id : '');
                const endpoint = playable ? new URL(playable.url)
                  : (urn
                    ? new URL('https://api.soundcloud.com/tracks/soundcloud:tracks:'
                      + urn.substring('soundcloud:tracks:'.length) + '/streams') : null);
                if (endpoint && !playable) {
                  const clientId = context.headers.get('x-client-id')
                    || context.headers.get('x-soundcloud-client-id');
                  if (clientId) endpoint.searchParams.set('client_id', clientId);
                }
                if (endpoint.protocol === 'https:'
                    && ['api-v2.soundcloud.com', 'api.soundcloud.com'].includes(endpoint.hostname)
                    && !endpoint.port && !endpoint.username && !endpoint.password
                    && (endpoint.pathname.startsWith('/media/')
                      || /^\/tracks\/soundcloud:tracks:[0-9]+\/streams$/.test(endpoint.pathname))) {
                  track.playbackId = urn;
                  if (playbackTarget.size >= 200 && !playbackTarget.has(urn)) {
                    playbackTarget.delete(playbackTarget.keys().next().value);
                  }
                  playbackTarget.set(urn, {
                    endpoint: endpoint.href,
                    headers: new Headers(context.headers),
                    credentials: context.credentials,
                    protocol: playable ? playable.format.protocol : '',
                    mimeType: playable ? playable.format.mime_type : '',
                    streams: !playable,
                    waveformUrl: String(value.waveform_url || '').slice(0, 2048),
                    track
                  });
                }
              } catch (_) {}
            }
          }
          if (depth >= 8) continue;
          if (Array.isArray(value)) {
            for (const child of value.slice(0, 100)) queue.push([child, depth + 1]);
          } else {
            for (const key of Object.keys(value).slice(0, 100)) queue.push([value[key], depth + 1]);
          }
        }
        return tracks;
      };
      const homeTrackIds = (payload) => {
        const ids = [];
        const seen = new Set();
        const selections = payload && Array.isArray(payload.collection) ? payload.collection : [];
        for (const selection of selections.slice(0, 50)) {
          const itemState = selection && selection.items && typeof selection.items === 'object'
            ? selection.items : null;
          const items = Array.isArray(itemState) ? itemState
            : (itemState && Array.isArray(itemState.collection) ? itemState.collection : []);
          for (const item of items.slice(0, 50)) {
            const tracks = item && Array.isArray(item.tracks) ? item.tracks : [];
            for (const track of tracks.slice(0, 100)) {
              const id = track && Number.isSafeInteger(track.id) ? track.id : 0;
              if (id > 0 && !seen.has(id)) {
                seen.add(id);
                ids.push(id);
              }
              if (ids.length >= 50) return ids;
            }
          }
        }
        return ids;
      };
      const homeTracks = async (payload, context, playbackTarget = playbackTracks) => {
        const embedded = normalise(payload, context, playbackTarget);
        const ids = homeTrackIds(payload);
        if (!ids.length || !context || !(context.headers instanceof Headers)) return embedded;
        const request = new URL(context.requestUrl);
        if (request.protocol !== 'https:' || request.hostname !== 'api-v2.soundcloud.com'
            || request.port || request.username || request.password) return embedded;
        const clientId = request.searchParams.get('client_id');
        request.pathname = '/tracks';
        request.search = '';
        request.searchParams.set('ids', ids.join(','));
        if (clientId) request.searchParams.set('client_id', clientId);
        const response = await originalFetch(request.href, {
          method: 'GET', headers: context.headers, credentials: context.credentials,
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) throw new Error('home tracks unavailable');
        const text = await boundedText(response, 2 * 1024 * 1024);
        if (!text.length) throw new Error('invalid home tracks response');
        const expanded = normalise(JSON.parse(text), context, playbackTarget);
        const merged = new Map(embedded.map((track) => [track.url, track]));
        for (const track of expanded) merged.set(track.url, track);
        return Array.from(merged.values()).slice(0, 50);
      };
      const validNextUrl = (payload, path) => {
        try {
          const value = payload && typeof payload.next_href === 'string' ? payload.next_href : '';
          if (!value || value.length > 4096) return '';
          const url = new URL(value);
          if (url.protocol !== 'https:'
              || url.hostname !== 'api-v2.soundcloud.com'
              || (url.port && url.port !== '443')
              || url.username || url.password
              || url.pathname !== path) return '';
          return url.href;
        } catch (_) { return ''; }
      };
      const requestContext = (input, init) => {
        const request = input instanceof Request ? input : null;
        const headers = new Headers(request ? request.headers : undefined);
        if (init && init.headers) {
          for (const [name, value] of new Headers(init.headers)) headers.set(name, value);
        }
        const privateHeaders = new Headers();
        for (const name of ['authorization', 'x-client-id', 'x-soundcloud-client-id']) {
          const value = headers.get(name);
          if (value && value.length <= 2048) privateHeaders.set(name, value);
        }
        return {
          headers: privateHeaders,
          credentials: String((init && init.credentials) || (request && request.credentials) || 'omit'),
          requestUrl: new URL(request ? request.url : input, location.href).href
        };
      };
      const finishWithoutPage = (source, error, requestUrl, routeGeneration) => {
        if (routeGeneration !== routeGenerations[source]) return;
        const state = pagination[source];
        if (state) {
          state.loading = false;
          if (requestUrl && state.seen instanceof Set) state.seen.delete(requestUrl);
          if (state.lastRequested === requestUrl) state.lastRequested = '';
        }
        const current = cache[source];
        if (current) {
          current.loading = false;
          current.completed = true;
          current.error = String(error || '').slice(0, 128);
        }
        window.webkit.messageHandlers.omarchyTracks.postMessage(JSON.stringify({
          source,
          tracks: [],
          reset: false,
          hasMore: true,
          addedCount: 0,
          error: String(error || 'Could not load more tracks').slice(0, 128)
        }));
      };
      const publish = async (path, text, context, routeGeneration) => {
        try {
          if (!(text.length > 0 && text.length <= 2 * 1024 * 1024)) throw new Error('invalid response');
          const payload = JSON.parse(text);
          const stagedPlayback = new Map();
          const tracks = path === '/mixed-selections'
            ? await homeTracks(payload, context, stagedPlayback)
            : normalise(payload, context, stagedPlayback);
          const source = path === '/stream' ? 'feed' : 'home';
          if (routeGeneration !== routeGenerations[source]) return;
          for (const [urn, item] of stagedPlayback) {
            if (playbackTracks.size >= 200 && !playbackTracks.has(urn)) {
              playbackTracks.delete(playbackTracks.keys().next().value);
            }
            playbackTracks.set(urn, item);
          }
          const reset = !publishedSources.has(source);
          publishedSources.add(source);
          const previous = !reset && cache[source] && Array.isArray(cache[source].tracks)
            ? cache[source].tracks : [];
          const merged = new Map(previous.map((track) => [track.url, track]));
          for (const track of tracks) merged.set(track.url, track);
          const visibleTracks = Array.from(merged.values()).slice(0, 100);
          const addedCount = reset ? visibleTracks.length
            : Math.max(0, visibleTracks.length - previous.length);
          const candidateNext = validNextUrl(payload, path);
          const seen = context.seen instanceof Set ? context.seen : new Set();
          if (context.requestUrl) seen.add(context.requestUrl);
          const hasMore = visibleTracks.length < 100
            && (reset || addedCount > 0)
            && !!candidateNext
            && !seen.has(candidateNext);
          pagination[source] = {
            next: hasMore ? candidateNext : '',
            path,
            headers: context.headers,
            credentials: context.credentials,
            loading: false,
            lastRequested: String(context.requestUrl || ''),
            seen
          };
          cache[source] = {
            tracks: visibleTracks,
            loading: false,
            completed: true,
            error: ''
          };
          window.webkit.messageHandlers.omarchyTracks.postMessage(
            JSON.stringify({ source, tracks, reset, hasMore, addedCount })
          );
        } catch (_) {
          if (context.requestUrl) {
            finishWithoutPage(
              path === '/stream' ? 'feed' : 'home',
              'Could not load more tracks',
              context.requestUrl,
              routeGeneration
            );
          }
        }
      };
      const routeFor = (input) => {
        try {
          const request = input instanceof Request ? input : null;
          const url = new URL(request ? request.url : input, location.href);
          if (url.protocol !== 'https:'
              || url.hostname !== 'api-v2.soundcloud.com'
              || (url.port && url.port !== '443')
              || url.username || url.password
              || !allowedPaths.has(url.pathname)) return '';
          return url.pathname;
        } catch (_) { return ''; }
      };
      const markLoading = (path) => {
        if (!path) return;
        const source = path === '/stream' ? 'feed' : 'home';
        const previous = cache[source];
        cache[source] = {
          tracks: previous && Array.isArray(previous.tracks) ? previous.tracks.slice(0, 100) : [],
          loading: true,
          completed: false,
          error: ''
        };
      };
      const originalFetch = window.fetch.bind(window);
      const loadWaveform = async (value) => {
        const source = new URL(String(value || '').replace(/\.png$/, '.json'));
        if (source.protocol !== 'https:' || source.hostname !== 'wave.sndcdn.com'
            || source.port || source.username || source.password
            || !/^\/[A-Za-z0-9_-]+\.json$/.test(source.pathname)) return [];
        const response = await originalFetch(source.href, {
          method: 'GET', credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) return [];
        const payload = JSON.parse(await boundedText(response, 262144));
        const samples = payload && Array.isArray(payload.samples) ? payload.samples : [];
        const height = Number(payload.height);
        if (!samples.length || !(height > 0)) return [];
        const count = 76;
        const bars = [];
        for (let index = 0; index < count; index++) {
          const start = Math.floor(index * samples.length / count);
          const end = Math.max(start + 1, Math.floor((index + 1) * samples.length / count));
          let peak = 0;
          for (let sample = start; sample < end && sample < samples.length; sample++) {
            const level = Number(samples[sample]);
            if (Number.isFinite(level) && level > peak) peak = level;
          }
          bars.push(Math.min(1, peak / height) * 100);
        }
        // Stretch each track's own range so dense mixes still show shape.
        const lowest = Math.min(...bars);
        const highest = Math.max(...bars);
        if (highest - lowest < 5) return bars.map(Math.round);
        return bars.map((level) => Math.round(20 + (level - lowest) / (highest - lowest) * 80));
      };
      window.__omarchyPlayApiTrack = function(value, requestId) {
        const target = String(value || '');
        if (!/^soundcloud:tracks:[0-9]+$/.test(target)
            || !Number.isSafeInteger(requestId) || requestId < 0) return false;
        const generation = ++playbackGeneration;
        const pageAudio = document.querySelector('audio');
        if (pageAudio && !pageAudio.paused) pageAudio.pause();
        const loadItem = async () => {
          let item = playbackTracks.get(target);
          if (item) return item;
          const waitDeadline = Date.now() + 30000;
          while (!latestRequestContext && Date.now() < waitDeadline) {
            if (generation !== playbackGeneration) throw new Error('selection superseded');
            await new Promise((resolve) => setTimeout(resolve, 25));
          }
          const id = target.substring('soundcloud:tracks:'.length);
          const context = latestRequestContext;
          if (!context) throw new Error('track context unavailable');
          const request = new URL(context.requestUrl);
          if (request.protocol !== 'https:' || request.hostname !== 'api-v2.soundcloud.com'
              || request.port || request.username || request.password)
            throw new Error('track context unavailable');
          const clientId = request.searchParams.get('client_id');
          request.pathname = '/tracks';
          request.search = '';
          request.searchParams.set('ids', id);
          if (clientId) request.searchParams.set('client_id', clientId);
          const response = await originalFetch(request.href, {
            method: 'GET', headers: context.headers, credentials: context.credentials,
            cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
          });
          if (!response.ok) throw new Error('track unavailable');
          const text = await boundedText(response, 2 * 1024 * 1024);
          if (!text.length) throw new Error('invalid track response');
          normalise(JSON.parse(text), context);
          item = playbackTracks.get(target);
          if (!item) throw new Error('track unavailable');
          return item;
        };
        const itemLoaded = loadItem();
        const waveformLoaded = itemLoaded.then((item) => loadWaveform(item.waveformUrl)).catch(() => []);
        itemLoaded.then((item) => originalFetch(item.endpoint, {
          method: 'GET', headers: item.headers, credentials: item.credentials,
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
        }).then((response) => {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return boundedText(response, 16384);
        }).then((text) => {
          if (generation !== playbackGeneration) return;
          if (!text || text.length > 16384) throw new Error('invalid stream response');
          const payload = JSON.parse(text);
          let resolved = String(payload.url || '');
          let protocol = item.protocol;
          let mimeType = item.mimeType;
          if (item.streams) {
            if (typeof payload.hls_aac_160_url === 'string') {
              resolved = payload.hls_aac_160_url;
              protocol = 'hls';
              mimeType = 'audio/mp4';
            } else if (typeof payload.hls_aac_96_url === 'string') {
              resolved = payload.hls_aac_96_url;
              protocol = 'hls';
              mimeType = 'audio/mp4';
            } else if (typeof payload.http_mp3_128_url === 'string') {
              resolved = payload.http_mp3_128_url;
              protocol = 'progressive';
              mimeType = 'audio/mpeg';
            } else if (typeof payload.hls_mp3_128_url === 'string') {
              resolved = payload.hls_mp3_128_url;
              protocol = 'hls';
              mimeType = 'audio/mpeg';
            }
          }
          const stream = new URL(resolved);
          const host = stream.hostname.toLowerCase();
          if (stream.protocol !== 'https:' || stream.port || stream.username || stream.password
              || !(host.endsWith('.sndcdn.com') || host.endsWith('.soundcloud.cloud')))
            throw new Error('invalid stream URL');
          // Never hold playback for the waveform: a slow fetch just means no bars.
          let timer;
          const waveformDeadline = new Promise((resolve) => { timer = setTimeout(() => resolve([]), 500); });
          return Promise.race([waveformLoaded, waveformDeadline]).then((waveform) => {
            clearTimeout(timer);
            if (generation !== playbackGeneration) return;
            window.webkit.messageHandlers.omarchyPlayback.postMessage(JSON.stringify({
              ok: true,
              requestId,
              streamUrl: stream.href,
              protocol,
              mimeType,
              track: item.track,
              waveform
            }));
          });
        })).catch((error) => {
          if (generation !== playbackGeneration) return;
          const detail = /^HTTP [0-9]{3}$/.test(String(error && error.message || ''))
            ? ' (' + error.message + ')' : '';
          window.webkit.messageHandlers.omarchyPlayback.postMessage(JSON.stringify({
            ok: false,
            requestId,
            error: 'SoundCloud could not resolve this track' + detail
          }));
        });
        return true;
      };
      window.__omarchyPlaybackTrackCount = () => playbackTracks.size;
      window.fetch = function(input, init) {
        const path = routeFor(input);
        const context = path ? requestContext(input, init) : null;
        const routeGeneration = path ? nextRouteGeneration(path) : 0;
        if (context) latestRequestContext = context;
        markLoading(path);
        return originalFetch(input, init).then((response) => {
          if (path && response.ok) {
            boundedText(response.clone(), 2 * 1024 * 1024)
              .then((text) => publish(path, text, context, routeGeneration))
              .catch(() => {
                if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) return;
                const current = cache[path === '/stream' ? 'feed' : 'home'];
                if (current) current.loading = false;
              });
          } else if (path) {
            if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) return response;
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
          return response;
        }).catch((error) => {
          if (path) {
            if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) throw error;
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
          throw error;
        });
      };
      const originalOpen = XMLHttpRequest.prototype.open;
      const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
      const originalSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url) {
        const path = routeFor(url);
        xhrRequests.set(this, {
          path,
          headers: new Headers(),
          credentials: 'include',
          requestUrl: path ? new URL(url, location.href).href : ''
        });
        return originalOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        const context = xhrRequests.get(this);
        if (context && context.path
            && /^(authorization|x-client-id|x-soundcloud-client-id)$/i.test(String(name))) {
          const bounded = String(value);
          if (bounded.length <= 2048) context.headers.set(String(name), bounded);
        }
        return originalSetRequestHeader.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function() {
        const context = xhrRequests.get(this) || { path: '', headers: new Headers(), credentials: 'omit' };
        const path = context.path;
        const routeGeneration = path ? nextRouteGeneration(path) : 0;
        if (path) latestRequestContext = context;
        markLoading(path);
        if (path) this.addEventListener('load', function() {
          if (this.status >= 200 && this.status < 300
              && (!this.responseType || this.responseType === 'text')) {
            const text = this.responseText;
            if (typeof text === 'string' && text.length <= 2 * 1024 * 1024)
              publish(path, text, context, routeGeneration);
          } else {
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
        }, { once: true });
        for (const eventName of ['error', 'abort', 'timeout']) {
          if (path) this.addEventListener(eventName, function() {
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }, { once: true });
        }
        return originalSend.apply(this, arguments);
      };
      window.__omarchyLoadMore = function(source) {
        const expectedPath = source === 'feed' ? '/stream'
          : (source === 'home' ? '/mixed-selections' : '');
        const state = pagination[source];
        if (!expectedPath || !state || state.path !== expectedPath || !state.next) {
          return { started: false, hasMore: false };
        }
        if (state.loading) return { started: false, hasMore: true };
        const requestUrl = state.next;
        const seen = state.seen instanceof Set ? state.seen : new Set();
        if (seen.has(requestUrl) || seen.size >= 128) {
          state.next = '';
          return { started: false, hasMore: false };
        }
        seen.add(requestUrl);
        state.seen = seen;
        state.loading = true;
        state.lastRequested = requestUrl;
        markLoading(expectedPath);
        const routeGeneration = nextRouteGeneration(expectedPath);
        originalFetch(requestUrl, {
          method: 'GET',
          headers: state.headers,
          credentials: state.credentials
        }).then((response) => {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return boundedText(response, 2 * 1024 * 1024);
        }).then((text) => publish(expectedPath, text, {
          headers: state.headers,
          credentials: state.credentials,
          requestUrl,
          seen
        }, routeGeneration)).catch(() => finishWithoutPage(
          source, 'Could not load more tracks', requestUrl, routeGeneration
        ));
        return { started: true, hasMore: true };
      };
    })()"""


def api_observation_script() -> str:
    return r"""(() => {
      const paths = [];
      for (const name of performance.getEntriesByType('resource').slice(-500).map((entry) => entry.name)) {
        try {
          const candidate = new URL(name);
          if (candidate.hostname === 'api-v2.soundcloud.com' && paths.indexOf(candidate.pathname) < 0) {
            paths.push(candidate.pathname);
          }
        } catch (_) {}
        if (paths.length >= 32) break;
      }
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      const lists = Object.create(null);
      for (const source of ['home', 'feed']) {
        const state = cache[source];
        if (!state) continue;
        lists[source] = {
          count: Array.isArray(state.tracks) ? state.tracks.length : 0,
          loading: state.loading === true,
          error: String(state.error || '').slice(0, 128)
        };
      }
      return {
        paths: paths.slice(0, 32),
        lists,
        playbackCount: typeof window.__omarchyPlaybackTrackCount === 'function'
          ? Number(window.__omarchyPlaybackTrackCount()) : 0
      };
    })()"""


def play_track_script(playback_id: str, request_id: int) -> str | None:
    if (not is_playback_id(playback_id) or isinstance(request_id, bool)
            or not isinstance(request_id, int) or not 0 <= request_id <= 2**31 - 1):
        return None
    target = json.dumps(playback_id)
    return rf"""(() => {{
      const playbackId = {target};
      return typeof window.__omarchyPlayApiTrack === 'function'
        && window.__omarchyPlayApiTrack(playbackId, {request_id});
    }})()"""


def status_script() -> str:
    return r"""(() => {
      const audio = document.querySelector('audio');
      const playerPresent = !!audio;
      const playControl = document.querySelector('.playControls__play, .playControl');
      const controlSaysPause = !!playControl && (
        playControl.classList.contains('playing') ||
        /Pause/i.test(playControl.getAttribute('title') || '') ||
        /Pause/i.test(playControl.getAttribute('aria-label') || '')
      );
      const text = (...selectors) => {
        for (const selector of selectors) {
          const node = document.querySelector(selector);
          if (node && node.textContent.trim()) return node.textContent.trim().slice(0, 512);
        }
        return '';
      };
      const parseClock = (value) => {
        const parts = String(value || '').trim().split(':').map(Number);
        if (!parts.length || parts.some((part) => !Number.isFinite(part))) return 0;
        return parts.reduce((total, part) => total * 60 + part, 0);
      };
      const timelinePosition = parseClock(text(
        '.playbackTimeline__timePassed span[aria-hidden="true"]',
        '.playbackTimeline__timePassed'
      ));
      const timelineDuration = parseClock(text(
        '.playbackTimeline__duration span[aria-hidden="true"]',
        '.playbackTimeline__duration'
      ));
      let artUrl = '';
      for (const selector of [
        '.playbackSoundBadge__avatar img',
        '.playbackSoundBadge__avatar [style*="background-image"]',
        '.playbackSoundBadge__avatar .sc-artwork',
        '.playbackSoundBadge__avatar'
      ]) {
        const art = document.querySelector(selector);
        if (!art) continue;
        if (art.src) { artUrl = art.src.slice(0, 2048); break; }
        const background = art.style.backgroundImage || getComputedStyle(art).backgroundImage || '';
        const match = background.match(/url\(["']?(.*?)["']?\)/);
        if (match) { artUrl = match[1].slice(0, 2048); break; }
      }
      return {
        playerPresent,
        playing: playerPresent ? !audio.paused : false,
        title: playerPresent
          ? text('.playbackSoundBadge__titleLink span[aria-hidden="true"]', '.playbackSoundBadge__titleLink') : '',
        artist: playerPresent
          ? text('.playbackSoundBadge__lightLink', '.playbackSoundBadge__titleContextContainer a') : '',
        artUrl: playerPresent ? artUrl : '',
        loggedIn: !/^\/(signin|register)/.test(location.pathname) &&
          !!document.querySelector('.header__userNavButton, a[href*="/you/library"]'),
        url: location.href,
        duration: playerPresent && isFinite(audio.duration) ? audio.duration : 0,
        position: playerPresent && isFinite(audio.currentTime) ? audio.currentTime : 0
      };
    })()"""


def _bounded_string(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        return None
    return value


def _finite_number(value: object, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return minimum
    return value if minimum <= value <= maximum else minimum


def validate_status_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    title = _bounded_string(payload.get("title", ""), MAX_TITLE_BYTES)
    artist = _bounded_string(payload.get("artist", ""), MAX_ARTIST_BYTES)
    page_url = _bounded_string(payload.get("url", ""), MAX_URL_BYTES)
    art_url = _bounded_string(payload.get("artUrl", ""), MAX_URL_BYTES)
    duration = _finite_number(payload.get("duration", 0), 0, 60 * 60 * 24 * 7)
    position = _finite_number(payload.get("position", 0), 0, 60 * 60 * 24 * 7)
    error = _bounded_string(payload.get("error", ""), 128)
    player_present = payload.get("playerPresent", True)
    playback_state = payload.get("playbackState")
    if playback_state is None:
        playback_state = "playing" if payload.get("playing") is True else (
            "paused" if player_present is True else "idle"
        )
    if (None in (title, artist, page_url, art_url, duration, position, error)
            or not isinstance(player_present, bool)
            or playback_state not in {"idle", "resolving", "buffering", "playing", "paused", "error"}):
        return None
    if page_url and not is_soundcloud_uri(page_url):
        return None
    if art_url and not is_allowed_artwork_uri(art_url):
        art_url = ""
    return {
        "playerPresent": player_present,
        "playbackState": playback_state,
        "playing": payload.get("playing") is True,
        "playbackId": payload.get("playbackId") if is_playback_id(payload.get("playbackId")) else "",
        "title": title,
        "artist": artist,
        "artUrl": art_url,
        "loggedIn": payload.get("loggedIn") is True,
        "url": page_url,
        "duration": duration,
        "position": min(position, duration) if duration else position,
        "waveform": validate_waveform(payload.get("waveform", [])),
        **({"error": error} if error else {}),
        **_queue_status_fields(payload),
        **_setting_status_fields(payload),
    }


def _queue_status_fields(payload: dict[str, object]) -> dict[str, object]:
    """The optional queue keys of a status payload; anything missing or invalid is left out."""
    fields: dict[str, object] = {}
    for key in ("canNext", "canPrevious", "shuffle"):
        if isinstance(payload.get(key), bool):
            fields[key] = payload[key]
    for key in ("cursor", "length"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_TRACKS:
            fields[key] = value
    if payload.get("repeat") in REPEAT_MODES:
        fields["repeat"] = payload["repeat"]
    return fields


def _setting_status_fields(payload: dict[str, object]) -> dict[str, object]:
    """The optional volume, setting and track link keys of a status payload."""
    fields: dict[str, object] = {}
    volume = _finite_number(payload.get("volume"), 0, 1)
    if volume is not None:
        fields["volume"] = volume
    for key in ("muted", "autoplay"):
        if isinstance(payload.get(key), bool):
            fields[key] = payload[key]
    if "trackUrl" in payload:
        track_url = _bounded_string(payload["trackUrl"], MAX_URL_BYTES)
        fields["trackUrl"] = track_url if track_url and is_soundcloud_uri(track_url) else ""
    return fields


def queue_status(queue: "PlayQueue") -> dict[str, object]:
    return {
        "canNext": queue.can_next,
        "canPrevious": queue.can_previous,
        "cursor": queue.cursor,
        "length": queue.length,
        "repeat": queue.repeat,
        "shuffle": queue.shuffle,
    }


def restored_status(row: dict[str, object], position_ms: int) -> dict[str, object]:
    """The saved track shown as paused at its saved position, with no audio started."""
    duration = _bounded_integer(row.get("durationMs"), 0, MAX_TRACK_DURATION_MS) / 1000
    position = position_ms / 1000
    return {
        "playerPresent": True,
        "playbackState": "paused",
        "playing": False,
        "playbackId": str(row.get("playbackId", "")),
        "title": str(row.get("title", "")),
        "artist": str(row.get("artist", "")),
        "artUrl": str(row.get("artUrl", "")),
        "duration": duration,
        "position": min(position, duration) if duration else position,
        "waveform": [],
    }


def merge_status(page_cache: dict[str, object], gst_status: dict[str, object]) -> dict[str, object]:
    # Playback fields come from GStreamer; only the page knows login and URL.
    merged = dict(gst_status)
    merged["loggedIn"] = page_cache.get("loggedIn") is True
    merged["url"] = page_cache.get("url", "")
    merged["playerPresent"] = (
        gst_status.get("playerPresent") is True or page_cache.get("playerPresent") is True
    )
    error = gst_status.get("error") or page_cache.get("error")
    if error:
        merged["error"] = error
    return merged


class PageStatusCache:
    """Slow-changing facts from the hidden page, polled so the fast status tick never waits."""

    def __init__(self, evaluate, script: str):
        self.evaluate = evaluate
        self.script = script
        self.values: dict[str, object] = {
            "loggedIn": False, "url": "", "playerPresent": False, "error": "",
        }
        self.seeded = False
        self.waiting: list = []
        self.pending = False

    def update(self, payload: object) -> bool:
        """Store a page answer. Returns True when loggedIn or url changed."""
        if not isinstance(payload, dict):
            return False
        if payload.get("error"):
            # Keep the previous values; the page may only be navigating
            self.values["error"] = str(payload["error"])[:100]
            return False
        before = (self.values["loggedIn"], self.values["url"])
        url = payload.get("url")
        self.values.update({
            "loggedIn": payload.get("loggedIn") is True,
            "url": url if isinstance(url, str) else "",
            "playerPresent": payload.get("playerPresent") is True,
            "error": "",
        })
        self.seeded = True
        return before != (self.values["loggedIn"], self.values["url"])

    def poll(self, callback=None) -> None:
        if callback:
            self.waiting.append(callback)
        if self.pending:
            return
        self.pending = True

        def finished(payload):
            self.pending = False
            changed = self.update(payload)
            waiting, self.waiting = self.waiting, []
            for notify in waiting:
                notify(changed)

        self.evaluate(self.script, finished)


class AutoSkip:
    """Remembers which track start was automatic, so a track that fails at once can be skipped.

    A chain of skips starts when a track ends by itself and is capped at the queue length
    then, so a queue where nothing plays cannot loop. A track the user picks is never skipped.
    The user's own Next (`direction` 1) and Previous (-1) are skipped on failure too, in their
    direction, with the same cap.
    """

    def __init__(self):
        self.automatic = False
        self.direction = 0
        self.generation = -1
        self.started_at = 0.0
        self.skipped = 0
        self.limit = 0

    def begin_chain(self, queue_length: int) -> None:
        self.skipped = 0
        self.limit = queue_length

    def started(self, generation: int, automatic: bool, now: float, direction: int = 0) -> None:
        self.generation = generation
        self.automatic = automatic
        self.direction = direction
        self.started_at = now
        if not automatic and not direction:
            self.skipped = 0

    def should_skip(self, generation: int, now: float) -> bool:
        """Whether the failure of the track started under `generation` moves on. Counts it."""
        if (not (self.automatic or self.direction) or generation != self.generation
                or now - self.started_at > AUTO_SKIP_WINDOW_SECONDS
                or self.skipped >= self.limit):
            return False
        self.skipped += 1
        return True

    def notice(self) -> str:
        return f"Skipped {self.skipped} track{'' if self.skipped == 1 else 's'} that can't play"


def validate_waveform(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) > MAX_WAVEFORM_BARS:
        return []
    if not all(
        isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 100
        for level in value
    ):
        return []
    return list(value)


def validate_tracks_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
        return None
    incoming = payload["tracks"]
    if len(incoming) > MAX_TRACKS:
        return None
    tracks = []
    for item in incoming:
        if not isinstance(item, dict):
            return None
        title = _bounded_string(item.get("title", ""), MAX_TITLE_BYTES)
        artist = _bounded_string(item.get("artist", ""), MAX_ARTIST_BYTES)
        track_url = _bounded_string(item.get("url", ""), MAX_URL_BYTES)
        art_url = _bounded_string(item.get("artUrl", ""), MAX_URL_BYTES)
        playback_id = _bounded_string(item.get("playbackId", ""), 64)
        played_at = _bounded_integer(item.get("playedAt"), 0, MAX_PLAYED_AT_MS)
        if (None in (title, artist, track_url, art_url, playback_id)
                or not title or not isinstance(track_url, str) or not is_soundcloud_uri(track_url)
                or (playback_id and not is_playback_id(playback_id))):
            return None
        if art_url and not is_allowed_artwork_uri(art_url):
            art_url = ""
        tracks.append(
            {
                "playbackId": playback_id,
                "title": title,
                "artist": artist,
                "url": track_url,
                "artUrl": art_url,
                "playCount": _bounded_integer(item.get("playCount"), 0, MAX_PLAY_COUNT),
                "durationMs": _bounded_integer(
                    item.get("durationMs"), 0, MAX_TRACK_DURATION_MS
                ),
                **({"playedAt": played_at} if played_at else {}),
                # optional flags, kept only when true
                **{flag: True for flag in TRACK_FLAGS if item.get(flag) is True},
            }
        )
    return {"tracks": tracks}


def sanitize_track_cache(payload: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(payload, dict):
        return {}
    sanitized = {}
    for source in ("home", "feed", "plays", *API_TRACK_LISTS):
        tracks = payload.get(source)
        if not isinstance(tracks, list) or not tracks:
            continue
        result = validate_tracks_payload({"tracks": tracks})
        validated_tracks = result.get("tracks") if result is not None else None
        if isinstance(validated_tracks, list) and validated_tracks:
            sanitized[source] = validated_tracks
    return sanitized


def sanitize_queue(payload: object) -> dict[str, object]:
    """Saved queue data checked field by field. Anything invalid gives the empty queue."""
    empty: dict[str, object] = {
        "v": 1, "items": [], "cursor": 0, "explicit": [], "added": [], "repeat": "none",
        "shuffle": False, "source": "", "positionMs": 0, "savedAt": 0,
    }
    if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload["v"] != 1:
        return empty
    lists = []
    for key in ("items", "explicit", "added"):  # older saves have no "added"
        result = validate_tracks_payload({"tracks": payload.get(key, [])})
        if result is None:
            return empty
        lists.append(_queue_rows(result["tracks"]))
    items, explicit, added = lists
    cursor = payload.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int):
        cursor = 0
    repeat = payload.get("repeat")
    return {
        "v": 1,
        "items": items,
        "cursor": max(0, min(cursor, len(items) - 1)),
        "explicit": explicit,
        "added": added,
        "repeat": repeat if repeat in REPEAT_MODES else "none",
        "shuffle": payload.get("shuffle") is True,
        "source": _bounded_string(payload.get("source", ""), MAX_QUEUE_SOURCE_BYTES) or "",
        "positionMs": _bounded_integer(payload.get("positionMs"), 0, MAX_TRACK_DURATION_MS),
        "savedAt": _bounded_integer(payload.get("savedAt"), 0, MAX_PLAYED_AT_MS),
    }


DEFAULT_SETTINGS: dict[str, object] = {"v": 1, "volume": 1.0, "muted": False, "autoplay": True}


def sanitize_settings(payload: object) -> dict[str, object]:
    """Saved settings checked key by key. Anything missing or invalid gets its default."""
    settings = dict(DEFAULT_SETTINGS)
    if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload["v"] != 1:
        return settings
    volume = _finite_number(payload.get("volume"), 0, 1)
    if volume is not None:
        settings["volume"] = volume
    for key in ("muted", "autoplay"):
        if isinstance(payload.get(key), bool):
            settings[key] = payload[key]
    return settings


class SettingsStore:
    """Volume, mute and the autoplay choice in `settings.json`."""

    def __init__(self, directory_fd: int, maximum_bytes: int = MAX_SETTINGS_BYTES):
        self._store = PrivateJsonStore(directory_fd, "settings.json", maximum_bytes)

    def load(self) -> dict[str, object]:
        return sanitize_settings(self._store.load())

    def save(self, settings: dict[str, object]) -> None:
        self._store.save(sanitize_settings(settings))


class QueueStore:
    """The play queue in `queue.json`, next to the track cache."""

    def __init__(self, directory_fd: int, maximum_bytes: int = MAX_QUEUE_BYTES):
        self._store = PrivateJsonStore(directory_fd, "queue.json", maximum_bytes)

    def load(self) -> tuple[PlayQueue, int]:
        """The saved queue and its position in ms. A missing or bad file gives an empty queue."""
        data = sanitize_queue(self._store.load())
        return PlayQueue.from_dict(data), cast(int, data["positionMs"])

    def save(self, queue: PlayQueue, position_ms: int = 0) -> None:
        # Like the track cache, a payload over the size cap is not written.
        self._store.save(queue.to_dict(position_ms))


def _image_kind_and_dimensions(data: bytes) -> tuple[str, int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(data[index + 3:index + 5], "big")
                width = int.from_bytes(data[index + 5:index + 7], "big")
                return "image/jpeg", width, height
            index += length
    return None


def _decode_chunked_body(encoded: bytes, maximum: int) -> bytes | None:
    position = 0
    decoded = bytearray()
    while True:
        line_end = encoded.find(b"\r\n", position)
        if line_end < 0 or line_end - position > 64:
            return None
        size_text = encoded[position:line_end].split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError:
            return None
        if size < 0 or len(decoded) + size > maximum:
            return None
        position = line_end + 2
        if size == 0:
            return bytes(decoded) if encoded[position:position + 2] == b"\r\n" else None
        end = position + size
        if end + 2 > len(encoded) or encoded[end:end + 2] != b"\r\n":
            return None
        decoded.extend(encoded[position:end])
        position = end + 2


def fetch_artwork_data_url(
    uri: str,
    max_bytes: int = MAX_ARTWORK_BYTES,
    max_pixels: int = MAX_ARTWORK_PIXELS,
    max_dimension: int = 2048,
) -> str:
    if not is_allowed_artwork_uri(uri):
        return ""
    parsed = urlparse(uri)
    host = parsed.hostname.lower().rstrip(".")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    try:
        request_target = target.encode("ascii").decode("ascii")
    except UnicodeEncodeError:
        return ""
    deadline = time.monotonic() + 5.0
    resolver = (
        "import ipaddress,json,socket,sys;"
        "values=[];"
        "[(values.append(x[4][0])) for x in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM) "
        "if x[4][0] not in values];"
        "print(json.dumps(values[:16]))"
    )
    try:
        process = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-c", resolver, host],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except OSError:
        return ""
    try:
        output, _ = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.2)
        return ""
    if process.returncode != 0 or len(output) > 4096:
        return ""
    try:
        resolved = json.loads(output.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(resolved, list) or len(resolved) > 16:
        return ""
    addresses = []
    for value in resolved:
        try:
            address = ipaddress.ip_address(value)
        except (TypeError, ValueError):
            return ""
        if not address.is_global:
            return ""
        if address.version == 6:
            addresses.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, (str(address), 443, 0, 0)))
        else:
            addresses.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (str(address), 443)))
    if not addresses:
        return ""

    context = ssl.create_default_context()
    for family, socktype, proto, sockaddr in addresses:
        raw = None
        tls = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(remaining)
            raw.connect(sockaddr)
            tls = context.wrap_socket(raw, server_hostname=host)
            raw = None
            request = (
                f"GET {request_target} HTTP/1.1\r\nHost: {host}\r\n"
                "Accept: image/png,image/jpeg\r\nConnection: close\r\n"
                "User-Agent: Omarchy-SoundCloud/0.1\r\n\r\n"
            ).encode("ascii")
            tls.sendall(request)
            received = bytearray()
            while b"\r\n\r\n" not in received:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(received) > 16384:
                    return ""
                tls.settimeout(remaining)
                chunk = tls.recv(min(4096, 16385 - len(received)))
                if not chunk:
                    return ""
                received.extend(chunk)
            header_bytes, body_prefix = bytes(received).split(b"\r\n\r\n", 1)
            try:
                lines = header_bytes.decode("iso-8859-1").split("\r\n")
                status_parts = lines[0].split(" ", 2)
                status = int(status_parts[1])
                headers = {}
                for line in lines[1:]:
                    name, value = line.split(":", 1)
                    headers[name.lower().strip()] = value.strip()
            except (UnicodeDecodeError, ValueError, IndexError):
                return ""
            if status != 200:
                continue
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"image/png", "image/jpeg"}:
                continue
            length = headers.get("content-length")
            transfer_encoding = headers.get("transfer-encoding", "").lower()
            if transfer_encoding:
                if transfer_encoding != "chunked" or length is not None:
                    continue
                encoded = bytearray(body_prefix)
                encoded_limit = max_bytes + 65536
                data = _decode_chunked_body(encoded, max_bytes)
                while data is None and len(encoded) <= encoded_limit:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ""
                    tls.settimeout(remaining)
                    chunk = tls.recv(min(16384, encoded_limit + 1 - len(encoded)))
                    if not chunk:
                        return ""
                    encoded.extend(chunk)
                    data = _decode_chunked_body(encoded, max_bytes)
                if data is None:
                    return ""
            else:
                if length is None or not length.isdigit() or int(length) > max_bytes:
                    continue
                expected = int(length)
                if len(body_prefix) > expected:
                    body_prefix = body_prefix[:expected]
                chunks = [body_prefix]
                size = len(body_prefix)
                while size < expected:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ""
                    tls.settimeout(remaining)
                    chunk = tls.recv(min(16384, expected - size))
                    if not chunk:
                        return ""
                    chunks.append(chunk)
                    size += len(chunk)
                data = b"".join(chunks)
            details = _image_kind_and_dimensions(data)
            if details is None:
                return ""
            mime, width, height = details
            if (
                width <= 0
                or height <= 0
                or width > max_dimension
                or height > max_dimension
                or width * height > max_pixels
            ):
                return ""
            return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        except (OSError, ssl.SSLError):
            continue
        finally:
            if tls is not None:
                try:
                    tls.close()
                except OSError:
                    pass
            if raw is not None:
                raw.close()
    return ""


class ArtworkCache:
    def __init__(self, maximum_entries: int = 8, maximum_pending: int = 8, fetcher=None):
        self.maximum_entries = maximum_entries
        self.maximum_pending = maximum_pending
        self.fetcher = fetcher
        self.values: OrderedDict[str, str] = OrderedDict()
        self.inflight: set[str] = set()
        self.waiters: dict[str, list] = {}
        self.lock = threading.Lock()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="soundcloud-artwork"
        )

    def get(self, uri: str, on_ready) -> str:
        if not uri:
            return ""
        with self.lock:
            if uri in self.values:
                self.values.move_to_end(uri)
                return self.values[uri]
            if uri in self.inflight:
                self.waiters.setdefault(uri, []).append(lambda _value: on_ready())
                return ""
            if len(self.inflight) >= self.maximum_pending:
                return ""
            self.inflight.add(uri)
            self.waiters[uri] = [lambda _value: on_ready()]

        self._submit(uri)
        return ""

    def request(self, uri: str, callback) -> None:
        if not uri:
            callback("")
            return
        with self.lock:
            if uri in self.values:
                self.values.move_to_end(uri)
                value = self.values[uri]
            else:
                value = None
                if uri in self.inflight:
                    self.waiters.setdefault(uri, []).append(callback)
                    return
                if len(self.inflight) >= self.maximum_pending:
                    callback("")
                    return
                self.inflight.add(uri)
                self.waiters[uri] = [callback]
        if value is not None:
            callback(value)
            return
        self._submit(uri)

    def _submit(self, uri: str) -> None:

        def worker():
            try:
                fetcher = self.fetcher or fetch_artwork_data_url
                value = fetcher(uri)
            except Exception:
                value = ""
            finally:
                with self.lock:
                    self.inflight.discard(uri)
                    waiters = self.waiters.pop(uri, [])
                    self.values[uri] = value
                    self.values.move_to_end(uri)
                    while len(self.values) > self.maximum_entries:
                        self.values.popitem(last=False)
                for waiter in waiters:
                    waiter(value)

        self.executor.submit(worker)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


def command_script(action: str) -> str | None:
    scripts = {
        "play-pause": r"""(() => {
          const button = document.querySelector(
            '.playControls__play, .playControl, button[title="Play"], button[title="Pause"], button[aria-label="Play"], button[aria-label="Pause"]'
          );
          if (button) { button.click(); return true; }
          const audio = document.querySelector('audio');
          if (!audio) return false;
          if (audio.paused) { audio.play(); } else { audio.pause(); }
          return true;
        })()""",
    }
    return scripts.get(action)


def seek_script(ratio: float) -> str:
    clamped = max(0.0, min(1.0, float(ratio)))
    return rf"""(() => {{
      const ratio = {clamped!r};
      const audio = document.querySelector('audio');
      if (audio && isFinite(audio.duration) && audio.duration > 0) {{
        audio.currentTime = audio.duration * ratio;
        return true;
      }}
      const timeline = document.querySelector(
        '.playbackTimeline__progressWrapper, .playbackTimeline__progressBackground, [role="slider"]'
      );
      if (!timeline) return false;
      const bounds = timeline.getBoundingClientRect();
      const clientX = bounds.left + bounds.width * ratio;
      const eventOptions = {{ bubbles: true, cancelable: true, clientX, clientY: bounds.top + bounds.height / 2 }};
      for (const type of ['mousedown', 'mouseup', 'click'])
        timeline.dispatchEvent(new MouseEvent(type, eventOptions));
      return true;
    }})()"""


GSTREAMER_PACKAGE_ELEMENTS = (
    ("gst-plugins-good", (
        "playbin3", "appsrc", "mpegaudioparse", "mpg123audiodec",
        "audioconvert", "audioresample", "autoaudiosink", "hlsdemux2", "souphttpsrc",
    )),
    ("gst-libav", ("avdec_aac",)),
)


def missing_gstreamer_packages(find) -> list[str]:
    return [
        package for package, elements in GSTREAMER_PACKAGE_ELEMENTS
        if not all(find(name) for name in elements)
    ]


def install_hint(packages: list[str]) -> str:
    return f"Missing {', '.join(packages)}. Run: omarchy pkg add {' '.join(packages)}"


def dependency_check() -> dict[str, object]:
    result: dict[str, object] = {
        "python_gobject": False,
        "gtk": "3.0",
        "webkit2": "4.1",
        "gstreamer_autoaudiosink": False,
        "gstreamer_aac_decoder": False,
        "gstreamer_playback_elements": False,
        "ok": False,
    }
    try:
        import gi
    except ImportError:
        result["error"] = install_hint(["python-gobject"])
        return result
    result["python_gobject"] = True
    missing_libraries = []
    for namespace, version, package in (
        ("Gtk", "3.0", "gtk3"), ("WebKit2", "4.1", "webkit2gtk-4.1"), ("Gst", "1.0", "gstreamer"),
    ):
        try:
            gi.require_version(namespace, version)
        except ValueError:
            missing_libraries.append(package)
    if missing_libraries:
        result["error"] = install_hint(missing_libraries)
        return result
    try:
        from gi.repository import Gst, Gtk, WebKit2  # noqa: F401

        Gst.init(None)
        find = lambda name: bool(Gst.ElementFactory.find(name))
        result["gstreamer_autoaudiosink"] = find("autoaudiosink")
        result["gstreamer_aac_decoder"] = find("avdec_aac")
        result["gstreamer_playback_elements"] = all(
            find(name) for name in GSTREAMER_PACKAGE_ELEMENTS[0][1]
        )
        missing_packages = missing_gstreamer_packages(find)
        if missing_packages:
            result["error"] = install_hint(missing_packages)
        result["ok"] = not missing_packages
    except (ImportError, ValueError) as exc:
        result["error"] = str(exc)
    return result


def _load_gi():
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("Gtk", "3.0")
    gi.require_version("Soup", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gio, GLib, Gst, Gtk, Soup, WebKit2

    return Gio, GLib, Gst, Gtk, Soup, WebKit2


def validate_stream_uri(uri: object) -> str | None:
    value = _bounded_string(uri, 8192)
    if value is None:
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    host = parsed.hostname.lower().rstrip(".")
    if not (host.endswith(".sndcdn.com") or host.endswith(".soundcloud.cloud")):
        return None
    return value


class _NoMediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


_media_opener = build_opener(ProxyHandler({}), _NoMediaRedirect)


def _set_media_response_timeout(response, timeout: float) -> None:
    candidates = (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
    )
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "settimeout"):
            candidate.settimeout(timeout)
            return


def fetch_private_media(
    uri: str,
    maximum: int,
    *,
    deadline: float | None = None,
    cancelled=lambda: False,
) -> bytes:
    if cancelled():
        raise concurrent.futures.CancelledError
    remaining = 15.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("media operation deadline exceeded")
    validated = validate_stream_uri(uri)
    if validated is None:
        raise ValueError("invalid media URL")
    request = Request(validated, headers={"User-Agent": "Mozilla/5.0"})
    with _media_opener.open(request, timeout=min(15.0, remaining)) as response:
        if cancelled():
            raise concurrent.futures.CancelledError
        if validate_stream_uri(response.geturl()) is None:
            raise ValueError("invalid media response URL")
        length = response.headers.get("Content-Length")
        if length and int(length) > maximum:
            raise ValueError("media response too large")
        data = bytearray()
        while len(data) <= maximum:
            if cancelled():
                raise concurrent.futures.CancelledError
            remaining = 15.0 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("media operation deadline exceeded")
            _set_media_response_timeout(response, min(15.0, remaining))
            read = getattr(response, "read1", None)
            chunk_size = min(64 * 1024, maximum + 1 - len(data))
            part = cast(
                bytes,
                read(chunk_size) if callable(read) else response.read(min(1, chunk_size)),
            )
            if not part:
                break
            data.extend(part)
    if cancelled():
        raise concurrent.futures.CancelledError
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("media operation deadline exceeded")
    if len(data) > maximum:
        raise ValueError("media response too large")
    return bytes(data)


def hls_segment_list(
    uri: str,
    depth: int = 0,
    *,
    deadline: float | None = None,
    cancelled=lambda: False,
) -> list[tuple[str, float]]:
    """The (segment URL, seconds) pairs of an HLS playlist; the seconds come from #EXTINF."""
    if depth > 2:
        raise ValueError("nested HLS playlist")
    text = fetch_private_media(
        uri, 512 * 1024, deadline=deadline, cancelled=cancelled
    ).decode("utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    media_lines = [line for line in lines if not line.startswith("#")]
    if not media_lines:
        raise ValueError("empty HLS playlist")
    resolved = [urljoin(uri, line) for line in media_lines]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        return hls_segment_list(
            resolved[0], depth + 1, deadline=deadline, cancelled=cancelled
        )
    if len(resolved) > 4096 or any(validate_stream_uri(value) is None for value in resolved):
        raise ValueError("invalid HLS segments")
    durations, pending = [], 0.0
    for line in lines:
        if line.startswith("#EXTINF:"):
            try:
                pending = max(0.0, float(line[8:].partition(",")[0]))
            except ValueError:
                pending = 0.0
        elif not line.startswith("#"):
            durations.append(pending)
            pending = 0.0
    return list(zip(resolved, durations))


def hls_segment_urls(uri: str, depth: int = 0, **kwargs) -> list[str]:
    return [url for url, _seconds in hls_segment_list(uri, depth, **kwargs)]


# A relative seek stops this far before the end, so it never ends the track by itself.
SEEK_END_MARGIN = 1.0
SEEK_CHAIN_SECONDS = 1.5


def seek_by_target(position: float, delta: float, duration: float) -> float:
    """Where a relative seek lands: clamped to [0, duration - margin] (just >= 0 if unknown)."""
    target = position + delta
    if duration > 0:
        target = min(target, max(0.0, duration - SEEK_END_MARGIN))
    return max(0.0, target)


def seek_by_from_command(command: str) -> float | None:
    """The signed seconds of `seek-by:<seconds>`, or None if it isn't a sane number."""
    try:
        seconds = float(command.removeprefix("seek-by:"))
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and abs(seconds) <= 86400 else None


_api_opener = build_opener(ProxyHandler({}), _NoMediaRedirect)


def soundcloud_api_request(method: str, path: str, token: str, maximum: int = 256 * 1024) -> object:
    """Call SoundCloud's private web API directly with the signed-in session token."""
    if not path.startswith("/") or method not in {"GET", "PUT", "DELETE"}:
        raise ValueError("invalid API request")
    request = Request(
        "https://api-v2.soundcloud.com" + path,
        method=method,
        headers={
            "Authorization": "OAuth " + token,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with _api_opener.open(request, timeout=10.0) as response:
            data = response.read(maximum + 1)
    except http.client.HTTPException as error:  # IncompleteRead and friends: a cut or garbled reply
        raise OSError(f"API connection failed: {type(error).__name__}") from error
    if len(data) > maximum:
        raise ValueError("API response too large")
    return json.loads(data) if data.strip() else None


def _truncate_bytes(text: str, maximum: int) -> str:
    """`text` cut to at most `maximum` UTF-8 bytes, at a character boundary."""
    return text.encode("utf-8")[:maximum].decode("utf-8", "ignore")


def track_from_entry(item: object) -> dict[str, object] | None:
    """Turn one likes or play-history entry into a track row, or None if unusable."""
    track = item.get("track") if isinstance(item, dict) else None
    if not isinstance(track, dict):
        return None
    track_id, duration = track.get("id"), track.get("duration")
    title, permalink = track.get("title"), track.get("permalink_url")
    if (isinstance(track_id, bool) or not isinstance(track_id, int) or track_id <= 0
            or isinstance(duration, bool) or not isinstance(duration, int)
            or not isinstance(title, str) or not title.strip()
            or not isinstance(permalink, str) or not permalink):
        return None
    user = track.get("user") if isinstance(track.get("user"), dict) else {}
    publisher = track.get("publisher_metadata") if isinstance(track.get("publisher_metadata"), dict) else {}
    artist = user.get("username") or user.get("full_name") or publisher.get("artist") or ""
    art_url = track.get("artwork_url") or user.get("avatar_url") or ""
    play_count = track.get("playback_count")
    played_at = item.get("played_at")
    media = track.get("media") if isinstance(track.get("media"), dict) else {}
    transcodings = media.get("transcodings") if isinstance(media.get("transcodings"), list) else []
    # Offered streams but none the plugin can play: only encrypted HLS.
    encrypted = bool(transcodings) and not any(
        isinstance(entry, dict) and isinstance(entry.get("format"), dict)
        and entry["format"].get("protocol") in ("hls", "progressive")
        for entry in transcodings
    )
    return {
        "playbackId": f"soundcloud:tracks:{track_id}",
        "title": _truncate_bytes(title.strip()[:MAX_TITLE_BYTES], MAX_TITLE_BYTES),
        "artist": _truncate_bytes(str(artist)[:MAX_ARTIST_BYTES], MAX_ARTIST_BYTES),
        "url": permalink[:2048],
        "artUrl": str(art_url)[:2048],
        "playCount": play_count if isinstance(play_count, int) and not isinstance(play_count, bool) else 0,
        "durationMs": duration,
        **({"playedAt": played_at} if isinstance(played_at, int) and not isinstance(played_at, bool)
           and 0 < played_at <= MAX_PLAYED_AT_MS else {}),
        **({"encrypted": True} if encrypted else {}),
    }


class LinkError(ValueError):
    """A pasted link that cannot be played. The message is fit to show the user."""


class NotSoundCloudLink(LinkError):
    """Pasted text that is not a link to soundcloud.com at all."""


NOT_SOUNDCLOUD_MESSAGE = "That isn't a SoundCloud link"
ARTIST_PAGE_MESSAGE = "That's an artist page, not a track"
PLAYLIST_MESSAGE = "Playlists aren't supported yet. Copy a track link"
NOT_A_TRACK_MESSAGE = "That isn't a track link"
CANT_PLAY_MESSAGE = "SoundCloud can't play that track"


SOUNDCLOUD_LINK_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
# First path segments that are SoundCloud pages rather than artists.
SOUNDCLOUD_PAGE_SEGMENTS = {
    "discover", "feed", "you", "stations", "search", "charts", "upload", "settings", "stream",
    "messages", "notifications", "people", "pages", "terms-of-use", "jobs", "mobile", "popular",
    "tags", "go", "music",
}
# Second path segments that make an artist's own page rather than a track.
SOUNDCLOUD_ARTIST_SECTIONS = {
    "tracks", "albums", "likes", "reposts", "popular-tracks", "following", "followers",
    "comments", "spotlight",
}
LINK_SEGMENT = re.compile(r"[A-Za-z0-9_.~%-]{1,255}")
LINK_SECRET_TOKEN = re.compile(r"s-[A-Za-z0-9]{1,32}")
RESOLVE_ERRORS = {
    404: "Track not found. It may be private or deleted",
    401: "SoundCloud refused the request. Try signing in again",
    403: "SoundCloud refused the request. Try signing in again",
    429: "SoundCloud is busy. Try again in a moment",
}


def parse_track_link(text: object) -> tuple[str, str]:
    """Classify pasted text as a SoundCloud track link.

    Returns ("track", url) for a track page or ("short", url) for an on.soundcloud.com
    short link. Tracking parameters, fragments and the m./www. host are dropped, and
    only a private track's secret token is kept. Anything else raises LinkError.
    """
    value = text.strip() if isinstance(text, str) else ""
    if not value or len(value.splitlines()) != 1:
        raise LinkError("Copy a single SoundCloud track link first")
    if len(value.encode("utf-8")) > MAX_URL_BYTES - 16:
        raise LinkError("That link is too long")
    try:
        parsed = urlparse(value if "://" in value else "https://" + value)
        port = parsed.port
    except ValueError:
        raise NotSoundCloudLink(NOT_SOUNDCLOUD_MESSAGE) from None
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in {"http", "https"} or parsed.username is not None
            or parsed.password is not None or port not in (None, 80, 443)):
        raise NotSoundCloudLink(NOT_SOUNDCLOUD_MESSAGE)
    if host == "on.soundcloud.com":
        if re.fullmatch(r"/[A-Za-z0-9]{1,32}/?", parsed.path):
            return "short", "https://on.soundcloud.com" + parsed.path.rstrip("/")
        raise LinkError("That isn't a valid SoundCloud short link")
    if host not in SOUNDCLOUD_LINK_HOSTS:
        raise NotSoundCloudLink(NOT_SOUNDCLOUD_MESSAGE)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or segments[0] in SOUNDCLOUD_PAGE_SEGMENTS:
        raise LinkError("That's a SoundCloud page, not a track")
    if len(segments) == 1 or segments[1] in SOUNDCLOUD_ARTIST_SECTIONS:
        raise LinkError(ARTIST_PAGE_MESSAGE)
    if segments[1] == "sets":
        raise LinkError(PLAYLIST_MESSAGE)
    private = len(segments) == 3 and LINK_SECRET_TOKEN.fullmatch(segments[2]) is not None
    if (len(segments) > 3 or (len(segments) == 3 and not private)
            or not all(LINK_SEGMENT.fullmatch(segment) and segment.strip(".") for segment in segments)):
        raise LinkError(NOT_A_TRACK_MESSAGE)
    link = "https://soundcloud.com/" + "/".join(segments)
    secret = parse_qs(parsed.query).get("secret_token", [""])[0]
    if len(segments) == 2 and LINK_SECRET_TOKEN.fullmatch(secret):
        link += "?secret_token=" + secret
    return "track", link


def follow_short_link(url: str, opener=_api_opener) -> str:
    """The address an on.soundcloud.com short link redirects to. Sends no credentials."""
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(request, timeout=10.0):
            pass
    except HTTPError as error:
        location = error.headers.get("Location") if error.code in {301, 302, 303, 307, 308} else None
        if location:
            return urljoin(url, location)
    raise LinkError("Couldn't open that short link. Copy the full track link instead")


class TrackLikes:
    """The signed-in account's liked tracks, read over the private API.

    Writes go through `write` because SoundCloud's bot protection rejects
    like/unlike requests that do not come from its own web page.
    """

    RETRY_SECONDS = 60.0
    MAX_LIKE_PAGES = 50
    PAGE_SIZE = 24

    def __init__(self, load_token, write, request=soundcloud_api_request, spawn=None):
        self.load_token = load_token
        self.write = write
        self.request = request
        self.spawn = spawn or (lambda target: threading.Thread(target=target, daemon=True).start())
        self.lock = threading.Lock()
        self.token: str | None = None
        self.user_id: int | None = None
        self.liked_ids: set[int] | None = None
        self.loading = False
        self.last_attempt = -math.inf

    def _identify(self) -> str:
        token = self.load_token()
        if not token:
            raise PermissionError("not signed in")
        if token != self.token:
            self.token, self.user_id, self.liked_ids = token, None, None
        if self.user_id is None:
            me = self.request("GET", "/me", token)
            user_id = me.get("id") if isinstance(me, dict) else None
            if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                raise ValueError("invalid account response")
            self.user_id = user_id
        return token

    def _session(self) -> str:
        token = self._identify()
        if self.liked_ids is None:
            liked_ids = set()
            path = "/me/track_likes/ids?limit=200"
            # SoundCloud pages these 200 at a time; stop at 10,000 likes.
            for _page in range(self.MAX_LIKE_PAGES):
                payload = self.request("GET", path, token, 1024 * 1024)
                ids = payload.get("collection") if isinstance(payload, dict) else None
                if not isinstance(ids, list):
                    raise ValueError("invalid likes response")
                liked_ids.update(
                    value for value in ids
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0
                )
                next_href = payload.get("next_href")
                parsed = urlparse(next_href) if isinstance(next_href, str) else None
                if (not ids or parsed is None or parsed.scheme != "https"
                        or parsed.netloc != "api-v2.soundcloud.com"
                        or parsed.path != "/me/track_likes/ids"):
                    break
                path = parsed.path + "?" + parsed.query
            self.liked_ids = liked_ids
        return token

    def tracks_page(self, source: str, cursor: str | None = None) -> tuple[list[dict[str, object]], str | None]:
        """One page of an `API_TRACK_LISTS` list, newest first, and the cursor for the next page.

        `cursor` is only ever a value this method returned earlier.
        """
        with self.lock:
            token = self._identify()
            list_path = API_TRACK_LISTS[source].format(user_id=self.user_id)
        payload = self.request(
            "GET", cursor or f"{list_path}?limit={self.PAGE_SIZE}", token, 2 * 1024 * 1024
        )
        items = payload.get("collection") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("invalid likes response")
        tracks = [track for track in map(track_from_entry, items) if track is not None]
        next_href = payload.get("next_href")
        parsed = urlparse(next_href) if isinstance(next_href, str) else None
        if (not items or parsed is None or parsed.scheme != "https"
                or parsed.netloc != "api-v2.soundcloud.com" or parsed.path != list_path):
            return tracks, None
        return tracks, parsed.path + "?" + parsed.query

    def related_tracks(self, track_id: int, cursor: str | None = None) -> tuple[list[dict[str, object]], str | None]:
        """Tracks SoundCloud suggests after `track_id` (autoplay), and the cursor for the next page.

        Blocked tracks and tracks that only offer encrypted streams are left out.
        `cursor` is only ever a value this method returned earlier.
        """
        with self.lock:
            token = self._identify()
        list_path = f"/tracks/{track_id}/related"
        payload = self.request(
            "GET", cursor or f"{list_path}?limit={RELATED_TRACKS_LIMIT}", token, 2 * 1024 * 1024
        )
        items = payload.get("collection") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("invalid related tracks response")
        tracks = [
            track for item in items
            if isinstance(item, dict) and item.get("policy") != "BLOCK"
            and (track := track_from_entry({"track": item})) is not None
            and track.get("encrypted") is not True
        ]
        next_href = payload.get("next_href")
        parsed = urlparse(next_href) if isinstance(next_href, str) else None
        if (not items or parsed is None or parsed.scheme != "https"
                or parsed.netloc != "api-v2.soundcloud.com" or parsed.path != list_path):
            return tracks, None
        return tracks, parsed.path + "?" + parsed.query

    def resolve_link(self, text: str, opener=_api_opener) -> dict[str, object]:
        """Turn pasted text into a playable track row, or raise LinkError."""
        kind, link = parse_track_link(text)
        if kind == "short":
            try:
                kind, link = parse_track_link(follow_short_link(link, opener))
            except NotSoundCloudLink:
                raise LinkError("That short link doesn't lead to SoundCloud") from None
        with self.lock:
            token = self._identify()
        try:
            payload = self.request("GET", "/resolve?url=" + quote(link, safe=""), token, 2 * 1024 * 1024)
        except HTTPError as error:
            raise LinkError(RESOLVE_ERRORS.get(error.code, "SoundCloud couldn't open that link")) from error
        resolved = payload.get("kind") if isinstance(payload, dict) else None
        if resolved in {"playlist", "system-playlist"}:
            raise LinkError(PLAYLIST_MESSAGE)
        if resolved == "user":
            raise LinkError(ARTIST_PAGE_MESSAGE)
        if resolved != "track":
            raise LinkError(NOT_A_TRACK_MESSAGE)
        if payload.get("policy") == "BLOCK":
            raise LinkError("SoundCloud doesn't allow playing that track here")
        track = track_from_entry({"track": payload})
        if track is None:
            raise LinkError(CANT_PLAY_MESSAGE)
        return track

    def is_liked(self, track_id: int) -> bool | None:
        liked_ids = self.liked_ids
        return None if liked_ids is None else track_id in liked_ids

    def load_in_background(self, on_loaded) -> None:
        now = time.monotonic()
        if self.liked_ids is not None or self.loading or now - self.last_attempt < self.RETRY_SECONDS:
            return
        self.loading = True
        self.last_attempt = now

        def worker():
            try:
                with self.lock:
                    self._session()
                on_loaded()
            except (OSError, ValueError):
                pass
            finally:
                self.loading = False

        self.spawn(worker)

    def set_liked(self, track_id: int, liked: bool) -> None:
        with self.lock:
            token = self._session()
            self.write(cast(int, self.user_id), track_id, liked, token)
            liked_ids = cast(set[int], self.liked_ids)
            if liked:
                liked_ids.add(track_id)
            else:
                liked_ids.discard(track_id)


BOT_CHECK_MESSAGE = "SoundCloud wants a quick check. Solve it in the SoundCloud window, then tap the heart again."


class BotCheckRequired(OSError):
    """SoundCloud's bot protection answered with a captcha instead of the API."""


# Runs in the SoundCloud page so its DataDome tag, which watches
# /users/*/track_likes/*, vouches for the request and shows any captcha.
LIKE_TRACK_FUNCTION = r"""
  if (!Number.isSafeInteger(userId) || userId <= 0 || !Number.isSafeInteger(trackId) || trackId <= 0
      || (method !== 'PUT' && method !== 'DELETE') || typeof token !== 'string') return 'failed';
  const response = await fetch(
    'https://api-v2.soundcloud.com/users/' + userId + '/track_likes/' + trackId,
    {
      method,
      headers: { Authorization: 'OAuth ' + token },
      credentials: 'include',
      cache: 'no-store',
      redirect: 'error'
    }
  );
  if (response.ok) return 'ok';
  if (response.status !== 403) return 'failed';
  try {
    const text = await response.text();
    const url = new URL(text.length <= 4096 ? JSON.parse(text).url : '');
    return url.protocol === 'https:' && url.hostname === 'geo.captcha-delivery.com'
      ? 'bot-check' : 'failed';
  } catch (_) {
    return 'failed';
  }
"""


class GstPlayback:
    # `on_end` runs on the main loop when a track ends by itself; `on_error` when a track
    # fails. Neither runs for a user stop, and `on_end` not if another track began meanwhile.
    on_end = None
    on_error = None
    pending_seek = 0.0
    pending_seek_tries = 0
    # A flush seek makes playbin3 report PAUSED for a moment; that isn't a pause
    seek_in_flight = False
    last_seek = None  # (target seconds, monotonic time, playing) of the latest seek
    # HLS mp3 through appsrc can't seek, so a seek restarts the feed at a segment:
    # `hls_base` is the stream time its first segment starts at, `hls_skip` how far into
    # that segment playback begins (position = pipeline position + both), `hls_cache` the playlist (uri, [(url, seconds)]).
    hls_base = 0.0
    hls_skip = 0.0
    hls_cache = None
    hls_uri = ""

    def __init__(self, Gst, on_change, schedule=lambda callback, *args: callback(*args),
                 on_end=None, on_error=None, volume: float = 1.0, muted: bool = False):
        self.Gst = Gst
        Gst.init(None)
        self.current = None
        self.volume = max(0.0, min(1.0, volume))
        self.muted = muted is True
        # The level an unmute from volume 0 returns to
        self.restore_volume = self.volume if self.volume > 0 else 0.5
        self.volume_target = None
        self.on_change = on_change
        self.on_end = on_end
        self.on_error = on_error
        self.schedule = schedule
        self.active = False
        self.playing = False
        self.state = "idle"
        self.has_started = False
        self.metadata: dict[str, object] = {}
        self.error = ""
        self.generation = 0
        self.bus_watches = {}
        self.hls_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=HLS_MAX_WORKERS,
            thread_name_prefix="soundcloud-hls",
        )
        self.hls_slots = threading.BoundedSemaphore(HLS_MAX_WORKERS)

    def _watch_bus(self, pipeline, generation: int) -> None:
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        handler = bus.connect("message", self._message, pipeline, generation)
        self.bus_watches[id(pipeline)] = (bus, handler)

    def _message(self, _bus, message, pipeline, generation):
        if pipeline is not self.current or generation != self.generation:
            return
        if message.type == self.Gst.MessageType.ERROR:
            self.stop("SoundCloud could not play this track")
        elif message.type == self.Gst.MessageType.EOS:
            self.playing = False
            self.state = "paused"
            self.on_change()
            if self.on_end is not None:
                self.schedule(self._ended, generation)
        elif message.type == self.Gst.MessageType.STATE_CHANGED and message.src == self.current:
            _old, current, _pending = message.parse_state_changed()
            if self.seek_in_flight and current == self.Gst.State.PAUSED:
                return
            self.playing = current == self.Gst.State.PLAYING
            if self.playing:
                self.has_started = True
                self.seek_in_flight = False
                self.state = "playing"
                self._apply_pending_seek()
            elif current == self.Gst.State.PAUSED:
                self.state = "paused" if self.has_started else "buffering"
            self.on_change()

    def _ended(self, generation: int) -> bool:
        if generation == self.generation and self.on_end is not None:
            self.on_end()
        return False

    def _failed(self, generation: int) -> bool:
        if generation == self.generation and self.on_error is not None:
            self.on_error()
        return False

    def set_volume(self, level: float) -> None:
        """Set the volume (0..1). Volume 0 mutes, as on the website; any other level unmutes."""
        self.volume = round(max(0.0, min(1.0, level)), 4)
        if self.volume > 0:
            self.restore_volume = self.volume
        self.muted = self.volume == 0
        self._apply_volume()

    def adjust_volume(self, step: float) -> None:
        self.set_volume(self.volume + step)

    def toggle_mute(self) -> None:
        """Mute keeps the volume. Unmuting from volume 0 returns to the last audible level."""
        if self.muted:
            if self.volume == 0:
                self.volume = self.restore_volume
            self.muted = False
        else:
            self.muted = True
        self._apply_volume()

    def volume_status(self) -> dict[str, object]:
        return {"volume": self.volume, "muted": self.muted}

    def _apply_volume(self) -> None:
        if self.volume_target is None:
            return
        try:
            self.volume_target.set_property("volume", 0.0 if self.muted else self.volume)
        except (AttributeError, TypeError, ValueError):
            pass

    def _stop_current(self) -> None:
        self.generation += 1
        self.seek_in_flight = False
        self.last_seek = None
        previous = self.current
        self.current = None
        self.volume_target = None
        if previous is None:
            return
        watch = self.bus_watches.pop(id(previous), None)
        if watch:
            bus, handler = watch
            bus.disconnect(handler)
            bus.remove_signal_watch()
        previous.set_state(self.Gst.State.NULL)

    def stop(self, error: str = "") -> None:
        self._stop_current()
        self.active = False
        self.playing = False
        self.has_started = False
        # Keep the failed track's id so the popup can show the error on its row.
        self.metadata = (
            {"playbackId": self.metadata.get("playbackId", "")} if error else {}
        )
        self.error = error[:128]
        self.state = "error" if self.error else "idle"
        self.pending_seek = 0.0
        self.on_change()
        if self.error and self.on_error is not None:
            self.schedule(self._failed, self.generation)

    def begin(self, metadata: dict[str, object]) -> int:
        self._stop_current()
        self.active = False
        self.playing = False
        self.has_started = False
        self.metadata = dict(metadata)
        self.pending_seek = 0.0
        self.error = ""
        self.state = "resolving"
        self.on_change()
        return self.generation

    def play(
        self,
        uri: str,
        metadata: dict[str, object],
        protocol: str = "",
        mime_type: str = "",
    ) -> bool:
        self.metadata = dict(metadata)
        self.error = ""
        self.active = True
        self.playing = False
        self.has_started = False
        self.state = "buffering"
        self.hls_base = self.hls_skip = 0.0
        self.hls_cache = None
        self.hls_uri = ""
        if protocol == "hls" and mime_type == "audio/mpeg":
            self.hls_uri = uri
            return self._play_hls(uri)
        pipeline = self.Gst.ElementFactory.make(
            "playbin3", f"omarchy-soundcloud-player-{self.generation}"
        )
        if pipeline is None:
            self.active = False
            self.error = "SoundCloud could not initialize audio playback"
            self.state = "error"
            return False
        self.current = pipeline
        self.volume_target = pipeline
        self._apply_volume()
        self._watch_bus(pipeline, self.generation)
        pipeline.set_property("uri", uri)
        result = pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            self._stop_current()
            self.active = False
            self.error = "SoundCloud could not play this track"
            self.state = "error"
            return False
        return True

    def _play_hls(self, uri: str, start: int = 0, paused: bool = False, wait: float = 0.0) -> bool:
        try:
            pipeline = self.Gst.parse_launch(
                "appsrc name=source format=time is-live=false block=true "
                "caps=audio/mpeg,mpegversion=1,layer=3 ! mpegaudioparse ! "
                "mpg123audiodec ! audioconvert ! audioresample ! volume name=volume ! autoaudiosink"
            )
        except Exception:
            self.active = False
            self.error = "SoundCloud could not initialize audio playback"
            self.state = "error"
            return False
        source = pipeline.get_by_name("source")
        if source is None:
            self.active = False
            self.error = "SoundCloud could not initialize audio playback"
            self.state = "error"
            return False
        self.current = pipeline
        self.volume_target = pipeline.get_by_name("volume")
        self._apply_volume()
        self._watch_bus(pipeline, self.generation)
        generation = self.generation
        target = self.Gst.State.PAUSED if paused else self.Gst.State.PLAYING
        if pipeline.set_state(target) == self.Gst.StateChangeReturn.FAILURE:
            self._stop_current()
            self.active = False
            self.error = "SoundCloud could not initialize audio playback"
            self.state = "error"
            return False
        if not self.hls_slots.acquire(blocking=wait > 0, timeout=wait if wait > 0 else None):
            self._stop_current()
            self.active = False
            self.error = "SoundCloud playback is busy"
            self.state = "error"
            return False
        self.hls_executor.submit(
            self._feed_hls_bounded,
            generation,
            source,
            uri,
            time.monotonic() + HLS_OPERATION_SECONDS,
            start,
            self.hls_skip,
        )
        return True

    def _feed_hls_bounded(self, generation: int, source, uri: str, deadline: float,
                          start: int = 0, skip: float = 0.0) -> None:
        try:
            self._feed_hls(generation, source, uri, deadline, start, skip)
        finally:
            self.hls_slots.release()

    def _feed_hls(self, generation: int, source, uri: str, deadline: float,
                  start: int = 0, skip: float = 0.0) -> None:
        try:
            total_bytes = 0
            cancelled = lambda: generation != self.generation
            if self.hls_cache is not None and self.hls_cache[0] == uri:
                segments = self.hls_cache[1]
            else:
                segments = hls_segment_list(uri, deadline=deadline, cancelled=cancelled)
                if generation == self.generation:
                    self.hls_cache = (uri, segments)
            for index, (segment, seconds) in enumerate(segments[start:]):
                if generation != self.generation:
                    return
                data = fetch_private_media(
                    segment,
                    4 * 1024 * 1024,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                if index == 0 and skip > 0 and seconds > 0:
                    # Drop the start of the first segment, in proportion to its bytes;
                    # the parser finds the next frame on its own.
                    data = data[min(len(data) - 1, int(len(data) * skip / seconds)):]
                total_bytes += len(data)
                if total_bytes > 128 * 1024 * 1024:
                    raise ValueError("track stream too large")
                buffer = self.Gst.Buffer.new_allocate(None, len(data), None)
                buffer.fill(0, data)
                if source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
                    raise RuntimeError("audio pipeline rejected data")
            if generation == self.generation:
                source.emit("end-of-stream")
        except (OSError, RuntimeError, TimeoutError, UnicodeError, ValueError,
                concurrent.futures.CancelledError):
            if generation == self.generation:
                self.schedule(self._hls_failed, generation)

    def _hls_failed(self, generation: int) -> bool:
        if generation == self.generation:
            self.stop("SoundCloud could not stream this track")
        return False

    def toggle(self) -> bool:
        if not self.active:
            return False
        target = self.Gst.State.PAUSED if self.playing else self.Gst.State.PLAYING
        self.seek_in_flight = False
        return self.current.set_state(target) != self.Gst.StateChangeReturn.FAILURE

    def seek(self, ratio: float) -> bool:
        if not self.active:
            return False
        duration = self._duration()
        if duration <= 0:
            return False
        return self.seek_seconds(duration * max(0.0, min(1.0, ratio)))

    def _duration(self) -> float:
        duration = self._query_time("query_duration")
        if duration <= 0:
            duration = float(self.metadata.get("durationMs", 0) or 0) / 1000
        return duration

    def _position(self) -> float:
        """The playing position; right after a seek, the seek target plus the time since."""
        if self.last_seek is not None:
            target, at, was_playing = self.last_seek
            elapsed = time.monotonic() - at
            if 0 <= elapsed < SEEK_CHAIN_SECONDS:
                return target + (elapsed if was_playing else 0.0)
        return self._query_time("query_position") + self.hls_base + self.hls_skip

    def _shown_position(self) -> float:
        """The pipeline position, except while a seek settles: the demuxer then reports 0 for
        a moment, which would flash 0:00 in the popup, so show where the seek is heading."""
        reported = self._query_time("query_position") + self.hls_base + self.hls_skip
        if self.last_seek is not None:
            target, at, was_playing = self.last_seek
            elapsed = time.monotonic() - at
            expected = target + (elapsed if was_playing else 0.0)
            if 0 <= elapsed < SEEK_CHAIN_SECONDS and abs(reported - expected) > 3.0:
                return expected
        return reported

    def seek_by(self, delta: float) -> bool:
        """Seek `delta` seconds from where playback is, within [0, duration - margin]."""
        if not self.active or self.current is None:
            return False
        return self.seek_seconds(seek_by_target(self._position(), delta, self._duration()))

    def seek_seconds(self, seconds: float) -> bool:
        """Seek to `seconds`. ACCURATE, not KEY_UNIT: hlsdemux would snap a key-unit seek to
        its 10 s fragment start (measured: a +5 s seek landed up to 9 s early)."""
        if not self.active:
            return False
        seconds = max(0.0, seconds)
        if self.hls_uri:
            done = self._seek_hls(seconds)
        else:
            was_playing = self.playing
            done = bool(self.current.seek_simple(
                self.Gst.Format.TIME,
                self.Gst.SeekFlags.FLUSH | self.Gst.SeekFlags.ACCURATE,
                int(seconds * self.Gst.SECOND),
            ))
            if done and was_playing:
                self.seek_in_flight = True
        if done:
            self.last_seek = (seconds, time.monotonic(), self.playing)
        return done

    def _seek_hls(self, seconds: float) -> bool:
        """The appsrc pipeline can't seek (appsrc isn't seekable), so feed it again from the
        segment holding `seconds` and cut into that segment to land on the exact second."""
        if self.hls_cache is None or self.hls_cache[0] != self.hls_uri:
            return False
        segments = self.hls_cache[1]
        start, base = 0, 0.0
        for index, (_url, length) in enumerate(segments):
            if base + length > seconds or index == len(segments) - 1:
                start = index
                break
            base += length
        paused = self.has_started and not self.playing
        self._stop_current()
        self.hls_base, self.hls_skip = base, max(0.0, seconds - base)
        self.playing = False
        # The feeder this replaces may still be inside a network read and holding its slot.
        if not self._play_hls(self.hls_uri, start, paused, wait=HLS_SEEK_WAIT_SECONDS):
            self.on_change()  # show the error; on_error lets the app react like any failed track
            if self.on_error is not None:
                self.schedule(self._failed, self.generation)
            return False
        self.has_started = paused
        self.state = "paused" if paused else "buffering"
        self.on_change()
        return True

    def seek_when_playing(self, seconds: float) -> None:
        """Seek to `seconds` once the pipeline plays, retrying on later status ticks."""
        self.pending_seek = max(0.0, seconds)
        self.pending_seek_tries = 0

    def _apply_pending_seek(self) -> None:
        if self.pending_seek <= 0 or not self.playing:
            return
        self.pending_seek_tries += 1
        if self.seek_seconds(self.pending_seek) or self.pending_seek_tries >= 20:
            self.pending_seek = 0.0

    def _query_time(self, method: str) -> float:
        try:
            ok, value = getattr(self.current, method)(self.Gst.Format.TIME)
            return value / self.Gst.SECOND if ok and value >= 0 else 0.0
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def status(self) -> dict[str, object]:
        if not self.active:
            return {
                "playerPresent": False,
                "playbackState": self.state,
                "playing": False,
                "playbackId": str(self.metadata.get("playbackId", "")),
                "title": str(self.metadata.get("title", "")),
                "artist": str(self.metadata.get("artist", "")),
                "artUrl": "",
                "duration": 0.0,
                "position": 0.0,
                "waveform": [],
                **({"error": self.error} if self.error else {}),
            }
        self._apply_pending_seek()
        duration = self._query_time("query_duration")
        if duration <= 0:
            duration = float(self.metadata.get("durationMs", 0) or 0) / 1000
        return {
            "playerPresent": True,
            "playbackState": self.state,
            "playing": self.playing,
            "playbackId": str(self.metadata.get("playbackId", "")),
            "title": str(self.metadata.get("title", "")),
            "artist": str(self.metadata.get("artist", "")),
            "artUrl": str(self.metadata.get("artUrl", "")),
            "duration": duration,
            "position": self._shown_position(),
            "waveform": list(self.metadata.get("waveform", [])),
            **({"error": self.error} if self.error else {}),
        }

    def close(self) -> None:
        self._stop_current()
        self.hls_executor.shutdown(wait=False, cancel_futures=True)


MPRIS_BUS_NAME = "org.mpris.MediaPlayer2.omarchy_soundcloud"
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_ROOT_INTERFACE = "org.mpris.MediaPlayer2"
MPRIS_PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
MPRIS_NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
MPRIS_TRACK_PATH = "/org/omarchy/soundcloud/track/"
MPRIS_MAX_TEXT = 256
MPRIS_LOOP_BY_REPEAT = {"none": "None", "one": "Track", "all": "Playlist"}
MPRIS_REPEAT_BY_LOOP = {loop: repeat for repeat, loop in MPRIS_LOOP_BY_REPEAT.items()}
MPRIS_METADATA_TYPES = {
    "mpris:trackid": "o", "mpris:length": "x", "mpris:artUrl": "s",
    "xesam:title": "s", "xesam:artist": "as", "xesam:url": "s",
}
MPRIS_PLAYER_TYPES = {
    "PlaybackStatus": "s", "LoopStatus": "s", "Rate": "d", "Shuffle": "b", "Volume": "d",
    "Position": "x", "MinimumRate": "d", "MaximumRate": "d", "CanGoNext": "b",
    "CanGoPrevious": "b", "CanPlay": "b", "CanPause": "b", "CanSeek": "b", "CanControl": "b",
}
MPRIS_ROOT_PROPERTIES = {
    "CanQuit": ("b", False), "CanRaise": ("b", False), "HasTrackList": ("b", False),
    "Identity": ("s", "SoundCloud"), "SupportedUriSchemes": ("as", []),
    "SupportedMimeTypes": ("as", []),
}
MPRIS_INTROSPECTION = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read">
      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="false"/>
    </property>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


def _mpris_text(value: object, maximum: int = MPRIS_MAX_TEXT) -> str:
    """A string that is safe to put on D-Bus: no NUL, valid UTF-8, bounded; otherwise empty."""
    if not isinstance(value, str) or "\x00" in value:
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    return value if len(value) <= maximum else ""


def _mpris_track_id(status: Mapping[str, object]) -> str:
    playback_id = status.get("playbackId")
    if not is_playback_id(playback_id):
        return MPRIS_NO_TRACK
    return MPRIS_TRACK_PATH + str(playback_id).rpartition(":")[2]


def mpris_position_us(seconds: object) -> int:
    value = _finite_number(seconds, 0, 60 * 60 * 24 * 7)
    return int(value * 1_000_000) if value is not None else 0


def mpris_metadata(status: Mapping[str, object]) -> dict[str, object]:
    """The MPRIS Metadata map as plain Python values, built only from validated status fields."""
    metadata: dict[str, object] = {"mpris:trackid": _mpris_track_id(status)}
    duration = _finite_number(status.get("duration"), 0, 60 * 60 * 24 * 7)
    if duration:
        metadata["mpris:length"] = mpris_position_us(duration)
    title = _mpris_text(status.get("title"))
    if title:
        metadata["xesam:title"] = title
    artist = _mpris_text(status.get("artist"))
    if artist:
        metadata["xesam:artist"] = [artist]
    track_url = _mpris_text(status.get("trackUrl"), MAX_URL_BYTES)
    if track_url and is_soundcloud_uri(track_url):
        metadata["xesam:url"] = track_url
    art_url = _mpris_text(status.get("artUrl"), MAX_URL_BYTES)
    if art_url and is_allowed_artwork_uri(art_url):  # https only, never a data URL
        metadata["mpris:artUrl"] = art_url
    return metadata


def mpris_properties(status: Mapping[str, object]) -> dict[str, object]:
    """Every Player property except Position (read on demand) as plain Python values."""
    state = status.get("playbackState")
    playback = {"playing": "Playing", "buffering": "Playing", "resolving": "Playing",
                "paused": "Paused"}.get(state if isinstance(state, str) else "", "Stopped")
    has_track = is_playback_id(status.get("playbackId"))
    duration = _finite_number(status.get("duration"), 0, 60 * 60 * 24 * 7) or 0
    volume = _finite_number(status.get("volume"), 0, 1)
    repeat = status.get("repeat")
    return {
        "PlaybackStatus": playback,
        "LoopStatus": MPRIS_LOOP_BY_REPEAT.get(repeat if isinstance(repeat, str) else "", "None"),
        "Rate": 1.0,
        "Shuffle": status.get("shuffle") is True,
        "Metadata": mpris_metadata(status),
        "Volume": round(volume, 3) if volume is not None else 1.0,
        "MinimumRate": 1.0,
        "MaximumRate": 1.0,
        "CanGoNext": status.get("canNext") is True,
        "CanGoPrevious": status.get("canPrevious") is True,
        "CanPlay": has_track,
        "CanPause": has_track,
        "CanSeek": has_track and duration > 0,
        "CanControl": True,
    }


def mpris_changed(previous: Mapping[str, object] | None, current: Mapping[str, object]) -> dict[str, object]:
    """The properties whose value differs from what was last emitted."""
    return {name: value for name, value in current.items()
            if previous is None or previous.get(name) != value}


def mpris_method_commands(method: str, args: tuple, status: Mapping[str, object]) -> list[str] | None:
    """The socket commands a Player method runs (maybe none); None for an unsupported method."""
    state = status.get("playbackState")
    playing = state in ("playing", "buffering")
    if method in ("Next", "Previous"):
        return [method.lower()]
    if method == "PlayPause":
        return ["play-pause"]
    if method == "Play":
        return ["play-pause"] if state in ("paused", "idle", "error") else []
    if method in ("Pause", "Stop"):
        return ["play-pause"] if playing else []
    if method in ("Seek", "SetPosition"):
        duration = _finite_number(status.get("duration"), 0, 60 * 60 * 24 * 7) or 0
        if duration <= 0:
            return []
        if method == "Seek":
            offset = args[0] if args and isinstance(args[0], int) else 0
            target = mpris_position_us(status.get("position")) + offset
        else:
            if (len(args) != 2 or args[0] != _mpris_track_id(status)
                    or not isinstance(args[1], int)):
                return []
            target = args[1]
        if method == "SetPosition" and not 0 <= target <= duration * 1_000_000:
            return []
        if target >= duration * 1_000_000:
            return ["next"]
        return [f"seek:{max(0, target) / (duration * 1_000_000):.6f}"]
    return None


def mpris_property_commands(name: str, value: object) -> list[str] | None:
    """The socket commands a writable Player property runs; None if it can't be written."""
    if name == "Volume":
        level = _finite_number(value, -1e9, 1e9)
        return [f"volume:{max(0.0, min(1.0, level)):.4f}"] if level is not None else []
    if name == "LoopStatus":
        mode = MPRIS_REPEAT_BY_LOOP.get(value) if isinstance(value, str) else None
        return [f"repeat:{mode}"] if mode else []
    if name == "Shuffle":
        return ["shuffle:on" if value is True else "shuffle:off"] if isinstance(value, bool) else []
    return None


class MprisService:
    """Shows the player on the session bus as an MPRIS player so media keys and playerctl work.

    `backend` provides mpris_status(), mpris_position_ms() and mpris_command(command). Every
    failure (no bus, name taken) leaves the plugin working without MPRIS. With `connection` (a
    ready Gio.DBusConnection) the service uses that connection and never GLib's shared session
    one: tests pass their private bus this way.
    """

    def __init__(self, Gio, GLib, backend, bus_name: str = MPRIS_BUS_NAME, bus_type=None,
                 connection=None):
        self.Gio, self.GLib, self.backend = Gio, GLib, backend
        self._given_connection = connection
        self.bus_name = bus_name
        self.bus_type = bus_type if bus_type is not None else Gio.BusType.SESSION
        self.owned = False
        self.connection = None
        self._owner_id = 0
        self._registrations: list[int] = []
        self._last: dict[str, object] | None = None
        self._nodes = None

    def start(self) -> None:
        try:
            self._nodes = self.Gio.DBusNodeInfo.new_for_xml(MPRIS_INTROSPECTION)
            if self._given_connection is not None:
                self._bus_acquired(self._given_connection, self.bus_name)
                self._owner_id = self.Gio.bus_own_name_on_connection(
                    self._given_connection, self.bus_name, self.Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                    self._name_acquired, self._name_lost,
                )
            else:
                self._owner_id = self.Gio.bus_own_name(
                    self.bus_type, self.bus_name, self.Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                    self._bus_acquired, self._name_acquired, self._name_lost,
                )
        except Exception:
            self._owner_id = 0

    def stop(self) -> None:
        owner_id, self._owner_id = self._owner_id, 0
        connection, self.connection = self.connection, None
        self.owned = False
        for registration in self._registrations:
            try:
                connection.unregister_object(registration)
            except Exception:
                pass
        self._registrations = []
        if owner_id:
            try:
                self.Gio.bus_unown_name(owner_id)
            except Exception:
                pass

    def _bus_acquired(self, connection, _name) -> None:
        try:
            for interface in self._nodes.interfaces:
                self._registrations.append(connection.register_object(
                    MPRIS_OBJECT_PATH, interface,
                    self._method_call, self._get_property, self._set_property,
                ))
            self.connection = connection
        except Exception:
            self.connection = None

    def _name_acquired(self, _connection, _name) -> None:
        self.owned = self.connection is not None
        if self.owned:
            try:
                self._last = mpris_properties(self.backend.mpris_status())
            except Exception:
                self._last = None

    def _name_lost(self, _connection, _name) -> None:
        self.owned = False

    def _variant(self, signature: str, value):
        return self.GLib.Variant(signature, value)

    def _metadata_variant(self, metadata: Mapping[str, object]):
        entries = {key: self._variant(MPRIS_METADATA_TYPES[key], value)
                   for key, value in metadata.items() if key in MPRIS_METADATA_TYPES}
        return self._variant("a{sv}", entries)

    def _property_variant(self, name: str, value):
        if name == "Metadata":
            return self._metadata_variant(value)
        return self._variant(MPRIS_PLAYER_TYPES[name], value)

    def _get_property(self, _connection, _sender, _path, interface, name):
        if interface == MPRIS_ROOT_INTERFACE:
            signature, value = MPRIS_ROOT_PROPERTIES[name]
            return self._variant(signature, value)
        if name == "Position":
            return self._variant("x", mpris_position_us(self.backend.mpris_position_ms() / 1000))
        if self._last is None:
            self._last = mpris_properties(self.backend.mpris_status())
        return self._property_variant(name, self._last[name])

    def _set_property(self, _connection, _sender, _path, interface, name, value) -> bool:
        if interface != MPRIS_PLAYER_INTERFACE:
            return False
        try:
            commands = mpris_property_commands(name, value.unpack())
            for command in commands or []:
                self.backend.mpris_command(command)
            return commands is not None
        except Exception:
            return False

    def _method_call(self, _connection, _sender, _path, interface, method, parameters, invocation) -> None:
        try:
            if interface == MPRIS_ROOT_INTERFACE:
                invocation.return_value(None)  # Raise and Quit do nothing: the plugin has no window to show
                return
            args = parameters.unpack() if parameters is not None else ()
            status = self.backend.mpris_status()
            commands = mpris_method_commands(method, args, status)
            if commands is None:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.NotSupported", f"{method} is not supported")
                return
            for command in commands:
                self.backend.mpris_command(command)
            invocation.return_value(None)
            if method in ("Seek", "SetPosition") and commands and commands[0].startswith("seek:"):
                ratio = float(commands[0].partition(":")[2])
                duration = _finite_number(status.get("duration"), 0, 60 * 60 * 24 * 7) or 0
                self._emit_seeked(mpris_position_us(ratio * duration))
        except Exception:
            try:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", "request failed")
            except Exception:
                pass

    def _emit_seeked(self, position_us: int) -> None:
        if self.connection is not None:
            self.connection.emit_signal(
                None, MPRIS_OBJECT_PATH, MPRIS_PLAYER_INTERFACE, "Seeked",
                self._variant("(x)", (position_us,)))

    def update(self, status: Mapping[str, object]) -> None:
        """Emit PropertiesChanged for what differs from the last emitted values."""
        if not self.owned or self.connection is None:
            return
        try:
            current = mpris_properties(status)
            changed = mpris_changed(self._last, current)
            self._last = current
            if changed:
                self.connection.emit_signal(
                    None, MPRIS_OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                    self._variant("(sa{sv}as)", (
                        MPRIS_PLAYER_INTERFACE,
                        {name: self._property_variant(name, value) for name, value in changed.items()},
                        [],
                    )))
        except Exception:
            pass


class ClientConnection:
    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.send_lock = threading.Lock()

    def send(self, payload: object) -> bool:
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = b'{"ok":false,"error":"response exceeds security limit"}\n'
        try:
            with self.send_lock:
                previous_timeout = self.connection.gettimeout()
                self.connection.settimeout(SEND_TIMEOUT_SECONDS)
                self.connection.sendall(encoded)
                self.connection.settimeout(previous_timeout)
            return True
        except OSError:
            return False

    def close(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class StatusSubscribers:
    def __init__(self):
        self.connections: set[ClientConnection] = set()
        self.lock = threading.Lock()

    def add(self, connection: ClientConnection) -> None:
        with self.lock:
            self.connections.add(connection)

    def remove(self, connection: ClientConnection) -> None:
        with self.lock:
            self.connections.discard(connection)

    def __bool__(self) -> bool:
        with self.lock:
            return bool(self.connections)

    def count(self) -> int:
        with self.lock:
            return len(self.connections)

    def publish(self, state: dict[str, object]) -> None:
        self.publish_message({"type": "status", "state": state})

    def publish_message(self, payload: dict[str, object]) -> None:
        with self.lock:
            connections = list(self.connections)
        failed = [
            connection
            for connection in connections
            if not connection.send(payload)
        ]
        if failed:
            with self.lock:
                for connection in failed:
                    self.connections.discard(connection)


def send_response(
    connection: ClientConnection,
    payload: dict[str, object],
    request_id: object | None = None,
) -> bool:
    response = dict(payload)
    if request_id is not None:
        response = {"type": "response", "id": request_id, **response}
    return connection.send(response)


class ControlServer(threading.Thread):
    def __init__(self, socket_path: Path, dispatcher, on_disconnect=None):
        super().__init__(name="soundcloud-control", daemon=True)
        self.socket_path = socket_path
        self.dispatcher = dispatcher
        self.on_disconnect = on_disconnect
        self.server: socket.socket | None = None
        self.connections: set[ClientConnection] = set()
        self.connections_lock = threading.Lock()
        self.lock_fd: int | None = None
        self.bound_identity: tuple[int, int] | None = None

    def _serve_client(self, client: ClientConnection) -> None:
        buffer = b""
        last_input = time.monotonic()
        client.connection.settimeout(CLIENT_TIMEOUT_SECONDS)
        try:
            while True:
                try:
                    chunk = client.connection.recv(MAX_REQUEST_BYTES + 1)
                except socket.timeout:
                    if buffer or time.monotonic() - last_input >= CLIENT_IDLE_SECONDS:
                        break
                    continue
                if not chunk:
                    break
                last_input = time.monotonic()
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if len(raw) > MAX_REQUEST_BYTES:
                        return
                    if not raw.strip():
                        continue
                    try:
                        request = json.loads(raw.decode())
                        if not isinstance(request, dict):
                            raise TypeError
                        command = str(request.get("command", ""))
                        request_id = request.get("id")
                        if len(command.encode("utf-8")) > MAX_URL_BYTES:
                            raise ValueError
                        if request_id is not None and (
                            isinstance(request_id, bool)
                            or not isinstance(request_id, int)
                            or not 0 <= request_id <= 2**31 - 1
                        ):
                            raise ValueError
                    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                        send_response(client, {"ok": False, "error": "invalid request"})
                        continue
                    self.dispatcher(command, client, request_id)
                if len(buffer) > MAX_REQUEST_BYTES:
                    break
        except OSError:
            pass
        finally:
            with self.connections_lock:
                self.connections.discard(client)
            if self.on_disconnect:
                self.on_disconnect(client)
            client.close()

    def run(self) -> None:
        try:
            parent = self.socket_path.parent.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or parent.st_mode & 0o077
            ):
                return
            lock_path = self.socket_path.with_suffix(".lock")
            self.lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                existing = self.socket_path.stat(follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
                    return
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                try:
                    probe.connect(str(self.socket_path))
                    return
                except OSError:
                    self.socket_path.unlink()
                finally:
                    probe.close()
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            previous_umask = os.umask(0o077)
            try:
                self.server.bind(str(self.socket_path))
            finally:
                os.umask(previous_umask)
            bound = self.socket_path.stat(follow_symlinks=False)
            self.bound_identity = (bound.st_dev, bound.st_ino)
            self.server.listen(8)
            while True:
                connection, _ = self.server.accept()
                try:
                    _pid, uid, _gid = struct.unpack(
                        "3i",
                        connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
                    )
                except (OSError, struct.error):
                    connection.close()
                    continue
                if uid != os.geteuid():
                    connection.close()
                    continue
                client = ClientConnection(connection)
                with self.connections_lock:
                    if len(self.connections) >= MAX_CLIENTS:
                        client.close()
                        continue
                    self.connections.add(client)
                threading.Thread(
                    target=self._serve_client,
                    args=(client,),
                    name="soundcloud-client",
                    daemon=True,
                ).start()
        except OSError:
            return
        finally:
            self._unlink_owned_socket()
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None

    def _unlink_owned_socket(self) -> None:
        if self.bound_identity is None:
            return
        try:
            details = self.socket_path.stat(follow_symlinks=False)
            if (details.st_dev, details.st_ino) == self.bound_identity:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self.bound_identity = None

    def close(self) -> None:
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        with self.connections_lock:
            connections = list(self.connections)
            self.connections.clear()
        for connection in connections:
            connection.close()
        self._unlink_owned_socket()


def build_application(show_on_start: bool = False):
    Gio, GLib, Gst, Gtk, Soup, WebKit2 = _load_gi()
    paths = profile_paths()
    prepare_profile_paths(paths)
    profile_fd = ensure_private_directory(paths.data_dir)
    cookie_store = CookieStore(profile_fd)
    cookie_records = cookie_store.load()
    if not cookie_records:
        cookie_records = cookie_store.migrate_legacy_sqlite()
    track_cache_store = PrivateJsonStore(profile_fd, "tracks.json", MAX_TRACK_CACHE_BYTES)
    track_cache = sanitize_track_cache(track_cache_store.load())
    queue_store = QueueStore(profile_fd)
    try:
        saved_queue, saved_position_ms = queue_store.load()
    except OSError:
        saved_queue, saved_position_ms = PlayQueue(), 0
    settings_store = SettingsStore(profile_fd)
    try:
        saved_settings = settings_store.load()
    except OSError:
        saved_settings = dict(DEFAULT_SETTINGS)
    socket_path = runtime_socket_path()
    prepare_runtime_socket_path(socket_path)
    install_path = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".config/omarchy/plugins/brunosilveira.soundcloud"

    GLib.set_application_name(APP_NAME)
    GLib.set_prgname("omarchy-soundcloud")

    class SoundCloudApplication(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.window = None
            self.webview = None
            self.mpris = None
            self.status_subscribers = StatusSubscribers()
            self.page_status = PageStatusCache(self._evaluate, status_script())
            self.status_hold_until = 0.0
            self.artwork_cache = ArtworkCache()
            self.playback = GstPlayback(
                Gst,
                lambda: GLib.idle_add(self._publish_status_once),
                GLib.idle_add,
                on_end=self._track_ended,
                on_error=self._track_failed,
                volume=cast(float, saved_settings["volume"]),
                muted=saved_settings["muted"] is True,
            )
            self.list_artwork_cache = ArtworkCache(
                maximum_entries=48,
                maximum_pending=8,
                fetcher=lambda uri: fetch_artwork_data_url(
                    uri,
                    max_bytes=LIST_ARTWORK_BYTES,
                    max_pixels=LIST_ARTWORK_PIXELS,
                    max_dimension=LIST_ARTWORK_DIMENSION,
                ),
            )
            self.track_artwork_urls: OrderedDict[str, str] = OrderedDict()
            self.track_likes = TrackLikes(
                lambda: oauth_token_from_cookies(cookie_store.load()),
                self._write_like_in_page,
            )
            self.track_cache = track_cache
            self.playback_tracks = {
                track["playbackId"]: track
                for tracks in track_cache.values()
                for track in tracks
                if is_playback_id(track.get("playbackId"))
            }
            self.api_lists = {
                source: {"cursor": None, "loading": False, "loaded_at": -math.inf}
                for source in API_TRACK_LISTS
            }
            self.selection_generation = 0
            self.pending_start = 0
            self.resume_ms = 0
            self.queue = saved_queue
            # A saved track is shown paused at its saved position until the user plays it.
            self.restored_position_ms = saved_position_ms if saved_queue.current else None
            self.auto_skip = AutoSkip()
            self.notice = ""
            self.notice_until = 0.0
            self.queue_finished = False  # the last queued track ended and nothing followed
            self.queue_save_pending = False
            self.autoplay = saved_settings["autoplay"] is True
            self.settings_save_pending = False
            self.autoplay_loading = False
            self.run_in_thread = lambda target: threading.Thread(target=target, daemon=True).start()
            self.track_cache_save_pending = False
            self.had_subscriber = False
            self.disconnect_generation = 0
            self.cookie_save_source = 0
            self.cookie_manager = None
            self.dispatch_slots = threading.BoundedSemaphore(MAX_PENDING_DISPATCHES)
            self.control_server = ControlServer(
                socket_path,
                self._dispatch_from_thread,
                self._disconnect_from_thread,
            )

        def do_activate(self):
            self._create_window()
            self.hold()
            self.control_server.start()
            self._start_mpris()
            GLib.timeout_add(STATUS_INTERVAL_MS, self._publish_status)
            GLib.timeout_add_seconds(PAGE_STATUS_INTERVAL_SECONDS, self._poll_page_status)
            GLib.timeout_add_seconds(5, self._check_installation)
            GLib.timeout_add_seconds(QUEUE_POSITION_SAVE_SECONDS, self._save_position_tick)
            if show_on_start:
                self.show_login()
            else:
                self.window.show_all()
                GLib.idle_add(self.window.hide)

        def do_shutdown(self):
            self._save_queue()
            self._save_settings()
            if self.mpris is not None:
                self.mpris.stop()
            self.control_server.close()
            self.playback.close()
            self.artwork_cache.close()
            self.list_artwork_cache.close()
            os.close(profile_fd)
            Gtk.Application.do_shutdown(self)

        def _check_installation(self):
            if not install_path.exists():
                self.quit()
                return False
            return True

        def _create_window(self):
            manager = WebKit2.WebsiteDataManager.new_ephemeral()
            context = WebKit2.WebContext.new_with_website_data_manager(manager)
            cookies = context.get_cookie_manager()
            self.cookie_manager = cookies
            cookies.set_accept_policy(WebKit2.CookieAcceptPolicy.ALWAYS)
            cookies.connect("changed", self._cookies_changed)

            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("SoundCloud sign in — close when finished")
            self.window.set_default_size(1080, 760)
            self.window.set_icon_name("audio-x-generic")
            self.window.connect("delete-event", self._hide_window)

            self.webview = WebKit2.WebView.new_with_context(context)
            content_manager = self.webview.get_user_content_manager()
            if not content_manager.register_script_message_handler("omarchyTracks"):
                raise RuntimeError("could not register SoundCloud track message handler")
            if not content_manager.register_script_message_handler("omarchyPlayback"):
                raise RuntimeError("could not register SoundCloud playback message handler")
            content_manager.connect(
                "script-message-received::omarchyTracks",
                self._tracks_message_received,
            )
            content_manager.connect(
                "script-message-received::omarchyPlayback",
                self._playback_message_received,
            )
            content_manager.add_script(WebKit2.UserScript.new(
                api_request_capture_script(),
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START,
                ["https://soundcloud.com/*"],
                [],
            ))
            settings = self.webview.get_settings()
            for name, value in (
                ("enable-webaudio", True),
                ("enable-mediasource", True),
                ("enable-media-stream", True),
                ("enable-media", True),
                ("media-playback-requires-user-gesture", False),
            ):
                settings.set_property(name, value)
            self.webview.connect("create", self._create_popup)
            self.webview.connect("decide-policy", self._decide_policy)
            self.window.add(self.webview)
            restored = []
            now = int(time.time())
            for record in cookie_records:
                expires = record["expires"]
                max_age = -1 if expires is None else max(0, int(expires) - now)
                cookie = Soup.Cookie.new(
                    record["name"],
                    record["value"],
                    record["domain"],
                    record["path"],
                    max_age,
                )
                cookie.set_secure(record["secure"])
                cookie.set_http_only(record["httpOnly"])
                restored.append(cookie)
            if restored:
                cookies.replace_cookies(restored, None, self._cookies_restored, None)
            else:
                self.webview.load_uri(START_URI)

        def _tracks_message_received(self, _manager, message):
            try:
                value = message.get_js_value()
                encoded = value.to_json(0)
                raw = json.loads(encoded)
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    return
                payload = json.loads(raw)
                source = payload.get("source") if isinstance(payload, dict) else None
                if source not in {"home", "feed"}:
                    return
                page = validate_tracks_payload({"tracks": payload.get("tracks")})
                if page is None:
                    return
                incoming_tracks = page.get("tracks")
                if not isinstance(incoming_tracks, list):
                    return
                for track in incoming_tracks:
                    playback_id = track.get("playbackId")
                    if is_playback_id(playback_id):
                        self.playback_tracks[playback_id] = track
                while len(self.playback_tracks) > MAX_TRACKS * 2:
                    self.playback_tracks.pop(next(iter(self.playback_tracks)))
                existing_tracks = self.track_cache.get(source, [])
                reset = payload.get("reset") is True
                merged_tracks = merge_track_pages(
                    existing_tracks,
                    incoming_tracks,
                    reset=reset,
                )
                previous_count = 0 if reset else len(existing_tracks)
                added_count = max(0, len(merged_tracks) - previous_count)
                pagination_error = _bounded_string(payload.get("error", ""), 128) or ""
                has_more = payload.get("hasMore") is True and (
                    added_count > 0 or bool(pagination_error)
                )
                result = self._secure_tracks({"tracks": merged_tracks})
                if result is None:
                    return
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return
            self.status_subscribers.publish_message(
                {
                    "type": "tracks",
                    "source": source,
                    "reset": reset,
                    "hasMore": has_more,
                    "addedCount": added_count,
                    **({"error": pagination_error} if pagination_error else {}),
                    **result,
                }
            )
            log_event(
                "track-page",
                source=source,
                reset=reset,
                incomingCount=len(incoming_tracks),
                previousCount=previous_count,
                resultCount=len(merged_tracks),
                addedCount=added_count,
                hasMore=has_more,
            )
            if result["tracks"]:
                self.track_cache[source] = merged_tracks
                if not self.track_cache_save_pending:
                    self.track_cache_save_pending = True
                    GLib.idle_add(self._save_track_cache)

        def _playback_message_received(self, _manager, message):
            try:
                value = message.get_js_value()
                encoded = value.to_json(0)
                raw = json.loads(encoded)
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32768:
                    return
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    return
                request_id = payload.get("requestId")
                if (isinstance(request_id, bool) or not isinstance(request_id, int)
                        or request_id != self.selection_generation):
                    return
                if payload.get("ok") is not True:
                    self.playback.stop(
                        _bounded_string(payload.get("error", ""), 128)
                        or "SoundCloud could not resolve this track"
                    )
                    return
                stream_uri = validate_stream_uri(payload.get("streamUrl"))
                validated = validate_tracks_payload({"tracks": [payload.get("track")]})
                validated_tracks = validated.get("tracks") if validated is not None else None
                if (stream_uri is None or not isinstance(validated_tracks, list)
                        or len(validated_tracks) != 1):
                    self.playback.stop("SoundCloud returned invalid stream details")
                    return
                selected_track = validated_tracks[0]
                if not isinstance(selected_track, dict):
                    self.playback.stop("SoundCloud returned invalid track details")
                    return
                if selected_track.get("playbackId") != self.playback.metadata.get("playbackId"):
                    return
                protocol = payload.get("protocol") if payload.get("protocol") in {"hls", "progressive"} else ""
                mime_type = payload.get("mimeType") if payload.get("mimeType") in {"audio/mpeg", "audio/mp4"} else ""
                started = self.playback.play(
                    stream_uri,
                    {**selected_track, "waveform": validate_waveform(payload.get("waveform"))},
                    protocol,
                    mime_type,
                )
                if started:
                    self._record_local_play(selected_track)
                    if self.resume_ms:
                        self.playback.seek_when_playing(self.resume_ms / 1000)
                else:
                    self.playback.error = "SoundCloud could not play this track"
                    GLib.idle_add(self._track_failed)
                self.resume_ms = 0
                self._publish_status_once()
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return

        def _save_settings(self):
            self.settings_save_pending = False
            try:
                settings_store.save({
                    "v": 1, **self.playback.volume_status(), "autoplay": self.autoplay,
                })
            except OSError:
                pass
            return False

        def _settings_changed(self):
            if not self.settings_save_pending:
                self.settings_save_pending = True
                GLib.idle_add(self._save_settings)

        def _save_track_cache(self):
            self.track_cache_save_pending = False
            try:
                track_cache_store.save(self.track_cache)
            except OSError:
                pass
            return False

        def _record_local_play(self, track):
            """Put a track this player just started at the front of History, now and after refreshes."""
            track = {**track, "playedAt": int(time.time() * 1000)}
            self.track_cache["plays"] = with_play_first(
                self.track_cache.get("plays", []), track, MAX_LOCAL_PLAYS
            )
            history = with_play_first(self.track_cache.get("history", []), track)
            self.track_cache["history"] = history
            result = self._secure_tracks({"tracks": history})
            if result is not None:
                self.status_subscribers.publish_message(
                    {
                        "type": "tracks",
                        "source": "history",
                        "reset": False,
                        "hasMore": self.api_lists["history"]["cursor"] is not None,
                        "addedCount": 1,
                        **result,
                    }
                )
            if not self.track_cache_save_pending:
                self.track_cache_save_pending = True
                GLib.idle_add(self._save_track_cache)

        def _remember_playback_track(self, track):
            """Make a track playable by id. Rows are validated the same way list rows are."""
            checked = validate_tracks_payload({"tracks": [track]})
            if checked is None or not checked["tracks"] or not checked["tracks"][0]["playbackId"]:
                return False
            row = checked["tracks"][0]
            self.playback_tracks[row["playbackId"]] = row
            while len(self.playback_tracks) > MAX_TRACKS * 2:
                self.playback_tracks.pop(next(iter(self.playback_tracks)))
            return True

        def _keep_page_on_track_source(self):
            """Playback needs the request context the page only records on Home and Feed."""
            if self.webview and not is_track_source_page(self.webview.get_uri() or ""):
                self.webview.load_uri(source_uri("home"))

        def _play_when_page_ready(self, row, connection, request_id, automatic=False, resume_ms=0,
                                  direction=0):
            """Start `row` once the page is on Home or Feed, so its request context exists."""
            self._keep_page_on_track_source()
            self.pending_start += 1
            token = self.pending_start
            deadline = time.monotonic() + 20.0

            def check():
                if token != self.pending_start:
                    self._respond(
                        connection, request_id, {"ok": False, "error": "selection superseded"}
                    )
                    return False
                ready = (
                    self.webview
                    and is_track_source_page(self.webview.get_uri() or "")
                    and not self.webview.is_loading()
                )
                if ready or time.monotonic() > deadline:
                    self._start_track(row, connection, request_id, automatic, resume_ms, direction)
                    return False
                return True

            if check():
                GLib.timeout_add(250, check)

        def _start_track(self, row, connection, request_id, automatic=False, resume_ms=0, direction=0):
            """The one way a track starts: resolve it in the page, then play it with GStreamer.

            `direction` (1 next, -1 previous) marks a step by the user's Next or Previous: a
            track that can't play is then skipped in that direction, as after an automatic end."""
            playback_id = row.get("playbackId")
            if not self._remember_playback_track(row):
                if automatic or direction:
                    # No caller to tell: fail the track so the normal skip path moves on.
                    self.selection_generation = (self.selection_generation + 1) % (2**31)
                    self.auto_skip.started(self.selection_generation, automatic, time.monotonic(),
                                           direction)
                    self.playback.stop("SoundCloud track details are not ready")
                self._respond(
                    connection,
                    request_id,
                    {"ok": False, "error": "SoundCloud track details are not ready"},
                )
                return
            self.restored_position_ms = None
            self.queue_finished = False
            self.resume_ms = resume_ms
            self.selection_generation = (self.selection_generation + 1) % (2**31)
            generation = self.selection_generation
            self.auto_skip.started(generation, automatic, time.monotonic(), direction)
            self.playback.begin(self.playback_tracks[playback_id])
            if self.playback_tracks[playback_id].get("encrypted") is True:
                # No point asking the page: this track has no stream the plugin can play.
                # An automatic start is then skipped like any track that fails at once.
                self.playback.stop(ENCRYPTED_MESSAGE)
                self._respond(
                    connection, request_id,
                    {"ok": False, "running": True, "error": ENCRYPTED_MESSAGE},
                    publish_status=True,
                )
                return
            self._publish_status_once()
            script = play_track_script(playback_id, generation)

            def track_selected(payload):
                if generation != self.selection_generation:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": False, "error": "selection superseded"},
                    )
                    return
                if not payload:
                    self.playback.stop("SoundCloud track details are not ready")
                self._respond(
                    connection,
                    request_id,
                    {
                        "ok": bool(payload),
                        "running": True,
                        **({} if payload else {"error": "SoundCloud track details are not ready"}),
                    },
                    publish_status=True,
                )

            self._evaluate(script, track_selected)
            GLib.timeout_add_seconds(15, self._resolution_timed_out, generation)

        def _position_ms(self):
            if self.playback.pending_seek > 0:
                return int(self.playback.pending_seek * 1000)
            if self.playback.active:
                return int(self.playback.status()["position"] * 1000)
            return self.restored_position_ms or 0

        def _save_queue(self):
            self.queue_save_pending = False
            try:
                queue_store.save(self.queue, self._position_ms())
            except OSError:
                pass
            return False

        def _queue_changed(self):
            """Save after a structural change, once per main loop turn like the track cache."""
            if not self.queue_save_pending:
                self.queue_save_pending = True
                GLib.idle_add(self._save_queue)

        def _save_position_tick(self):
            if self.playback.playing:
                self._save_queue()
            return True

        def _next_playable(self, user_initiated):
            """Step the queue, dropping rows that can't be played by id."""
            row = self.queue.advance(user_initiated)
            while row is not None and not is_playback_id(row.get("playbackId")):
                row = self.queue.mark_dead(row.get("url"))
            return row

        def _step_user(self, direction, position_seconds=0.0):
            """One queue step for the user's Next (1) or Previous (-1): a row, "restart" or None."""
            if direction > 0:
                return self._next_playable(True)
            return self.queue.previous(position_seconds)

        def _skip_unplayable(self, direction, target):
            """From `target`, keep stepping in `direction` past rows known to have no playable
            stream (flagged encrypted), as far as the queue is long. Counts them for the notice.
            Returns the first row that may play, or None when the queue has nothing more."""
            while isinstance(target, dict) and target.get("encrypted") is True:
                if self.auto_skip.skipped >= self.auto_skip.limit:
                    return None
                self.auto_skip.skipped += 1
                target = self._step_user(direction)
            return target if isinstance(target, dict) else None

        def _user_step(self, direction, first):
            """Next or Previous by the user. `first` makes the first queue step. Returns
            (target, skipped): a row to play, "restart", or None when nothing is left, which
            leaves the queue where it was."""
            snapshot = self.queue.snapshot()
            self.auto_skip.begin_chain(self.queue.length)
            target = first()
            if not (isinstance(target, dict) and target.get("encrypted") is True):
                return target, 0
            target = self._skip_unplayable(direction, target)
            if target is None:
                self.queue.restore(snapshot)
                return None, self.auto_skip.skipped
            return target, self.auto_skip.skipped

        def _show_notice(self, message):
            self.notice = message
            self.notice_until = time.monotonic() + SKIP_NOTICE_SECONDS

        def _track_ended(self):
            if self.auto_skip.generation != self.selection_generation:
                return  # a newer selection began after the end was reported
            self.auto_skip.begin_chain(self.queue.length)
            row = self._next_playable(False)
            self.queue_finished = row is None
            self._queue_changed()
            if row is not None:
                self._play_when_page_ready(row, None, None, automatic=True)
            else:
                self._start_autoplay()

        def _start_autoplay(self):
            """The queue ran out: fetch related tracks off the main loop and keep playing."""
            current = self.queue.current
            if (not self.autoplay or self.autoplay_loading or current is None
                    or not is_playback_id(current.get("playbackId"))):
                return
            self.autoplay_loading = True
            generation = self.selection_generation
            track_id = int(str(current["playbackId"]).rpartition(":")[2])

            def worker():
                tracks = []
                try:
                    tracks, cursor = self.track_likes.related_tracks(track_id)
                    if not tracks and cursor:  # a page of unplayable rows: try the next one
                        tracks, _cursor = self.track_likes.related_tracks(track_id, cursor)
                except Exception:
                    tracks = []  # no suggestions: the queue stays ended
                finally:
                    GLib.idle_add(self._autoplay_loaded, generation, tracks)

            self.run_in_thread(worker)

        def _autoplay_loaded(self, generation, tracks):
            self.autoplay_loading = False
            if generation != self.selection_generation or not self.autoplay:
                return False  # the user picked something else meanwhile
            # Suggestions come from the network: only rows the validator accepts reach the queue.
            tracks = [row for track in tracks
                      for row in (validate_tracks_payload({"tracks": [track]}) or {"tracks": []})["tracks"]]
            if not self.queue.extend_auto(tracks):
                return False
            self.auto_skip.begin_chain(self.queue.length)
            row = self._next_playable(False)
            self._queue_changed()
            if row is not None:
                self._play_when_page_ready(row, None, None, automatic=True)
            return False

        def _track_failed(self):
            """A track failed. If an automatic start failed at once, drop it and play the next."""
            current = self.queue.current
            if current is None or not self.auto_skip.should_skip(
                self.selection_generation, time.monotonic()
            ):
                return False
            direction = self.auto_skip.direction
            if direction:
                # The user's own Next or Previous: keep the rows, go on in the same direction.
                row = self._skip_unplayable(direction, self._step_user(direction))
                self._queue_changed()
                if row is None:
                    self._show_notice(NO_PLAYABLE_MESSAGE)
                    self._publish_status_once()
                else:
                    self._show_notice(self.auto_skip.notice())
                    self._play_when_page_ready(row, None, None, direction=direction)
                return False
            row = self.queue.mark_dead(current.get("url"))
            while row is not None and not is_playback_id(row.get("playbackId")):
                row = self.queue.mark_dead(row.get("url"))
            self._queue_changed()
            if row is not None:
                self.notice = self.auto_skip.notice()
                self.notice_until = time.monotonic() + SKIP_NOTICE_SECONDS
                self._play_when_page_ready(row, None, None, automatic=True)
            return False

        def _nothing_playing(self):
            """Whether a pasted link should start at once: no track loaded, or the last one ended."""
            state = self.playback.state
            if state in ("idle", "error"):
                return self.restored_position_ms is None
            return state == "paused" and self.queue_finished

        def _play_link(self, mode, link_text, connection, request_id, show_error=False):
            """Play a pasted track link ("play"), or queue it as play next, or play it at once
            when nothing is playing ("next"). The link is checked here the way the CLI checks it;
            the lookup runs off the main loop. A failure goes in the reply and, with `show_error`,
            in the status for the popup."""

            def fail(message):
                if show_error:
                    self.notice = message
                    self.notice_until = time.monotonic() + SKIP_NOTICE_SECONDS
                self._respond(
                    connection, request_id, {"ok": False, "running": True, "error": message},
                    publish_status=show_error,
                )
                return False

            try:
                parse_track_link(link_text)
            except LinkError as problem:
                return fail(str(problem))

            def link_resolved(track, error):
                if track is None or not self._remember_playback_track(track):
                    return fail(error or CANT_PLAY_MESSAGE)
                track = self.playback_tracks[track["playbackId"]]
                if mode == "next" and not self._nothing_playing():
                    self.queue.play_next(track)
                    self._queue_changed()
                    self._respond(
                        connection, request_id,
                        {"ok": True, "running": True, "queued": True,
                         "title": track["title"], "artist": track["artist"]},
                        publish_status=True,
                    )
                    return False
                self.queue.replace([track], 0, "")
                self._queue_changed()
                self._play_when_page_ready(track, connection, request_id)
                return False

            def resolve_link():
                track, error = None, ""
                try:
                    track = self.track_likes.resolve_link(link_text)
                except LinkError as problem:
                    error = str(problem)
                except PermissionError:
                    error = "Sign in to SoundCloud first"
                except Exception:
                    error = "Couldn't reach SoundCloud. Try again"
                finally:
                    GLib.idle_add(link_resolved, track, error)

            self.run_in_thread(resolve_link)
            return False

        def _restart_current(self, connection, request_id):
            if self.playback.active and self.playback.seek_seconds(0.0):
                if not self.playback.playing:
                    self.playback.toggle()
                self._respond(connection, request_id, {"ok": True, "running": True},
                              publish_status=True)
            else:
                self._play_when_page_ready(self.queue.current, connection, request_id)

        def _load_api_list(self, source, reset):
            """Fetch one page of an account list on a worker thread; False if one is already loading."""
            state = self.api_lists[source]
            if state["loading"]:
                return False
            state["loading"] = True
            cursor = None if reset else state["cursor"]

            def worker():
                tracks, next_cursor, error = [], None, ""
                try:
                    tracks, next_cursor = self.track_likes.tracks_page(source, cursor)
                except PermissionError:
                    error = f"Sign in to SoundCloud to see your {source}"
                except Exception:
                    error = f"Could not load your {source}"
                finally:
                    GLib.idle_add(self._api_list_loaded, source, reset, tracks, next_cursor, error)

            self.run_in_thread(worker)
            return True

        def _api_list_loaded(self, source, reset, tracks, next_cursor, error):
            state = self.api_lists[source]
            state["loading"] = False
            existing = self.track_cache.get(source, [])
            if error:
                reset, merged, added_count = False, existing, 0
            else:
                if source == "history" and reset:
                    tracks = with_local_plays(tracks, self.track_cache.get("plays", []))
                merged = merge_track_pages(existing, tracks, reset=reset)
                added_count = max(0, len(merged) - (0 if reset else len(existing)))
                state["cursor"] = (
                    next_cursor if len(merged) < MAX_TRACKS and (reset or added_count > 0) else None
                )
                state["loaded_at"] = time.monotonic()
            result = self._secure_tracks({"tracks": merged})
            if result is None:
                return False
            for track in merged:
                self._remember_playback_track(track)
            self.status_subscribers.publish_message(
                {
                    "type": "tracks",
                    "source": source,
                    "reset": reset,
                    "hasMore": state["cursor"] is not None,
                    "addedCount": added_count,
                    **({"error": error} if error else {}),
                    **result,
                }
            )
            if not error and merged:
                self.track_cache[source] = merged
                if not self.track_cache_save_pending:
                    self.track_cache_save_pending = True
                    GLib.idle_add(self._save_track_cache)
            return False

        def _publish_cached_tracks(self, source):
            cached = self.track_cache.get(source)
            if not cached:
                return
            result = self._secure_tracks({"tracks": cached})
            if result is not None and result["tracks"]:
                self.status_subscribers.publish_message(
                    {"type": "tracks", "source": source, "cached": True, **result}
                )

        def _cookies_restored(self, manager, result, _data):
            try:
                manager.replace_cookies_finish(result)
            except Exception:
                pass
            self.webview.load_uri(START_URI)

        def _cookies_changed(self, _manager):
            if self.cookie_save_source:
                GLib.source_remove(self.cookie_save_source)
            self.cookie_save_source = GLib.timeout_add_seconds(2, self._save_cookies)

        def _save_cookies(self):
            self.cookie_save_source = 0
            self.cookie_manager.get_all_cookies(None, self._cookies_collected, None)
            return False

        def _cookies_collected(self, manager, result, _data):
            try:
                cookies = manager.get_all_cookies_finish(result)
                records = []
                for cookie in cookies[:MAX_COOKIES]:
                    domain = cookie.get_domain() or ""
                    normalized = domain.lower().lstrip(".").rstrip(".")
                    if normalized != "soundcloud.com" and not normalized.endswith(".soundcloud.com"):
                        continue
                    expires = cookie.get_expires()
                    records.append({
                        "name": cookie.get_name() or "",
                        "value": cookie.get_value() or "",
                        "domain": domain,
                        "path": cookie.get_path() or "/",
                        "expires": int(expires.to_unix()) if expires else None,
                        "secure": bool(cookie.get_secure()),
                        "httpOnly": bool(cookie.get_http_only()),
                    })
                cookie_store.save(records)
            except Exception:
                pass

        def _hide_window(self, *_args):
            self.window.hide()
            return True

        def show_login(self):
            self.window.show_all()
            self.window.present()

        def _create_popup(self, source, _navigation_action):
            popup = WebKit2.WebView.new_with_related_view(source)
            popup.connect("decide-policy", self._decide_policy)
            popup_window = Gtk.Window(application=self)
            popup_window.set_default_size(720, 720)
            popup_window.set_title("SoundCloud sign in")
            popup_window.add(popup)
            popup.connect("close", lambda *_: popup_window.destroy())
            popup_window.show_all()
            return popup

        def _decide_policy(self, _webview, decision, decision_type):
            if decision_type not in {
                WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
            }:
                return False
            request = decision.get_request()
            uri = request.get_uri() if request else ""
            if is_soundcloud_uri(uri) or is_bot_check_uri(uri):
                decision.use()
            else:
                decision.ignore()
            return True

        def _dispatch_from_thread(self, command, connection, request_id):
            if not self.dispatch_slots.acquire(blocking=False):
                send_response(connection, {"ok": False, "error": "server busy"}, request_id)
                return
            GLib.idle_add(self._handle_command_safely, command, connection, request_id)

        def _handle_command_safely(self, command, connection, request_id):
            try:
                return self._handle_command(command, connection, request_id)
            except Exception:
                self._respond(connection, request_id, {"ok": False, "error": "request failed"})
                return False

        def _disconnect_from_thread(self, connection):
            self.status_subscribers.remove(connection)
            if self.had_subscriber and self.status_subscribers.count() == 0:
                self.disconnect_generation += 1
                generation = self.disconnect_generation
                GLib.timeout_add_seconds(10, self._quit_if_abandoned, generation)

        def _quit_if_abandoned(self, generation):
            if generation == self.disconnect_generation and self.status_subscribers.count() == 0:
                self.quit()
            return False

        def _artwork_ready_from_thread(self):
            GLib.idle_add(self._publish_status_once)

        def _secure_status(self, payload):
            state = validate_status_payload(payload)
            if state is None:
                return {}
            remote_art = state.pop("artUrl")
            state["artDataUrl"] = self.artwork_cache.get(
                remote_art, self._artwork_ready_from_thread
            )
            playback_id = str(state["playbackId"])
            liked = None
            if playback_id:
                liked = self.track_likes.is_liked(int(playback_id.rpartition(":")[2]))
                if liked is None and state["loggedIn"]:
                    self.track_likes.load_in_background(
                        lambda: GLib.idle_add(self._publish_status_once)
                    )
            state["liked"] = liked is True
            return state

        def _secure_tracks(self, payload):
            result = validate_tracks_payload(payload)
            if result is None:
                return None
            for track in result["tracks"]:
                original_art = track.pop("artUrl")
                remote_art = normalize_list_artwork_url(original_art)
                track["artworkId"] = ""
                if not remote_art:
                    continue
                token = hashlib.sha256(remote_art.encode("utf-8")).hexdigest()[:24]
                self.track_artwork_urls[token] = remote_art
                self.track_artwork_urls.move_to_end(token)
                while len(self.track_artwork_urls) > 200:
                    self.track_artwork_urls.popitem(last=False)
                track["artworkId"] = token
            return result

        def _respond_track_artwork(self, connection, request_id, artwork_id, data_url):
            self._respond(
                connection,
                request_id,
                {"ok": bool(data_url), "artworkId": artwork_id, "artDataUrl": data_url},
            )
            return False

        def _queue_payload(self):
            """The queue as the popup shows it: validated rows (at most 100) and the cursor."""
            rows, cursor, explicit_count = self.queue.view(MAX_TRACKS)
            result = self._secure_tracks({"tracks": rows})
            if result is None:
                rows, cursor, explicit_count, result = [], -1, 0, {"tracks": []}
            for row, source in zip(result["tracks"], rows):
                # the row validator leaves false flags out; the popup always gets all four
                for flag in ("explicit", "added", "auto", "current"):
                    row[flag] = source.get(flag) is True
            return {"queue": {
                "cursor": cursor,
                "items": result["tracks"],
                "explicit_count": explicit_count,
                "added_count": len(self.queue.added),
                "autoplay": self.autoplay,
            }}

        def _publish_status_once(self):
            self._publish_status()
            return False

        def build_status(self):
            return self._secure_status(self._raw_status())

        def _raw_status(self):
            gst_status = self.playback.status()
            saved = self.queue.current
            track_url = str(self.playback.metadata.get("url", ""))
            if (self.restored_position_ms is not None and saved is not None
                    and not self.playback.active and gst_status["playbackState"] == "idle"):
                gst_status = restored_status(saved, self.restored_position_ms)
                track_url = str(saved.get("url", ""))
            status = {
                **merge_status(self.page_status.values, gst_status),
                **queue_status(self.queue),
                **self.playback.volume_status(),
                "autoplay": self.autoplay,
                "trackUrl": track_url,
            }
            if not status.get("error") and time.monotonic() < self.notice_until:
                status["error"] = self.notice
            self._keep_failed_track(status, ("error",))
            return status

        def _keep_failed_track(self, status, states, with_duration=False):
            """A failed track loses its details in the player. Put the queue row's title and
            artist back (and its length, for MPRIS) so the track stays on show; the state,
            `playing` and the error text are left as they are."""
            row = self.queue.current
            if (row is None or status.get("playbackState") not in states
                    or status.get("playbackId") not in ("", row.get("playbackId"))):
                return
            if not (status.get("title") or status.get("artist")):
                status["title"], status["artist"] = str(row.get("title", "")), str(row.get("artist", ""))
                if not status.get("trackUrl"):
                    status["trackUrl"] = str(row.get("url", ""))
            if with_duration and not status.get("duration"):
                status["duration"] = _bounded_integer(
                    row.get("durationMs"), 0, MAX_TRACK_DURATION_MS) / 1000

        def _start_mpris(self):
            """Own the MPRIS name; without a bus, or if the name is taken, the plugin runs without it."""
            try:
                self.mpris = MprisService(Gio, GLib, self)
                self.mpris.start()
            except Exception:
                self.mpris = None

        def mpris_status(self):
            """The validated status, with the remote artwork URL kept, for MPRIS."""
            raw = self._raw_status()
            # The shell's media widget hides a player without title and artist: keep the track
            # of an error, or of a stop after one, on show.
            self._keep_failed_track(raw, ("error", "idle"), with_duration=True)
            return validate_status_payload(raw) or {}

        def mpris_position_ms(self):
            return self._position_ms()

        def mpris_command(self, command):
            """Run a socket command for MPRIS: no connection, request id or dispatch slot."""
            try:
                self._handle_command(command, None, None)
            except Exception:
                pass

        def _mpris_update(self):
            if self.mpris is not None and self.mpris.owned:
                self.mpris.update(self.mpris_status())

        def _publish_status(self):
            self._mpris_update()
            if not self.status_subscribers:
                return True
            if not self.page_status.seeded and time.monotonic() < self.status_hold_until:
                # The first publish waits briefly for the page so it carries loggedIn
                return True
            self.status_subscribers.publish({"running": True, **self.build_status()})
            return True

        def _poll_page_status(self):
            if self.status_subscribers:
                self.page_status.poll(
                    lambda changed: self._publish_status_once() if changed else None
                )
            return True

        def _resolution_timed_out(self, generation):
            if (generation == self.selection_generation
                    and self.playback.state == "resolving"):
                self.selection_generation += 1
                self.playback.stop("SoundCloud took too long to resolve this track")
            return False

        def _respond(self, connection, request_id, payload, publish_status=False):
            if connection is None:  # an automatic advance has no caller
                if publish_status:
                    GLib.timeout_add(80, self._publish_status_once)
                return
            try:
                send_response(connection, payload, request_id=request_id)
                if publish_status:
                    GLib.timeout_add(80, self._publish_status_once)
            finally:
                self.dispatch_slots.release()

        def _handle_command(self, command, connection, request_id):
            command = normalize_backend_command(command)
            if command == "status":
                self.page_status.poll()
                self._respond(
                    connection,
                    request_id,
                    {"ok": True, "running": True, **self.build_status()},
                )
            elif command == "subscribe":
                self.status_subscribers.add(connection)
                self.had_subscriber = True
                self.disconnect_generation += 1
                self._respond(connection, request_id, {"ok": True, "running": True})
                if self.page_status.seeded:
                    GLib.idle_add(self._publish_status_once)
                    self._poll_page_status()
                else:
                    self.status_hold_until = time.monotonic() + 2.0
                    self.page_status.poll(lambda _changed: self._publish_status_once())
            elif (source := api_list_source_from_command(command, "tracks:")) is not None:
                self._keep_page_on_track_source()
                cached = self._secure_tracks({"tracks": self.track_cache.get(source, [])})
                if time.monotonic() - self.api_lists[source]["loaded_at"] > API_LIST_REFRESH_SECONDS[source]:
                    self._load_api_list(source, reset=True)
                self._respond(
                    connection,
                    request_id,
                    {
                        "ok": cached is not None,
                        "running": True,
                        **(cached or {"tracks": [], "error": "invalid track data"}),
                        **({"pending": True} if cached is not None and not cached["tracks"] else {}),
                    },
                )
            elif (source := api_list_source_from_command(command, "load-more:")) is not None:
                has_cursor = self.api_lists[source]["cursor"] is not None
                started = has_cursor and self._load_api_list(source, reset=False)
                self._respond(
                    connection,
                    request_id,
                    {
                        "ok": True,
                        "running": True,
                        "started": started,
                        "hasMore": self.api_lists[source]["cursor"] is not None,
                    },
                )
            elif (source := load_more_source_from_command(command)) is not None:
                def load_more_finished(payload):
                    valid = isinstance(payload, dict)
                    started = valid and payload.get("started") is True
                    has_more = valid and payload.get("hasMore") is True
                    log_event(
                        "lazy-load-request",
                        source=source,
                        started=started,
                        hasMore=has_more,
                    )
                    self._respond(
                        connection,
                        request_id,
                        {
                            "ok": valid,
                            "running": True,
                            "started": started,
                            "hasMore": has_more,
                        },
                    )

                self._evaluate(
                    load_more_script(source),
                    load_more_finished,
                )
            elif (source := track_source_from_command(command)) is not None:
                def tracks_finished(payload):
                    pending = isinstance(payload, dict) and payload.get("pending") is True
                    remote_error = (
                        _bounded_string(payload.get("error", ""), 128)
                        if isinstance(payload, dict)
                        else None
                    ) or ""
                    result = self._secure_tracks(payload)
                    if pending and result is not None:
                        result["pending"] = True
                    if remote_error and result is not None:
                        result["error"] = remote_error
                    self._respond(
                        connection,
                        request_id,
                        {
                            "ok": result is not None,
                            "running": True,
                            **(result or {"tracks": [], "error": "invalid track data"}),
                        },
                    )

                self._evaluate(
                    api_tracks_script(source),
                    tracks_finished,
                )
            elif command == "inspect-api":
                def observation_finished(payload):
                    paths = payload.get("paths", []) if isinstance(payload, dict) else []
                    paths = [
                        path for path in paths[:32]
                        if isinstance(path, str) and len(path.encode("utf-8")) <= 512
                    ]
                    lists = payload.get("lists", {}) if isinstance(payload, dict) else {}
                    safe_lists = {
                        source: {
                            "count": _bounded_integer(state.get("count"), 0, MAX_TRACKS),
                            "loading": state.get("loading") is True,
                            "error": _bounded_string(state.get("error", ""), 128) or "",
                        }
                        for source, state in lists.items()
                        if source in {"home", "feed"} and isinstance(state, dict)
                    }
                    playback_count = _bounded_integer(
                        payload.get("playbackCount"), 0, MAX_TRACKS * 2
                    ) if isinstance(payload, dict) else 0
                    self._respond(connection, request_id, {
                        "ok": True,
                        "paths": paths,
                        "lists": safe_lists,
                        "playbackCount": playback_count,
                    })

                self._evaluate(api_observation_script(), observation_finished)
            elif command.startswith("artwork:"):
                artwork_id = command.partition(":")[2]
                remote_art = self.track_artwork_urls.get(artwork_id)
                if (
                    len(artwork_id) != 24
                    or any(character not in "0123456789abcdef" for character in artwork_id)
                    or not remote_art
                ):
                    self._respond(connection, request_id, {"ok": False, "error": "unknown artwork"})
                else:
                    self.list_artwork_cache.request(
                        remote_art,
                        lambda data_url: GLib.idle_add(
                            self._respond_track_artwork,
                            connection,
                            request_id,
                            artwork_id,
                            data_url,
                        ),
                    )
            elif command == "play-pause":
                if self.playback.active:
                    if self.playback.playing:
                        self._save_queue()  # keep the position the pause happened at
                    self._respond(
                        connection,
                        request_id,
                        {"ok": self.playback.toggle(), "running": True},
                        publish_status=True,
                    )
                elif self.restored_position_ms is not None and self.queue.current is not None:
                    self._play_when_page_ready(
                        self.queue.current, connection, request_id,
                        resume_ms=self.restored_position_ms,
                    )
                else:
                    if self.playback.error:
                        self.playback.stop()
                    self._evaluate(
                        command_script(command),
                        lambda payload: self._respond(
                            connection,
                            request_id,
                            {"ok": bool(payload), "running": True},
                            publish_status=True,
                        ),
                    )
            elif command in {"next", "previous"}:
                direction = 1 if command == "next" else -1
                target, skipped = self._user_step(
                    direction, lambda: self._step_user(direction, self._position_ms() / 1000))
                self._queue_changed()
                if target is None and skipped:
                    self._show_notice(NO_PLAYABLE_MESSAGE)
                    self._respond(
                        connection, request_id,
                        {"ok": False, "running": True, "error": NO_PLAYABLE_MESSAGE},
                        publish_status=True,
                    )
                elif target is None:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": False, "running": True, "error": "Nothing else in the queue"},
                    )
                elif target == "restart":
                    self._restart_current(connection, request_id)
                else:
                    if skipped:
                        self._show_notice(self.auto_skip.notice())
                    self._play_when_page_ready(target, connection, request_id, direction=direction)
            elif (queued := play_from_command(command)) is not None:
                tab, playback_id = queued
                rows = self.track_cache.get(tab, [])
                index = next(
                    (n for n, row in enumerate(rows) if row.get("playbackId") == playback_id), None
                )
                if index is None:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": False, "error": "SoundCloud track details are not ready"},
                    )
                    return False
                self.queue.replace(rows, index, tab)
                self._queue_changed()
                self._play_when_page_ready(self.queue.current, connection, request_id)
            elif (mode := repeat_from_command(command, self.queue.repeat)) is not None \
                    or command.startswith("repeat"):
                if mode is not None:
                    self.queue.repeat = mode
                    self._queue_changed()
                self._respond(
                    connection,
                    request_id,
                    {"ok": mode is not None, "running": True, "repeat": self.queue.repeat},
                    publish_status=True,
                )
            elif (shuffle := shuffle_from_command(command, self.queue.shuffle)) is not None \
                    or command.startswith("shuffle"):
                if shuffle is not None:
                    self.queue.shuffle = shuffle
                    self._queue_changed()
                self._respond(
                    connection,
                    request_id,
                    {"ok": shuffle is not None, "running": True, "shuffle": self.queue.shuffle},
                    publish_status=True,
                )
            elif (queue_next_id := queue_next_from_command(command)) is not None:
                row = self.playback_tracks.get(queue_next_id)
                if row is not None:
                    self.queue.play_next(row)
                    self._queue_changed()
                self._respond(
                    connection,
                    request_id,
                    {"ok": row is not None, "running": True,
                     **({} if row is not None else {"error": "SoundCloud track details are not ready"})},
                    publish_status=True,
                )
            elif command.startswith("queue-add:"):
                row = self.playback_tracks.get(queue_add_from_command(command))
                added = row is not None and self.queue.add_last(row)
                if added:
                    self._queue_changed()
                self._respond(
                    connection,
                    request_id,
                    {"ok": row is not None, "running": True, "added": added,
                     **({} if row is not None else {"error": "SoundCloud track details are not ready"})},
                    publish_status=True,
                )
            elif command.startswith("queue-play:"):
                row = self.queue.jump_to(queue_play_from_command(command))
                if row is None:
                    self._respond(
                        connection, request_id,
                        {"ok": False, "running": True, "error": "That track isn't in the queue"},
                    )
                else:
                    self._queue_changed()
                    self._play_when_page_ready(row, connection, request_id)
            elif command.startswith("queue-move:"):
                moved = queue_move_from_command(command)
                done = moved is not None and self.queue.move(*moved)
                if done:
                    self._queue_changed()
                self._respond(
                    connection, request_id,
                    {"ok": done, "running": True, **self._queue_payload(),
                     **({} if done else {"error": "That track can't be moved"})},
                    publish_status=done,
                )
            elif (link := link_from_command(command)) is not None:
                self._play_link(link[0], link[1], connection, request_id, show_error=True)
            elif command.startswith(("volume:", "volume-step:")) or command == "mute":
                level = volume_from_command(command)
                step = volume_step_from_command(command)
                ok = command == "mute" or level is not None or step is not None
                if command == "mute":
                    self.playback.toggle_mute()
                elif level is not None:
                    self.playback.set_volume(level)
                elif step is not None:
                    self.playback.adjust_volume(step)
                if ok:
                    self._settings_changed()
                self._respond(
                    connection, request_id,
                    {"ok": ok, "running": True, **self.playback.volume_status()},
                    publish_status=True,
                )
            elif command.startswith("autoplay:"):
                enabled = on_off_from_command(command, "autoplay")
                if enabled is not None:
                    self.autoplay = enabled
                    self._settings_changed()
                self._respond(
                    connection, request_id,
                    {"ok": enabled is not None, "running": True, "autoplay": self.autoplay},
                    publish_status=True,
                )
            elif command == "queue":
                self._respond(
                    connection, request_id, {"ok": True, "running": True, **self._queue_payload()}
                )
            elif command == "queue-clear" or command.startswith("queue-remove:"):
                if command == "queue-clear":
                    self.queue.clear()
                    done = True
                else:
                    done = self.queue.remove(queue_remove_from_command(command))
                if done:
                    self._queue_changed()
                self._respond(
                    connection, request_id,
                    {"ok": done, "running": True, **self._queue_payload(),
                     **({} if done else {"error": "That track can't be removed"})},
                    publish_status=True,
                )
            elif command.startswith("seek:"):
                try:
                    ratio = float(command.partition(":")[2])
                    if not math.isfinite(ratio):
                        raise ValueError("seek position is not a finite number")
                except ValueError:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": False, "error": "invalid seek position"},
                    )
                else:
                    if self.playback.active:
                        self._respond(
                            connection,
                            request_id,
                            {"ok": self.playback.seek(ratio), "running": True},
                            publish_status=True,
                        )
                    else:
                        self._evaluate(
                            seek_script(ratio),
                            lambda payload: self._respond(
                                connection,
                                request_id,
                                {"ok": bool(payload), "running": True},
                                publish_status=True,
                            ),
                        )
            elif command.startswith("seek-by:"):
                seconds = seek_by_from_command(command)
                done = seconds is not None and self.playback.seek_by(seconds)
                self._respond(
                    connection, request_id,
                    {"ok": done, "running": True,
                     **({} if done else {"error": "Can't seek"})},
                    publish_status=True,
                )
            elif command.startswith("play-url:"):
                self._play_link("play", command.removeprefix("play-url:"), connection, request_id)
            elif (playback_id := playback_id_from_command(command)) is not None:
                row = self.playback_tracks.get(playback_id)
                if row is None:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": False, "error": "SoundCloud track details are not ready"},
                    )
                    return False
                self.queue.replace([row], 0, "")  # a track picked on its own is a queue of one
                self._queue_changed()
                self._play_when_page_ready(row, connection, request_id)
            elif (like := track_like_from_command(command)) is not None:
                playback_id, liked = like

                def update_like():
                    payload = {"ok": False, "error": "SoundCloud could not update this like"}
                    try:
                        self.track_likes.set_liked(int(playback_id.rpartition(":")[2]), liked)
                        self.api_lists["likes"]["loaded_at"] = -math.inf  # refresh the Likes tab on next view
                        payload = {"ok": True, "running": True, "playbackId": playback_id, "liked": liked}
                    except BotCheckRequired:
                        GLib.idle_add(self.show_login)
                        payload = {"ok": False, "error": BOT_CHECK_MESSAGE}
                    except Exception:
                        pass
                    finally:
                        GLib.idle_add(
                            lambda: self._respond(connection, request_id, payload, publish_status=True)
                        )

                self.run_in_thread(update_like)
            elif source_uri(command):
                self._publish_cached_tracks(command)
                self.webview.load_uri(source_uri(command))
                self._respond(connection, request_id, {"ok": True, "running": True})
            elif command == "show":
                self.show_login()
                self._respond(connection, request_id, {"ok": True, "running": True})
            elif command == "stop":
                self._respond(connection, request_id, {"ok": True, "running": False})
                self.quit()
            else:
                self._respond(connection, request_id, {"ok": False, "error": "unknown command"})
            return False

        def _write_like_in_page(self, user_id, track_id, liked, token):
            """Run on a worker thread; blocks until the page's fetch finishes."""
            done = threading.Event()
            result = {"outcome": "failed"}

            def finished(webview, async_result, _data):
                try:
                    value = webview.call_async_javascript_function_finish(async_result)
                    result["outcome"] = value.to_string() if value.is_string() else "failed"
                except Exception:
                    pass
                done.set()

            def start():
                if not self.webview or not is_soundcloud_uri(self.webview.get_uri() or ""):
                    done.set()
                    return False
                arguments = GLib.Variant("a{sv}", {
                    "userId": GLib.Variant("d", user_id),
                    "trackId": GLib.Variant("d", track_id),
                    "method": GLib.Variant("s", "PUT" if liked else "DELETE"),
                    "token": GLib.Variant("s", token),
                })
                self.webview.call_async_javascript_function(
                    LIKE_TRACK_FUNCTION, -1, arguments, None, None, None, finished, None
                )
                return False

            GLib.idle_add(start)
            if not done.wait(15.0):
                raise TimeoutError("like request timed out")
            if result["outcome"] == "bot-check":
                raise BotCheckRequired("SoundCloud asked for a captcha")
            if result["outcome"] != "ok":
                raise OSError("like request failed")

        def _evaluate(self, script, callback):
            if not self.webview or not is_soundcloud_uri(self.webview.get_uri() or ""):
                callback({"error": "refusing untrusted page"})
                return
            def finished(webview, result, _data):
                try:
                    js_result = webview.run_javascript_finish(result)
                    value = js_result.get_js_value()
                    payload = json.loads(value.to_json(0))
                except Exception as exc:
                    payload = {"error": str(exc)}
                callback(payload)

            self.webview.run_javascript(script, None, finished, None)

    return SoundCloudApplication()


def request_backend(command: str, timeout: float = 4.0) -> dict[str, object] | None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    try:
        client.settimeout(max(0.1, deadline - time.monotonic()))
        client.connect(str(runtime_socket_path()))
        client.sendall((json.dumps({"command": command}) + "\n").encode())
        response = bytearray()
        while b"\n" not in response:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            client.settimeout(remaining)
            chunk = client.recv(min(16384, MAX_RESPONSE_BYTES + 1 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_RESPONSE_BYTES:
                return None
        raw, marker, trailing = bytes(response).partition(b"\n")
        if not marker or trailing or len(raw) > MAX_RESPONSE_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, RuntimeError):
        return None
    finally:
        client.close()


def launch_backend(show: bool) -> dict[str, object]:
    existing = request_backend("show" if show else "status", timeout=0.8)
    if existing is not None:
        return existing
    check = dependency_check()
    if not check["ok"]:
        return {"ok": False, "running": False, "error": check.get("error", "")}
    command = ["/usr/bin/python3", "-I", str(Path(__file__).resolve()), "daemon"]
    if show:
        command.append("--show")
    allowed_environment = {
        key: value
        for key, value in os.environ.items()
        if key in {
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "GDK_BACKEND",
            "HOME",
            "HYPRLAND_INSTANCE_SIGNATURE",
            "LANG",
            "LC_ALL",
            "PIPEWIRE_REMOTE",
            "PULSE_SERVER",
            "WAYLAND_DISPLAY",
            "WEBKIT_DISABLE_DMABUF_RENDERER",
            "XAUTHORITY",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR",
            "XDG_SESSION_TYPE",
        }
    }
    allowed_environment["PATH"] = "/usr/bin:/bin"
    event_log = open_runtime_event_log()
    try:
        event_log_fd = event_log.fileno()
        allowed_environment["OMARCHY_SOUNDCLOUD_EVENT_LOG_FD"] = str(event_log_fd)
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(event_log_fd,),
            start_new_session=True,
            env=allowed_environment,
        )
    finally:
        event_log.close()
    return {"ok": True, "starting": True, "running": True}


def wait_for_backend(timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if request_backend("status", timeout=1.0) is not None:
            return True
        time.sleep(0.3)
    return False


def wait_for_playback(timeout: float = 12.0) -> dict[str, object]:
    """The backend status once a track is playing or has failed, or the last status on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        state = request_backend("status", timeout=4.0) or {}
        if state.get("playing") or state.get("playbackState") == "error" or time.monotonic() >= deadline:
            return state
        time.sleep(0.4)


# Bidi controls can reorder a title on screen (a right-to-left override hides what it is).
_BIDI_CONTROLS = dict.fromkeys(
    [0x61C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)])


def clean_notification_text(text: str) -> str:
    """Text from SoundCloud made safe for a notification: one line, no control or bidi
    characters (an ESC could drive the terminal or daemon), and markup escaped."""
    text = " ".join(text.split()).translate(_BIDI_CONTROLS)
    text = "".join(char for char in text if unicodedata.category(char) != "Cc")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_desktop_notification(message: str) -> None:
    notifier = "/usr/bin/notify-send"
    if os.access(notifier, os.X_OK):
        try:
            subprocess.run(
                [notifier, "--app-name=SoundCloud", "--expire-time=4000", "--", "SoundCloud",
                 clean_notification_text(message)],
                check=False, timeout=NOTIFICATION_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def open_popup() -> None:
    shell = shutil.which("omarchy-shell")
    if shell:
        subprocess.run([shell, "shell", "summon", "brunosilveira.soundcloud"], check=False)


def play_url_command(text: str, notify: bool, show: bool = False, queue_next: bool = False) -> int:
    """Play a pasted SoundCloud track link, starting the backend if needed.

    With `queue_next` the track plays after the current one instead, or at once if nothing is playing."""
    def fail(message: str) -> int:
        print(json.dumps({"ok": False, "error": message}, sort_keys=True))
        if notify:
            send_desktop_notification(message)
        return 1

    try:
        _kind, link = parse_track_link(text)
    except LinkError as error:
        return fail(str(error))
    if notify and request_backend("status", timeout=0.8) is None:
        send_desktop_notification("Starting SoundCloud…")
    if not launch_backend(show=False).get("running") or not wait_for_backend():
        return fail("The SoundCloud backend didn't start")
    response = request_backend(("link-next:" if queue_next else "play-url:") + link, timeout=40.0)
    if response is None:
        return fail("The SoundCloud backend didn't answer")
    if response.get("ok") and response.get("queued") is True:
        print(json.dumps(response, sort_keys=True))
        if notify:
            label = " — ".join(str(response[key]) for key in ("title", "artist") if response.get(key))
            send_desktop_notification(f"Playing next: {label}" if label else "Playing next")
        if show:
            open_popup()
        return 0
    if response.get("ok"):
        state = wait_for_playback()
        if state.get("playbackState") == "error":
            return fail(CANT_PLAY_MESSAGE)
        print(json.dumps(response, sort_keys=True))
        if notify:
            label = " — ".join(str(state[key]) for key in ("title", "artist") if state.get(key))
            prefix = "Playing" if state.get("playing") else "Loading"
            send_desktop_notification(f"{prefix}: {label}" if label else prefix)
        if show:
            open_popup()
        return 0
    return fail(str(response.get("error") or CANT_PLAY_MESSAGE))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SoundCloud backend for the Omarchy bar")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check")
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--show", action="store_true")
    seek = subparsers.add_parser("seek")
    seek.add_argument("ratio", type=float)
    play = subparsers.add_parser("play")
    play.add_argument("playback_id")
    play_url = subparsers.add_parser("play-url", help="play a SoundCloud track link")
    play_url.add_argument("--notify", action="store_true", help="show what is playing, or what went wrong, as a desktop notification")
    play_url.add_argument("--open", action="store_true", help="open the bar popup once the track starts")
    play_url.add_argument("--next", action="store_true", help="play after the current track instead of now (plays at once if nothing is playing)")
    play_url.add_argument("url")
    subparsers.add_parser("launch")
    subparsers.add_parser("ensure")
    for action in ("status", "play-pause", "next", "previous", "home", "likes", "feed", "show", "stop"):
        subparsers.add_parser(action)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(sys.argv[1:] if argv is None else argv)
    command = args.command or "status"
    if command == "check":
        result = dependency_check()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    if command in {"launch", "ensure"}:
        result = launch_backend(show=command == "launch")
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("running") else 1
    if command == "play-url":
        return play_url_command(args.url, args.notify, args.open, args.next)
    if command != "daemon":
        if command == "seek":
            if not math.isfinite(args.ratio):
                print(json.dumps({"ok": False, "error": "invalid seek position"}, sort_keys=True))
                return 1
            backend_command = f"seek:{args.ratio}"
        elif command == "play":
            backend_command = f"play:{args.playback_id}"
        else:
            backend_command = command
        response = request_backend(backend_command)
        if response is None:
            response = {"ok": False, "running": False, "loggedIn": False}
        print(json.dumps(response, sort_keys=True))
        return 0 if response.get("ok") or command == "status" else 1

    configure_runtime_event_log()
    try:
        apply_graphics_workarounds()
        check = dependency_check()
        if not check["ok"]:
            print(check.get("error", "GTK/WebKit dependencies are missing"), file=sys.stderr)
            return 1
        app = build_application(show_on_start=bool(args.show))
        return app.run([sys.argv[0]])
    finally:
        close_runtime_event_log()


if __name__ == "__main__":
    raise SystemExit(main())
