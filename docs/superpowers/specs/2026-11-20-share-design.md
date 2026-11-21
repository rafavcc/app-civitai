# Share button design

Date: 2026-11-20. Status: approved (decisions below are binding).

## Why
The gallery is used from iPhone, iPad, Windows and Mac. Getting a gif, mp4 or image out of it into Messages, Mail or another app currently means saving it by hand. The OS share sheet does this in one tap, but browsers only offer file sharing (`navigator.share({files})`) on secure contexts, and the app is plain HTTP on the LAN.

## Goals
- A **Share** button in the lightbox that shares the current file, unchanged, through the OS share sheet.
- A **Download** fallback wherever sharing is not possible (plain-HTTP page, Firefox, old browsers) and for files over 30 MB.
- The LAN address `http://<LAN IP>:8765` keeps working exactly as today.

## Non-goals
Multi-file share from selection mode; video to GIF conversion; public share links; changing iCloud behaviour.

## Anti-goals
- No certificates, TLS code or new dependencies in the app; no build step.
- No conversion or re-encoding: always the original bytes.
- No blocking of the lightbox while a file is being fetched.
- Nothing in the app may depend on the tailnet existing (without it the button is simply Download).

## Approaches considered
**HTTPS**
- `tailscale serve` in WSL (chosen): proxies `https://w11-wsl-ts.<tailnet>.ts.net` to `http://localhost:8765`. Real certificate, nothing to install on devices, app untouched. Cost: Host may be rewritten, so the CSRF origin guard needs `X-Forwarded-Host`; devices must be on Tailscale.
- Self-signed certificate: rejected, every device must trust a profile and Safari still warns.
- mkcert: rejected, same per-device trust-profile problem, plus a CA to protect.

**Getting the File to `navigator.share`**
- Fetch on tap: simplest, but Safari requires the share call within transient activation; awaiting a download first loses it and the call is rejected (`NotAllowedError`).
- Prefetch on open (chosen): when the lightbox opens an item of at most 30 MB, fetch the original as a Blob in the background, so the click handler calls `navigator.share` synchronously with a ready File. Cost: one extra download per lightbox view and bounded memory (one file at a time), hence the size cap.

## Decisions (as approved)
1. HTTPS via `tailscale serve` in WSL; the app stays plain HTTP on 0.0.0.0:8765.
2. Share = Web Share Level 2, `navigator.share({files: [File]})`, offered only when `window.isSecureContext` and `navigator.canShare?.({files: [file]})` is true; otherwise the button is **Download** (`<a download>` of `/media/<id>?k=`).
3. Items of at most `30 * 1024 * 1024` bytes (item `size`) are prefetched on lightbox open (real name, MIME type from the response Content-Type). The button reads "Preparing…" and is disabled until ready. Navigating aborts the prefetch (`AbortController`) and drops the previous Blob; one prefetched file at a time. The click handler calls `navigator.share` synchronously.
4. Larger items: "Too large to share" (disabled) plus a Download link.
5. Always the original. Cancelling the sheet (`AbortError`) shows nothing; other errors toast "Couldn't share".
6. Download is shown where sharing is unsupported and for files over the limit.
7. CSRF guard in `read_json_body`: accept when `urlparse(Origin).netloc` equals `Host` or `X-Forwarded-Host`; JSON content-type rule unchanged. A cross-site browser request cannot add custom headers without a preflight this server never approves.
8. Online-only iCloud files: the prefetch goes through `/media` unchanged; the client never blocks the UI waiting.
9. README gets a setup guide for the manual Tailscale steps (not run by agents): enable MagicDNS and HTTPS certificates in the admin console, `sudo tailscale serve --bg 8765` in WSL (syntax checked against `tailscale serve --help` on 1.104.7), open the https URL with Tailscale connected, turn off with `tailscale serve reset`.

## Left to the builder
- Markup and CSS for the buttons (reuse the lightbox bar styles), exact element ids, where the share code lives (`static/select.js` next to the Rename sheet is the natural place; `openLb`/`closeLb` in `app.js` call into it).
- Whether support is detected before the File exists. The plan's default: a cheap pre-check (`isSecureContext`, `navigator.share` and `navigator.canShare` are functions), then the real `canShare` check once the File is built; if it fails, the button becomes Download.
- What the button does when the prefetch fails (404 stale key, 503 downloading, network error). The plan's default: fall back to Download, no retry loop.
- Keeping the file-handling logic small and testable; no new files unless it clearly helps.
