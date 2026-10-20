#!/usr/bin/env python3
"""Live check that SoundCloud's private API still has the shapes the plugin relies on.

Read-only GET calls with the signed-in session. The token is never printed. Run it before a
release, or when something stops working: it names the endpoint that changed.
Needs network and a signed-in plugin session. Not run by scripts/verify.sh unless --live is given.

Exit codes: 0 all checks pass, or SKIP (not signed in, no network, session expired);
            1 an interface changed (a line starting "SoundCloud changed an interface").
Usage: python3 -I scripts/api-contract.py
"""
import importlib.util
import json
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("sc", os.path.join(HERE, "soundcloud_app.py"))
sc = importlib.util.module_from_spec(spec)
sys.modules["sc"] = sc
spec.loader.exec_module(sc)

failed = []  # endpoint names, in order


def skip(reason):
    print(f"SKIP  API contract check: {reason}")
    sys.exit(0)


def load_token():
    path = sc.profile_paths().data_dir / "cookies.json"
    try:
        records = json.load(open(path))
    except (OSError, ValueError):
        return None
    return sc.oauth_token_from_cookies(records) if isinstance(records, list) else None


def has(obj, *keys):
    return isinstance(obj, dict) and all(k in obj for k in keys)


def row_ok(row):
    track = row["track"] if has(row, "track") else row
    return has(track, "id", "title", "permalink_url", "duration", "user", "media") and isinstance(track["media"].get("transcodings"), list)


def check(endpoint, name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + f"{endpoint}: {name}" + (f" ({detail})" if detail and not condition else ""))
    if not condition and endpoint not in failed:
        failed.append(endpoint)


token = load_token()
if not token:
    skip("not signed in (no session found in the plugin's profile); sign in from the popup first")


def get(path):
    try:
        return sc.soundcloud_api_request("GET", path, token, 2 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403) and path == "/me":
            skip(f"the saved session was rejected (HTTP {error.code}); sign in again")
        return {"__error__": f"HTTP {error.code}"}
    except (urllib.error.URLError, OSError, TimeoutError):
        if path == "/me":
            skip("SoundCloud is not reachable (no network?)")
        return {"__error__": "request failed"}
    except ValueError:
        return {"__error__": "reply was not valid JSON or too large"}


me = get("/me")
check("/me", "has id, username, permalink_url, avatar_url", has(me, "id", "username", "permalink_url", "avatar_url"), me.get("__error__", "") if isinstance(me, dict) else "")
check("/me", "avatar url uses the -large size name the plugin upgrades", "-large." in str((me or {}).get("avatar_url", "")))
if not has(me, "id"):
    print("\nSoundCloud changed an interface: /me (no user id). Nothing else can be checked.")
    sys.exit(1)
uid = me["id"]
likes = get(f"/users/{uid}/track_likes?limit=5")
check("/users/{id}/track_likes", "collection of {track} rows", has(likes, "collection") and bool(likes["collection"]) and has(likes["collection"][0], "track") and row_ok(likes["collection"][0]))
history = get("/me/play-history/tracks?limit=5")
check("/me/play-history/tracks", "rows have played_at (ms) and a track", has(history, "collection") and bool(history["collection"]) and has(history["collection"][0], "played_at", "track"))
rows = (history.get("collection") if isinstance(history, dict) else None) or (likes.get("collection") if isinstance(likes, dict) else None) or []
if not rows:
    print("\nSKIP  the remaining checks need at least one liked or played track on this account")
    sys.exit(1 if failed else 0)
first = rows[0]["track"]
related = get(f"/tracks/{first['id']}/related?limit=5")
check("/tracks/{id}/related", "bare track objects", has(related, "collection") and bool(related["collection"]) and row_ok(related["collection"][0]))
resolved = get("/resolve?url=" + sc.quote(first["permalink_url"], safe=""))
check("/resolve", "a track page URL gives a track with a policy", has(resolved, "id", "kind", "policy") and resolved.get("kind") == "track")
search = get("/search/tracks?q=daft%20punk&limit=3")
check("/search/tracks", "collection and total_results (planned search endpoint)", has(search, "collection", "total_results"))
trans = first.get("media", {}).get("transcodings", [])
check("track.media.transcodings", "have preset, url and format.protocol", bool(trans) and all(has(t, "preset", "format", "url") and has(t["format"], "protocol") for t in trans))
protocols = {t["format"]["protocol"] for t in trans if has(t, "format") and has(t["format"], "protocol")}
check("track.media.transcodings", "include a protocol the plugin knows", bool(protocols & {"hls", "progressive", "cbc-encrypted-hls", "ctr-encrypted-hls"}), str(sorted(protocols)))

if failed:
    for endpoint in failed:
        print(f"\nSoundCloud changed an interface: {endpoint}")
    print("Update the matching parsing code in soundcloud_app.py and its tests, then re-run this check.")
    sys.exit(1)
print("\nALL CONTRACT CHECKS PASSED")
