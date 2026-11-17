# Speed and UX improvements: design

Date: 2026-11-16. Status: approved by Rafael in conversation (decisions 1-3 accepted, "apply all other fixes").

## Why

The gallery is used mostly from an iPhone over the LAN. The library is mostly video (`ai`: 177 of 205 files are mp4, median 12 MB, p90 295 MB, max 1.4 GB). Today the grid streams original videos and the browser re-downloads everything after every restart, rescan or hour. Goal: the grid feels instant on the phone, and four small UX requests land.

## Goals

1. **Cache-stable links.** Thumbnail, preview and media links are keyed on the file (`k = <mtime>-<size>`), not on the scan version, so unchanged files keep the same link across restarts and rescans. Keyed responses are cached for a year (`immutable`).
2. **Revalidation.** Every served file sends `Last-Modified`, and `If-Modified-Since` gets a `304` (this also covers css/js, which stay `no-cache`).
3. **Preview clips.** Each video gets a short silent clip, made once with ffmpeg: about **6 s at 480 px wide**, H.264, no audio. The grid plays the clip; the lightbox still plays the original.
   - Stored **outside the media folder**, in a cache folder on the Linux disk: `~/.cache/civit-rv/previews` (honours `XDG_CACHE_HOME`).
   - **Tracked by last use**: each file's mtime is its last-used stamp, refreshed when served (at most once a day per file). A sweep deletes clips **not used for 30 days**, at startup and then every 24 h. A clip that is used again has its timer reset.
   - If the clip can't be made, the server falls back to the original video.
   - Measured: one clip from an 863 MB source took 0.35 s and is 72 KB.
4. **Smaller wins.** Show the thumbnail instantly in the lightbox while the full image loads. Preload the next and previous lightbox image. Generate missing thumbnails and clips for the current folder in the background right after a scan. Remember thumbnails or clips that failed (per file version, in memory) so ffmpeg doesn't retry them on every view.
5. **Muted lightbox videos.** Videos always open muted; the controls can unmute.
6. **Column toggle.** One button in the second header row cycles 4 → 5 → 6 → 4 columns. Remembered per device with the other preferences. Works on phone and desktop.
7. **Speed slider.** Seconds per grid row: left 10 s, middle 4.5 s, right 2/1.3 ≈ 1.54 s, geometric within each half (left half 10 → 4.5, right half 4.5 → 1.54).
8. **Rename.** A Rename button in the lightbox opens a sheet with the name (without extension) pre-filled.
   - The extension can't be changed.
   - Refused if another entry in the same folder has the new name, ignoring upper/lower case (the library lives on NTFS via `/mnt/c`). A case-only change of the file's own name is allowed.
   - Refused if empty, if it contains `/ \ : * ? " < > |` or control characters, if it starts with `.`, or if the full name exceeds 255 bytes.
   - Same name → no-op success. The tile's label and the lightbox info update in place; the cached thumbnail and clip are carried over to the new name instead of regenerated.
   - Errors show inside the sheet, which stays open.

9. **Report.** A small **Report** button in the second header row opens a read-only sheet (approved by Rafael 2026-11-16) showing:
   - Thumbnails and preview clips: file count, total size, oldest last use (days ago, against the 30-day sweep).
   - Clips: videos in the current folder that have no clip yet, and thumbnails/clips that failed in this process.
   - The old project `.cache/thumbs` folder size while it still exists (a reminder to delete it).
   - This folder: image and video counts and the originals' total size.
   - This page (from the browser's own timing data): load time, bytes downloaded, files served from the browser cache vs. the network. Shows "n/a" where the browser doesn't report it.
   - No "clear cache" action: the sweep already cleans up.

## Non-goals

- Re-encoding originals, HLS/adaptive streaming, HTTP/2.
- Select-all / bulk rename.
- A persistent index of failed files (in-memory is enough; a restart retries once).

## Anti-goals

- Writing anything inside the media folder (clips, indexes, temp files).
- A rename that silently overwrites, or merges with, another file.
- Any change that weakens the scan-version guard: rename uses it like move/delete (409 on a stale `ver`) and goes through the same CSRF checks (JSON content type, same Origin).
- Hurting the live service during development: build in the worktree, never run `server.py`'s `main()` on 8765.

## Approaches considered

- **Preview clips:** (a) on-demand + background warm, cached on disk, last-use sweep (chosen); (b) generate all at startup (slow first start, wasted work on folders never opened); (c) serve originals with smaller `preload` (doesn't fix bandwidth).
- **Cache keys:** (a) `k = mtime-size` in the URL with `immutable` (chosen); (b) ETag on content hash (needs hashing GB-sized files); (c) keep `ver` (busts the cache on every restart: the current problem).
- **Usage tracking:** (a) file mtime as last-used stamp (chosen: no extra state, survives restarts); (b) a JSON index (another file to keep consistent).

## Calls I made (Rafael can override)

- Thumbnails move to the same cache root (`~/.cache/civit-rv/thumbs`) and get the same 30-day last-use sweep. Today they sit inside the iCloud-synced project folder (`.cache/thumbs`, 2,449 files). The old folder is left in place for Rafael to delete.
- Rename returns 400 for name problems and conflicts, 409 only for a stale `ver`, matching move/delete.
- Clip encode: `-ss 0.1 -t 6 -an -vf scale='min(480,iw)':-2,fps=24 -c:v libx264 -preset veryfast -crf 30 -pix_fmt yuv420p -movflags +faststart`. Max 2 concurrent clip encodes (separate from the 4 thumbnail slots).
- Background warm processes the current folder newest-first, one file at a time, and stops when the folder changes.
