# Civit-RV

Civitai-style tile gallery for local images and videos. Optimized for iPhone 15 Pro with iOS meta tags, touch-friendly UI, CLS prevention, and dark theme. Features infinite scroll, hover-to-play videos, keyboard lightbox (arrow keys / Esc), search, sort and filter. Pure vanilla JS/CSS frontend served by Python HTTP server.

## Installation
No external dependencies required.

```
python3 server.py /path/to/your/media
```

## Usage

```bash
python3 server.py /path/to/your/media
```

The server runs on the host that holds the media (WSL, 192.168.0.33) and listens on `0.0.0.0:8765`, so open http://192.168.0.33:8765 from any LAN machine. The port (8765) and host are fixed constants at the top of `server.py`. Use "Rescan" after adding files.

## Features
- **Infinite scroll** with automatic pagination
- **Hover-to-play videos** for quick preview
- **Keyboard lightbox** navigation (arrow keys / Esc)
- **Search** across filenames and metadata
- **Sort and filter** by type, size, date
- **Dark theme** with iOS-optimized UI
- **No external dependencies** - pure vanilla JS/CSS
- **Mobile optimized** for iPhone 15 Pro with touch-friendly controls
