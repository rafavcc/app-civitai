# Civit-RV

Civitai-style tile gallery for local images and videos, tuned for iPhone (Safari). Python 3 standard library only (ffmpeg is optional, for thumbnails); the UI is plain HTML/CSS/JS.

```
python3 server.py /path/to/your/media [--reset]
```

The server listens on `0.0.0.0:8765` (constants at the top of `server.py`) and prints its LAN address, e.g. http://192.168.0.33:8765. Use Safari on iPhone: Chrome/Brave can't play HEVC video.

## Features
- Infinite scroll grid (4 columns, 2px gaps on mobile); landscape media spans 2 columns
- Thumbnails and video posters generated with ffmpeg and cached in `.cache/` (falls back to originals without ffmpeg)
- Videos autoplay muted while on screen and load their source only near the viewport
- Lightbox with arrow keys / Esc, Back, Delete (to `../.trash`) and Move
- Search, kind filter, sort, random order, auto-scroll; your choices are remembered per device
- Folder picker (sibling folders); the last folder is remembered across restarts
- Dark theme, iOS meta tags, safe-area padding for the notch / Dynamic Island

## Remembered folder
The last folder is saved in `state.json`. `FOLDER` on the command line is only used when nothing is saved; pass `--reset` to force it.

## Tests
```
python3 -m unittest discover -s tests
```

## Files
`server.py` (server), `static/index.html`, `static/style.css`, `static/app.js` (UI), `tests/test_server.py`.
