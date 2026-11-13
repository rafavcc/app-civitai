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


def post_json(url, body, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


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
    def post(self, path, body):
        return post_json(f"{self.base}{path}", body)

    def id_of(self, name):
        return next(i["id"] for i in self.items()["items"] if i["name"] == name)

    def ids(self, *names):
        return [self.id_of(n) for n in names]

    def ver(self):
        return self.items()["ver"]

    def test_delete_is_permanent(self):
        aid = self.id_of("a.png")
        status, r = self.post("/api/delete", {"ids": [aid], "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertEqual(r["results"], [{"id": aid, "ok": True}])
        self.assertFalse((self.root / "a.png").exists())
        self.assertFalse((self.tmp / ".trash").exists())
        self.assertEqual(self.items()["total"], 2)

    def test_move_to_sibling_folder(self):
        status, r = self.post("/api/move", {"ids": self.ids("bb.png"), "dest": "other", "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertTrue(r["results"][0]["ok"])
        self.assertTrue((self.tmp / "other" / "bb.png").exists())

    def test_move_into_nested_folder(self):
        (self.tmp / "other" / "deep").mkdir()
        status, _ = self.post("/api/move", {"ids": self.ids("bb.png"), "dest": "other/deep", "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertTrue((self.tmp / "other" / "deep" / "bb.png").exists())

    def test_move_rejects_bad_destination_and_ids(self):
        mid, ver = self.id_of("bb.png"), self.ver()
        for dest in ("..", ".trash", "nope", "../x", "other/../..", "/etc"):
            status, _ = self.post("/api/move", {"ids": [mid], "dest": dest, "ver": ver})
            self.assertEqual(status, 400, dest)
        self.assertTrue((self.root / "bb.png").exists())
        self.assertEqual(list((self.tmp / "other").iterdir()), [])
        # Unknown ids are reported per file, not as a failed request
        status, r = self.post("/api/delete", {"ids": [-1, 999], "ver": ver})
        self.assertEqual(status, 200)
        self.assertEqual([x["ok"] for x in r["results"]], [False, False])

    def test_batch_move_mixed_kinds(self):
        ids = self.ids("a.png", "bb.png", "clip.mp4")
        status, r = self.post("/api/move", {"ids": ids, "dest": "other", "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertEqual([x["ok"] for x in r["results"]], [True, True, True])
        for n in ("a.png", "bb.png", "clip.mp4"):
            self.assertTrue((self.tmp / "other" / n).exists(), n)
        self.assertEqual(self.items()["total"], 0)

    def test_batch_delete_several(self):
        status, r = self.post("/api/delete", {"ids": self.ids("a.png", "clip.mp4"), "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertFalse((self.root / "a.png").exists())
        self.assertFalse((self.root / "clip.mp4").exists())
        self.assertTrue((self.root / "bb.png").exists())

    def test_stale_ver_is_409_and_touches_nothing(self):
        old = self.ver()
        jget(f"{self.base}/api/rescan")
        ids = self.ids("a.png", "bb.png", "clip.mp4")
        status, r = self.post("/api/delete", {"ids": ids, "ver": old})
        self.assertEqual(status, 409)
        self.assertEqual(r["error"], "Folder changed — reload to continue")
        for n in ("a.png", "bb.png", "clip.mp4"):
            self.assertTrue((self.root / n).exists(), n)

    def test_partial_failure_reports_per_file(self):
        aid = self.id_of("a.png")
        status, r = self.post("/api/delete", {"ids": [aid, 9999, -1], "ver": self.ver()})
        self.assertEqual(status, 200)
        res = r["results"]
        self.assertEqual(res[0], {"id": aid, "ok": True})
        for x in res[1:]:
            self.assertEqual((x["ok"], x["error"]), (False, "File not found"))
        self.assertFalse((self.root / "a.png").exists())

    def test_duplicate_ids_do_not_crash(self):
        aid = self.id_of("a.png")
        status, r = self.post("/api/move", {"ids": [aid, aid], "dest": "other", "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertTrue(r["results"][0]["ok"])
        self.assertFalse(r["results"][1]["ok"])
        self.assertEqual([p.name for p in (self.tmp / "other").iterdir()], ["a.png"])
        status, r = self.post("/api/delete", {"ids": [self.ids("bb.png")[0]] * 2, "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertEqual([x["ok"] for x in r["results"]], [True, False])

    def test_malformed_bodies_are_400(self):
        v, aid = self.ver(), self.id_of("a.png")
        url = f"{self.base}/api/delete"
        bodies = [{"ids": "3", "ver": v}, {"ids": [], "ver": v}, {"ids": ["3"], "ver": v},
                  {"ids": [None], "ver": v}, {"ids": [True], "ver": v}, {"ids": [aid]}, {"ver": v}]
        self.assertEqual(post_json(url, None, raw=b"not json")[0], 400)
        for b in bodies:
            self.assertEqual(post_json(url, b)[0], 400, b)
        self.assertTrue((self.root / "a.png").exists())
        self.assertEqual(post_json(url, {"ids": [aid], "ver": v})[0], 200)

    def test_vanished_file_fails_only_that_id(self):
        aid, bid = self.ids("a.png", "bb.png")
        os.unlink(self.root / "a.png")
        status, r = self.post("/api/delete", {"ids": [aid, bid], "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertEqual((r["results"][0]["ok"], r["results"][0]["error"]), (False, "File not found"))
        self.assertTrue(r["results"][1]["ok"])

    def test_name_clash_gets_suffix(self):
        (self.tmp / "other" / "bb.png").write_bytes(PNG)
        status, r = self.post("/api/move", {"ids": self.ids("bb.png"), "dest": "other", "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertTrue((self.tmp / "other" / "bb.png").exists())
        self.assertTrue((self.tmp / "other" / "bb_1.png").exists())

    def test_move_to_current_folder_fails_per_file(self):
        aid = self.id_of("a.png")
        status, r = self.post("/api/move", {"ids": [aid], "dest": "main", "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertEqual((r["results"][0]["ok"], r["results"][0]["error"]),
                         (False, "File is already in that folder"))
        self.assertTrue((self.root / "a.png").exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_delete_removes_cached_thumbnail(self):
        server.FFMPEG = shutil.which("ffmpeg")
        img = self.tmp / "big.jpg"
        subprocess.run([server.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=red:s=1600x900",
                        "-frames:v", "1", str(img)], check=True)
        shutil.copy(img, self.root / "big.jpg")
        server.scan()
        tid = self.id_of("big.jpg")
        get(f"{self.base}/thumb/{tid}")
        cache = server.thumb_path(server.ITEMS[tid])
        self.assertTrue(cache.exists())
        status, _ = self.post("/api/delete", {"ids": [tid], "ver": self.ver()})
        self.assertEqual(status, 200)
        self.assertFalse(cache.exists())

    def test_switch_folder_persists_state(self):
        (self.tmp / "other" / "z.png").write_bytes(PNG)
        status, r = jget(f"{self.base}/api/folder?path=other")
        self.assertEqual((status, r["count"], r["path"]), (200, 1, "other"))
        self.assertEqual(server.load_state()["folder"], str(self.tmp / "other"))
        status, r = jget(f"{self.base}/api/folder?path=")
        self.assertEqual((status, r["path"]), (200, ""))


class Hardening(GalleryTestCase):
    """Guards that keep a stale or foreign request from deleting the wrong file."""

    def post(self, path, body):
        return post_json(f"{self.base}{path}", body)

    def id_of(self, name):
        return next(i["id"] for i in self.items()["items"] if i["name"] == name)

    def ver(self):
        return self.items()["ver"]

    def raw_post(self, path, data, headers):
        req = urllib.request.Request(f"{self.base}{path}", data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_scan_version_is_unique_per_process(self):
        code = "import server; print(server.SCAN_VERSION)"
        root = str(Path(__file__).resolve().parent.parent)
        vals = [int(subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                                   text=True, check=True).stdout) for _ in range(2)]
        for v in vals:
            self.assertGreater(v, 10**12)
            self.assertLess(v, 2**53)
        self.assertNotEqual(vals[0], vals[1])

    def test_folder_switch_never_pairs_new_root_with_old_items(self):
        (self.tmp / "other" / "a.png").write_bytes(PNG * 3)  # same name as an item of the old folder
        aid, old = self.id_of("a.png"), self.ver()
        entered, release = threading.Event(), threading.Event()
        real = os.scandir
        target = str(self.tmp / "other")

        def slow(path):
            if str(path) == target:
                entered.set()
                release.wait(5)
            return real(path)

        server.os.scandir = slow
        try:
            t = threading.Thread(target=jget, args=(f"{self.base}/api/folder?path=other",))
            t.start()
            self.assertTrue(entered.wait(5))
            status, _ = self.post("/api/delete", {"ids": [aid], "ver": old})
            release.set()
            t.join(5)
        finally:
            server.os.scandir = real
            release.set()
        self.assertIn(status, (200, 409))
        self.assertEqual((self.tmp / "other" / "a.png").read_bytes(), PNG * 3)

    def test_text_plain_body_is_415_and_touches_nothing(self):
        body = json.dumps({"ids": [self.id_of("a.png")], "ver": self.ver()}).encode()
        for ct in ("text/plain", "application/x-www-form-urlencoded"):
            status, r = self.raw_post("/api/delete", body, {"Content-Type": ct})
            self.assertEqual((status, r["error"]), (415, "Unsupported media type"), ct)
        self.assertTrue((self.root / "a.png").exists())
        status, _ = self.raw_post("/api/delete", body, {"Content-Type": "Application/JSON; charset=utf-8"})
        self.assertEqual(status, 200)

    def test_foreign_origin_is_403_and_same_origin_works(self):
        body = json.dumps({"ids": [self.id_of("a.png")], "ver": self.ver()}).encode()
        h = {"Content-Type": "application/json"}
        status, _ = self.raw_post("/api/delete", body, {**h, "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertTrue((self.root / "a.png").exists())
        status, _ = self.raw_post("/api/delete", body, {**h, "Origin": self.base})
        self.assertEqual(status, 200)
        self.assertFalse((self.root / "a.png").exists())

    def test_symlink_is_refused_and_target_survives(self):
        os.symlink(self.root / "a.png", self.root / "link.png")
        server.scan()
        lid, v = self.id_of("link.png"), self.ver()
        for path, extra in (("/api/delete", {}), ("/api/move", {"dest": "other"})):
            status, r = self.post(path, {"ids": [lid], "ver": v, **extra})
            self.assertEqual(status, 200)
            self.assertEqual((r["results"][0]["ok"], r["results"][0]["error"]),
                             (False, "Symlinks are not supported"), path)
        self.assertTrue((self.root / "a.png").exists())
        self.assertTrue((self.root / "link.png").is_symlink())

    def test_oversize_body_closes_the_connection(self):
        import socket
        with socket.create_connection(self.srv.server_address, timeout=2) as s:
            s.sendall(b"POST /api/delete HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Content-Length: 2000000\r\n\r\n")
            data = b""
            try:
                while chunk := s.recv(4096):
                    data += chunk
            except socket.timeout:
                self.fail("connection left open with an unread body")
        self.assertIn(b"400", data.split(b"\r\n")[0])


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

    def test_delete_in_subfolder_is_permanent(self):
        jget(f"{self.base}/api/folder?path=other/deep")
        data = self.items()
        status, _ = post_json(f"{self.base}/api/delete", {"ids": [data["items"][0]["id"]], "ver": data["ver"]})
        self.assertEqual(status, 200)
        self.assertFalse((self.tmp / "other" / "deep" / "z.png").exists())
        self.assertEqual(list(self.tmp.rglob(".trash")), [])



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
