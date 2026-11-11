const PAGE = 40;
const $ = id => document.getElementById(id);
let seed = Math.random().toString(36).slice(2), ver = 0;
let offset = 0, total = 0, loading = false, done = false, gen = 0, listed = [];

async function loadMore() {
  if (loading || done) return;
  loading = true;
  const g = gen;
  const p = new URLSearchParams({ offset, limit: PAGE, sort: $('rand').checked ? 'random' : $('sort').value, seed, kind: $('kind').value, q: $('q').value });
  let r;
  try {
    r = await (await fetch('/api/items?' + p)).json();
  } catch (e) {
    loading = false;
    setTimeout(loadMore, 1000);
    return;
  }
  if (g !== gen) { loading = false; return; }
  total = r.total;
  ver = r.ver;
  for (const it of r.items) { listed.push(it); $('grid').appendChild(tile(it, listed.length - 1)); }
  offset += r.items.length;
  done = offset >= total;
  $('count').textContent = `${offset} / ${total}`;
  $('status').textContent = total === 0 ? 'No files found.' : done ? 'End.' : '';
  loading = false;
  if (!done && $('sentinel').getBoundingClientRect().top < innerHeight + 600) loadMore();
}

// Only videos near the viewport hold a source, so they don't exhaust the browser's connection limit
const playObs = new IntersectionObserver(es => {
  for (const e of es) {
    const v = e.target;
    if (e.isIntersecting) {
      if (!v.getAttribute('src')) v.src = v.dataset.src;
      v.play().catch(() => {});
    } else {
      v.pause();
      if (v.getAttribute('src')) { v.removeAttribute('src'); v.load(); }
    }
  }
}, { rootMargin: '300px' });

function markWide(d, w, h) {
  if (w && h && w / h > 1.15) d.classList.add('wide');
}

function tile(it, index) {
  const d = document.createElement('div');
  d.className = 'tile';
  const src = '/media/' + it.id + '?v=' + ver;
  const thumb = '/thumb/' + it.id + '?v=' + ver;
  if (it.kind === 'image') {
    d.innerHTML = `<img loading="lazy" decoding="async" src="${thumb}" width="${it.width || '600'}" height="${it.height || '450'}">`;
    const im = d.firstChild;
    im.onload = () => markWide(d, im.naturalWidth, im.naturalHeight);
  } else {
    d.innerHTML = `<video muted loop playsinline preload="metadata" poster="${thumb}" data-src="${src}#t=0.1"></video>`;
    const v = d.firstChild;
    v.autoplay = true;
    v.onloadedmetadata = () => markWide(d, v.videoWidth, v.videoHeight);
    playObs.observe(v);
  }
  const l = document.createElement('div');
  l.className = 'label';
  l.textContent = it.path;
  d.appendChild(l);
  d.onclick = () => openLb(index);
  it.el = d;
  return d;
}

function reset() {
  gen++;
  seed = Math.random().toString(36).slice(2);
  closeLb();
  for (const v of $('grid').querySelectorAll('video')) { v.removeAttribute('src'); v.load(); }
  playObs.disconnect(); offset = 0; done = false; loading = false; listed = [];
  $('grid').innerHTML = '';
  scrollTo(0, 0);
  loadMore();
}

new IntersectionObserver(es => { if (es[0].isIntersecting) loadMore(); }, { rootMargin: '800px' }).observe($('sentinel'));

let t;
$('q').oninput = () => { clearTimeout(t); t = setTimeout(reset, 250); };
$('kind').onchange = $('sort').onchange = $('rand').onchange = reset;
fetch('/api/info').then(r => r.json()).then(r => { document.title = 'Civit-RV - ' + r.folder.split('/').pop(); });
$('rescan').onclick = async () => { await fetch('/api/rescan'); reset(); };

let cur = -1;
function openLb(i, dir = 1) {
  const it = listed[i];
  if (!it) return;
  if (it.moved) {
    const n = i + dir;
    return n >= 0 && n < listed.length ? openLb(n, dir) : closeLb();
  }
  cur = i;
  const src = '/media/' + it.id + '?v=' + ver;
  $('lbmedia').innerHTML = it.kind === 'image' ? `<img src="${src}">` : `<video src="${src}" controls autoplay loop></video>`;
  $('lbinfo').textContent = `${it.path} · ${(it.size / 1048576).toFixed(2)} MB · ${new Date(it.mtime * 1000).toLocaleString()}`;
  $('lb').classList.add('open');
  if (i >= listed.length - 3) loadMore();
}
function closeLb() { $('lb').classList.remove('open'); $('lbmedia').innerHTML = ''; cur = -1; }
$('backbtn').onclick = closeLb;
$('lb').onclick = e => { if (e.target === $('lb')) closeLb(); };
addEventListener('keydown', e => {
  if (cur < 0) return;
  if ($('mv').classList.contains('open')) { if (e.key === 'Escape') closeMv(); return; }
  if (e.key === 'Escape') closeLb();
  if (e.key === 'ArrowRight') openLb(Math.min(cur + 1, listed.length - 1), 1);
  if (e.key === 'ArrowLeft') openLb(Math.max(cur - 1, 0), -1);
});

