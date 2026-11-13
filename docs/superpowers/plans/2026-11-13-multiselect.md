# Multi-select Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select several images/videos in the grid (long-press on touch, Ctrl/⌘-click on desktop) and Move or permanently Delete them in one action.

**Architecture:** The server gets batch `POST /api/move` and `POST /api/delete` that take a JSON list of ids plus the page's scan `ver`, and reject the request with 409 if the file list changed since the page loaded. `.trash` is removed; delete is `unlink`. A new `static/select.js` owns selection state, long-press, the selection bar, the delete confirmation sheet and the batch calls; `app.js` keeps rendering and the lightbox but routes its Move/Delete through the same batch calls.

**Tech Stack:** Python 3 stdlib (`http.server`, `unittest`), plain HTML/CSS/JS (no build step, no dependencies).

**Spec:** `docs/superpowers/specs/2026-11-13-multiselect-design.md`. Approved look reference (not committed): `.superpowers/brainstorm/*/content/selection-look-v2.html`.

## Global Constraints

- Python standard library only; plain HTML/CSS/JS; no frameworks, no new dependencies.
- Main target is iPhone Safari over the LAN; desktop Chrome/Safari must work too.
- Nothing outside `BASE` is ever touched; `dest` is validated with `resolve_rel`.
- Delete is permanent everywhere (`os.unlink`); no `.trash` folder is created or referenced. The cached thumbnail of a deleted file is removed too.
- Long-press: **450 ms** hold; cancelled by more than **10 px** movement, early lift, or `pointercancel`. Pressed tile sinks to `scale(.94)` dimmed.
- Ring: **3px** rotating `conic-gradient`, `#ff2d55, #ff9500, #ffd60a, #30d158, #64d2ff, #5e5ce6, #bf5af2, #ff2d55`, **2.6s** linear infinite, driven by `@property --a` (`<angle>`). The gradient variable is declared on `*, *::before, *::after`, **never on `:root`** (a `:root` variable freezes the animation).
- Only **✕ Back** (or **Esc**) leaves selection mode. Unselecting down to 0 keeps the bar open at "0 selected"; Move/Delete are disabled at 0.
- Toast copy: `Folder changed — reload to continue` (409), `Moved N file(s)`, `Deleted N file(s)`; errors/partial failures use the existing red toast, successes a neutral one.
- Delete confirmation copy: title `Delete N file(s)?`, body `They'll be deleted for good. This can't be undone.`, buttons `Cancel` (default focus) / `Delete`. Applies to the lightbox Delete as well (N = 1).
- `css`/`js` are served uncached; no cache-busting needed.
- Work happens on branch `feature/multiselect`. Stop after build and test; do not merge, push, or restart the service.

## Review Focus

Inputs the spec implies but doesn't spell out; each has a test in the owning task.

1. **Duplicate ids in one request** (`ids: [3, 3]`): the second reports `Already moved`/`File not found`, nothing crashes or double-processes. (Task 1)
2. **Malformed bodies**: invalid JSON, `ids` not a list, an empty list, string/`null`/`true` ids, missing `ver`, no `Content-Length` → 400, and the connection stays usable. (Task 1)
3. **A file vanished from disk** after the scan (deleted outside the app): that id fails with `File not found`, the rest of the batch still succeeds. (Task 1)
4. **Name clash at the destination**: both files survive (`_1` suffix), also when two selected files share a name stem. (Task 1)
5. **Selection survives a cancelled Move and a 409**: Cancel in the folder picker or a stale-`ver` rejection leaves every selected tile selected. (Task 4)

---

### Task 1: Server batch actions, scan guard, permanent delete

**Files:**
- Modify: `server.py` (`do_POST`, `move_item`, `delete_item`, new helpers)
- Modify: `tests/test_server.py` (class `Actions`, `Navigation.test_delete_trash_is_at_base`)

**Interfaces:**
- Produces: `POST /api/move` body `{"ids": [int,...], "dest": str, "ver": int}` and `POST /api/delete` body `{"ids": [int,...], "ver": int}`. Success: `200 {"results": [{"id": int, "ok": true} | {"id": int, "ok": false, "error": str}]}`. Stale `ver`: `409 {"error": "Folder changed — reload to continue"}`. Malformed request or bad `dest`: `400 {"error": str}`. Per-file error strings: `"File not found"`, `"Already moved"`, `"File is already in that folder"`.
- Produces: `Handler.read_json_body(self) -> dict` (raises `ValueError` on anything invalid), `Handler.move_items(self, body: dict)`, `Handler.delete_items(self, body: dict)`, module function `drop_thumb(item: dict) -> None`.
- The single-file `?id=` forms of both endpoints stop working.

