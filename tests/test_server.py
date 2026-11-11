import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def start():
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def get(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def jget(url, **kw):
    status, body, _ = get(url, **kw)
    return status, json.loads(body)


class GalleryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.root = self.tmp / "main"
        self.root.mkdir()
        (self.tmp / "other").mkdir()
        (self.root / "a.png").write_bytes(PNG)
        (self.root / "bb.png").write_bytes(PNG * 2)
        (self.root / "clip.mp4").write_bytes(b"x" * 1000)
        (self.root / "sub").mkdir()
        (self.root / "sub" / "c.png").write_bytes(PNG)
        (self.root / ".hidden.png").write_bytes(PNG)
        (self.root / "notes.txt").write_text("ignored")
        os.utime(self.root / "a.png", (1000, 1000))
        os.utime(self.root / "bb.png", (2000, 2000))
        server.BASE = self.tmp
        server.ROOT = self.root
        # Keep tests away from the real state file and thumbnail cache
        self._saved = (server.STATE_FILE, server.CACHE_DIR, server.FFMPEG)
        server.STATE_FILE = self.tmp / "state.json"
        server.CACHE_DIR = self.tmp / "cache"
        server.scan()
        self.srv, self.base = start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        server.STATE_FILE, server.CACHE_DIR, server.FFMPEG = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def items(self, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return jget(f"{self.base}/api/items?{qs}")[1]


class ScanAndItems(GalleryTestCase):
    def test_scan_is_not_recursive_and_skips_hidden_and_non_media(self):
        names = {i["name"] for i in self.items()["items"]}
        self.assertEqual(names, {"a.png", "bb.png", "clip.mp4"})

    def test_pagination(self):
        r = self.items(sort="name", limit=2, offset=0)
        self.assertEqual(r["total"], 3)
        self.assertEqual(len(r["items"]), 2)
        r2 = self.items(sort="name", limit=2, offset=2)
        self.assertEqual(len(r2["items"]), 1)
        self.assertFalse({i["id"] for i in r["items"]} & {i["id"] for i in r2["items"]})

    def test_sorting(self):
        by_name = [i["path"] for i in self.items(sort="name")["items"]]
        self.assertEqual(by_name, sorted(by_name, key=str.lower))
        newest = self.items(sort="newest")["items"]
        self.assertTrue(all(newest[i]["mtime"] >= newest[i + 1]["mtime"] for i in range(len(newest) - 1)))
        oldest = self.items(sort="oldest")["items"]
        self.assertEqual(oldest[0]["name"], "a.png")
        largest = self.items(sort="size")["items"]
        self.assertEqual(largest[0]["name"], "clip.mp4")

    def test_kind_filter_and_search(self):
        self.assertEqual(self.items(kind="video")["total"], 1)
        self.assertEqual(self.items(kind="image")["total"], 2)
        self.assertEqual([i["name"] for i in self.items(q="BB")["items"]], ["bb.png"])

    def test_random_order_is_stable_per_seed(self):
        a = [i["id"] for i in self.items(sort="random", seed="s1")["items"]]
        b = [i["id"] for i in self.items(sort="random", seed="s1")["items"]]
        self.assertEqual(a, b)


class MediaServing(GalleryTestCase):
    def first_id(self, name):
        return next(i["id"] for i in self.items()["items"] if i["name"] == name)

    def test_full_and_range(self):
        mid = self.first_id("clip.mp4")
        status, body, h = get(f"{self.base}/media/{mid}")
        self.assertEqual((status, len(body)), (200, 1000))
        status, body, h = get(f"{self.base}/media/{mid}", {"Range": "bytes=10-19"})
        self.assertEqual((status, len(body), h["Content-Range"]), (206, 10, "bytes 10-19/1000"))
        status, body, h = get(f"{self.base}/media/{mid}", {"Range": "bytes=-5"})
        self.assertEqual((status, len(body)), (206, 5))
        status, _, _ = get(f"{self.base}/media/{mid}", {"Range": "bytes=5000-"})
        self.assertEqual(status, 416)

    def test_invalid_ids(self):
        for bad in ("9999", "-1", "abc"):
            status, _, _ = get(f"{self.base}/media/{bad}")
            self.assertIn(status, (400, 404), bad)

    def test_path_traversal_rejected(self):
        secret = self.tmp / "secret.png"
        secret.write_bytes(PNG)
        with server.LOCK:
            server.ITEMS.append({"path": "../secret.png", "name": "x", "kind": "image", "mtime": 0, "size": 1})
            idx = len(server.ITEMS) - 1
        for route in ("media", "thumb"):
            status, _, _ = get(f"{self.base}/{route}/{idx}")
            self.assertEqual(status, 403, route)
        status, _, _ = get(f"{self.base}/static/../server.py")
        self.assertIn(status, (403, 404))

    def test_thumb_falls_back_to_original_without_ffmpeg(self):
        server.FFMPEG = None
        status, body, _ = get(f"{self.base}/thumb/{self.first_id('a.png')}")
        self.assertEqual((status, body), (200, PNG))
        status, _, _ = get(f"{self.base}/thumb/{self.first_id('clip.mp4')}")
        self.assertEqual(status, 404)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_thumb_generated_and_cached(self):
        server.FFMPEG = shutil.which("ffmpeg")
        img = self.tmp / "big.jpg"
        subprocess.run([server.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=red:s=1600x900",
                        "-frames:v", "1", str(img)], check=True)
        shutil.copy(img, self.root / "big.jpg")
        server.scan()
        tid = self.first_id("big.jpg")
        status, body, h = get(f"{self.base}/thumb/{tid}")
        self.assertEqual((status, h["Content-Type"]), (200, "image/jpeg"))
        self.assertLess(len(body), (self.root / "big.jpg").stat().st_size + 1)
        self.assertEqual(len(list(server.CACHE_DIR.rglob("*.jpg"))), 1)
        get(f"{self.base}/thumb/{tid}")
        self.assertEqual(len(list(server.CACHE_DIR.rglob("*.jpg"))), 1)


    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_video_poster(self):
        server.FFMPEG = shutil.which("ffmpeg")
        subprocess.run([server.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:d=1",
                        "-pix_fmt", "yuv420p", str(self.root / "real.mp4")], check=True)
        server.scan()
        status, body, h = get(f"{self.base}/thumb/{self.first_id('real.mp4')}")
        self.assertEqual((status, h["Content-Type"]), (200, "image/jpeg"))
        self.assertTrue(body.startswith(b"\xff\xd8"))


class Actions(GalleryTestCase):
    def post(self, path):
        return jget(f"{self.base}{path}", method="POST")

    def id_of(self, name):
        return next(i["id"] for i in self.items()["items"] if i["name"] == name)

    def test_delete_goes_to_trash(self):
        status, r = self.post(f"/api/delete?id={self.id_of('a.png')}")
        self.assertEqual(status, 200)
        self.assertFalse((self.root / "a.png").exists())
        self.assertTrue((self.tmp / ".trash" / "a.png").exists())
        self.assertEqual(self.items()["total"], 2)

    def test_move_to_sibling_folder(self):
        status, r = self.post(f"/api/move?id={self.id_of('bb.png')}&dest=other")
        self.assertEqual(status, 200)
        self.assertTrue((self.tmp / "other" / "bb.png").exists())

    def test_move_into_nested_folder(self):
        (self.tmp / "other" / "deep").mkdir()
        status, _ = self.post(f"/api/move?id={self.id_of('bb.png')}&dest=other/deep")
        self.assertEqual(status, 200)
        self.assertTrue((self.tmp / "other" / "deep" / "bb.png").exists())

    def test_move_rejects_bad_destination_and_ids(self):
        mid = self.id_of("bb.png")
        for dest in ("..", ".trash", "nope", "../x", "other/../..", "/etc"):
            status, _ = self.post(f"/api/move?id={mid}&dest={dest}")
            self.assertEqual(status, 400, dest)
        self.assertEqual(self.post("/api/delete?id=-1")[0], 404)
        self.assertEqual(self.post("/api/delete?id=999")[0], 404)

    def test_switch_folder_persists_state(self):
        (self.tmp / "other" / "z.png").write_bytes(PNG)
        status, r = jget(f"{self.base}/api/folder?path=other")
        self.assertEqual((status, r["count"], r["path"]), (200, 1, "other"))
        self.assertEqual(server.load_state()["folder"], str(self.tmp / "other"))
        status, r = jget(f"{self.base}/api/folder?path=")
        self.assertEqual((status, r["path"]), (200, ""))


class Navigation(GalleryTestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / "other" / "deep").mkdir()
        (self.tmp / "other" / "deep" / "z.png").write_bytes(PNG)
        (self.tmp / "other" / ".secret").mkdir()
        outside = Path(tempfile.mkdtemp()).resolve()
        self.outside = outside
        (self.tmp / "escape").symlink_to(outside, target_is_directory=True)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.outside, ignore_errors=True)

    def folders(self, path=None):
        qs = "" if path is None else "?path=" + path
        return jget(f"{self.base}/api/folders{qs}")

    def test_lists_subfolders_with_counts_and_crumbs(self):
        status, r = self.folders("")
        self.assertEqual(status, 200)
        by = {f["name"]: f for f in r["folders"]}
        self.assertEqual(by["main"]["subdirs"], 1)
        self.assertEqual(by["main"]["files"], 3)
        self.assertEqual(by["other"]["subdirs"], 1)
        self.assertEqual(r["crumbs"], [{"name": self.tmp.name, "path": ""}])
        self.assertEqual(r["current"], "main")

    def test_nested_path_and_breadcrumbs(self):
        status, r = self.folders("other")
        self.assertEqual([f["path"] for f in r["folders"]], ["other/deep"])  # hidden folder excluded
        self.assertEqual([c["path"] for c in r["crumbs"]], ["", "other"])
        status, r = self.folders("other/deep")
        self.assertEqual((r["folders"], r["path"]), ([], "other/deep"))

    def test_default_is_parent_of_current_folder(self):
        status, r = self.folders()
        self.assertEqual(r["path"], "")  # current is main, whose parent is the base
        jget(f"{self.base}/api/folder?path=other/deep")
        status, r = self.folders()
        self.assertEqual(r["path"], "other")

    def test_empty_path_means_top_level_even_from_a_nested_folder(self):
        # Regression: "?path=" used to be dropped and fall back to the current folder's parent
        jget(f"{self.base}/api/folder?path=other/deep")
        status, r = self.folders("")
        self.assertEqual((status, r["path"]), (200, ""))
        self.assertIn("other", [f["name"] for f in r["folders"]])

    def test_open_nested_folder_and_back_to_root(self):
        status, r = jget(f"{self.base}/api/folder?path=other/deep")
        self.assertEqual((status, r["count"], r["path"]), (200, 1, "other/deep"))
        self.assertEqual([i["name"] for i in self.items()["items"]], ["z.png"])
        self.assertEqual(jget(f"{self.base}/api/info")[1]["path"], "other/deep")
        status, r = jget(f"{self.base}/api/folder?path=")
        self.assertEqual((status, r["path"]), (200, ""))

    def test_cannot_leave_base(self):
        for bad in ("..", "../x", "other/../..", "/etc", "%2Fetc", ".trash", "other/.secret", "escape", "nope"):
            self.assertEqual(self.folders(bad)[0], 400, bad)
            self.assertEqual(jget(f"{self.base}/api/folder?path={bad}")[0], 400, bad)
        self.assertEqual(server.ROOT, self.root)  # a rejected request must not change the folder

    def test_delete_trash_is_at_base(self):
        jget(f"{self.base}/api/folder?path=other/deep")
        item = self.items()["items"][0]
        self.assertEqual(jget(f"{self.base}/api/delete?id={item['id']}", method="POST")[0], 200)
        self.assertTrue((self.tmp / ".trash" / "z.png").exists())



class State(unittest.TestCase):
    def test_save_and_load(self):
        old = server.STATE_FILE
        with tempfile.TemporaryDirectory() as d:
            server.STATE_FILE = Path(d) / "s.json"
            self.assertEqual(server.load_state(), {})
            server.save_state(folder="/x")
            server.save_state(other=1)
            self.assertEqual(server.load_state(), {"folder": "/x", "other": 1})
            server.STATE_FILE.write_text("not json")
            self.assertEqual(server.load_state(), {})
        server.STATE_FILE = old


if __name__ == "__main__":
    unittest.main()
