#!/usr/bin/env python3
"""Civit-RV: local Civitai-style gallery for images and videos. No dependencies.

Usage: python3 server.py /path/to/media   (listens on 0.0.0.0:8765)
"""
import argparse
import email.utils
import errno
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"
STATE_FILE = APP_DIR / "state.json"
# Outside the project so a sync/backup of the app folder never carries the caches along
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "civit-rv"
CACHE_DIR = CACHE_ROOT / "thumbs"
PREVIEW_DIR = CACHE_ROOT / "previews"
# Pre-move thumbnail folder inside the project; only read, to remind the user to delete it
LEGACY_THUMBS = APP_DIR.resolve() / ".cache" / "thumbs"
CACHE_MAX_AGE = 30 * 86400  # cached files unused for this long are swept
TOUCH_EVERY = 86400  # refresh a file's last-use time at most this often
SWEEP_EVERY = 86400
HOST = "0.0.0.0"
PORT = 8765
THUMB_WIDTH = 512
CACHED_STATIC_EXT = {".png", ".svg", ".ico"}  # css/js stay uncached so edits show up right away

BASE: Path = Path(".")  # fixed top-level folder; nothing outside it is ever served or touched
ROOT: Path = Path(".")  # folder currently shown (BASE or a subfolder); only its own files are listed
ITEMS: list[dict] = []
LOCK = threading.Lock()
# Unique per process (microseconds since the epoch, exact as a JS number): a page left open across a
# restart must never hold a `ver` that matches the new process's list
SCAN_VERSION = time.time_ns() // 1000
FFMPEG = shutil.which("ffmpeg")
THUMB_SLOTS = threading.Semaphore(4)  # bound concurrent ffmpeg processes
PREVIEW_SLOTS = threading.Semaphore(2)  # clip encodes are heavy; keep them few
WARM = True  # pre-generate thumbnails and clips in the background after a scan
FAILED: set[str] = set()  # "kind:cache_key" whose generation failed in this process; not retried
FETCH_SLOTS = threading.Semaphore(4)  # bound concurrent whole-file reads of online-only files
FETCH_WAIT = 10  # seconds a request waits for an online-only file before answering 503
_FETCHING: dict[str, threading.Event] = {}  # source path -> Event set when its background read ends
_FETCH_LOCK = threading.Lock()
log = logging.getLogger("gallery")
LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK}


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**updates):
    state = load_state()
    state.update(updates)
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError:
        log.warning("Could not write %s", STATE_FILE)


def get_item(idx):
    # Negative indexes would silently wrap around to the end of the list
    with LOCK:
        return ITEMS[idx] if 0 <= idx < len(ITEMS) else None


def file_key(item):
    return f"{int(item['mtime'] * 1000)}-{item['size']}"


def cache_key(item):
    return hashlib.sha1(f"{ROOT}|{item['path']}|{item['mtime']}|{item['size']}".encode()).hexdigest()


def thumb_path(item):
    key = cache_key(item)
    return CACHE_DIR / key[:2] / f"{key}.jpg"


def preview_path(item):
    key = cache_key(item)
    return PREVIEW_DIR / key[:2] / f"{key}.mp4"