- [ ] **Step 1: Add the test helper and rewrite the existing action tests**

In `tests/test_server.py` add a module-level `post_json(url, body, raw=None) -> (status, dict)` that POSTs `json.dumps(body)` (or the bytes `raw` when given) with `Content-Type: application/json` and returns `(status, parsed_json)`, handling `HTTPError` like `get()` does. In `Actions`, replace `post(path)` with `post(path, body)` calling `post_json(f"{self.base}{path}", body)`, add `ver(self)` returning `self.items()["ver"]`, and `ids(self, *names)` returning the ids for those names. Replace `test_delete_goes_to_trash` with `test_delete_is_permanent`: delete `a.png` via `{"ids": [id], "ver": ver}`, assert 200, `results == [{"id": id, "ok": True}]`, the file is gone, **`(self.tmp / ".trash").exists()` is False**, and `items()["total"] == 2`. Rewrite `test_move_to_sibling_folder`, `test_move_into_nested_folder` and the dest loop in `test_move_rejects_bad_destination_and_ids` to the new body (bad dests `"..", ".trash", "nope", "../x", "other/../..", "/etc"` still → 400, and nothing moved). Update `Navigation.test_delete_trash_is_at_base` into `test_delete_in_subfolder_is_permanent`: after switching to `other/deep`, deleting `z.png` removes it and creates no `.trash` anywhere.

- [ ] **Step 2: Add the new failing tests (in `Actions`)**

