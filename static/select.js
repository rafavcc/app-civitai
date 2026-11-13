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
      im.src = '/thumb/' + it.id + '?v=' + ver;
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
  if (cur >= 0 || $('mv').classList.contains('open') || $('confirm').classList.contains('open')) return;
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