def drop_thumb(item):
    # A deleted file's cached thumbnail and preview would otherwise linger until swept
    for p in (thumb_path(item), preview_path(item)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def validate_name(stem, suffix):
    """Returns the full new file name for a rename, or raises HttpError(400) with the reason."""
    stem = stem.strip()
    if not stem:
        raise HttpError(400, "Name can't be empty")
    if re.search(r'[/\\:*?"<>|\x00-\x1f]', stem):
        raise HttpError(400, 'Name can\'t contain / \\ : * ? " < > |')
    if stem.startswith("."):
        raise HttpError(400, "Name can't start with a dot")
    name = stem + suffix
    if len(name.encode("utf-8", "surrogatepass")) > 255:
        raise HttpError(400, "Name is too long")
    return name


def touch_used(path):
    """Marks a cached file as used now, at most once per TOUCH_EVERY."""
    try:
        now = time.time()
        if now - path.stat().st_mtime > TOUCH_EVERY:
            os.utime(path, (now, now))
    except OSError:
        pass


def sweep_cache(now=None):
    """Deletes cached files unused for CACHE_MAX_AGE. Never follows symlinks. Returns the count."""
    cutoff = (time.time() if now is None else now) - CACHE_MAX_AGE
    removed = 0
    for base in (CACHE_DIR, PREVIEW_DIR):
        for dirpath, _dirs, files in os.walk(base, followlinks=False):
            for name in files:
                f = os.path.join(dirpath, name)
                try:
                    st = os.lstat(f)
                    if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                        os.unlink(f)
                        removed += 1
                except OSError:
                    pass
    return removed


def cache_stats(d):
    """File count, bytes and oldest mtime under d (regular files only, no symlinks, no *.tmp.*). None if d is missing."""
    if not os.path.isdir(d):
        return None
    files = total = 0
    oldest = None
    for dirpath, _dirs, names in os.walk(d, followlinks=False):
        for name in names:
            if ".tmp." in name:
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files += 1
            total += st.st_size
            oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
    return {"files": files, "bytes": total, "oldest_use": oldest}


def parse_ids(body):
    """Validates the ids/ver fields of a batch request. Raises ValueError."""
    ids, ver = body.get("ids"), body.get("ver")
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids must be a non-empty list")
    # bool is a subclass of int, so it has to be excluded explicitly
    if any(not isinstance(i, int) or isinstance(i, bool) for i in ids):
        raise ValueError("ids must be integers")
    if not isinstance(ver, int) or isinstance(ver, bool):
        raise ValueError("ver must be an integer")
    return ids


def is_online_only(path):
    """True for an iCloud placeholder (size known, no blocks on disk). os.stat does not trigger a download."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_size > 0 and st.st_blocks == 0


def _read_fully(path):
    """Reads the whole file and discards it: this is what makes iCloud download it."""
    with open(path, "rb") as f:
        while f.read(1 << 20):
            pass


def _fetch_worker(path, key, event, root, version):
    try:
        with FETCH_SLOTS:
            _read_fully(path)
        ok = True
    except Exception as e:
        ok = False
        log.warning("Download failed for %s: %s", path, e)
    finally:
        event.set()
        with _FETCH_LOCK:
            _FETCHING.pop(key, None)
    if ok:
        try:
            with LOCK:  # same pattern as warm(): only act if the folder is still the one fetch() saw
                items = list(ITEMS) if SCAN_VERSION == version else []
            for item in items:
                if not item.get("moved") and str(root / item["path"]) == key:
                    if SCAN_VERSION == version:
                        make_thumb(item)
                    break
        except Exception:
            log.exception("Thumbnail after download failed for %s", path)


def fetch(path):
    """Starts (once per path) a background read of an online-only file; returns an Event set when it ends."""
    key = str(path)
    with LOCK:
        root, version = ROOT, SCAN_VERSION
    with _FETCH_LOCK:
        event = _FETCHING.get(key)
        if event is None:
            event = _FETCHING[key] = threading.Event()
            try:
                threading.Thread(target=_fetch_worker, args=(path, key, event, root, version), daemon=True).start()
            except BaseException:
                del _FETCHING[key]
                event.set()
                raise
    return event


def make_thumb(item):
    """Returns the cached thumbnail path (creating it with ffmpeg if needed), or None on failure."""
    if not FFMPEG:
        return None
    out = thumb_path(item)
    if out.is_file():
        return out
    fail_key = f"thumb:{cache_key(item)}"
    if fail_key in FAILED:
        return None
    src = ROOT / item["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{threading.get_ident()}.tmp.jpg")
    cmd = [FFMPEG, "-v", "error", "-y"]
    if item["kind"] == "video":
        cmd += ["-ss", "0.1"]
    cmd += ["-i", str(src), "-frames:v", "1",
            "-vf", f"scale='min({THUMB_WIDTH},iw)':-2", "-q:v", "5", str(tmp)]
    with THUMB_SLOTS:
        if out.is_file():  # another request finished it while we waited
            return out
        if fail_key in FAILED:
            return None
        try:
            subprocess.run(cmd, check=True, timeout=60, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            os.replace(tmp, out)
            return out
        except subprocess.TimeoutExpired:
            log.warning("Thumbnail timed out for %s (not remembered as failed)", item["path"])
            return None
        except (subprocess.SubprocessError, OSError) as e:
            FAILED.add(fail_key)
            log.warning("Thumbnail failed for %s: %s", item["path"], getattr(e, "stderr", e))
            return None
        finally:
            tmp.unlink(missing_ok=True)


def make_preview(item, wait=True):
    """Returns the cached 6 s silent preview clip path (encoding it if needed), or None on failure.
    With wait=False it gives up (None) instead of queueing when both encode slots are busy."""
    if not FFMPEG:
        return None
    out = preview_path(item)
    if out.is_file():
        return out
    fail_key = f"preview:{cache_key(item)}"
    if fail_key in FAILED:
        return None
    src = ROOT / item["path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{threading.get_ident()}.tmp.mp4")
    cmd = [FFMPEG, "-v", "error", "-y", "-ss", "0.1", "-i", str(src), "-t", "6", "-an",
           "-vf", "fps=24,scale='min(480,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)]
    if not PREVIEW_SLOTS.acquire(blocking=wait):
        return None
    try:
        if out.is_file():  # another request finished it while we waited
            return out
        if fail_key in FAILED:
            return None
        try:
            subprocess.run(cmd, check=True, timeout=120, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            os.replace(tmp, out)
            return out
        except subprocess.TimeoutExpired:
            log.warning("Preview timed out for %s (not remembered as failed)", item["path"])
            return None
        except (subprocess.SubprocessError, OSError) as e:
            FAILED.add(fail_key)
            log.warning("Preview failed for %s: %s", item["path"], getattr(e, "stderr", e))
            return None
        finally:
            tmp.unlink(missing_ok=True)
    finally:
        PREVIEW_SLOTS.release()


def warm(version):
    """Pre-generates thumbnails and clips for the current items, newest first, until the folder changes."""
    with LOCK:
        items = list(ITEMS)
    for item in items:
        if SCAN_VERSION != version:
            return
        if item.get("moved"):
            continue
        if is_online_only(ROOT / item["path"]):  # never start a download from the warm-up
            continue
        try:
            make_thumb(item)
            if item["kind"] == "video" and SCAN_VERSION == version:
                make_preview(item, wait=False)  # never queue ahead of on-demand clip requests
        except Exception:
            log.exception("Warm-up failed for %s", item.get("path"))


def kind_of(name):
    ext = os.path.splitext(name)[1].lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None


def resolve_rel(rel):
    """Resolves a '/'-separated path relative to BASE. Returns None if it is hidden, outside BASE or not a folder."""
    parts = [p for p in rel.split("/") if p]
    if any(p.startswith(".") for p in parts):  # also rejects ".."
        return None
    p = (BASE / "/".join(parts)).resolve()
    if p != BASE and BASE not in p.parents:
        return None
    return p if p.is_dir() else None


def rel_of(p):
    return "" if p == BASE else p.relative_to(BASE).as_posix()


def scan(root=None):
    """Indexes `root` (default: the current folder) and swaps ROOT, ITEMS and SCAN_VERSION together under LOCK."""
    global ROOT, SCAN_VERSION
    root = ROOT if root is None else root
    items = []
    with os.scandir(root) as it:
        for e in it:
            if e.name.startswith(".") or not e.is_file():
                continue
            kind = kind_of(e.name)
            if not kind:
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            items.append({
                "path": e.name,
                "name": e.name,
                "kind": kind,
                "mtime": st.st_mtime,
                "size": st.st_size,
            })
    items.sort(key=lambda i: i["mtime"], reverse=True)
    with LOCK:
        ROOT = root
        ITEMS[:] = items
        SCAN_VERSION += 1
        version = SCAN_VERSION
    log.info("Scanned %s: %d files", root, len(items))
    if WARM and FFMPEG:
        threading.Thread(target=warm, args=(version,), daemon=True).start()
    return len(items)


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def origin_allowed(headers):
    """CSRF origin check: no Origin is fine; otherwise its host must be the Host or X-Forwarded-Host (set by a
    TLS-terminating proxy such as `tailscale serve`). An empty netloc (Origin: null) never matches."""
    origin = headers.get("Origin")
    if origin is None:
        return True
    netloc = urlparse(origin).netloc
    return bool(netloc) and netloc in (headers.get("Host", ""), headers.get("X-Forwarded-Host", ""))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Media requests are too frequent to be useful at INFO level
        if not self.path.startswith("/media/"):
            log.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, obj, status=200, no_store=False, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query, keep_blank_values=True)  # an empty path means the top-level folder
        try:
            if url.path == "/":
                return self.send_file(STATIC_DIR / "index.html")
            if url.path == "/api/items":
                return self.api_items(q)
            if url.path.startswith("/static/"):
                f = (STATIC_DIR / url.path[8:]).resolve()
                if STATIC_DIR.resolve() not in f.parents:
                    return self.send_json({"error": "forbidden"}, 403)
                return self.send_file(f, cache=f.suffix in CACHED_STATIC_EXT)
            if url.path == "/favicon.ico":
                return self.send_file(STATIC_DIR / "favicon-32x32.png", cache=True)
            if url.path == "/api/info":
                return self.send_json({"folder": str(ROOT), "path": rel_of(ROOT), "base": BASE.name})
            if url.path == "/api/folder":
                return self.switch_folder(q.get("path", [""])[0])
            if url.path == "/api/folders":
                return self.list_folders(q.get("path", [None])[0])
            if url.path == "/api/rescan":
                return self.send_json({"count": scan()})
            if url.path == "/api/report":
                return self.api_report()
            if url.path.startswith("/media/"):
                return self.media(int(url.path[7:]), q)
            if url.path.startswith("/thumb/"):
                return self.thumb(int(url.path[7:]), q)
            if url.path.startswith("/preview/"):
                return self.preview(int(url.path[9:]), q)
        except (BrokenPipeError, ConnectionResetError):
            return
        except ValueError:  # non-numeric ids and similar malformed input
            return self.send_json({"error": "bad request"}, 400)
        except Exception:
            log.exception("Request failed: %s %s", self.command, self.path)
            return self.send_json({"error": "bad request"}, 400)
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/move":
                return self.move_items(self.read_json_body())
            if url.path == "/api/delete":
                return self.delete_items(self.read_json_body())
            if url.path == "/api/rename":
                return self.rename_item(self.read_json_body())
        except (BrokenPipeError, ConnectionResetError):
            return
        except HttpError as e:
            return self.send_json({"error": e.message}, e.status)
        except ValueError:  # non-numeric ids and similar malformed input
            return self.send_json({"error": "bad request"}, 400)
        except Exception:
            log.exception("Request failed: %s %s", self.command, self.path)
            return self.send_json({"error": "bad request"}, 400)
        self.send_json({"error": "not found"}, 404)

    def list_folders(self, rel):
        """Subfolders of `rel` (default: the current folder's parent) with breadcrumbs, for the folder navigator."""
        if rel is None:
            rel = rel_of(ROOT.parent) if ROOT != BASE else ""
        d = resolve_rel(rel)
        if d is None:
            return self.send_json({"error": "Invalid folder"}, 400)
        base_rel = rel_of(d)
        folders = []
        with os.scandir(d) as it:
            entries = sorted((e for e in it if not e.name.startswith(".") and e.is_dir()),
                             key=lambda e: e.name.lower())
        for e in entries:
            subdirs = files = 0
            try:
                with os.scandir(e.path) as sub:
                    for c in sub:
                        if c.name.startswith("."):
                            continue
                        if c.is_dir():
                            subdirs += 1
                        elif kind_of(c.name):
                            files += 1
            except OSError:
                pass
            folders.append({"name": e.name, "path": f"{base_rel}/{e.name}".lstrip("/"),
                            "subdirs": subdirs, "files": files})
        crumbs, acc = [{"name": BASE.name, "path": ""}], ""
        for part in base_rel.split("/") if base_rel else []:
            acc = f"{acc}/{part}".lstrip("/")
            crumbs.append({"name": part, "path": acc})
        self.send_json({"path": base_rel, "crumbs": crumbs, "folders": folders, "current": rel_of(ROOT)})

    def read_json_body(self):
        # Always consume the body before replying so a keep-alive connection stays in sync
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            raise ValueError("Content-Length required")
        if not 0 < n <= 1_000_000:
            self.close_connection = True  # the body is left unread, so the stream can't be reused
            raise ValueError("bad body size")
        raw = self.rfile.read(n)
        # CSRF guard: a cross-site form/fetch can only send "simple" requests (text/plain etc.) with no
        # preflight, so demanding JSON forces a preflight this server never approves; also refuse foreign origins
        if not origin_allowed(self.headers):
            raise HttpError(403, "Forbidden origin")
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise HttpError(415, "Unsupported media type")
        try:
            body = json.loads(raw)
        except ValueError:
            raise ValueError("invalid JSON")
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        return body

    def batch(self, body, dest_dir, one):
        """Runs one(item, src) for each id under LOCK; replies with per-id results or 409 on a stale scan."""
        ids = parse_ids(body)
        with LOCK:
            if body["ver"] != SCAN_VERSION:
                return self.send_json({"error": "Folder changed — reload to continue"}, 409)
            results = []
            for idx in ids:
                r = {"id": idx, "ok": False}
                item = ITEMS[idx] if 0 <= idx < len(ITEMS) else None
                try:
                    if item is None:
                        r["error"] = "File not found"
                    elif item.get("moved"):
                        r["error"] = "Already moved"
                    elif (ROOT / item["path"]).is_symlink():
                        r["error"] = "Symlinks are not supported"  # resolving would act on the link's target
                    else:
                        src = (ROOT / item["path"]).resolve()
                        if ROOT not in src.parents or not src.is_file():
                            r["error"] = "File not found"
                        elif dest_dir is not None and src.parent == dest_dir:
                            r["error"] = "File is already in that folder"
                        else:
                            one(item, src)
                            item["moved"] = True
                            r = {"id": idx, "ok": True}
                except OSError as e:
                    log.warning("Failed on %s: %s", item and item["path"], e)
                    r["error"] = "File not found" if item is None else f"Could not process file: {e.strerror or e}"
                results.append(r)
        done = sum(r["ok"] for r in results)
        log.info("%s: %d of %d files", self.path, done, len(results))
        self.send_json({"results": results})

    def move_items(self, body):
        parse_ids(body)
        dest = body.get("dest")
        if not isinstance(dest, str):
            raise ValueError("dest must be a string")
        dest_dir = resolve_rel(dest)
        if dest_dir is None:
            return self.send_json({"error": "Invalid destination folder"}, 400)

        def one(item, src):
            target = dest_dir / src.name
            n = 1
            while target.exists():
                target = dest_dir / f"{src.stem}_{n}{src.suffix}"
                n += 1
            shutil.move(str(src), str(target))
            log.info("Moved %s -> %s", src, target)

        self.batch(body, dest_dir, one)

    def delete_items(self, body):
        def one(item, src):
            os.unlink(src)
            drop_thumb(item)
            log.info("Deleted %s", src)

        self.batch(body, None, one)

    def rename_item(self, body):
        idx, ver, stem = body.get("id"), body.get("ver"), body.get("name")
        if any(not isinstance(v, int) or isinstance(v, bool) for v in (idx, ver)) or not isinstance(stem, str):
            raise HttpError(400, "id and ver must be integers and name a string")
        with LOCK:
            if ver != SCAN_VERSION:
                return self.send_json({"error": "Folder changed — reload to continue"}, 409)
            item = ITEMS[idx] if 0 <= idx < len(ITEMS) else None
            if item is None or item.get("moved"):
                return self.send_json({"error": "File not found"}, 404)
            link = ROOT / item["path"]
            if link.is_symlink():
                return self.send_json({"error": "Symlinks are not supported"}, 400)
            src = link.resolve()
            if ROOT not in src.parents or not src.is_file():
                return self.send_json({"error": "File not found"}, 404)
            new_name = validate_name(stem, Path(item["name"]).suffix)
            if new_name == item["name"]:
                return self.send_json({"ok": True, "id": idx, "name": item["name"], "path": item["path"]})
            with os.scandir(src.parent) as it:
                for entry in it:
                    if entry.name != item["name"] and entry.name.casefold() == new_name.casefold():
                        raise HttpError(400, f"A file named {new_name} already exists")
            target = src.parent / new_name
            old_cache = (thumb_path(item), preview_path(item))
            exists = HttpError(400, f"A file named {new_name} already exists")
            try:
                if new_name.casefold() == item["name"].casefold():
                    os.rename(src, target)  # case-only change: the "target" is the file itself
                else:
                    # os.rename would silently replace a file created since the check above; os.link refuses
                    try:
                        os.link(src, target)
                    except FileExistsError:
                        raise exists
                    except OSError as e:
                        if e.errno not in LINK_UNSUPPORTED:
                            raise
                        if os.path.lexists(target):
                            raise exists
                        os.rename(src, target)
                    else:
                        try:
                            os.unlink(src)
                        except OSError:
                            os.unlink(target)  # never leave two names for one file
                            raise
            except HttpError:
                raise
            except OSError as e:
                log.warning("Rename failed for %s: %s", item["path"], e)
                raise HttpError(400, f"Could not rename file: {e.strerror or e}")
            old_path = item["path"]
            item["name"] = new_name
            item["path"] = target.relative_to(ROOT).as_posix()
            for old, new in zip(old_cache, (thumb_path(item), preview_path(item))):
                try:
                    new.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old, new)
                except OSError:
                    pass
            result = {"ok": True, "id": idx, "name": item["name"], "path": item["path"]}
        log.info("Renamed %s -> %s", old_path, result["path"])
        self.send_json(result)

    def set_folder(self, path):
        new = Path(path)
        count = scan(new)  # swaps ROOT/ITEMS/SCAN_VERSION atomically once the walk is done
        save_state(folder=str(new))
        log.info("Folder changed to %s", new)
        self.send_json({"folder": str(new), "path": rel_of(new), "count": count})

    def switch_folder(self, rel):
        new = resolve_rel(rel)
        if new is None:
            return self.send_json({"error": "Invalid folder"}, 400)
        self.set_folder(new)

    def api_report(self):
        with LOCK:
            items = [it for it in ITEMS if not it.get("moved")]
        videos = [it for it in items if it["kind"] == "video"]
        body = json.dumps({
            "thumbs": cache_stats(CACHE_DIR),
            "previews": cache_stats(PREVIEW_DIR),
            "legacy_thumbs": cache_stats(LEGACY_THUMBS),
            "failed": {k: sum(1 for f in list(FAILED) if f.startswith(k + ":")) for k in ("thumb", "preview")},
            "folder": {
                "images": len(items) - len(videos),
                "videos": len(videos),
                "bytes": sum(it["size"] for it in items),
                "videos_without_clip": sum(1 for it in videos if not preview_path(it).exists()),
            },
            "max_age_days": CACHE_MAX_AGE // 86400,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_items(self, q):
        offset = max(0, int(q.get("offset", ["0"])[0]))
        limit = min(200, max(1, int(q.get("limit", ["40"])[0])))
        sort = q.get("sort", ["random"])[0]
        kind = q.get("kind", ["all"])[0]
        search = q.get("q", [""])[0].lower().strip()
        with LOCK:
            indexed = [t for t in enumerate(ITEMS) if not t[1].get("moved")]
            ver = SCAN_VERSION  # same snapshot as ITEMS
        if kind in ("image", "video"):
            indexed = [t for t in indexed if t[1]["kind"] == kind]
        if search:
            indexed = [t for t in indexed if search in t[1]["path"].lower()]
        if sort == "random":
            # A per-session seed keeps the order stable across pages
            random.Random(q.get("seed", ["0"])[0]).shuffle(indexed)
        elif sort == "oldest":
            indexed.sort(key=lambda t: t[1]["mtime"])
        elif sort == "name":
            indexed.sort(key=lambda t: t[1]["path"].lower())
        elif sort == "size":
            indexed.sort(key=lambda t: t[1]["size"], reverse=True)
        page = indexed[offset:offset + limit]
        self.send_json({
            "total": len(indexed),
            "ver": ver,
            "items": [{"id": i, **it, "k": file_key(it)} for i, it in page],
        })

    def stale_key(self, item, q):
        """Ids are list positions that change on rescan, so a `k` that is not this item's key belongs to another
        file: answer 404 no-store (never its bytes, which a cache could pin) and return True."""
        if "k" in q and q["k"][0] != file_key(item):
            self.send_json({"error": "stale"}, 404, no_store=True)
            return True
        return False

    def media(self, idx, q):
        item = get_item(idx)
        if item is None or item.get("moved"):
            return self.send_json({"error": "not found"}, 404)
        full = (ROOT / item["path"]).resolve()
        if ROOT not in full.parents:
            return self.send_json({"error": "forbidden"}, 403)
        if self.stale_key(item, q):
            return
        self.send_file(full, immutable="k" in q)

    def still_downloading(self, full):
        """For an online-only file: starts its download and waits up to FETCH_WAIT. If it is not there yet,
        answers 503 (the download keeps going) and returns True."""
        # a finished fetch is not enough: it may have failed or the file been evicted again
        if is_online_only(full) and not (fetch(full).wait(FETCH_WAIT) and not is_online_only(full)):
            self.send_json({"error": "downloading"}, 503, no_store=True, headers={"Retry-After": "10"})
            return True
        return False

    def thumb(self, idx, q):
        item = get_item(idx)
        if item is None or item.get("moved"):
            return self.send_json({"error": "not found"}, 404)
        full = (ROOT / item["path"]).resolve()
        if ROOT not in full.parents:
            return self.send_json({"error": "forbidden"}, 403)
        if self.stale_key(item, q):
            return
        keyed = "k" in q
        if not thumb_path(item).is_file() and self.still_downloading(full):
            return
        t = make_thumb(item)
        if t:
            touch_used(t)
            return self.send_file(t, immutable=keyed)
        if item["kind"] == "image":  # no ffmpeg or it failed: the original still works
            return self.send_file(full)  # not immutable: a later thumbnail must not be hidden
        self.send_json({"error": "no thumbnail"}, 404)

    def preview(self, idx, q):
        item = get_item(idx)
        if item is None or item.get("moved") or item["kind"] != "video":
            return self.send_json({"error": "not found"}, 404)
        full = (ROOT / item["path"]).resolve()
        if ROOT not in full.parents:
            return self.send_json({"error": "forbidden"}, 403)
        if self.stale_key(item, q):
            return
        keyed = "k" in q
        if not preview_path(item).is_file() and self.still_downloading(full):
            return
        clip = make_preview(item)
        if clip:
            touch_used(clip)
            return self.send_file(clip, immutable=keyed)
        self.send_file(full)  # no ffmpeg or it failed: the original; not immutable, a clip may come later

    def send_file(self, path: Path, cache=False, immutable=False):
        if not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        st = path.stat()
        size = st.st_size
        last_mod = email.utils.formatdate(int(st.st_mtime), usegmt=True)
        ims = self.headers.get("If-Modified-Since")
        if ims and not self.headers.get("Range"):
            try:
                if email.utils.parsedate_to_datetime(ims).timestamp() >= int(st.st_mtime):
                    self.send_response(304)
                    self.send_header("Last-Modified", last_mod)
                    self.send_header("Cache-Control", cache_control(cache, immutable))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            except (TypeError, ValueError):
                pass
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Last-Modified", last_mod)
        self.send_header("Cache-Control", cache_control(cache, immutable))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def cache_control(cache, immutable):
    if immutable:
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600" if cache else "no-cache"


def sweep_loop():
    while True:
        time.sleep(SWEEP_EVERY)
        try:
            log.info("Cache sweep removed %d stale files", sweep_cache())
        except Exception:
            log.exception("Cache sweep failed")


def display_host(host):
    if host not in ("0.0.0.0", "::"):
        return host
    # Finds the LAN IP of the default route; no packets are actually sent
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class Server(ThreadingHTTPServer):
    request_queue_size = 128

    def handle_error(self, request, client_address):
        # Browsers routinely abort connections (scrolling, cancelled media loads)
        if isinstance(sys.exc_info()[1], ConnectionError):
            log.debug("Connection dropped by %s", client_address[0])
        else:
            log.exception("Error handling request from %s", client_address[0])


def main():
    global BASE, ROOT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="top-level folder; you can open it and any subfolder, nothing outside it")
    args = ap.parse_args()
    BASE = ROOT = Path(args.folder).expanduser().resolve()
    if not BASE.is_dir():
        raise SystemExit(f"Not a folder: {BASE}")
    saved = load_state().get("folder")
    if saved:
        saved = Path(saved).resolve()
        if saved.is_dir() and (saved == BASE or BASE in saved.parents):
            ROOT = saved
            log.info("Resuming in %s", ROOT)
    save_state(folder=str(ROOT))
    if not FFMPEG:
        log.warning("ffmpeg not found: tiles will load full-size originals and videos will have no posters")
    t = time.perf_counter()
    n = scan()
    log.info("Indexed %d files in %.1fs", n, time.perf_counter() - t)
    log.info("Cache sweep removed %d stale files", sweep_cache())
    threading.Thread(target=sweep_loop, daemon=True).start()
    log.info("Open http://%s:%d", display_host(HOST), PORT)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