- `test_batch_move_mixed_kinds`: move `a.png`, `bb.png`, `clip.mp4` to `other`; all three `ok`, all three exist in `other/`, `items()["total"] == 0`.
- `test_batch_delete_several`: delete `a.png` + `clip.mp4`; both gone, `bb.png` remains.
- `test_stale_ver_is_409_and_touches_nothing`: take `ver`, call `/api/rescan`, then send the old `ver` for a delete → 409, error text `Folder changed — reload to continue`, all files still on disk.
- `test_partial_failure_reports_per_file`: ids `[valid, 9999, -1]` → 200; first `ok: True`, the others `ok: False, error: "File not found"`; the valid file was processed.
- `test_duplicate_ids_do_not_crash` (Review Focus 1): `ids: [x, x]` → 200, first ok, second `ok: False`, one file moved/deleted exactly once.
- `test_malformed_bodies_are_400` (Review Focus 2): for each of raw `b"not json"`, `{"ids": "3", "ver": v}`, `{"ids": [], "ver": v}`, `{"ids": ["3"], "ver": v}`, `{"ids": [None], "ver": v}`, `{"ids": [True], "ver": v}`, `{"ids": [id]}` (no ver), and a delete without `ids` → status 400 and the file still exists; afterwards a normal request on a **new** request still works.
- `test_vanished_file_fails_only_that_id` (Review Focus 3): `os.unlink` `a.png` behind the server's back, then batch `[a, bb]` → `a` fails `File not found`, `bb` ok.
- `test_name_clash_gets_suffix` (Review Focus 4): put `bb.png` into `other/` first, then move `bb.png` from the folder → both `other/bb.png` and `other/bb_1.png` exist. Also move two files named the same stem from different scans is not needed; one clash is enough.
- `test_move_to_current_folder_fails_per_file`: `dest` = the current folder's relative path → that id `ok: False, error: "File is already in that folder"`, file untouched.
- `test_delete_removes_cached_thumbnail`: set `server.FFMPEG` to `shutil.which("ffmpeg")` (skip the test if absent), fetch `/thumb/<id>` for an image to create the cache, assert the cache file exists (`server.thumb_path(item)`), delete it, assert the cache file is gone.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests 2>&1 | tail -30`
Expected: the new/rewritten action tests FAIL or ERROR (old query-string handlers still in place); the other test classes still pass.

- [ ] **Step 4: Implement the server changes in `server.py`**

- `drop_thumb(item)`: `thumb_path(item).unlink(missing_ok=True)`, swallowing `OSError`.
- `Handler.read_json_body(self) -> dict`: require a `Content-Length` header (int, `0 < n <= 1_000_000`), read exactly `n` bytes (always read it before replying so a keep-alive connection stays in sync), `json.loads`, require a `dict`; raise `ValueError` otherwise. Add a second small validator `parse_ids(body) -> list[int]` (module function): `ids` must be a non-empty list of real ints (reject `bool`, `None`, strings) and `ver` must be an int; raise `ValueError`.
- `do_POST`: route `/api/move` → `self.move_items(self.read_json_body())`, `/api/delete` → `self.delete_items(self.read_json_body())`; keep the existing `ValueError` → 400 and generic-exception handling, and keep the empty-path-means-top-level `parse_qs` line only if still used (remove it if unused).
- `move_items(body)`: `ids = parse_ids(body)`; `dest` must be a `str` (else `ValueError`); `dest_dir = resolve_rel(dest)` → 400 `Invalid destination folder` if `None`. Then `with LOCK:` first check `body["ver"] == SCAN_VERSION` (else 409 with the spec's text), then loop the ids reusing the existing per-file checks from today's `move_item` (in range, not `moved`, still a file inside `ROOT`, `src.parent != dest_dir`, `_1`/`_2` suffix on clash, `shutil.move`, set `item["moved"] = True`, one `log.info` per file), collecting `{"id", "ok", "error"?}` per id. A `shutil.move`/`OSError` for one file becomes that file's error, not a failed request. Log one summary line. Reply `{"results": [...]}`.
- `delete_items(body)`: same structure; per file `os.unlink(src)` then `drop_thumb(item)` then `item["moved"] = True`. Remove the `trash` variable and the `.trash` directory logic entirely.
- Delete the old `move_item` and `delete_item` methods.

- [ ] **Step 5: Run the full test suite**

Run: `python3 -m unittest discover -s tests 2>&1 | tail -15`
Expected: `OK` with all tests passing (the previous count minus the 2 replaced tests plus the new ones). Also `grep -n "trash" server.py tests/test_server.py` should show only assertions that `.trash` is **not** created.

- [ ] **Step 6: Commit** (first create the branch: `git checkout -b feature/multiselect`, then include the spec, plan and `.gitignore`)

```bash
git checkout -b feature/multiselect
git add .gitignore docs server.py tests/test_server.py
git commit -m "feat(api): batch move/delete with scan guard, permanent delete"
```

---

### Task 2: Client plumbing: batch call, confirmation sheet, lightbox on the new API

This restores a working app after Task 1 (the old single-file calls no longer exist) and delivers the confirmation sheet that Task 3 reuses.

**Files:**
- Create: `static/select.js` (only the shared helpers in this task)
- Modify: `static/index.html` (confirm sheet markup, second `<script>`)
- Modify: `static/style.css` (confirm sheet styles)
- Modify: `static/app.js` (lightbox Delete / Move; tile index)

**Interfaces:**
- Produces, as globals shared between the two classic scripts (`select.js` loads **after** `app.js`; everything is referenced only at call time):
  - `async function batch(action, items, extra = {}) -> Promise<{ok: object[], failed: {item: object, error: string}[]} | null>` where `action` is `'move' | 'delete'`, `items` are entries of `listed`, `extra` is e.g. `{dest}`. It POSTs `{ids: items.map(i => i.id), ver, ...extra}` as JSON to `/api/<action>`; on 409 or network/other error it shows the red toast (`Folder changed — reload to continue` for 409, the server's `error` otherwise) and returns `null`; on 200 it calls `dropItem(item)` for every ok result, and returns the split.
  - `function confirmDelete(items) -> Promise<boolean>`: opens the sheet, resolves `true` on Delete, `false` on Cancel / scrim click / Esc.
  - `function plural(n, one = 'file', many = 'files') -> string` (`"1 file"`, `"12 files"`).
- Modifies `toast(msg, kind = 'error')` in `app.js` so `kind === 'ok'` uses a neutral background; default stays red.
- `tile()` sets `d.dataset.index = index`, and **no longer assigns `d.onclick`** (Task 3 adds delegated click handling; until then, Task 2 keeps a temporary `d.onclick = () => openLb(index)` which Task 3 removes).

- [ ] **Step 1: Write `select.js` helpers and the sheet markup/styles**

`index.html`: before `</body>` add `<script src="/static/select.js"></script>` after the existing `app.js` tag; add `<div id="confirm" class="scrim" hidden>` containing the sheet: `#cstack` (thumbnail stack), `<h3 id="ctitle">`, the body line, and `#ccancel` / `#cok` buttons. `style.css`: sheet styles from the approved mockup (`.scrim` blurred backdrop, bottom-sheet on phones with `max-width: 700px`, centered on desktop, fanned thumbnails with alternating ±6° / 5° rotation, red `#cok`, safe-area bottom padding). Sheet thumbnails use `/thumb/<id>?v=<ver>` for up to 4 items, plus `+N`.

