// Shared helpers for batch actions (move/delete) and the delete confirmation sheet.
// Classic script loaded after app.js; uses its globals (listed, ver, dropItem, toast) at call time.
function plural(n, one = 'file', many = 'files') {
  return `${n} ${n === 1 ? one : many}`;
}

async function batch(action, items, extra = {}) {
  let res, data;
  try {
    res = await fetch('/api/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: items.map(i => i.id), ver, ...extra }),
    });
    data = await res.json();
  } catch (e) {
    toast('Network error');
    return null;
  }
  if (res.status === 409) { toast('Folder changed — reload to continue'); return null; }
  if (!res.ok || !Array.isArray(data.results)) { toast(data.error || 'Request failed'); return null; }
  const byId = new Map(items.map(i => [i.id, i]));
  const ok = [], failed = [];
  for (const r of data.results) {
    const item = byId.get(r.id);
    if (!item) continue;
    if (r.ok) { dropItem(item); ok.push(item); }
    else failed.push({ item, error: r.error });
  }
  return { ok, failed };
}

let closeConfirm = null;
function confirmDelete(items) {
  closeConfirm?.(false);  // a sheet that is still open ends first, so its Esc listener doesn't leak
  return new Promise(resolve => {
    const box = $('confirm');
    $('ctitle').textContent = `Delete ${plural(items.length)}?`;
    const stack = $('cstack');
    stack.innerHTML = '';
    for (const it of items.slice(0, 4)) {
      const im = document.createElement('img');
      im.src = url('thumb', it);
      stack.appendChild(im);
    }
    if (items.length > 4) {
      const m = document.createElement('div');
      m.className = 'more';
      m.textContent = '+' + (items.length - 4);
      stack.appendChild(m);
    }
    const finish = v => {
      box.classList.remove('open');
      removeEventListener('keydown', onKey, true);
      box.removeEventListener('click', onScrim);
      $('ccancel').onclick = $('cok').onclick = null;
      closeConfirm = null;
      resolve(v);
    };
    // Capture phase so Esc closes only the sheet, not the lightbox underneath
    const onKey = e => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      finish(false);
    };
    const onScrim = e => { if (e.target === box) finish(false); };
    closeConfirm = finish;
    addEventListener('keydown', onKey, true);
    box.addEventListener('click', onScrim);
    $('ccancel').onclick = () => finish(false);
    $('cok').onclick = () => finish(true);
    box.classList.add('open');
    $('ccancel').focus();
  });
}

// Rename sheet for the lightbox item. The server takes the stem and keeps the extension.
let closeRename = null;
function openRename(it) {
  closeRename?.();
  const box = $('rename'), input = $('rnname'), ok = $('rnok');
  const dot = it.name.lastIndexOf('.');
  const ext = dot > 0 ? it.name.slice(dot) : '';
  input.value = it.name.slice(0, it.name.length - ext.length);
  $('rnext').textContent = ext;
  $('rnerr').textContent = '';
  ok.disabled = false;
  const finish = () => {
    box.classList.remove('open');
    removeEventListener('keydown', onKey, true);
    box.removeEventListener('click', onScrim);
    input.onkeydown = $('rncancel').onclick = ok.onclick = null;
    closeRename = null;
  };
  // Capture phase so Esc closes only the sheet, not the lightbox underneath
  const onKey = e => {
    if (e.key !== 'Escape') return;
    e.stopPropagation();
    finish();
  };
  const onScrim = e => { if (e.target === box) finish(); };
  const submit = async () => {
    if (ok.disabled) return;
    ok.disabled = true;
    let res, data;
    try {
      res = await fetch('/api/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: it.id, ver, name: input.value }),
      });
      data = await res.json();
    } catch (e) {
      if (closeRename === finish) { $('rnerr').textContent = 'Network error'; ok.disabled = false; }
      return;
    }
    if (closeRename !== finish) return;  // sheet was closed meanwhile
    if (res.status === 409) { finish(); toast('Folder changed — reload to continue'); return; }
    if (!res.ok || !data.ok) { $('rnerr').textContent = data.error || 'Request failed'; ok.disabled = false; return; }
    it.name = data.name;
    it.path = data.path;
    const label = it.el?.querySelector('.label');
    if (label) label.textContent = it.path;
    if (listed[cur] === it) $('lbinfo').textContent = `${it.path} · ${(it.size / 1048576).toFixed(2)} MB · ${new Date(it.mtime * 1000).toLocaleString()}`;
    finish();
    toast('Renamed to ' + it.name, 'ok');
  };
  closeRename = finish;
  addEventListener('keydown', onKey, true);
  box.addEventListener('click', onScrim);
  input.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); submit(); } };
  $('rncancel').onclick = finish;
  ok.onclick = submit;
  box.classList.add('open');
  input.focus();
  input.select();
}
$('renbtn').onclick = () => { const it = listed[cur]; if (it) openRename(it); };

