const $ = id => document.getElementById(id);
const itemId = Number(new URLSearchParams(location.search).get('item'));
const endpoint = `/api/items/${itemId}/photo-sync`;
let busy = false;
let scanning = false, indexRunning = false, scanTimer;
let previewObserver, previewQueue = [], activePreviews = 0;
let previewController = new AbortController();
let largePreviewController, largePreviewUrl;

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Hiking-Request': '1'},
    body: JSON.stringify(body)
  });
  if (response.status === 401) {
    location.replace('/login.html');
    throw new Error('登录已过期');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}

function updateSelection() {
  const boxes = [...$('sync-photos').querySelectorAll('input')];
  const count = boxes.filter(box => box.checked).length;
  $('sync-selection-count').textContent = `已选择 ${count} 张`;
  $('sync-selected-photos').disabled = busy || !count;
  $('select-all-photos').disabled = busy || !boxes.some(box => !box.dataset.synced && !box.dataset.previewError);
  $('clear-photo-selection').disabled = busy || !count;
  $('scan-photos').disabled = busy || scanning || indexRunning;
  boxes.forEach(box => { box.disabled = busy || !!box.dataset.synced || !!box.dataset.previewError; });
}

function resetPreviews() {
  previewController.abort(); previewController = new AbortController(); previewQueue = [];
  previewObserver?.disconnect();
  $('sync-photos').querySelectorAll('img[data-preview-url]').forEach(image => URL.revokeObjectURL(image.dataset.previewUrl));
  previewObserver = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting && entry.target.isConnected) {
      previewObserver.unobserve(entry.target);
      queuePreview(entry.target);
    }
  }, {rootMargin: '200px'});
}

function queuePreview(card) {
  card.querySelector('.sync-photo-retry').disabled = true;
  previewQueue.push({card, signal: previewController.signal});
  loadPreview();
}

async function loadPreview() {
  if (activePreviews >= 2 || !previewQueue.length) return;
  const {card, signal} = previewQueue.shift();
  const image = card.querySelector('img'), box = card.querySelector('input');
  const caption = card.querySelector('.sync-photo-state'), retry = card.querySelector('.sync-photo-retry');
  activePreviews++;
  try {
    const response = await fetch(`${endpoint}/preview?token=${encodeURIComponent(box.value)}`, {signal});
    if (!response.ok) {
      const data = response.headers.get('Content-Type')?.includes('application/json') ? await response.json() : {};
      throw new Error(data.error || `预览请求失败（${response.status}），请重试`);
    }
    const blob = await response.blob();
    if (signal.aborted) return;
    if (image.dataset.previewUrl) URL.revokeObjectURL(image.dataset.previewUrl);
    image.src = image.dataset.previewUrl = URL.createObjectURL(blob);
    await image.decode().catch(() => { throw new Error('预览图片无法显示，请重试'); });
    delete box.dataset.previewError; retry.hidden = true;
    card.querySelector('.sync-photo-preview').disabled = false;
    caption.textContent = box.dataset.synced ? '已同步' : '选择同步';
    caption.removeAttribute('title');
  } catch (error) {
    if (signal.aborted) return;
    box.checked = false; box.dataset.previewError = '1';
    card.querySelector('.sync-photo-preview').disabled = true;
    caption.textContent = error.message; caption.title = error.message;
    retry.hidden = false; retry.disabled = false;
  } finally {
    activePreviews--; updateSelection(); loadPreview();
  }
}

function showPhoto(photo) {
  const card = document.createElement('label');
  card.className = 'sync-photo-card';
  const image = document.createElement('img');
  image.alt = photo.filename;
  const open = document.createElement('button');
  open.type = 'button'; open.className = 'sync-photo-preview'; open.disabled = true;
  open.setAttribute('aria-label', `放大查看 ${photo.filename}`);
  open.append(image);
  open.addEventListener('click', event => {
    event.preventDefault(); event.stopPropagation();
    openPreview(card);
  });
  const box = document.createElement('input');
  box.type = 'checkbox'; box.value = photo.token;
  if (photo.synced) box.dataset.synced = '1';
  box.addEventListener('change', updateSelection);
  const filename = document.createElement('strong'); filename.textContent = photo.filename;
  const time = document.createElement('time');
  time.dateTime = photo.taken_at; time.textContent = new Date(photo.taken_at).toLocaleString('zh-CN');
  const caption = document.createElement('span');
  caption.className = 'sync-photo-state'; caption.textContent = photo.synced ? '已同步' : '选择同步';
  const retry = document.createElement('button');
  retry.className = 'sync-photo-retry';
  retry.type = 'button'; retry.textContent = '重试预览'; retry.hidden = true;
  retry.addEventListener('click', event => {
    event.preventDefault(); event.stopPropagation();
    queuePreview(card);
  });
  card.append(open, box, filename, time, caption, retry);
  $('sync-photos').append(card);
  previewObserver.observe(card);
}