- [ ] **Step 2: Implement `batch`, `confirmDelete`, `plural` in `select.js`**

As specified under Interfaces. `confirmDelete` sets the title with `plural(items.length)`, focuses `#ccancel`, and removes its own listeners when it resolves so repeated use doesn't stack handlers.

- [ ] **Step 3: Switch the lightbox to the batch API in `app.js`**

`$('delbtn').onclick`: `const it = listed[cur]`; `if (!it || !await confirmDelete([it])) return`; `const r = await batch('delete', [it])`; if `r?.ok.length` → `closeLb()` and `toast('Deleted 1 file', 'ok')`; if `r?.failed.length` → red toast with its error. `$('mvbtn').onclick`: inside the `pickFolder` callback call `batch('move', [it], {dest})`; on `null` keep the picker open; on `ok` → `closeMv(); closeLb(); toast('Moved 1 file', 'ok')`; on a failed result show its error and keep the picker open. Make `Esc` while the sheet is open close only the sheet (the existing keydown handler must not also close the lightbox). Remove the now-dead direct `fetch('/api/delete?id=…')` / `/api/move?…` code.

- [ ] **Step 4: Verify in the browser**

Run: `python3 server.py <a throwaway folder with a few copied images>` (any free port is fine by editing nothing: use the default 8765 if free), open the page, open an image in the lightbox, press **Delete** → sheet shows "Delete 1 file?" with the thumbnail; **Cancel** keeps the file; confirm deletes it from disk, with no `.trash` created. **Move** → pick a folder → the file lands there. Expected: both work, no console errors.

- [ ] **Step 5: Commit**

```bash
git add static/
git commit -m "feat(ui): confirm sheet and batch calls; lightbox uses batch API"
```

---

### Task 3: Selection mode: state, gestures, bar, ring

**Files:**
- Modify: `static/select.js` (selection logic)
- Modify: `static/app.js` (remove temporary tile click, auto-scroll hook)
- Modify: `static/index.html` (selection bar markup)
- Modify: `static/style.css` (ring, bar, press feedback, selection lock)

**Interfaces:**
- Consumes: `batch`, `confirmDelete`, `plural`, `toast`, `listed`, `pickFolder`, `closeMv`, `openLb`, `dropItem` from Task 2 / `app.js`.
- Produces: `const Sel = { active: boolean, size: number, has(item), toggle(item), clear() }` where `active` is true from the first selected tile until Back/Esc. `app.js`'s `autoStep` returns early (pauses, re-syncing `autoPos = scrollY`) when `Sel.active`. State lives as a `Set` of `listed` entries; a selected tile has class `sel`; `<body>` gets class `selecting` while `Sel.active`.

- [ ] **Step 1: Selection bar markup and header swap**

`index.html`: add `<div id="selbar" hidden>` as a sibling right after `<header>`: `#selback` (`✕`), `#selcount` ("N selected"), `#selmove` ("Move"), `#seldel` ("Delete"). CSS: `body.selecting header { display: none }`, `body.selecting #selbar { display: flex }`; `#selbar` is `position: sticky; top: 0; z-index: 10` with the header's safe-area padding, blurred translucent background (`rgba(20,21,23,.86)` + `backdrop-filter: blur(16px)`) and a 1px bottom border painted with the rainbow gradient (`border-image`), `#seldel` red (`#e03131`), disabled buttons at `opacity: .35` with `pointer-events: none`.

- [ ] **Step 2: The ring and press feedback in `style.css`**

`@property --a { syntax: '<angle>'; inherits: true; initial-value: 0deg; }`, `@keyframes spin { to { --a: 360deg } }`, the gradient declared as `*, *::before, *::after { --rainbow: conic-gradient(from var(--a), …) }` with the Global Constraints colours. `.tile::before` = the 3px masked ring (`mask-composite: exclude` plus the `-webkit-mask-composite: xor` pair), `opacity: 0; transform: scale(1.08)`, transitions of .2s/.25s; `.tile.sel::before { opacity: 1; transform: none; animation: spin 2.6s linear infinite }`; `.tile.sel:hover` has no blue outline; `.tile.pressing img { transform: scale(.94); filter: brightness(.8) }`. Add `user-select: none; -webkit-user-select: none; -webkit-touch-callout: none` on `.tile` and its `img`/`video`. The existing reduced-motion rule stays.