// Share / Download for the lightbox item. The original is prefetched as a Blob in the background so the click can call
// navigator.share synchronously (Safari drops the user activation after any await). One prefetch at a time.
const SHARE_MAX = 30 * 1024 * 1024;
let share = null;  // { it, ctrl, file } for the current lightbox item
function shareUi(mode, text) {
  const sb = $('sharebtn'), dl = $('dlbtn');
  sb.hidden = mode !== 'share';
  sb.disabled = mode === 'share' && text !== 'Share';
  if (mode === 'share') sb.textContent = text;
  dl.hidden = mode !== 'download' && mode !== 'large';
  if (mode === 'large') { sb.hidden = false; sb.disabled = true; sb.textContent = 'Too large to share'; }
}
function cancelShare() {
  share?.ctrl?.abort();
  share = null;
  $('sharebtn').hidden = true; $('sharebtn').disabled = true;
  $('dlbtn').hidden = true;
}
function prepareShare(it) {
  cancelShare();
  $('dlbtn').href = url('media', it);
  $('dlbtn').download = it.name;
  if (it.size > SHARE_MAX) return shareUi('large');
  if (!isSecureContext || typeof navigator.share !== 'function' || typeof navigator.canShare !== 'function') return shareUi('download');
  const mine = share = { it, ctrl: new AbortController(), file: null };
  shareUi('share', 'Preparing…');
  (async () => {
    try {
      const r = await fetch(url('media', it), { signal: mine.ctrl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const blob = await r.blob();
      if (share !== mine) return;
      const file = new File([blob], it.name, { type: r.headers.get('Content-Type') || blob.type });
      if (!navigator.canShare({ files: [file] })) return shareUi('download');
      mine.file = file;
      shareUi('share', 'Share');
    } catch (e) {
      if (share === mine && e.name !== 'AbortError') shareUi('download');
    }
  })();
}
$('sharebtn').onclick = () => {
  const file = share?.file;
  if (!file) return;
  navigator.share({ files: [file] }).catch(e => { if (e.name !== 'AbortError') toast("Couldn't share"); });
};

// Report sheet: read-only cache, folder and page statistics
function fmtBytes(n) {
  if (!Number.isFinite(n)) return 'n/a';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return Math.round(n / 1024) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(1) + ' GB';
}
function pageStats() {
  const nav = performance.getEntriesByType?.('navigation')?.[0];
  const entries = [...(performance.getEntriesByType?.('resource') || []), ...(nav ? [nav] : [])];
  const load = nav && nav.loadEventEnd > 0 ? ((nav.loadEventEnd - nav.startTime) / 1000).toFixed(2) + ' s' : 'n/a';
  if (!entries.length || !entries.some(e => 'transferSize' in e)) return { load, bytes: 'n/a', cached: 'n/a', network: 'n/a' };
  let bytes = 0, cached = 0, network = 0;
  for (const e of entries) {
    bytes += e.transferSize || 0;
    if (e.transferSize === 0 && e.decodedBodySize > 0) cached++; else network++;
  }
  return { load, bytes: fmtBytes(bytes), cached, network };
}
function reportHtml(r) {
  const age = c => c.oldest_use == null ? 'n/a'
    : Math.max(0, Math.floor((Date.now() / 1000 - c.oldest_use) / 86400)) + ` days ago (deleted after ${r.max_age_days})`;
  const cache = (title, c) => section(title, c ? [['Files', c.files], ['Size', fmtBytes(c.bytes)], ['Oldest use', age(c)]] : [['Files', 'none']]);
  const p = pageStats(), f = r.folder;
  return cache('Thumbnails', r.thumbs) + cache('Preview clips', r.previews) +
    section('Clips', [['Videos without a clip', f.videos_without_clip], ['Failed thumbnails', r.failed.thumb], ['Failed clips', r.failed.preview]]) +
    (r.legacy_thumbs ? section('Old thumbs folder', [['Files', r.legacy_thumbs.files], ['Size', fmtBytes(r.legacy_thumbs.bytes)], ['Note', 'You can delete .cache/thumbs']]) : '') +
    section('This folder', [['Images', f.images], ['Videos', f.videos], ['Originals', fmtBytes(f.bytes)]]) +
    section('This page', [['Load time', p.load], ['Downloaded', p.bytes], ['From browser cache', p.cached], ['From network', p.network]]);
}
function section(title, rows) {
  const esc = v => String(v).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  return `<section class="rpsec"><h4>${esc(title)}</h4>` + rows.map(([k, v]) => `<div class="rprow"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('') + '</section>';
}
let closeReport = null;
function openReport() {
  closeReport?.();
  const box = $('reportsheet'), body = $('rpbody');
  body.textContent = 'Loading…';
  const finish = () => {
    box.classList.remove('open');
    removeEventListener('keydown', onKey, true);
    box.removeEventListener('click', onScrim);
    $('rpclose').onclick = null;
    closeReport = null;
  };
  // Capture phase so Esc closes only the sheet, not the lightbox underneath
  const onKey = e => {
    if (e.key !== 'Escape') return;
    e.stopPropagation();
    finish();
  };
  const onScrim = e => { if (e.target === box) finish(); };
  closeReport = finish;
  addEventListener('keydown', onKey, true);
  box.addEventListener('click', onScrim);
  $('rpclose').onclick = finish;
  box.classList.add('open');
  fetch('/api/report', { cache: 'no-store' }).then(res => { if (!res.ok) throw 0; return res.json(); }).then(r => {
    if (closeReport === finish) body.innerHTML = reportHtml(r);
  }).catch(() => { if (closeReport === finish) body.textContent = "Couldn't load the report"; });
}
$('report').onclick = openReport;

// Selection mode: long-press / Ctrl-click starts it, then plain clicks toggle. State is a Set of `listed` entries.
const picked = new Set();
const Sel = {
  active: false,
  get size() { return picked.size; },
  has: item => picked.has(item),
  toggle(item) {
    if (!item || item.moved) return;
    if (picked.has(item)) picked.delete(item); else picked.add(item);
    item.el.classList.toggle('sel', picked.has(item));
    Sel.active = true;
    document.body.classList.add('selecting');
    $('selcount').textContent = picked.size + ' selected';
    $('selmove').disabled = $('seldel').disabled = !picked.size;
  },
  clear() {
    for (const it of picked) it.el.classList.remove('sel');
    picked.clear();
    Sel.active = false;
    document.body.classList.remove('selecting');
    $('selcount').textContent = '0 selected';
  },
};

const grid = $('grid');
const tileItem = e => {
  const t = e.target.closest?.('.tile');
  return t ? { t, item: listed[+t.dataset.index] } : null;
};
let pressTimer = 0, pressTile = null, pressX = 0, pressY = 0, swallowClick = false, gestureDone = false;

function cancelPress() {
  clearTimeout(pressTimer);
  pressTile?.classList.remove('pressing');
  pressTile = null;
}
grid.addEventListener('pointerdown', e => {
  swallowClick = false;
  gestureDone = false;
  if (Sel.active || e.button !== 0 || !e.isPrimary) return;
  const hit = tileItem(e);
  if (!hit) return;
  cancelPress();
  pressTile = hit.t; pressX = e.clientX; pressY = e.clientY;
  hit.t.classList.add('pressing');
  pressTimer = setTimeout(() => {
    cancelPress();
    swallowClick = true;
    gestureDone = true;
    Sel.toggle(hit.item);
    navigator.vibrate?.(12);
  }, 450);
});
grid.addEventListener('pointermove', e => {
  if (pressTile && Math.hypot(e.clientX - pressX, e.clientY - pressY) > 10) cancelPress();
});
for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) grid.addEventListener(ev, cancelPress);

// One physical click can raise both click and contextmenu (Ctrl-click): the first one toggles,
// the rest of the same gesture (reset on pointerdown) is ignored
function tapToggle(item) {
  if (gestureDone) return;
  gestureDone = true;
  Sel.toggle(item);
}
grid.addEventListener('click', e => {
  const hit = tileItem(e);
  if (!hit) return;
  if (swallowClick) { swallowClick = false; return; }
  if (Sel.active || e.metaKey || e.ctrlKey) { e.preventDefault(); tapToggle(hit.item); }
  else openLb(+hit.t.dataset.index);
});
grid.addEventListener('contextmenu', e => {
  e.preventDefault();
  const hit = tileItem(e);
  if (!hit || !e.ctrlKey) return;
  cancelPress();  // otherwise the long-press timer would toggle this tile a second time
  if (!swallowClick) tapToggle(hit.item);
});
grid.addEventListener('dragstart', e => { if (tileItem(e)) e.preventDefault(); });
addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !Sel.active) return;
  if (cur >= 0 || $('mv').classList.contains('open') || $('confirm').classList.contains('open') || $('reportsheet').classList.contains('open')) return;
  Sel.clear();
});

// A fresh listing invalidates the selection
const resetListing = reset;
reset = function () { Sel.clear(); return resetListing.apply(this, arguments); };

$('selback').onclick = Sel.clear;

// Report a batch outcome: drop finished items from the selection, keep failures selected
function finishBatch(r, doneMsg) {
  if (!r) return false;
  for (const it of r.ok) { picked.delete(it); }
  if (r.ok.length) toast(doneMsg(r.ok.length), 'ok');
  if (r.failed.length) toast(`${r.failed.length} failed: ${r.failed[0].error}`);
  if (picked.size) refreshBar();
  else Sel.clear();
  return r.ok.length > 0;
}
function refreshBar() {
  $('selcount').textContent = picked.size + ' selected';
  $('selmove').disabled = $('seldel').disabled = !picked.size;
}

$('seldel').onclick = async () => {
  const items = [...picked];
  if (!items.length || !await confirmDelete(items)) return;
  finishBatch(await batch('delete', items), n => `Deleted ${plural(n)}`);
};
$('selmove').onclick = () => {
  const items = [...picked];
  if (!items.length) return;
  pickFolder('Move ' + plural(items.length), 'Move here', '', async dest => {
    const r = await batch('move', items, { dest });
    if (finishBatch(r, n => `Moved ${plural(n)}`)) closeMv();
  });
};