async function openPreview(card) {
  largePreviewController?.abort();
  const controller = largePreviewController = new AbortController();
  $('lightbox-title').textContent = card.querySelector('strong').textContent;
  $('lightbox-img').src = card.querySelector('img').src;
  $('lightbox-status').textContent = '正在加载大图…';
  $('photo-lightbox').showModal();
  try {
    const token = card.querySelector('input').value;
    const response = await fetch(`${endpoint}/preview?size=large&token=${encodeURIComponent(token)}`, {signal: controller.signal});
    if (!response.ok) {
      const data = response.headers.get('Content-Type')?.includes('application/json') ? await response.json() : {};
      throw new Error(data.error || `大图请求失败（${response.status}），关闭后可重试`);
    }
    const blob = await response.blob();
    if (controller.signal.aborted) return;
    largePreviewUrl = URL.createObjectURL(blob);
    $('lightbox-img').src = largePreviewUrl;
    await $('lightbox-img').decode();
    $('lightbox-status').textContent = '';
  } catch (error) {
    if (!controller.signal.aborted) $('lightbox-status').textContent = error.message;
  }
}

$('lightbox-close').addEventListener('click', () => $('photo-lightbox').close());
$('photo-lightbox').addEventListener('click', event => {
  if (event.target === $('photo-lightbox')) $('photo-lightbox').close();
});
$('photo-lightbox').addEventListener('close', () => {
  largePreviewController?.abort();
  if (largePreviewUrl) URL.revokeObjectURL(largePreviewUrl);
  largePreviewUrl = undefined;
  $('lightbox-img').removeAttribute('src');
});

async function scanPhotos(refresh = false) {
  clearTimeout(scanTimer);
  if (busy) { scanTimer = setTimeout(() => scanPhotos(), 1000); return; }
  if (scanning) return;
  scanning = true; updateSelection();
  try {
    const data = await request(`${endpoint}/scan`, {refresh});
    $('sync-time-range').textContent = `${new Date(data.start).toLocaleString('zh-CN')} — ${new Date(data.end).toLocaleString('zh-CN')}`;
    const cards = new Map([...$('sync-photos').children].map(card => [card.querySelector('input').value, card]));
    const tokens = new Set(data.photos.map(photo => photo.token));
    for (const [token, card] of cards) if (!tokens.has(token)) {
      previewObserver.unobserve(card);
      const image = card.querySelector('img');
      if (image.dataset.previewUrl) URL.revokeObjectURL(image.dataset.previewUrl);
      card.remove();
    }
    for (const photo of data.photos) {
      const card = cards.get(photo.token);
      if (!card) showPhoto(photo);
      else if (photo.synced) {
        const box = card.querySelector('input');
        box.checked = false; box.dataset.synced = '1';
        card.querySelector('.sync-photo-state').textContent = '已同步';
      }
    }
    const synced = data.photos.filter(photo => photo.synced).length;
    const index = data.index;
    indexRunning = index.running;
    const progress = index.stage === 'metadata' ? `正在读取新增照片时间 ${index.processed} / ${index.total}…` : `正在检查备份目录，已找到 ${index.checked} 张照片…`;
    $('sync-status').textContent = `找到 ${data.photos.length} 张时间内照片，${synced} 张已同步。${indexRunning ? ` ${progress} 结果会自动更新，可先选择已有照片。` : ''}${index.error ? ` 更新失败：${index.error}，可点击更新照片重试。` : ''}${!indexRunning && data.skipped ? ` ${data.skipped} 张缺少拍摄时间或无法读取。` : ''}${!indexRunning && !index.error && !data.photos.length ? ' 没有符合时间的照片。' : ''}`;
  } catch (error) {
    indexRunning = false;
    $('sync-status').textContent = error.message;
  } finally {
    scanning = false; updateSelection();
    if (indexRunning) scanTimer = setTimeout(() => scanPhotos(), 1000);
  }
}

$('select-all-photos').addEventListener('click', () => {
  $('sync-photos').querySelectorAll('input:not(:disabled)').forEach(box => { box.checked = true; });
  updateSelection();
});
$('clear-photo-selection').addEventListener('click', () => {
  $('sync-photos').querySelectorAll('input').forEach(box => { box.checked = false; });
  updateSelection();
});
$('scan-photos').addEventListener('click', () => scanPhotos(true));
$('sync-selected-photos').addEventListener('click', async () => {
  const selected = [...$('sync-photos').querySelectorAll('input:checked')];
  busy = true; updateSelection(); $('sync-errors').replaceChildren(); $('sync-errors').hidden = true;
  let success = 0;
  for (const [index, box] of selected.entries()) {
    const card = box.closest('label');
    $('sync-status').textContent = `正在处理 ${index + 1} / ${selected.length} 张：${card.querySelector('strong').textContent}…`;
    try {
      await request(`${endpoint}/import`, {token: box.value});
      success++; box.checked = false; box.dataset.synced = '1';
      card.querySelector('.sync-photo-state').textContent = '已同步';
    } catch (error) {
      const message = document.createElement('li');
      message.textContent = `${card.querySelector('strong').textContent}：${error.message}`;
      $('sync-errors').append(message); $('sync-errors').hidden = false;
    }
  }
  $('sync-status').textContent = `同步完成：成功 ${success} / ${selected.length} 张。${success < selected.length ? '失败照片保留选择，可重试。' : '返回记录即可查看照片和地图位置。'}`;
  busy = false; updateSelection();
});

try {
  if (!Number.isSafeInteger(itemId) || itemId <= 0) throw new Error('请从记录页打开照片同步');
  $('back-record').href = `./?item=${itemId}`;
  const record = await request(`/api/items/${itemId}`);
  if (record.kind !== 'record') throw new Error('只有徒步记录可以同步照片');
  $('sync-record-name').textContent = record.name;
  document.title = `${record.name} · 照片同步`;
  resetPreviews();
  await scanPhotos();
} catch (error) {
  $('sync-status').textContent = error.message; $('scan-photos').disabled = true;
}