- [ ] **Step 3: Gesture and toggle logic in `select.js`**

Event delegation on `#grid` (resolve the tile with `e.target.closest('.tile')`, the item with `listed[+tile.dataset.index]`):
- `pointerdown` (primary button/touch only, not while `Sel.active`): start a 450 ms timer, add `.pressing`, remember the start point and tile. On fire: remove `.pressing`, call `toggle(item)`, set a `swallowClick` flag, `navigator.vibrate?.(12)`.
- `pointermove` > 10 px, `pointerup`, `pointercancel`, `pointerleave`: cancel the press and remove `.pressing`.
- `click`: if `swallowClick`, clear it and stop. Else if `Sel.active` → `toggle(item)`; else if `e.metaKey || e.ctrlKey` → `toggle(item)`; else `openLb(index)`. In all selecting cases `preventDefault`.
- `contextmenu` on the grid: always `preventDefault`; if `e.ctrlKey` (macOS Ctrl-click) and the target is a tile and the same physical click did not already toggle via `click`, toggle once (guard with a short flag so one click never toggles twice).
- `dragstart` prevented on tiles.
- `addEventListener('keydown')`: `Escape` while `Sel.active` and no sheet/folder picker/lightbox open → `Sel.clear()`.
- `toggle(item)`: add/remove in the set, set `.sel` on `item.el`, set `Sel.active = true` and `body.selecting`, update `#selcount` (`"N selected"`) and disable `#selmove`/`#seldel` at 0. Removing the last tile does **not** clear `Sel.active`.
- `Sel.clear()`: clear the set, remove `.sel` from every tile, `Sel.active = false`, remove `body.selecting`.
- Remove the temporary `d.onclick` from `tile()` in `app.js`; add `|| Sel.active` to the `autoStep` early-return condition.

- [ ] **Step 4: Bar actions in `select.js`**

`#selback` → `Sel.clear()`. `#seldel` → `confirmDelete([...set])`; on `true` → `batch('delete', items)`; on a result: toast `Deleted N files` (ok kind) for the ok count, and if any failed show the red toast `N failed: <first error>`; then deselect the ok items, keep the failed ones selected, and if none remain selected call `Sel.clear()`. `#selmove` → `pickFolder('Move ' + plural(n), 'Move here', '', dest => …)` calling `batch('move', items, {dest})`, same result handling, `closeMv()` after a result with at least one ok, leaving the picker open on `null`. Cancel in the picker leaves everything selected (no extra code: Cancel already only calls `closeMv`).

- [ ] **Step 5: Verify manually in the browser**

Run the server on a throwaway folder; Ctrl-click a tile → ring spins, header becomes the bar showing `1 selected`; click two more → `3 selected`; click one off → `2 selected`; click it back; unselect everything → bar stays at `0 selected` with Move/Delete disabled; Esc → header returns. Move opens the picker titled "Move 3 files"; Cancel leaves the 3 selected; choosing a folder moves them, tiles vanish, success toast shows.

- [ ] **Step 6: Commit**

```bash
git add static/
git commit -m "feat(ui): multi-select with long-press, ctrl-click, rainbow ring and bar"
```

---

### Task 4: Automated browser checks

**Files:**
- Create: `browser_check.cjs` in the session scratchpad directory, **not** in the repo and not committed (the project has no Node tooling and no test dependencies).

**Interfaces:**
- Consumes: the running app from Tasks 1-3, a throwaway media folder, `playwright-core` plus the cached `chromium_headless_shell` already on this machine (see how it was launched earlier in the session: `chromium.launch({executablePath: …/chrome-headless-shell})`).

- [ ] **Step 1: Write the script and run it against a throwaway folder**

