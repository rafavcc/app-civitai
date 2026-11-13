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

function confirmDelete(items) {
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
      resolve(v);
    };
    // Capture phase so Esc closes only the sheet, not the lightbox underneath
    const onKey = e => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      finish(false);
    };
    const onScrim = e => { if (e.target === box) finish(false); };
    addEventListener('keydown', onKey, true);
    box.addEventListener('click', onScrim);
    $('ccancel').onclick = () => finish(false);
    $('cok').onclick = () => finish(true);
    box.classList.add('open');
    $('ccancel').focus();
  });
}
