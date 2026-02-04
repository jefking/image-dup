const statusMeta = document.getElementById('statusMeta');
const loadMoreBtn = document.getElementById('loadMoreBtn');
const doneEl = document.getElementById('done');
const loadingEl = document.getElementById('loading');
const pairsGrid = document.getElementById('pairsGrid');
const sentinel = document.getElementById('sentinel');
const subfolderSelect = document.getElementById('subfolderSelect');

let cursor = 0;
let done = false;
let loading = false;
let loadedPairs = 0;
let totalCandidatePairs = null;

function humanBytes(bytes) {
  const units = ['B','KB','MB','GB','TB'];
  let b = bytes;
  let u = 0;
  while (b >= 1024 && u < units.length-1) { b /= 1024; u++; }
  return `${b.toFixed(u === 0 ? 0 : 2)} ${units[u]}`;
}

function renderInfo(info, resSpanId, isLarger) {
  const sizeText = `${humanBytes(info.size_bytes)} (${info.size_bytes} bytes)`;
  const sizeDisplay = isLarger ? `<strong>${sizeText}</strong>` : sizeText;
  return `
    <div class="name">${info.name}</div>
    <div class="path">${info.relpath}</div>
    <div>Size: ${sizeDisplay}</div>
    <div>Modified: ${info.mtime_iso}</div>
    <div>Resolution: <span id="${resSpanId}">loading…</span></div>
    <div style="margin-top:6px; opacity:0.9;">Click image to delete</div>
  `;
}

async function apiDelete(id) {
  const r = await fetch('/api/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id })
  });
  return await r.json();
}

async function apiPairs(nextCursor, limit = 24) {
  const r = await fetch(`/api/pairs?cursor=${encodeURIComponent(nextCursor)}&limit=${encodeURIComponent(limit)}`);
  return await r.json();
}

async function apiSubfolders() {
  const r = await fetch('/api/subfolders');
  return await r.json();
}

async function apiSetSubfolder(subfolder) {
  const r = await fetch('/api/set-subfolder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subfolder: subfolder || null })
  });
  return await r.json();
}

function setDone(isDone) {
  doneEl.classList.toggle('hidden', !isDone);
}

function setLoading(isLoading) {
  loadingEl.classList.toggle('hidden', !isLoading);
}

function updateStatus() {
  const total = totalCandidatePairs == null ? '?' : totalCandidatePairs;
  const tail = done ? ' — done' : (loading ? ' — loading…' : '');
  statusMeta.textContent = `Loaded ${loadedPairs} pairs (of ${total} candidates)` + tail;
}

async function loadSubfolders() {
  const data = await apiSubfolders();
  if (data.subfolders) {
    data.subfolders.forEach(folder => {
      const option = document.createElement('option');
      option.value = folder;
      option.textContent = folder;
      if (folder === data.current) {
        option.selected = true;
      }
      subfolderSelect.appendChild(option);
    });
  }
}

async function changeSubfolder(subfolder) {
  setLoading(true);
  setDone(false);
  pairsGrid.innerHTML = '';
  cursor = 0;
  done = false;
  loadedPairs = 0;
  totalCandidatePairs = null;

  const result = await apiSetSubfolder(subfolder);
  if (result.error) {
    alert(result.error);
    setLoading(false);
    return;
  }

  setLoading(false);
  updateStatus();
  loadMore();
}

async function deleteFile(info) {
  const ok = window.confirm(
    `Delete this file?\n\n${info.relpath}\n\nSize: ${humanBytes(info.size_bytes)}\nModified: ${info.mtime_iso}`
  );
  if (!ok) return;
  const res = await apiDelete(info.id);
  if (res.error) {
    alert(res.error);
    return;
  }
  // Remove any pair cards currently showing this file.
  document.querySelectorAll(`[data-file-id="${info.id}"]`).forEach((img) => {
    const card = img.closest('.card');
    if (card) card.remove();
  });
}

function appendPair(pair) {
  const pairId = pair.pair_id;

  const card = document.createElement('div');
  card.className = 'card pairCard';

  const leftResId = `res-${pairId}-l-${pair.left.id}`;
  const rightResId = `res-${pairId}-r-${pair.right.id}`;

  // Determine which file is larger
  const leftIsLarger = pair.left.size_bytes > pair.right.size_bytes;
  const rightIsLarger = pair.right.size_bytes > pair.left.size_bytes;

  card.innerHTML = `
    <div class="pairHeader">
      <div>key: ${pair.group_key}</div>
      <div>#${pairId}</div>
    </div>
    <div class="pairImgs">
      <div class="imgWrap"><img loading="lazy" decoding="async" data-file-id="${pair.left.id}" /></div>
      <div class="imgWrap"><img loading="lazy" decoding="async" data-file-id="${pair.right.id}" /></div>
    </div>
    <div class="pairInfo">
      <div class="info">${renderInfo(pair.left, leftResId, leftIsLarger)}</div>
      <div class="info">${renderInfo(pair.right, rightResId, rightIsLarger)}</div>
    </div>
  `;

  const imgs = card.querySelectorAll('img');
  const leftImg = imgs[0];
  const rightImg = imgs[1];
  leftImg.src = `/img/${pair.left.id}`;
  rightImg.src = `/img/${pair.right.id}`;

  leftImg.addEventListener('click', () => deleteFile(pair.left));
  rightImg.addEventListener('click', () => deleteFile(pair.right));

  leftImg.onload = () => {
    const el = document.getElementById(leftResId);
    if (el) el.textContent = `${leftImg.naturalWidth}×${leftImg.naturalHeight}`;
  };
  rightImg.onload = () => {
    const el = document.getElementById(rightResId);
    if (el) el.textContent = `${rightImg.naturalWidth}×${rightImg.naturalHeight}`;
  };

  pairsGrid.appendChild(card);
  loadedPairs += 1;
}

async function loadMore() {
  if (loading || done) return;
  loading = true;
  updateStatus();
  const data = await apiPairs(cursor, 24);
  if (data.error) {
    loading = false;
    alert(data.error);
    return;
  }
  totalCandidatePairs = data.total_candidate_pairs;
  cursor = data.next_cursor;
  done = data.done;
  (data.pairs || []).forEach(appendPair);
  loading = false;
  setDone(done);
  updateStatus();
}

loadMoreBtn.addEventListener('click', () => loadMore());

subfolderSelect.addEventListener('change', (e) => {
  changeSubfolder(e.target.value);
});

// Infinite scrolling: when the sentinel is near the viewport, fetch more.
const io = new IntersectionObserver(
  (entries) => {
    if (entries.some(e => e.isIntersecting)) loadMore();
  },
  { root: null, rootMargin: '800px 0px', threshold: 0 }
);
io.observe(sentinel);

// Initialize
(async () => {
  await loadSubfolders();
  updateStatus();
  loadMore();
})();