Start `server.py` on a copy of ~8 sample images + 1 mp4 in a temp dir (never the real library). Scripted assertions, each printed PASS/FAIL:
1. **Ctrl-click** a tile → body has `selecting`, tile has `sel`, the bar says `1 selected`, the lightbox is closed.
2. **Ring spins**: the tile's `::before` `background-image` string differs between two samples taken 700 ms apart.
3. **Plain click** on a second tile adds it (`2 selected`); clicking a selected tile removes only it.
4. **Unselect to zero**: bar still visible with `0 selected` and Move/Delete disabled.
5. **Long-press**: `mouse.down` on a tile, wait 600 ms, `mouse.up` → selected and the lightbox did **not** open.
6. **Press cancelled by movement**: `mouse.down`, move 30 px, wait 600 ms, `up` → not selected.
7. **Press cancelled by early release** (200 ms) → not selected, the lightbox opens (normal tap).
8. **Esc** clears; header returns.
9. **Delete flow**: select 2, Delete → sheet says `Delete 2 files?`; **Cancel** keeps both selected (Review Focus 5); confirm → both files gone from disk, no `.trash`, tiles removed, count updated.
10. **Move flow**: select 2, Move, Cancel in the picker → still selected (Review Focus 5); choose a destination → files land there.
11. **Stale `ver`** (Review Focus 5): select 2 tiles, call `/api/rescan` from outside, press Delete → confirm → red toast `Folder changed — reload to continue`, nothing deleted, tiles still selected.
12. Selecting a **video** tile by long-press works and does not start playback controls or text selection.
13. No `pageerror` / console errors during the whole run.

- [ ] **Step 2: Fix anything that fails**

Fix in `static/` or `server.py`, re-run the scratch script and `python3 -m unittest discover -s tests` until all pass. Commit fixes with a descriptive `fix(...)` message each.

- [ ] **Step 3: Report**

Output the PASS/FAIL list verbatim in the final report. Say plainly that the iPhone long-press feel (scroll never selects, no callout menu, sink animation) could only be checked by simulation, and needs Rafael's manual check on the real device.

---

### Task 5: Documentation with mermaid diagrams

**Files:**
- Modify: `README.md`
- Modify: `SESSION.md`

- [ ] **Step 1: Update `README.md`**

In **Features**: replace "Delete (to `FOLDER/.trash`)" with permanent delete wording; add a multi-select entry (long-press / Ctrl-⌘-click; Move, Delete, Back; one confirmation for deletes). Add a short **Multi-select** section with the spec's `stateDiagram-v2` (Browsing / Pressing / Selecting / ConfirmDelete / FolderPicker). Mention in a warning line that **deleting is permanent** and that an old `.trash` folder from earlier versions is no longer used and can be removed by hand. Add `static/select.js` to the **Files** list.

- [ ] **Step 2: Update `SESSION.md`**

API lines: `POST /api/move` and `/api/delete` now take JSON `{ids, dest?, ver}` and return per-file `results`; 409 on stale `ver`; remove every `.trash` mention. Layout: add `static/select.js`. Add a `sequenceDiagram` of the batch request (page → server: lock, `ver` check, per-file loop, results) and a `flowchart` of the files. Under **Known limitations**: rewrite the id-shifting note (the `ver` guard now prevents wrong-file actions; ids still shift on rescan) and add "delete is permanent, no undo". Remove the line about the old `feature-multiselect` branch (deleted).

- [ ] **Step 3: Verify the diagrams render**

Run: `grep -c '```mermaid' README.md SESSION.md` (expect ≥1 and ≥2) and check each diagram's syntax by pasting it through the mermaid CLI if available (`npx -y @mermaid-js/mermaid-cli@11 -i … -o /dev/null`); if it can't be installed offline, state that the diagrams were checked by eye only.

- [ ] **Step 4: Final checks and commit**

Run: `python3 -m unittest discover -s tests 2>&1 | tail -5` → `OK`; `git status --short` clean apart from the commit.

```bash
git add README.md SESSION.md
git commit -m "docs: multi-select, permanent delete, mermaid diagrams"
```

Then stop: report the branch name, the commits, the test results from Tasks 1 and 4, and what needs Rafael's manual iPhone check. Do not merge, push or restart the service.

---

## Self-review notes

- **Spec coverage:** goals (selection, both gestures, Move/Delete/Back, ring, permanent delete) → Tasks 2-3; anti-goals (lost selection, wrong-file delete, long-press side effects, scroll-start) → `ver` guard (T1), Esc/Back-only clearing and `swallowClick`/touch-callout rules (T3), cancel-by-move (T3, T4 #6); scan guard + per-file results + thumb cleanup + single-file form removed → T1; confirmation for lightbox too → T2; Mac Ctrl-click → T3 `contextmenu`; auto-scroll pause → T3; docs with mermaid → T5; branch deletion already done.
- **Consistency:** `batch`/`confirmDelete`/`plural`/`Sel` names and the `{results}` shape are used identically in Tasks 1-3. `ver` is the existing global from `app.js`.
- **Ordering caveat:** between Task 1 and Task 2 the UI's Move/Delete are broken on the branch (single-file endpoints removed); Task 2 restores them. Nothing is deployed from the branch.