const RED = new Set(['hen', 'ai', 'big', 'nn', 'hot']);
function dropItem(it) {
  it.moved = true;
  it.el.remove();
  offset--; total--;
  $('count').textContent = `${offset} / ${total}`;
}

let toastTimer;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.style.display = 'none', 3000);
}

// Shared folder-grid overlay used by both Move and the Folder button
async function pickFolder(title, onPick) {
  let r;
  try { r = await (await fetch('/api/folders')).json(); } catch (e) { return toast('Could not load folders'); }
  $('mvtitle').textContent = title;
  const grid = $('mvgrid');
  grid.innerHTML = '';
  for (const name of r.folders) {
    const b = document.createElement('button');
    b.className = 'fbtn' + (name === r.current ? ' cur' : '') + (RED.has(name.toLowerCase()) ? ' red' : '');
    b.textContent = name === r.current ? name + ' (current)' : name;
    b.onclick = () => onPick(name);
    grid.appendChild(b);
  }
  $('mv').classList.add('open');
}

$('folder').onclick = () => pickFolder('Open folder', async name => {
  const res = await (await fetch('/api/folder?name=' + encodeURIComponent(name))).json();
  if (res.error) return toast(res.error);
  closeMv();
  document.title = 'Civit-RV - ' + name;
  reset();
});

$('delbtn').onclick = async () => {
  const it = listed[cur];
  if (!it) return;
  const res = await (await fetch('/api/delete?id=' + it.id, { method: 'POST' })).json();
  if (res.error) return toast(res.error);
  closeLb();
  dropItem(it);
};

function closeMv() { $('mv').classList.remove('open'); }
$('mvclose').onclick = closeMv;
$('mvbtn').onclick = () => {
  const it = listed[cur];
  if (!it) return;
  pickFolder('Move to folder', async name => {
    const res = await (await fetch('/api/move?' + new URLSearchParams({ id: it.id, dest: name }), { method: 'POST' })).json();
    if (res.error) return toast(res.error);
    closeMv();
    closeLb();
    if (res.moved) dropItem(it);
  });
};

// Auto-scroll: seconds per grid row set by the speed slider (20s left, 10s middle, 2s right)
function rowSeconds() {
  const v = +$('speed').value;
  return v <= 50 ? 20 - v / 5 : 10 - (v - 50) * 0.16;
}
let autoPos = 0, autoLast = 0;
function autoStep(now) {
  if (!$('auto').checked) return;
  requestAnimationFrame(autoStep);
  const dt = Math.min(now - autoLast, 100) / 1000;
  autoLast = now;
  if (cur >= 0 || $('mv').classList.contains('open')) { autoPos = scrollY; return; }
  // Re-sync when the user scrolled manually or the page was reset
  if (Math.abs(scrollY - autoPos) > 2) autoPos = scrollY;
  const tile = $('grid').firstElementChild;
  if (!tile) return;
  const row = tile.offsetHeight + parseFloat(getComputedStyle($('grid')).rowGap || 0);
  autoPos += row / rowSeconds() * dt;
  scrollTo(0, autoPos);
}
$('auto').onchange = () => {
  if ($('auto').checked) { autoPos = scrollY; autoLast = performance.now(); requestAnimationFrame(autoStep); }
};

// Remember sort/filter/random/speed per device (localStorage can throw in private mode, so it is optional)
const PREFS = ['kind', 'sort', 'rand', 'speed'];
function prefValue(id) { return $(id).type === 'checkbox' ? $(id).checked : $(id).value; }
try {
  const saved = JSON.parse(localStorage.getItem('prefs') || '{}');
  for (const id of PREFS) {
    if (!(id in saved)) continue;
    if ($(id).type === 'checkbox') $(id).checked = !!saved[id]; else $(id).value = saved[id];
  }
} catch (e) {}
for (const id of PREFS) {
  $(id).addEventListener('change', () => {
    try { localStorage.setItem('prefs', JSON.stringify(Object.fromEntries(PREFS.map(k => [k, prefValue(k)])))); } catch (e) {}
  });
}

loadMore();
