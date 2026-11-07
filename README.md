# Local Gallery

Civitai-style tile gallery for local images and videos. Infinite scroll, hover-to-play videos, lightbox (arrow keys / Esc), search, sort and filter. Python 3 standard library only.

```
python3 server.py /path/to/your/media
```

The server runs on the host that holds the media (WSL, 192.168.0.33) and listens on 0.0.0.0, so open http://192.168.0.33:8765 from any LAN machine. The port (8765) and host are fixed constants at the top of `server.py`. Use "Rescan" after adding files.
