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

ROOT: Path = Path(".")
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


def scan():
    items = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
            if not kind:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            items.append({
                "path": os.path.relpath(full, ROOT),
                "name": name,
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
        q = parse_qs(url.query)
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
                return self.send_json({"folder": str(ROOT)})
            if url.path == "/api/folder":
                return self.switch_folder(q.get("name", [""])[0])
            if url.path == "/api/folders":
                return self.list_folders()
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
        q = parse_qs(url.query)
        try:
            if url.path == "/api/move":
                return self.move_item(int(q["id"][0]), q["dest"][0])
            if url.path == "/api/delete":
                return self.delete_item(int(q["id"][0]))
        except (BrokenPipeError, ConnectionResetError):
            return
        except ValueError:  # non-numeric ids and similar malformed input
            return self.send_json({"error": "bad request"}, 400)
        except Exception:
            log.exception("Request failed: %s %s", self.command, self.path)
            return self.send_json({"error": "bad request"}, 400)
        self.send_json({"error": "not found"}, 404)

    def list_folders(self):
        # Sibling folders of the current folder (same root, different leaf)
        parent = ROOT.parent
        names = sorted((p.name for p in parent.iterdir() if p.is_dir() and not p.name.startswith(".")),
                       key=str.lower)
        self.send_json({"root": str(parent), "current": ROOT.name, "folders": names})

    def move_item(self, idx, dest):
        parent = ROOT.parent
        dest_dir = (parent / dest).resolve()
        if dest_dir.parent != parent.resolve() or dest.startswith(".") or not dest_dir.is_dir():
            return self.send_json({"error": "Invalid destination folder"}, 400)
        with LOCK:
            item = ITEMS[idx] if 0 <= idx < len(ITEMS) else None
            if item is None:
                return self.send_json({"error": "File not found"}, 404)
            if item.get("moved"):
                return self.send_json({"error": "Already moved"}, 409)
            src = (ROOT / item["path"]).resolve()
            if ROOT not in src.parents or not src.is_file():
                return self.send_json({"error": "File not found"}, 404)
            if src.parent == dest_dir:
                return self.send_json({"error": "File is already in that folder"}, 400)
            target = dest_dir / src.name
            n = 1
            while target.exists():
                target = dest_dir / f"{src.stem}_{n}{src.suffix}"
                n += 1
            shutil.move(str(src), str(target))
            log.info("Moved %s -> %s", src, target)
            if dest_dir == ROOT:
                item["path"] = os.path.relpath(target, ROOT)
                item["name"] = target.name
                moved = False
            else:
                item["moved"] = True
                moved = True
        self.send_json({"ok": True, "moved": moved, "to": str(target)})

    def delete_item(self, idx):
        # Deleted files go to a hidden .trash folder next to the leaf folders, so they stay recoverable
        trash = ROOT.parent / ".trash"
        with LOCK:
            item = ITEMS[idx] if 0 <= idx < len(ITEMS) else None
            if item is None:
                return self.send_json({"error": "File not found"}, 404)
            if item.get("moved"):
                return self.send_json({"error": "Already moved"}, 409)
            src = (ROOT / item["path"]).resolve()
            if ROOT not in src.parents or not src.is_file():
                return self.send_json({"error": "File not found"}, 404)
            trash.mkdir(exist_ok=True)
            target = trash / src.name
            n = 1
            while target.exists():
                target = trash / f"{src.stem}_{n}{src.suffix}"
                n += 1
            shutil.move(str(src), str(target))
            item["moved"] = True
        log.info("Deleted %s -> %s", src, target)
        self.send_json({"ok": True})

    def set_folder(self, path):
        global ROOT
        ROOT = Path(path)
        save_state(folder=str(ROOT))
        log.info("Folder changed to %s", ROOT)
        self.send_json({"folder": str(ROOT), "count": scan()})

    def switch_folder(self, name):
        parent = ROOT.parent
        new = (parent / name).resolve()
        if name.startswith(".") or new.parent != parent.resolve() or not new.is_dir():
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
    global ROOT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", help="folder with your images/videos; used only when no folder was saved "
                    "from a previous run (see --reset)")
    ap.add_argument("--reset", action="store_true", help="ignore the saved folder and use FOLDER")
    args = ap.parse_args()
    saved = None if args.reset else load_state().get("folder")
    if saved and Path(saved).is_dir():
        ROOT = Path(saved).resolve()
        log.info("Using saved folder %s", ROOT)
    elif args.folder:
        ROOT = Path(args.folder).expanduser().resolve()
    else:
        raise SystemExit("No folder given and none saved. Usage: server.py /path/to/media")
    if not ROOT.is_dir():
        raise SystemExit(f"Not a folder: {ROOT}")
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
