#!/usr/bin/env python3
"""Local Civitai-style gallery for images and videos. No dependencies.

Usage: python3 server.py /path/to/media   (listens on 0.0.0.0:8765)
"""
import argparse
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
STATIC_DIR = Path(__file__).parent / "static"
HOST = "0.0.0.0"
PORT = 8765

ROOT: Path = Path(".")
ITEMS: list[dict] = []
LOCK = threading.Lock()
SCAN_VERSION = 0
log = logging.getLogger("gallery")


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
        except (BrokenPipeError, ConnectionResetError):
            return
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
            item = ITEMS[idx]
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
            item = ITEMS[idx]
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
        with LOCK:
            item = ITEMS[idx]
        if item.get("moved"):
            return self.send_json({"error": "moved"}, 404)
        full = (ROOT / item["path"]).resolve()
        if ROOT not in full.parents:
            return self.send_json({"error": "forbidden"}, 403)
        self.send_file(full, cache=True)

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
    ap.add_argument("folder", help="folder containing your images/videos")
    args = ap.parse_args()
    ROOT = Path(args.folder).expanduser().resolve()
    if not ROOT.is_dir():
        raise SystemExit(f"Not a folder: {ROOT}")
    t = time.perf_counter()
    n = scan()
    log.info("Indexed %d files in %.1fs", n, time.perf_counter() - t)
    log.info("Open http://%s:%d", display_host(HOST), PORT)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
