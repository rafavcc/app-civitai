# Session summary: Local Gallery (Civitai-style local media browser)

## Goal
A local app that shows my own images/videos as small tiles like civitai.com, loading more as I scroll.

## Current state
Working app in this folder (Python 3 stdlib only; Node is not installed on this machine, Python 3.15 is).

- `server.py`: HTTP server (`ThreadingHTTPServer`). Scans a folder recursively for images (jpg, jpeg, png, gif, webp, avif, bmp) and videos (mp4, webm, mov, m4v, mkv). Skips dotfiles/dot-folders.
  - `GET /` serves `static/index.html`
  - `GET /api/items?offset&limit&sort(newest|oldest|name|size)&kind(all|image|video)&q` returns paginated items (`id`, `path`, `name`, `kind`, `mtime`, `size`)
  - `GET /media/<id>` serves the file with HTTP Range support (video seeking) and a path-traversal check
  - `GET /api/rescan` re-indexes the folder
  - `GET /api/info` returns the current folder
  - `GET /api/choose` opens a native macOS folder dialog (`osascript`) and switches folder
  - `GET /api/folder?path=` switches the folder
- `static/index.html`: single-file UI (vanilla JS)
  - fixed 4-column grid, infinite scroll via `IntersectionObserver` on a sentinel (40 items per page)
  - videos autoplay muted and looping while on screen and pause when off screen
  - lightbox (click a tile; arrow keys navigate, Esc closes)
  - header: search, kind filter, sort, **📁 Folder** button (native picker, falls back to a `prompt()` for the path), Rescan, item count
- `README.md`: usage.

## Run
```
python3 server.py /path/to/media [--port 8765] [--host 127.0.0.1]
```
Open http://127.0.0.1:8765

## Decisions / history
1. User first asked whether such an app exists. I suggested Immich, PhotoPrism, Stash, Stable Diffusion WebUI Infinite Image Browsing, etc., then built a custom one.
2. Videos originally played only on hover. The user asked for autoplay, so they now autoplay when visible.
3. The grid was changed from auto-fill to exactly 4 columns.
4. Added a header button to change the loaded folder.
5. Server runs on the WSL host and listens on a hardcoded 0.0.0.0:8765 (prints its LAN IP). Used from Safari: Brave/Chrome can't play HEVC videos.
6. Mobile: 2px gaps at <=700px; videos load their source only near the viewport (fixes paging stalling after 10 rows); landscape media spans 2 columns (client-side detection, experimental).
7. Tested via curl: items API, range requests, traversal rejected (400), folder switching, invalid folder error. Not tested in a real browser.

## Known limitations / ideas
- Tiles load full-size originals (no thumbnails); large images may be slow. Idea: generate/cache thumbnails (Pillow, `sips`, or ffmpeg for video posters).
- Sorting is by file mtime only.
- Idea: read prompt/metadata from AI-generated PNGs (A1111/ComfyUI) and show it in the lightbox.
- Idea: favorites, tags, delete/move, filesystem watching instead of manual Rescan.
- Folder is not persisted across server restarts (CLI argument required).

## To continue in another workspace
Copy this folder (`server.py`, `static/index.html`, `README.md`, this file) and tell the assistant: "Continue from SESSION.md".
