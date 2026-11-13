#!/usr/bin/env python3
"""Civit-RV: local Civitai-style gallery for images and videos. No dependencies.

Usage: python3 server.py /path/to/media   (listens on 0.0.0.0:8765)
"""
import argparse
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import socket
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
CACHE_DIR = APP_DIR / ".cache" / "thumbs"
HOST = "0.0.0.0"
PORT = 8765
THUMB_WIDTH = 512
CACHED_STATIC_EXT = {".png", ".svg", ".ico"}  # css/js stay uncached so edits show up right away

BASE: Path = Path(".")  # fixed top-level folder; nothing outside it is ever served or touched
ROOT: Path = Path(".")  # folder currently shown (BASE or a subfolder); only its own files are listed
ITEMS: list[dict] = []
LOCK = threading.Lock()
SCAN_VERSION = 0
FFMPEG = shutil.which("ffmpeg")
THUMB_SLOTS = threading.Semaphore(4)  # bound concurrent ffmpeg processes
log = logging.getLogger("gallery")


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


def thumb_path(item):
    key = hashlib.sha1(f"{ROOT}|{item['path']}|{item['mtime']}|{item['size']}".encode()).hexdigest()
    return CACHE_DIR / key[:2] / f"{key}.jpg"


def drop_thumb(item):
    # A deleted file's cached thumbnail would otherwise linger forever
    try:
        thumb_path(item).unlink(missing_ok=True)
    except OSError:
        pass


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


def make_thumb(item):
    """Returns the cached thumbnail path (creating it with ffmpeg if needed), or None on failure."""
    if not FFMPEG:
        return None
    out = thumb_path(item)
    if out.is_file():
        return out
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
        try:
            subprocess.run(cmd, check=True, timeout=60, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            os.replace(tmp, out)
            return out
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("Thumbnail failed for %s: %s", item["path"], getattr(e, "stderr", e))
            return None
        finally:
            tmp.unlink(missing_ok=True)


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


def scan():
    items = []
    with os.scandir(ROOT) as it:
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
    global SCAN_VERSION
    with LOCK:
        ITEMS[:] = items
        SCAN_VERSION += 1
    log.info("Scanned %s: %d files", ROOT, len(items))
    return len(items)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Media requests are too frequent to be useful at INFO level
        if not self.path.startswith("/media/"):
            log.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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
            if url.path.startswith("/media/"):
                return self.media(int(url.path[7:]))
            if url.path.startswith("/thumb/"):
                return self.thumb(int(url.path[7:]))
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
        except (BrokenPipeError, ConnectionResetError):
            return
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
            raise ValueError("Content-Length required")
        if not 0 < n <= 1_000_000:
            raise ValueError("bad body size")
        raw = self.rfile.read(n)
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

    def set_folder(self, path):
        global ROOT
        ROOT = Path(path)
        save_state(folder=str(ROOT))
        log.info("Folder changed to %s", ROOT)
        self.send_json({"folder": str(ROOT), "path": rel_of(ROOT), "count": scan()})

    def switch_folder(self, rel):
        new = resolve_rel(rel)
        if new is None:
            return self.send_json({"error": "Invalid folder"}, 400)
        self.set_folder(new)

    def api_items(self, q):
        offset = max(0, int(q.get("offset", ["0"])[0]))
        limit = min(200, max(1, int(q.get("limit", ["40"])[0])))
        sort = q.get("sort", ["random"])[0]
        kind = q.get("kind", ["all"])[0]
        search = q.get("q", [""])[0].lower().strip()
        with LOCK:
            indexed = [t for t in enumerate(ITEMS) if not t[1].get("moved")]
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
            "ver": SCAN_VERSION,
            "items": [{"id": i, **it} for i, it in page],
        })

    def media(self, idx):
        item = get_item(idx)
        if item is None or item.get("moved"):
            return self.send_json({"error": "not found"}, 404)
        full = (ROOT / item["path"]).resolve()
        if ROOT not in full.parents:
            return self.send_json({"error": "forbidden"}, 403)
        self.send_file(full, cache=True)

    def thumb(self, idx):
        item = get_item(idx)
        if item is None or item.get("moved"):
            return self.send_json({"error": "not found"}, 404)
        full = (ROOT / item["path"]).resolve()
        if ROOT not in full.parents:
            return self.send_json({"error": "forbidden"}, 403)
        t = make_thumb(item)
        if t:
            return self.send_file(t, cache=True)
        if item["kind"] == "image":  # no ffmpeg or it failed: the original still works
            return self.send_file(full, cache=True)
        self.send_json({"error": "no thumbnail"}, 404)

    def send_file(self, path: Path, cache=False):
        if not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        size = path.stat().st_size
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
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-cache")
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
    log.info("Open http://%s:%d", display_host(HOST), PORT)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
