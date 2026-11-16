import { difficulty, difficultyLevel, routeDays, filterLibrary, routeView, routeCamera, parseLocation } from './route.js';
import { normalizeRoute, parseGeoJSON, parseGPX, analyze, coloredGeoJSON, exportGPX, findMaxMetricPoint } from './route.js';
import { replayTimeline, replayFrame, replayPhotos, replayHeading, replayCameraBearing } from './route.js';
import { DEM_TILES, fillElevation } from './elevation.js';
import { parseExif, matchPhotoToTrack, compressImage } from './exif.js';

const $ = id => document.getElementById(id);
const sharePrefix = location.pathname.match(/^(\/share\/[A-Za-z0-9_-]{43})(?:\/|$)/)?.[1] || '';
const resource = path => sharePrefix + path;
let route, stats, map, mapReady = false;
let items = [], selected = null, tab = 'route', selectionVersion = 0;
let terrain = true, mode = 'satellite', markers = [], cursor, noticeTimer, elevationTask;
let photos = [], photoMarkers = [], expandedPhoto = null, showPhotos = true, photoUploadBusy = false;
let activeLivePlayer = null;
let pendingImportFile = null, importBusy = false, editBusy = false;
let mapLoading = false, colorMetric = 'ele', ascending = false;
let labels = true, maxMarker = null;
let filterStatus = 'all', filterDays = 'all';
const libraryMap = createLibraryMap();
let replayMode;
const replay = createReplay({
  getMap: () => mapReady ? map : null, resource, notify, showPoint,
  prepareMap: () => { replayMode = {terrain, mode}; terrain = true; mode = 'satellite'; updateMapMode(); },
  restoreMap: () => { ({terrain, mode} = replayMode); updateMapMode(); }
});
let libraryLayout = 'cards';
try { libraryLayout = localStorage.getItem('library-layout') === 'table' ? 'table' : 'cards'; } catch {}
let libraryScrollX = 0, libraryScrollY = 0;
function restoreLibraryScroll() {
  if (typeof window !== 'undefined') {
    window.scrollTo(libraryScrollX, libraryScrollY);
    requestAnimationFrame(() => window.scrollTo(libraryScrollX, libraryScrollY));
  }
}
const metricMeta = {
  ele: { label: '最高海拔', format: v => `${Math.round(v).toLocaleString('zh-CN')} m` },
  hr: { label: '最高心率', format: v => `${Math.round(v)} bpm` },
  speed: { label: '最高速度', format: v => `${v.toFixed(1)} km/h` },
  slope: { label: '最大坡度', format: v => `${v.toFixed(1)}%` },
  cad: { label: '最高步频', format: v => `${Math.round(v)} 步/分` }
};
const profileMetrics = [['hr','心率','bpm','#c14b64'],['ele','海拔','m','#39815c'],['speed','速度','km/h','#287fc0'],['cad','步频','步/分','#9668b1'],['slope','坡度','%','#b77624']];
const defaultProfileMetrics = kind => kind === 'record' ? ['hr','ele','speed'] : ['ele'];
let shownMetrics = new Set(['hr','ele','speed']), profileWidth = 900;
try {
  const saved = JSON.parse(localStorage.getItem('profile-metrics'));
  if (Array.isArray(saved) && saved.every(key => profileMetrics.some(([metric]) => metric === key))) shownMetrics = new Set(saved);
} catch {}
for (const [key,label,unit,color] of profileMetrics) {
  const control = document.createElement('label'); control.style.setProperty('--metric-color',color);
  const input = document.createElement('input'); input.type='checkbox'; input.value=key; input.checked=shownMetrics.has(key);
  const name = document.createElement('span'); name.textContent=label; name.className='profile-metric-name';
  input.setAttribute('aria-label',`${label} (${unit})`);
  control.append(input,name); $('profile-metrics').append(control);
}
$('profile-metrics').addEventListener('change',e => {
  const input=e.target;
  if (input.type !== 'checkbox') return;
  if (input.checked) shownMetrics.add(input.value); else shownMetrics.delete(input.value);
  try { localStorage.setItem('profile-metrics',JSON.stringify([...shownMetrics])); } catch {}
  renderProfile(); showPoint(Number($('route-position').value));
  if (mapReady) map.resize();
});
function applyProfileMetrics(metrics) {
  shownMetrics = new Set(metrics);
  const inputs = $('profile-metrics').querySelectorAll ? $('profile-metrics').querySelectorAll('input[type="checkbox"]') : [];
  if (inputs.length) {
    for (const input of inputs) input.checked = shownMetrics.has(input.value);
  } else {
    for (const label of $('profile-metrics').children || []) {
      const input = label.children?.[0];
      if (input && input.type === 'checkbox') input.checked = shownMetrics.has(input.value);
    }
  }
  try { localStorage.setItem('profile-metrics',JSON.stringify([...shownMetrics])); } catch {}
}
function notify(message) {
  $('notice').textContent = message; $('notice').hidden = false;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => $('notice').hidden = true,9000);
}
function status(message, warning = false) {
  $('map-status').textContent = message; $('map-status').hidden = !message;
  $('map-status').classList.toggle('warning',warning);
}
function metric(id,value,unit,decimals = 0) {
  const el = $(id); el.replaceChildren(document.createTextNode(value == null ? '—' : value.toLocaleString('zh-CN',{maximumFractionDigits:decimals,minimumFractionDigits:decimals})));
  if (value != null) { const small = document.createElement('small'); small.textContent=unit; el.append(small); }
}
function updateRoute(next) {
  replay.close();
  $('photo-lightbox').close();
  route = next; stats = analyze(route);
  replay.setRoute(stats.points);
  replay.setPhotos(photos);
  $('route-name').textContent = route.name;
  $('route-name').title = route.name;
  const locText = route.location || '';
  $('route-location').textContent = locText;
  $('route-location').title = route.location ? `起点：${route.location}` : '';
  $('route-location').hidden = !route.location;
  $('overview-location').textContent = locText;
  $('overview-location').title = route.location ? `起点：${route.location}` : '';
  $('overview-location').hidden = !route.location;
  $('route-type').textContent = selected?.kind === 'record' ? '徒步记录' : '计划路线';
  metric('stat-distance',stats.distance/1000,'km',2);
  metric('stat-ascent',stats.ascent,'m'); metric('stat-descent',stats.descent,'m');
  metric('stat-max',stats.max,'m'); metric('stat-min',stats.min,'m');
  $('stat-duration').textContent = stats.duration ? `${Math.floor(stats.duration/3600000)}h ${Math.floor(stats.duration/60000)%60}m` : '—';
  $('stats-note').hidden = !stats.partialElevation;
  $('stats-note').textContent = '海拔缺失，爬升 / 下降仅含已知数据，难度暂缺。';
  $('sample-elevation').disabled = stats.known === stats.count;
  $('elevation-actions').hidden = !!sharePrefix || stats.known === stats.count;
  $('elevation-note').textContent = `缺失 ${stats.count-stats.known} 个点的海拔`;
  $('stat-hr').textContent = stats.hrMean == null ? '—' : `${Math.round(stats.hrMean)} / ${stats.hrMax} bpm`;
  metric('stat-cad',stats.cadMean,'步/分');
  metric('stat-speed',stats.speed,'km/h',1);
  $('stat-moving').textContent = stats.duration == null ? '—' : `${Math.floor(stats.moving/3600000)}h ${Math.floor(stats.moving/60000)%60}m`;
  const days = routeDays(route);
  metric('stat-days',days.days,'天');
  $('stat-difficulty').replaceChildren(difficultyDisplay(stats.partialElevation ? null : difficulty(stats.distance,stats.ascent,days.days)));
  $('detail-compact-summary').textContent = `${(stats.distance/1000).toFixed(2)} km${stats.ascent == null ? '' : ` · ↑${Math.round(stats.ascent).toLocaleString('zh-CN')} m`}${stats.duration ? ` · ${durationText(stats.duration)}` : ''}`;
  $('route-position').max = stats.count-1; $('route-position').value = 0;
  renderProfile(); showPoint(0);
  if (mapReady) { drawRoute(); fitRoute(); }
}

function renderProfile() {
  if (!stats) return;
  const wrap=document.createElement('div'); wrap.className='chart-wrap';
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('preserveAspectRatio','none'); svg.setAttribute('role','img');
  svg.addEventListener('pointermove',profilePointer); wrap.append(svg); $('profiles').replaceChildren(wrap);
  profileWidth = Math.max(80,$('profiles').clientWidth);
  svg.setAttribute('viewBox',`0 0 ${profileWidth} 180`);
  svg.setAttribute('aria-label',profileMetrics.filter(([key])=>shownMetrics.has(key)).map(([,label])=>label).join('、')+'沿途曲线');
  const bottom = 150, left = 20, width = profileWidth-40;
  const x = p => left+(stats.distance ? p.distance/stats.distance : 0)*width;
  let markup = '<defs><linearGradient id="ele-area-grad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#39815c" stop-opacity="0.22"/><stop offset="100%" stop-color="#39815c" stop-opacity="0.02"/></linearGradient></defs>';
  const missing = [];
  for (let i=0;i<3;i++) {
    const yy = 25+i*62.5;
    markup += `<line x1="${left}" x2="${left+width}" y1="${yy}" y2="${yy}" stroke="#e2e8dd" stroke-dasharray="4 5"/>`;
  }
  for (let i=0;i<=4;i++) markup += `<text x="${left+i*width/4}" y="174" text-anchor="${i===0?'start':i===4?'end':'middle'}" fill="#5f7268" font-size="11">${(stats.distance/1000*i/4).toFixed(1)} km</text>`;
  for (const [key,label,unit,color] of profileMetrics) {
    if (!shownMetrics.has(key)) continue;
    const values = stats.points.map(p=>p[key]).filter(Number.isFinite);
    const lo = values.length ? Math.floor(Math.min(...values)-5) : 0;
    const hi = values.length ? Math.ceil(Math.max(...values)+5) : 100;
    if (!values.length) { missing.push(label); continue; }
    const y = p => bottom-(p[key]-lo)/(hi-lo)*125;
    let chunk = [];
    function flush() {
      if (!chunk.length) return;
      if (key === 'ele' && chunk.length > 1) {
        const areaPoints = [`${x(chunk[0]).toFixed(2)},${bottom}`, ...chunk.map(p => `${x(p).toFixed(2)},${y(p).toFixed(2)}`), `${x(chunk.at(-1)).toFixed(2)},${bottom}`].join(' ');
        markup += `<polygon class="profile-area" points="${areaPoints}" fill="url(#ele-area-grad)"/>`;
      }
      const path = chunk.map((p,i) => `${i ? 'L' : 'M'}${x(p).toFixed(2)},${y(p).toFixed(2)}`).join(' ');
      markup += `<path data-metric="${key}" d="${path}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>`;
      if (chunk.length===1) markup += `<circle cx="${x(chunk[0])}" cy="${y(chunk[0])}" r="2" fill="${color}"/>`;
      chunk=[];
    }
    for (const p of stats.points) { if (p.start || !Number.isFinite(p[key])) flush(); if (Number.isFinite(p[key])) chunk.push(p); } flush();
  }
  const message = !shownMetrics.size ? '勾选上方参数查看曲线' : missing.length ? `${missing.join('、')}无数据` : '';
  if (message) markup += `<text x="${profileWidth/2}" y="18" text-anchor="middle" fill="#5f7268" font-size="11">${message}</text>`;
  markup += `<line class="profile-cursor" x1="${left}" x2="${left}" y1="20" y2="150" stroke="#c98b32" stroke-width="1.5" stroke-dasharray="3 3"/>`;
  svg.innerHTML = markup;
}
function showPoint(index) {
  const p = stats?.points[index]; if (!p) return;
  $('route-position').value = index;
  $('position-label').textContent = `${(p.distance/1000).toFixed(2)} km`;
  const values = profileMetrics.filter(([key])=>shownMetrics.has(key)).map(([key,label,unit])=>`${label} ${p[key] == null ? '—' : p[key].toLocaleString('zh-CN',{maximumFractionDigits:key==='speed'||key==='slope'?1:0})} ${unit}`);
  $('point-info').textContent = [`里程 ${(p.distance/1000).toFixed(2)} km`,...values].join(' · ');
  $('point-info').title = `坐标 ${p.lng.toFixed(5)} / ${p.lat.toFixed(5)}${p.time == null ? '' : ' · 时间 '+new Date(p.time).toLocaleString('zh-CN')}`;
  const x = 20+(stats.distance ? p.distance/stats.distance : 0)*(profileWidth-40);
  document.querySelectorAll('.profile-cursor').forEach(line=>{line.setAttribute('x1',x);line.setAttribute('x2',x);});
  if (mapReady) {
    if (!cursor) { const el = document.createElement('div'); el.className='route-marker cursor'; cursor = new maplibregl.Marker({element:el}).setLngLat([p.lng,p.lat]).addTo(map); }
    else cursor.setLngLat([p.lng,p.lat]);
  }
}
$('route-position').addEventListener('input',e => showPoint(Number(e.target.value)));
function profilePointer(e) {
  if (!stats) return;
  const bounds = e.currentTarget.getBoundingClientRect();
  const d = Math.max(0,Math.min(1,((e.clientX-bounds.left)/bounds.width*profileWidth-20)/(profileWidth-40)))*stats.distance;
  let low=0, high=stats.points.length-1;
  while (low < high) { const mid=(low+high)>>1; if (stats.points[mid].distance < d) low=mid+1; else high=mid; }
  if (low && d-stats.points[low-1].distance < stats.points[low].distance-d) low--;
  showPoint(low);
}
function cancelElevation() { elevationTask?.abort(); elevationTask = null; }
function chooseImportFile(file) {
  if (importBusy) return;
  pendingImportFile = null;
  $('import-status').hidden = true;
  if (file) {
    const error = file.size > 15*1024*1024 ? '文件超过 15 MB，请先简化轨迹。'
      : !/\.(gpx|geojson|json)$/i.test(file.name) ? '请选择 GPX 或 GeoJSON 文件。' : '';
    if (error) { $('import-status').textContent=error; $('import-status').hidden=false; }
    else pendingImportFile=file;
  }
  $('import-filename').textContent = pendingImportFile?.name || '选择或拖入轨迹文件';
  $('confirm-import').disabled = !pendingImportFile;
}
async function importFile(file) {
  if (!file || importBusy) return;
  importBusy=true;
  $('import-fields').disabled=true; $('close-import').disabled=true;
  $('confirm-import').textContent='正在导入…'; $('import-status').hidden=true;
  const kind=$('import-kind').value, parent=$('import-parent').value, system=$('coordinate-system').value;
  try {
    const text = await file.text();
    const parsed = /\.gpx$/i.test(file.name) ? parseGPX(text) : parseGeoJSON(text);
    parsed.name ||= file.name.replace(/\.[^.]+$/,'');
    const normalized = normalizeRoute(parsed,system);
    const parentId = kind === 'record' && parent && parent !== 'none' ? Number(parent) : null;
    const saved = await api('/api/items',{method:'POST',body:{name:normalized.name,kind,route_id:parentId,auto_route:kind==='record' && parent==='',track:normalized}});
    $('import-dialog').close(); tab=kind;
    await refreshLibrary(); await selectItem(saved.id); notify('已导入');
  } catch (err) {
    $('import-status').textContent=`导入失败：${err.message}`; $('import-status').hidden=false;
    if (!$('import-dialog').open) notify(err.message);
  } finally {
    importBusy=false; $('import-fields').disabled=false; $('close-import').disabled=false;
    $('confirm-import').textContent='导入';
  }
}
$('file-input').addEventListener('change',e => chooseImportFile(e.target.files[0]));
$('import-form').addEventListener('submit',e=>{e.preventDefault();importFile(pendingImportFile);});
$('import-dialog').addEventListener('cancel',e=>{if(importBusy)e.preventDefault();});
const drop = $('drop-zone');
for (const event of ['dragenter','dragover']) drop.addEventListener(event,e => {e.preventDefault();drop.classList.add('drag');});
for (const event of ['dragleave','drop']) drop.addEventListener(event,e => {e.preventDefault();drop.classList.remove('drag');});
drop.addEventListener('drop',e => chooseImportFile(e.dataTransfer.files[0]));
$('sample-elevation').addEventListener('click',async () => {
  if (!selected) return;
  const itemId=selected.id; const task = new AbortController(); elevationTask=task; $('sample-elevation').disabled=true;
  try {
    const next=await fillElevation(route,(done,total) => {
      if (elevationTask === task) $('elevation-note').textContent=`正在读取 DEM：${done} / ${total} 张瓦片…`;
    },task.signal);
    if (elevationTask !== task) return;
    next.demFilled=true;
    const saved=await api(`/api/items/${itemId}`,{method:'PATCH',body:{name:selected.name,notes:selected.notes,kind:selected.kind,route_id:selected.route_id,track:next}});
    if(elevationTask !== task) return;
    selected=saved; updateRoute(saved.track); await refreshLibrary(); notify('缺失海拔已补全并保存。');
  } catch(err) {
    task.abort();
    if (elevationTask === task) {
      $('elevation-note').textContent='补全失败，原始海拔未修改。可检查网络后重试。';
      notify(`海拔补全失败：${err.message}`);
    }
  } finally {
    if (elevationTask === task) { elevationTask=null; $('sample-elevation').disabled=stats.known === stats.count; }
  }
});

function updateHudMetrics() {
  if (typeof document === 'undefined' || !document.body?.classList?.contains('detail-page')) return;
  const headerRect = $('header-detail')?.getBoundingClientRect?.();
  const controlsRect = document.querySelector('.map-controls')?.getBoundingClientRect?.();
  const topEdge = Math.max(headerRect?.bottom || 0, controlsRect?.bottom || 0, 76);
  document.documentElement.style.setProperty('--hud-top', `${Math.ceil(topEdge)}px`);
}
function setDetailExpanded(expanded) {
  document.body.classList.toggle('detail-collapsed', !expanded);
  $('detail-toggle').textContent = expanded ? '收起' : '展开详情';
  $('detail-toggle').setAttribute('aria-expanded', String(expanded));
}
function setProfileVisible(visible) {
  document.body.classList.toggle('profile-collapsed', !visible);
  $('profile-toggle').classList.toggle('active', visible);
  $('profile-toggle').setAttribute('aria-pressed', String(visible));
  $('profile-toggle').setAttribute('aria-expanded', String(visible));
}
function setProfileExpanded(expanded) {
  $('profile-card').classList.toggle('expanded', expanded);
  $('profile-expand').textContent = expanded ? '精简曲线' : '放大曲线';
  $('profile-expand').setAttribute('aria-expanded', String(expanded));
}
$('detail-toggle').addEventListener('click', () => {
  const expanded = document.body.classList.contains('detail-collapsed');
  if (expanded && window.innerWidth <= 800) setProfileVisible(false);
  setDetailExpanded(expanded);
  renderProfile(); showPoint(Number($('route-position').value));
  fitRoute();
});
$('profile-toggle').addEventListener('click', () => {
  const visible = document.body.classList.contains('profile-collapsed');
  if (visible && window.innerWidth <= 800) setDetailExpanded(false);
  setProfileVisible(visible);
  renderProfile(); showPoint(Number($('route-position').value));
  fitRoute();
});
$('profile-collapse').addEventListener('click', () => {
  setProfileVisible(false);
  fitRoute();
});
$('profile-expand').addEventListener('click', () => {
  setProfileExpanded(!$('profile-card').classList.contains('expanded'));
  renderProfile(); showPoint(Number($('route-position').value));
  fitRoute();
});
if (typeof ResizeObserver !== 'undefined') {
  const hudObserver = new ResizeObserver(() => updateHudMetrics());
  for (const node of [$('header-detail'), document.querySelector('.map-controls')]) {
    if (node) hudObserver.observe(node);
  }
}

function detailCameraPadding(width, height) {
  const mobile = typeof window !== 'undefined' && window.innerWidth <= 800;
  const headerBottom = $('header-detail')?.getBoundingClientRect?.().bottom || 0;
  const controlsBottom = document.querySelector('.map-controls')?.getBoundingClientRect?.().bottom || 0;
  const topHud = Math.max(headerBottom, controlsBottom, mobile ? 72 : 84);
  let top = Math.min(height * 0.35, Math.round(topHud + 20));
  let left = mobile ? 24 : 40;
  let right = mobile ? 24 : 76;
  let bottom = 44;
  if (!mobile) {
    if (!$('detail').hidden && !document.body.classList.contains('detail-collapsed')) {
      const rect = $('detail').getBoundingClientRect?.();
      if (rect?.width) left = Math.min(width * 0.42, Math.round(rect.width + 36));
    }
    if (!$('profile-card').hidden && !document.body.classList.contains('profile-collapsed')) {
      const rect = $('profile-card').getBoundingClientRect?.();
      if (rect?.height) bottom = Math.min(height * 0.42, Math.round(rect.height + 44));
    }
  } else {
    const detailOpen = !$('detail').hidden && !document.body.classList.contains('detail-collapsed');
    const profileOpen = !$('profile-card').hidden && !document.body.classList.contains('profile-collapsed');
    const activeSheet = detailOpen ? $('detail') : profileOpen ? $('profile-card') : null;
    const rect = activeSheet?.getBoundingClientRect?.();
    if (rect?.height) bottom = Math.min(height * 0.48, Math.round(rect.height + 30));
    else bottom = 74;
  }
  if (top + bottom > height * 0.7) {
    const scale = (height * 0.7) / (top + bottom);
    top = Math.round(top * scale);
    bottom = Math.round(bottom * scale);
  }
  return { top, bottom, left, right };
}

function fitRoute() {
  if (!mapReady || !stats || $('detail-view').hidden || replay.active) return;
  map.resize(); map.stop();
  const width = $('map').clientWidth, height = $('map').clientHeight;
  const camera = routeCamera(stats.points, width, height, terrain, detailCameraPadding(width, height));
  if (!camera) return;
  map.jumpTo({ center: camera.center, zoom: camera.zoom, bearing: camera.bearing, pitch: camera.pitch });
}
function findMaxPoint(key) {
  return findMaxMetricPoint(stats?.points, key);
}

function renderMaxMarker() {
  if (maxMarker) {
    maxMarker.remove();
    maxMarker = null;
  }
  if (!mapReady || !stats) return;
  const maxInfo = findMaxPoint(colorMetric);
  if (!maxInfo) return;

  const { point, index, value } = maxInfo;
  const meta = metricMeta[colorMetric];
  if (!meta) return;

  const text = `${meta.label} ${meta.format(value)}`;
  const el = document.createElement('div');
  el.className = 'max-marker';
  el.title = `${text} (里程 ${(point.distance / 1000).toFixed(2)} km · 点击探索此点)`;

  const badge = document.createElement('div');
  badge.className = 'max-marker-badge';
  badge.textContent = text;

  const dot = document.createElement('div');
  dot.className = 'max-marker-dot';

  el.append(badge, dot);
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    showPoint(index);
  });

  maxMarker = new maplibregl.Marker({ element: el, anchor: 'bottom' })
    .setLngLat([point.lng, point.lat])
    .addTo(map);
}

function drawRoute() {
  if(!stats) return;
  map.getSource('route').setData(coloredGeoJSON(stats,colorMetric));
  updateColors();
  markers.forEach(marker => marker.remove()); markers=[];
  const ends = [[stats.points[0],'起',''],[stats.points.at(-1),'终','end']];
  ends.forEach(([p,label,cls]) => {
    const el=document.createElement('div'); el.className=`route-marker ${cls}`; el.textContent=label;
    markers.push(new maplibregl.Marker({element:el}).setLngLat([p.lng,p.lat]).addTo(map));
  });
  renderMaxMarker();
  renderPhotoMarkers();
  showPoint(0);
}

function renderPhotoMarkers() {
  collapsePhoto();
  photoMarkers.forEach(marker => marker.remove());
  photoMarkers = [];
  if (!mapReady || !photos.length || !showPhotos) return;
  photos.forEach(photo => {
    if (photo.lat == null || photo.lng == null) return;
    const el = document.createElement('div');
    el.setAttribute('aria-expanded', 'false');
    el.className = 'route-marker photo-marker';
    const img = document.createElement('img');
    img.src = resource(`/api/photos/${photo.id}?v=3`);
    img.alt = photo.filename;
    img.loading = 'lazy';
    const preview = document.createElement('button'); preview.type = 'button'; preview.className = 'photo-preview';
    preview.setAttribute('aria-expanded', 'false'); preview.append(img); el.append(preview);
    el.title = `${photo.filename}${photo.taken_at ? ' · ' + new Date(photo.taken_at).toLocaleString('zh-CN') : ''} (点击放大)`;
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      if (replay.active) replay.pause();
      togglePhoto(el);
      if (expandedPhoto === el && photo.live) {
        el.livePlayer ||= createLivePlayer(el, resource);
        el.livePlayer.show(photo);
      }
      if (photo.point_index != null) showPoint(photo.point_index);
    });
    const marker = new maplibregl.Marker({ element: el }).setLngLat([photo.lng, photo.lat]).addTo(map);
    photoMarkers.push(marker);
  });
}

function renderPhotos() {
  collapsePhoto();
  $('photos-count').textContent = photos.length;
  const container = $('photos-list');
  container.replaceChildren();
  photos.forEach(photo => {
    const card = document.createElement('div');
    card.className = 'photo-thumb-card';
    card.title = `${photo.filename} (点击放大)`;

    const img = document.createElement('img');
    img.src = resource(`/api/photos/${photo.id}?v=3`);
    img.alt = photo.filename;
    img.loading = 'lazy';

    const tag = document.createElement('span');
    tag.className = 'photo-match-tag';
    tag.textContent = photo.match_type === 'time' ? '⏱ 时间' : photo.match_type === 'gps' ? '📍 GPS' : '无位置';

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'photo-del-btn';
    del.title = '删除此照片';
    del.textContent = '删除照片';
    del.setAttribute('aria-label', `删除照片：${photo.filename}`);
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm(`确认删除照片“${photo.filename}”？`)) return;
      try {
        await api(`/api/photos/${photo.id}`, { method: 'DELETE' });
        photos = photos.filter(p => p.id !== photo.id);
        replay.setPhotos(photos);
        renderPhotos();
        renderPhotoMarkers();
        notify('照片已删除');
      } catch (err) {
        notify(err.message);
      }
    });

    const preview = document.createElement('button');
    preview.type = 'button';
    preview.className = 'photo-preview';
    preview.setAttribute('aria-haspopup', 'dialog');
    preview.append(img);
    preview.append(tag);
    if (photo.live) {
      const badge = document.createElement('span'); badge.className = 'photo-live-badge'; badge.textContent = 'LIVE'; preview.append(badge);
    }
    card.append(preview);
    if (!sharePrefix) card.append(del);
    preview.addEventListener('click', () => {
      openPhoto(photo);
      if (photo.point_index != null) showPoint(photo.point_index);
    });
    container.append(card);
  });
}

async function loadPhotos(itemId) {
  try {
    const next = await api(`/api/items/${itemId}/photos`);
    if (selected?.id !== itemId) return;
    photos = next;
    replay.setPhotos(photos);
    renderPhotos();
    renderPhotoMarkers();
  } catch (err) {
    if (selected?.id !== itemId) return;
    photos = [];
    replay.setPhotos(photos);
    renderPhotos();
    renderPhotoMarkers();
  }
}

async function uploadPhotos(files) {
  if (sharePrefix || !selected || !files || !files.length || photoUploadBusy) return;
  const itemId=selected.id, itemName=selected.name, points=stats?.points || [];
  files=Array.from(files);photoUploadBusy=true;$('photo-file-input').disabled=true;
  const statusEl = $('photo-upload-status');
  statusEl.hidden = false;
  const total = files.length;
  let successCount = 0;

  for (let i = 0; i < total; i++) {
    const file = files[i];
    statusEl.textContent = `正在处理第 ${i + 1} / ${total} 张：${file.name}…`;
    try {
      const buffer = await file.arrayBuffer();
      const exif = parseExif(buffer);
      const match = matchPhotoToTrack(exif, points);
      const compressed = await compressImage(file, 720, 0.85);

      const payload = {
        name: file.name,
        data: compressed.data,
        width: compressed.width,
        height: compressed.height,
        taken_at: exif.isoTime || null,
        lat: match.lat,
        lng: match.lng,
        match_type: match.matchType,
        point_index: match.pointIndex
      };

      await api(`/api/items/${itemId}/photos`, { method: 'POST', body: payload });
      successCount++;
    } catch (err) {
      notify(`上传 ${file.name} 失败：${err.message}`);
    }
  }

  statusEl.textContent = `上传完成：成功 ${successCount} / ${total} 张`;
  await loadPhotos(itemId);
  notify(`已上传到“${itemName}”：${successCount} / ${total} 张照片`);
  $('photo-file-input').value='';$('photo-file-input').disabled=false;photoUploadBusy=false;
}

function collapsePhoto() {
  if (!expandedPhoto) return;
  expandedPhoto.livePlayer?.stop();
  expandedPhoto.classList.remove('photo-expanded');
  expandedPhoto.setAttribute('aria-expanded', 'false');
  expandedPhoto.querySelector('.photo-preview')?.setAttribute('aria-expanded', 'false');
  expandedPhoto = null;
}

function togglePhoto(element) {
  const wasExpanded = expandedPhoto === element;
  collapsePhoto();
  if (wasExpanded) return;
  expandedPhoto = element;
  element.classList.add('photo-expanded');
  element.setAttribute('aria-expanded', 'true');
  element.querySelector('.photo-preview')?.setAttribute('aria-expanded', 'true');
}

function setupPhotoPreview() {
  for (const event of ['pointerdown', 'click']) {
    document.addEventListener(event, e => {
      if (!expandedPhoto) return;
      if (!expandedPhoto.contains(e.target)) collapsePhoto();
    }, true);
  }
  window.addEventListener('keydown', e => {
    if (e.key === 'Escape') collapsePhoto();
  });
}
function createLivePlayer(host, resource, remainingTime) {
  const video = document.createElement('video');
  video.className = 'photo-live-video'; video.playsInline = true; video.preload = remainingTime ? 'auto' : 'none'; video.hidden = true;
  const retry = document.createElement('button');
  retry.type = 'button'; retry.className = 'photo-live-play'; retry.textContent = '播放 Live · 有声'; retry.hidden = true;
  host.append(video, retry);
  let busy = false, waiting = false, generation = 0, timer;
  function finish() {
    clearTimeout(timer); busy = false; waiting = false; video.pause(); video.hidden = true; retry.hidden = true;
    if (activeLivePlayer === player) activeLivePlayer = null;
  }
  function stop() {
    generation++; finish(); video.removeAttribute('src'); video.load();
  }
  function play() {
    if (!video.getAttribute('src')) return;
    if (video.ended) video.currentTime = 0;
    if (activeLivePlayer !== player) activeLivePlayer?.stop();
    activeLivePlayer = player;
    const current = ++generation;
    busy = true; retry.hidden = true; video.muted = false; video.loop = false;
    clearTimeout(timer);
    timer = setTimeout(() => { if (current === generation) { finish(); retry.hidden = false; } }, 45000);
    // Include loading time in the window before the next replay photo.
    if (remainingTime && video.readyState < 3) { waiting = true; return; }
    waiting = false;
    if (remainingTime && (!Number.isFinite(video.duration) || video.duration - video.currentTime > remainingTime())) { finish(); return; }
    video.play().catch(error => {
      if (current !== generation) return;
      clearTimeout(timer); video.hidden = true; retry.hidden = false;
      // Keep this photo available until the user permits sound.
      if (error.name !== 'NotAllowedError') busy = false;
    });
  }
  video.addEventListener('canplay', () => { if (waiting) play(); });
  video.addEventListener('playing', () => { if (busy) video.hidden = false; });
  video.addEventListener('ended', finish);
  video.addEventListener('error', () => { if (video.getAttribute('src')) { finish(); retry.hidden = false; } });
  retry.addEventListener('click', e => { e.stopPropagation(); play(); });
  const player = {
    show(photo) { stop(); if (photo?.live) { video.src = resource(`/api/photos/${photo.id}/live`); play(); } },
    stop,
    pause() { generation++; waiting = false; clearTimeout(timer); video.pause(); },
    resume() { if (busy) play(); },
    get busy() { return busy; }
  };
  return player;
}

let lightboxPlayer;
function openPhoto(photo) {
  if (replay.active) replay.pause();
  collapsePhoto();
  const dialog = $('photo-lightbox');
  $('lightbox-img').src = resource(`/api/photos/${photo.id}?v=3`);
  $('lightbox-img').alt = photo.filename;
  $('lightbox-title').textContent = photo.filename;
  lightboxPlayer ||= createLivePlayer($('lightbox-media'), resource);
  dialog.showModal();
  lightboxPlayer.show(photo);
}

function updateMapMode() {
  $('terrain-toggle').classList.toggle('active',terrain); $('terrain-toggle').setAttribute('aria-pressed',terrain);
  for (const kind of ['satellite','street']) { $(kind).classList.toggle('active',mode===kind); $(kind).setAttribute('aria-pressed',mode===kind); }

  if (!terrain && $('map-status').textContent.includes('地形加载失败')) status('');
  if (!mapReady) return;
  map.setLayoutProperty('satellite','visibility',mode==='satellite' ? 'visible' : 'none');
  map.setLayoutProperty('street','visibility',mode==='street' ? 'visible' : 'none');
  map.setTerrain(terrain ? {source:'dem',exaggeration:1.3} : null);
  updateOverlays(); fitRoute();
}
$('fit-route').addEventListener('click',() => { replay.close(); fitRoute(); });
$('map-retry').addEventListener('click',initMap);
$('terrain-toggle').addEventListener('click',() => {replay.close();terrain=!terrain;updateMapMode();});
for (const kind of ['satellite','street']) $(kind).addEventListener('click',() => {replay.close();mode=kind;updateMapMode();});

let mapLibraryTask;
function loadMapLibrary() {
  return mapLibraryTask ||= loadMapScript().catch(error => { mapLibraryTask = null; throw error; });
}
async function loadMapScript() {
  for (const base of [resource('/api/maplibre/')]) {
    try {
      await new Promise((resolve,reject) => {
        const script=document.createElement('script'); script.src=base+'maplibre-gl.js';
        const timer=setTimeout(() => {script.remove();reject(new Error('地图引擎加载超时'));},15000);
        script.onload=() => {clearTimeout(timer);resolve();};
        script.onerror=() => {clearTimeout(timer);script.remove();reject(new Error('无法连接地图引擎 CDN'));};
        document.head.append(script);
      });
      const css=document.createElement('link'); css.rel='stylesheet'; css.href=base+'maplibre-gl.css'; document.head.append(css);
      return;
    } catch { /* Try the second CDN. */ }
  }
  throw new Error('地图引擎加载失败。请确认浏览器可访问 jsDelivr 或 unpkg，然后刷新页面。路线导入和统计仍可使用。');
}
async function initMap() {
  if (mapLoading || map) return;
  mapLoading=true;
  $('map-retry').hidden=true;
  status('正在加载地图…');
  try {
    const mapConfig=await api('/api/map-config');
    await loadMapLibrary();
    const initialCamera = routeCamera(stats?.points, $('map').clientWidth, $('map').clientHeight, terrain);
    map = new maplibregl.Map({container:'map',
      center: initialCamera.center,
      zoom: initialCamera.zoom,
      bearing: initialCamera.bearing,
      pitch: initialCamera.pitch,
      maxZoom:19,maxPitch:75,
      attributionControl:false,
      style:{version:8,sources:{
        ...mapConfig.sources,
        dem:{type:'raster-dem',tiles:[resource(DEM_TILES)],tileSize:256,maxzoom:15,encoding:'terrarium',attribution:`<a href="${resource('/credits.html')}">Mapzen / USGS &amp; terrain data providers</a>`},
        // Keep short per-point color segments from being discarded when zooming out.
        route:{type:'geojson',tolerance:0,data:stats ? coloredGeoJSON(stats,colorMetric) : {type:'FeatureCollection',features:[]}}
      },layers:[
        {id:'background',type:'background',paint:{'background-color':'#344738'}},
        {id:'satellite',type:'raster',source:'satellite',paint:{'raster-fade-duration':0}},
        {id:'street',type:'raster',source:'street',layout:{visibility:'none'},paint:{'raster-fade-duration':0}},
        {id:'satellite-labels',type:'raster',source:'satellite-labels',paint:{'raster-fade-duration':0}},
        {id:'street-labels',type:'raster',source:'street-labels',layout:{visibility:'none'},paint:{'raster-fade-duration':0}},
        {id:'route-outline',type:'line',source:'route',layout:{'line-cap':'round','line-join':'round'},paint:{'line-color':'#253927','line-width':9,'line-opacity':.9}},
        {id:'route-line',type:'line',source:'route',layout:{'line-cap':'round','line-join':'round'},paint:{'line-color':'#fff386','line-width':5}}
      ],terrain:{source:'dem',exaggeration:1.3}}
    });
    map.addControl(new maplibregl.NavigationControl({visualizePitch:true}),'top-right');
    map.addControl(new maplibregl.AttributionControl({compact:false}),'bottom-right');
    let demLoaded = false, demErrorTimer = null;
    map.on('sourcedata', e => {
      if (e.sourceId === 'dem' && (e.tile?.state === 'loaded' || e.isSourceLoaded)) {
        demLoaded = true;
        clearTimeout(demErrorTimer);
        if ($('map-status').textContent.includes('地形加载失败')) status('');
      }
    });
    map.on('error', e => {
      if (e.error?.name === 'AbortError' || e.error?.message?.includes('abort') || e.error?.status === 0) return;
      const source = e.sourceId || e.tile?.sourceId || (e.tile?.tileID ? '瓦片' : '地图');
      if (source === 'dem') {
        if (!demLoaded && terrain) {
          clearTimeout(demErrorTimer);
          demErrorTimer = setTimeout(() => {
            if (!demLoaded && terrain && !map.isSourceLoaded?.('dem')) {
              status('地形加载失败，可关闭 3D 地形。', true);
            }
          }, 1500);
        }
      } else {
        status(`天地图图层加载失败（${source}），请检查直连网络或 Key 配额。`, true);
      }
    });
    map.on('idle', () => {
      if (terrain && (demLoaded || map.isSourceLoaded?.('dem'))) {
        if ($('map-status').textContent.includes('地形加载失败')) status('');
      }
      if (map.areTilesLoaded?.() && $('map-status').textContent.includes('天地图图层加载失败')) {
        status('');
      }
    });
    map.on('load', () => {
      mapReady = true;
      if ($('map-status').textContent === '正在加载地图…') status('');
      drawRoute(); updateMapMode(); fitRoute();
      map.once('idle', fitRoute);
      map.on('click', 'route-line', e => {
        if (!stats) return;
        let best = 0, delta = Infinity;
        stats.points.forEach((p, i) => { const xy = map.project([p.lng, p.lat]); const d = (xy.x - e.point.x) ** 2 + (xy.y - e.point.y) ** 2; if (d < delta) { best = i; delta = d; } });
        showPoint(best);
      });
      map.on('mouseenter', 'route-line', () => map.getCanvas().style.cursor = 'pointer');
      map.on('mouseleave', 'route-line', () => map.getCanvas().style.cursor = '');
    });
    setTimeout(() => {if(!mapReady) status('地图尚未加载完成，请检查外部瓦片服务连接。导入和海拔统计仍可使用。',true);},20000);
  } catch(err) {
    map?.remove();map=null;mapReady=false;
    status(err.message);$('map-retry').hidden=false;
  }
  finally {mapLoading=false;}
}


function createLibraryMap() {
  let atlas, ready = false, loading = false, kind = 'route', popup;
  const chosen = new Map();
  const color = id => `hsl(${(id * 137.508) % 360}, 85%, 65%)`;
  const message = text => { $('library-map-status').textContent = text; };
  function renderPicker() {
    const focused = document.activeElement?.dataset.mapItem;
    const query = $('map-search').value.trim().toLocaleLowerCase();
    const visible = items.filter(item => item.kind === kind &&
      [item.name, item.location, item.notes].some(value => (value || '').toLocaleLowerCase().includes(query)));
    $('map-pick-list').replaceChildren(...visible.map(item => {
      const label = document.createElement('label'); label.className = 'map-pick-item';
      const input = document.createElement('input'); input.type = 'checkbox'; input.checked = chosen.has(item.id);
      input.dataset.mapItem = item.id;
      input.addEventListener('change', () => choose(item, input.checked));
      const swatch = document.createElement('span'); swatch.className = 'map-track-swatch'; swatch.style.background = color(item.id);
      const text = document.createElement('span');
      const name = document.createElement('strong'); name.textContent = item.name;
      const meta = document.createElement('small');
      meta.textContent = `${format(item.summary.distance / 1000, 'km', 1)}${item.location ? ' · ' + item.location : ''}${chosen.get(item.id)?.pending ? ' · 加载中…' : ''}`;
      text.append(name, meta); label.append(input, swatch, text); return label;
    }));
    if (!visible.length) $('map-pick-list').textContent = query ? '没有符合条件的轨迹' : kind === 'route' ? '路线库暂无路线' : '记录库暂无记录';
    if (focused) $('map-pick-list').querySelector(`[data-map-item="${focused}"]`)?.focus({preventScroll:true});
    const records = items.filter(item => item.kind === 'record' && chosen.has(item.id)).length;
    $('map-selection-count').textContent = `已选 ${chosen.size} 条（路线 ${chosen.size - records} / 记录 ${records}）`;
    $('map-clear').disabled = !chosen.size;
    $('map-fit-all').disabled = !ready || ![...chosen.values()].some(entry => entry.track);
  }
  async function choose(item, checked) {
    popup?.remove();
    if (!checked) { chosen.delete(item.id); draw(); renderPicker(); return; }
    const entry = {pending:true}; chosen.set(item.id, entry); renderPicker();
    try {
      const data = await api(`/api/items/${item.id}`);
      // A deselected or reselected item must not be restored by an older request.
      if (chosen.get(item.id) !== entry) return;
      entry.track = data.track; entry.pending = false;
      draw(); renderPicker(); fit();
    } catch (error) {
      if (chosen.get(item.id) !== entry) return;
      chosen.delete(item.id); renderPicker(); draw(); notify(`${item.name}：${error.message}`);
    }
  }
  function draw() {
    if (!ready) return;
    const features = [];
    for (const [id, entry] of chosen) {
      for (const segment of entry.track?.segments || []) {
        if (!segment.length) continue;
        const coordinates = segment.map(point => [point.lng, point.lat]);
        features.push({type:'Feature', properties:{id, color:color(id)}, geometry:{type:'LineString', coordinates}});
      }
    }
    atlas.getSource('tracks').setData({type:'FeatureCollection', features});
  }
  function fit() {
    if (!ready) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const entry of chosen.values()) for (const segment of entry.track?.segments || [])
      for (const point of segment) bounds.extend([point.lng, point.lat]);
    if (bounds.isEmpty()) return;
    const panel = $('map-picker'), mobile = matchMedia('(max-width: 600px)').matches;
    atlas.fitBounds(bounds, {maxZoom:15, duration:500, padding:{top:70, right:60,
      left:mobile ? 40 : panel.getBoundingClientRect().width + 36,
      bottom:mobile ? panel.getBoundingClientRect().height + 50 : 60}});
  }
  function updateBase() {
    if (!ready) return;
    const base = $('library-map-base').value;
    for (const name of ['satellite', 'street']) {
      atlas.setLayoutProperty(name, 'visibility', name === base ? 'visible' : 'none');
      atlas.setLayoutProperty(name + '-labels', 'visibility', name === base && $('library-map-labels').checked ? 'visible' : 'none');
    }
  }
  async function init() {
    if (atlas || loading) return;
    loading = true; $('library-map-retry').hidden = true; message('正在加载地图…');
    try {
      const config = await api('/api/map-config'); await loadMapLibrary();
      atlas = new maplibregl.Map({container:'library-map', center:[104,35], zoom:3, maxZoom:19,
        attributionControl:false, style:{version:8, sources:{...config.sources,
          tracks:{type:'geojson', data:{type:'FeatureCollection', features:[]}}}, layers:[
          {id:'background', type:'background', paint:{'background-color':'#344738'}},
          ...['satellite','street','satellite-labels','street-labels'].map(id => ({id, type:'raster', source:id,
            layout:{visibility:id.startsWith('satellite') ? 'visible' : 'none'}, paint:{'raster-fade-duration':0}})),
          {id:'tracks-outline', type:'line', source:'tracks', paint:{'line-color':'#26382e', 'line-width':7}},
          {id:'tracks-line', type:'line', source:'tracks', layout:{'line-cap':'round','line-join':'round'}, paint:{'line-color':['get','color'], 'line-width':4}}
        ]}});
      atlas.addControl(new maplibregl.NavigationControl({showCompass:false}), 'bottom-right');
      atlas.addControl(new maplibregl.AttributionControl({compact:true}), 'bottom-right');
      atlas.on('load', () => {
        ready = true; updateBase(); draw(); renderPicker(); atlas.resize(); fit();
        message('勾选轨迹叠加查看，点击轨迹查看名称');
      });
      atlas.on('error', event => {
        if (event.error?.name === 'AbortError') return;
        message('地图加载失败，可重试；已选轨迹会保留。'); $('library-map-retry').hidden = false;
      });
        atlas.on('click', 'tracks-line', event => {
          const item = items.find(item => item.id === Number(event.features[0]?.properties.id));
          if (!item) return;
          popup?.remove();
          popup = new maplibregl.Popup().setLngLat(event.lngLat).setText(`${item.kind === 'record' ? '记录' : '路线'} · ${item.name}`).addTo(atlas);
        });
        atlas.on('mouseenter', 'tracks-line', () => { atlas.getCanvas().style.cursor = 'pointer'; });
        atlas.on('mouseleave', 'tracks-line', () => { atlas.getCanvas().style.cursor = ''; });
    } catch (error) {
      atlas?.remove(); atlas = null; ready = false; message(error.message); $('library-map-retry').hidden = false;
    } finally { loading = false; }
  }
  for (const value of ['route','record']) $('map-pick-' + value).addEventListener('click', () => {
    kind = value;
    for (const name of ['route','record']) { const node = $('map-pick-' + name); node.classList.toggle('active', name === kind); node.setAttribute('aria-pressed', name === kind); }
    renderPicker();
  });
  $('map-search').addEventListener('input', renderPicker);
  $('map-clear').addEventListener('click', () => { chosen.clear(); popup?.remove(); draw(); renderPicker(); });
  $('map-fit-all').addEventListener('click', fit);
  $('library-map-base').addEventListener('change', updateBase);
  $('library-map-labels').addEventListener('change', updateBase);
  $('library-map-retry').addEventListener('click', () => { if (loading) return; atlas?.remove(); atlas = null; ready = false; init(); });
  return {
    show() { renderPicker(); init(); if (ready) atlas.resize(); },
    reset() {
      const ids = [...chosen.keys()]; chosen.clear(); popup?.remove(); draw();
      for (const id of ids) { const item = items.find(item => item.id === id); if (item) choose(item, true); }
    }
  };
}

async function api(path, options={}) {
  const response=await fetch(resource(path),{...options,headers:{'Content-Type':'application/json','X-Hiking-Request':'1'},body:options.body ? JSON.stringify(options.body) : undefined});
  if(response.status===401 && !sharePrefix) { location.replace('/login.html'); throw new Error('登录已过期'); }
  if(path==='/api/map-config' && response.status===404 && !sharePrefix) {
    throw new Error('后端尚未加载地图配置接口。请重启 hiking 服务，再点击“重试地图”。');
  }
  const data=await response.json();
  if(!response.ok) throw new Error(data.error || '请求失败');
  return data;
}
function button(text, handler, className='library-item') {
  const node=document.createElement('button');node.type='button';node.textContent=text;node.className=className;
  node.addEventListener('click',()=>Promise.resolve().then(handler).catch(error=>notify(error.message)));return node;
}
function itemLink(item, text, className) {
  const node=document.createElement('a');node.href=`?item=${item.id}`;node.textContent=text;node.className=className;
  node.addEventListener('click',event=>{
    if(event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();selectItem(item.id).catch(error=>notify(error.message));
  });
  return node;
}
function hasActiveFilters() {
  return Boolean(
    $('search').value ||
    $('sort').value !== 'default' ||
    ascending ||
    (tab === 'route' && (
      filterStatus !== 'all' ||
      $('filter-province').value ||
      $('filter-city').value ||
      $('filter-district').value ||
      $('filter-difficulty').value ||
      filterDays !== 'all'
    ))
  );
}
function updateStatusButtons() {
  for (const [key, id] of [['all', 'filter-status-all'], ['unhiked', 'filter-status-unhiked'], ['hiked', 'filter-status-hiked']]) {
    const btn = $(id);
    const active = filterStatus === key;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active);
  }
}
function updateDaysButtons() {
  for (const [key, id] of [['all', 'filter-days-all'], ['single', 'filter-days-single'], ['multi', 'filter-days-multi']]) {
    const btn = $(id);
    const active = filterDays === key;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active);
  }
}
function updateLocationOptions() {
  const routes = items.filter(item => item.kind === 'route');
  const locMap = new Map();

  for (const item of routes) {
    if (!item.location) continue;
    const { province, city, district } = parseLocation(item.location);
    if (!province) continue;
    if (!locMap.has(province)) locMap.set(province, new Map());
    const cityMap = locMap.get(province);
    const c = city || '';
    if (!cityMap.has(c)) cityMap.set(c, new Set());
    if (district) cityMap.get(c).add(district);
  }

  const curProvince = $('filter-province').value;
  const curCity = $('filter-city').value;
  const curDistrict = $('filter-district').value;

  const provinces = [...locMap.keys()].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  const provinceOpts = [new Option('全部省份', '')];
  for (const p of provinces) provinceOpts.push(new Option(p, p));
  $('filter-province').replaceChildren(...provinceOpts);
  $('filter-province').value = provinces.includes(curProvince) ? curProvince : '';
  const selProvince = $('filter-province').value;

  let cities = [];
  if (selProvince && locMap.has(selProvince)) {
    cities = [...locMap.get(selProvince).keys()].filter(Boolean);
  } else {
    const allCities = new Set();
    for (const cityMap of locMap.values()) {
      for (const c of cityMap.keys()) if (c) allCities.add(c);
    }
    cities = [...allCities];
  }
  cities.sort((a, b) => a.localeCompare(b, 'zh-CN'));
  const cityOpts = [new Option('全部城市', '')];
  for (const c of cities) cityOpts.push(new Option(c, c));
  $('filter-city').replaceChildren(...cityOpts);
  $('filter-city').value = cities.includes(curCity) ? curCity : '';
  const selCity = $('filter-city').value;

  let districts = [];
  if (selCity) {
    const dSet = new Set();
    for (const [p, cityMap] of locMap.entries()) {
      if (!selProvince || p === selProvince) {
        if (cityMap.has(selCity)) {
          for (const d of cityMap.get(selCity)) dSet.add(d);
        }
      }
    }
    districts = [...dSet];
  } else if (selProvince && locMap.has(selProvince)) {
    const dSet = new Set();
    for (const dList of locMap.get(selProvince).values()) {
      for (const d of dList) dSet.add(d);
    }
    districts = [...dSet];
  } else {
    const dSet = new Set();
    for (const cityMap of locMap.values()) {
      for (const dList of cityMap.values()) {
        for (const d of dList) dSet.add(d);
      }
    }
    districts = [...dSet];
  }
  districts.sort((a, b) => a.localeCompare(b, 'zh-CN'));
  const districtOpts = [new Option('全部区县', '')];
  for (const d of districts) districtOpts.push(new Option(d, d));
  $('filter-district').replaceChildren(...districtOpts);
  $('filter-district').value = districts.includes(curDistrict) ? curDistrict : '';
}
function updateSortControls() {
  const sort=$('sort').value;
  const labels={default:['默认排序','默认排序 · 反向'],date:['活动日期 · 从新到旧','活动日期 · 从旧到新'],newest:['导入时间 · 从新到旧','导入时间 · 从旧到新'],name:['名称 · Z → A','名称 · A → Z'],distance:['距离 · 从长到短','距离 · 从短到长'],ascent:['爬升 · 从高到低','爬升 · 从低到高'],difficulty:['难度 · 从高到低','难度 · 从低到高']};
  for(const option of $('sort').options)option.textContent=labels[option.value][Number(option.value===sort ? ascending : option.value==='name')];
  const next=ascending?'切换为降序':'切换为升序';
  $('sort-direction').setAttribute('aria-label',next);$('sort-direction').title=next;
  $('sort-direction-label').textContent=ascending?'升序':'降序';$('sort-direction-arrow').textContent=ascending?'↑':'↓';
  $('reset-filters').hidden=!hasActiveFilters();
}
function renderParentOptions() {
  const routes=items.filter(item=>item.kind==='route');
  for(const id of ['import-parent','item-parent']) {
    const value=$(id).value;
    const defaults=id==='import-parent' ? [new Option('自动创建同名路线',''),new Option('不关联路线','none')] : [new Option('不关联路线','')];
    $(id).replaceChildren(...defaults,...routes.map(item=>new Option(item.name,item.id)));
    $(id).value=[...$(id).options].some(option=>option.value===value)?value:'';
  }
}
function showLibrary() {
  clearSelection();renderLibrary();
  $(tab==='map'?'map-tab':matchMedia('(max-width: 600px), (pointer: coarse)').matches?(tab==='route'?'routes-tab':'records-tab'):'search').focus({preventScroll:true});
  restoreLibraryScroll();
}
function selectionUrl(id) {
  const url=new URL(location.href);
  if(id==null)url.searchParams.delete('item');else url.searchParams.set('item',id);
  return url;
}
function format(value,unit='',digits=0) {
  return value==null ? '—' : value.toLocaleString('zh-CN',{maximumFractionDigits:digits})+(unit?' '+unit:'');
}
function difficultyDisplay(score) {
  const display=document.createElement('span');display.className='difficulty-display';
  const number=document.createElement('strong');number.textContent=format(score,'',1);
  const progress=document.createElement('progress');progress.max=500;progress.value=score ?? 0;progress.hidden=score==null;
  progress.setAttribute('aria-label',`难度分数 ${format(score,'',1)} / 500`);
  const level=document.createElement('span');level.className='difficulty-label';level.textContent=difficultyLevel(score).replace(/^\S+ /,'');
  if(score!=null)level.dataset.level=level.textContent;
  display.title=score==null?'海拔不完整，无法计算难度':'按距离、爬升和天数计算；进度为分数 / 500';
  display.append(level,number,progress);return display;
}
function durationText(value) {return value==null?'—':`${Math.floor(value/3600000)}h ${Math.floor(value/60000)%60}m`;}
function summaryCells(values,className) {
  const container=document.createElement('div');container.className=className;
  for(const [label,value] of values) {
    const cell=document.createElement('div');if(className==='library-summary')cell.className='summary-cell';
    const name=document.createElement('span');name.textContent=label;
    const data=document.createElement('strong');data.textContent=value;cell.append(name,data);container.append(cell);
  }
  return container;
}
function recordButton(item) {
  const s=item.summary;
  const node=itemLink(item,`${item.name} · ${s.date==null?'日期未知':new Date(s.date).toLocaleDateString('zh-CN')} · ${format(s.distance/1000,'km',1)} · 爬升 ${format(s.ascent,'m')} · ${durationText(s.duration)}`,'record-link');
  node.append(difficultyDisplay(s.difficulty));return node;
}
function routeThumbnail(item) {
  const image=document.createElement('img');image.className='route-thumbnail';
  image.src=`/api/items/${item.id}/thumbnail`;image.alt=`${item.name}路线缩略图（上北下南）`;
  image.loading='lazy';image.decoding='async';image.width=480;image.height=288;
  image.addEventListener('error',()=>{image.alt='缩略图加载失败，重新打开列表重试';});
  return image;
}
function relatedRecords(item) {
  return filterLibrary(items.filter(record=>record.route_id===item.id),{kind:'record'});
}
function recordGroup(related) {
  const group=document.createElement('details');group.className='card-records';
  const title=document.createElement('summary');title.textContent=`记录（${related.length}）`;
  group.append(title,...related.map(recordButton));return group;
}
function libraryTable(visible) {
  const table=document.createElement('table');table.className='library-table';
  const caption=table.createCaption();caption.className='sr-only';caption.textContent=tab==='route'?'路线列表':'徒步记录列表';
  const header=table.createTHead().insertRow();
  for(const label of ['路线图','名称 / 备注','活动日期','距离','爬升','难度','天数','时长',...(tab==='route'?['记录']:[])]) {
    const th=document.createElement('th');th.scope='col';th.textContent=label;header.append(th);
  }
  const body=table.createTBody();
  for(const item of visible) {
    const row=body.insertRow(),s=item.summary;
    const preview=itemLink(item,'','thumbnail-open');preview.setAttribute('aria-label',`查看 ${item.name}`);preview.append(routeThumbnail(item));row.insertCell().append(preview);
    const title=row.insertCell();title.append(itemLink(item,item.name,'text-button'));
    if(item.location){const loc=document.createElement('div');loc.className='route-location';loc.textContent=item.location;loc.title=`起点：${item.location}`;title.append(loc);}
    if(item.notes){const notes=document.createElement('p');notes.className='card-notes';notes.textContent=item.notes;title.append(notes);}
    for(const value of [s.date==null?'日期未知':new Date(s.date).toLocaleDateString('zh-CN'),format(s.distance/1000,'km',2),format(s.ascent,'m')+(s.partialElevation?'（部分）':''),difficultyDisplay(s.difficulty),format(s.days,'天'),durationText(s.duration)])row.insertCell().append(value);
    if(item.kind==='route') {
      const relation=row.insertCell();
      const related=relatedRecords(item);
      if(related.length)relation.append(recordGroup(related));else relation.textContent='0 次记录';
    }
  }
  return table;
}
function renderLibrary() {
  const mapTab = tab === 'map';
  document.body.classList.toggle('map-page', mapTab && !$('library-view').hidden);
  $('map-view').hidden = !mapTab;
  $('map-tab').classList.toggle('active', mapTab); $('map-tab').setAttribute('aria-pressed', mapTab);
  if (mapTab) {
    for (const id of ['routes-tab','records-tab']) { $(id).classList.remove('active'); $(id).setAttribute('aria-pressed', false); }
    libraryMap.show();
    return;
  }
  $('quick-filters').hidden = tab !== 'route';
  if (tab === 'route') {
    updateLocationOptions();
    updateStatusButtons();
    updateDaysButtons();
  }
  updateSortControls();
  for(const kind of ['route','record']) { const node=$(kind==='route'?'routes-tab':'records-tab');node.classList.toggle('active',tab===kind);node.setAttribute('aria-pressed',tab===kind); }
  const visible=filterLibrary(items,{
    kind:tab,
    query:$('search').value,
    sort:$('sort').value,
    ascending,
    hikeStatus:tab==='route'?filterStatus:'all',
    province:tab==='route'?$('filter-province').value:'',
    city:tab==='route'?$('filter-city').value:'',
    district:tab==='route'?$('filter-district').value:'',
    difficulty:tab==='route'?$('filter-difficulty').value:'',
    days:tab==='route'?filterDays:'all'
  });
  const records=items.filter(item=>item.kind==='record');
  $('library-summary').replaceChildren(...summaryCells([
    ['路线',items.filter(item=>item.kind==='route').length],['记录',records.length],
    ['累计记录距离',format(records.reduce((sum,r)=>sum+r.summary.distance,0)/1000,'km',1)],
    ['累计记录爬升'+(records.some(r=>r.summary.partialElevation)?'（已知部分）':''),format(records.reduce((sum,r)=>sum+(r.summary.ascent||0),0),'m')]
  ],'library-summary').children);
  $('result-count').textContent=`${visible.length} / ${items.filter(item=>item.kind===tab).length} 条`;
  for(const layout of ['cards','table']) {
    const node=$(layout+'-view');node.classList.toggle('active',libraryLayout===layout);node.setAttribute('aria-pressed',libraryLayout===layout);
  }
  $('library-list').classList.toggle('table-layout',libraryLayout==='table');
  $('library-list').replaceChildren(...(libraryLayout==='table' && visible.length ? [libraryTable(visible)] : visible.map(item=>{
    const s=item.summary, related=relatedRecords(item);
    const card=document.createElement('article');card.className='library-card';
    const open=itemLink(item,'','card-open');open.setAttribute('aria-label',`查看 ${item.name}`);
    const heading=document.createElement('div');heading.className='card-heading';
    const titleGroup=document.createElement('div');titleGroup.className='card-title-group';
    const name=document.createElement('h2');name.textContent=item.name;name.title=item.name;titleGroup.append(name);
    if(item.location){const loc=document.createElement('span');loc.className='route-location';loc.textContent=item.location;loc.title=`起点：${item.location}`;titleGroup.append(loc);}
    const badge=difficultyDisplay(s.difficulty);badge.classList.add('difficulty-badge');heading.append(titleGroup,badge);
    const meta=document.createElement('p');meta.className='card-meta';
    meta.textContent=`${s.date==null?'日期未知':new Date(s.date).toLocaleDateString('zh-CN')} · ${s.days} 天${s.daysSource==='default'?'（默认）':''}${item.kind==='route'?' · '+related.length+' 次记录':''}`;
    open.append(routeThumbnail(item),heading,meta,summaryCells([
      ['距离',format(s.distance/1000,'km',2)],['爬升'+(s.partialElevation?'（部分）':''),format(s.ascent,'m')],
      [item.kind==='record'?'时长':'最高海拔',item.kind==='record'?durationText(s.duration):format(s.max,'m')]
    ],'card-stats'));
    if(item.notes){const notes=document.createElement('p');notes.className='card-notes';notes.textContent=item.notes;open.append(notes);}
    card.append(open);
    if(related.length)card.append(recordGroup(related));
    return card;
  })));
  if(!visible.length){
    const empty=document.createElement('div');empty.className='empty-library';
    const filtered=items.some(item=>item.kind===tab);
    const title=document.createElement('span');title.textContent=filtered?'没有符合条件的条目':tab==='route'?'暂无路线':'暂无记录';
    empty.append(title,button(filtered?'清除筛选':'导入文件',()=>$(filtered?'reset-filters':'open-import').click(),'primary'));
    $('library-list').append(empty);
  }
}

async function refreshLibrary() {
  items=await api('/api/items');
  // Older running servers do not include summaries until restarted.
  for(const item of items)if(!item.summary) {
    const data=await api(`/api/items/${item.id}`), stats=analyze(data.track), day=routeDays(data.track);
    const times=stats.points.map(p=>p.time).filter(Number.isFinite);
    item.summary={...stats,points:undefined,days:day.days,daysSource:data.track.days?'manual':times.length?'time':'default',date:times.length?Math.min(...times):null,
      difficulty:stats.partialElevation?null:difficulty(stats.distance,stats.ascent,day.days)};
  }
  libraryMap.reset();
  renderParentOptions();renderLibrary();
}
async function selectItem(id, {historyUpdate=true}={}) {
  if (!$('library-view').hidden && typeof window !== 'undefined') {
    libraryScrollY = window.scrollY || window.pageYOffset || document.documentElement?.scrollTop || 0;
    libraryScrollX = window.scrollX || window.pageXOffset || document.documentElement?.scrollLeft || 0;
  }
  replay.close();
  const version=++selectionVersion;cancelElevation();
  const data=await api(`/api/items/${id}`);if(version!==selectionVersion) return;
  if(!sharePrefix && historyUpdate && Number(new URLSearchParams(location.search).get('item'))!==id) {
    history.pushState({fromLibrary:!selected},'',selectionUrl(id));
  }
  selected=data;
  document.body.classList.remove('map-page', 'replay-active');
  document.body.classList.add('detail-page');
  setDetailExpanded(true);
  setProfileExpanded(false);
  setProfileVisible(typeof window === 'undefined' || window.innerWidth > 800);
  $('photo-upload-status').hidden=true;
  $('map-options').open=false;
  $('edit-dialog').close();
  photos=[]; renderPhotoMarkers();
  if(data.location) data.track.location=data.location;
  if(!data.location && !sharePrefix) {
    api(`/api/items/${id}/location`).then(res=>{
      if(res?.location && selected?.id===id) {
        data.location=res.location; if(route) route.location=res.location;
        $('route-location').textContent=res.location;
        $('route-location').title=`起点：${res.location}`;
        $('route-location').hidden=false;
        $('overview-location').textContent=res.location;
        $('overview-location').title=`起点：${res.location}`;
        $('overview-location').hidden=false;
        const entry=items.find(i=>i.id===id);
        if(entry){entry.location=res.location;renderLibrary();}
      }
    }).catch(()=>{});
  }
  applyProfileMetrics(defaultProfileMetrics(data.kind));
  $('library-view').hidden=true;$('detail-view').hidden=false;$('workspace').hidden=false;
  $('header-library').hidden=true;$('header-detail').hidden=false;
  $('parent-route').hidden=!!sharePrefix || !data.route_id;
  $('open-photo-sync').hidden=!!sharePrefix || data.kind!=='record';
  $('open-photo-sync').href=`photo-sync.html?item=${id}`;
  $('item-days').value=data.track.days ?? '';
  document.title=data.name+' · Hiking';
  $('detail').hidden=false;$('profile-card').hidden=false;
  $('item-name').value=data.name;$('item-notes').value=data.notes;$('parent-field').hidden=data.kind!=='record';
  if (!sharePrefix) renderLibrary();
  $('shared-notes').hidden=false;
  $('shared-notes').textContent=data.notes || '暂无备注';
  $('related-records').replaceChildren(...items.filter(item=>item.route_id===id).sort((a,b)=>{
    const da=a.summary?.date, db=b.summary?.date;
    if(da==null || db==null) return da==null ? (db==null?b.id-a.id:1) : -1;
    return db-da || b.id-a.id;
  }).map(recordButton));
  $('related-section').hidden=!$('related-records').children.length;
  updateRoute(data.track);
  window.scrollTo(0,0);
  requestAnimationFrame(() => { updateHudMetrics(); if (mapReady) fitRoute(); });
  if(!map) initMap();else {map.resize();fitRoute();}
  loadPhotos(id);
  $(sharePrefix ? 'route-name' : 'back-library').focus();
}
function clearSelection() {
  replay.close(); collapsePhoto(); $('photo-lightbox').close();
  $('edit-dialog').close();$('map-options').open=false;
  selected=null;route=null;stats=null;selectionVersion++;cancelElevation();
  document.body.classList.remove('detail-page', 'detail-collapsed', 'profile-collapsed', 'replay-active');
  setProfileExpanded(false);
  $('library-view').hidden=false;$('detail-view').hidden=true;$('workspace').hidden=true;document.title='路线库 · Hiking';
  $('header-library').hidden=false;$('header-detail').hidden=true;
  $('detail').hidden=true;$('profile-card').hidden=true;
  markers.forEach(marker=>marker.remove());markers=[];cursor?.remove();cursor=null;
  if(maxMarker) { maxMarker.remove(); maxMarker=null; }
  photoMarkers.forEach(marker=>marker.remove());photoMarkers=[];photos=[];expandedPhoto=null;renderPhotos();
  showPhotos = true;
  $('photos-toggle').classList.add('active');
  $('photos-toggle').setAttribute('aria-pressed', 'true');
  if(mapReady) map.getSource('route').setData({type:'FeatureCollection',features:[]});
}
for(const [id,kind] of [['routes-tab','route'],['records-tab','record'],['map-tab','map']]) $(id).addEventListener('click',()=>{tab=kind;renderLibrary();});
$('search').addEventListener('input',renderLibrary);
$('import-kind').addEventListener('change',()=>{$('import-parent-field').hidden=$('import-kind').value!=='record';});
$('open-edit').addEventListener('click',()=>{
  if(!selected || sharePrefix)return;
  renderParentOptions();$('item-name').value=selected.name;$('item-notes').value=selected.notes;
  $('item-days').value=selected.track.days ?? '';$('item-parent').value=selected.route_id ?? '';
  $('parent-field').hidden=selected.kind!=='record';$('edit-error').hidden=true;
  $('edit-title').textContent=selected.kind==='record'?'编辑记录':'编辑路线';
  $('edit-dialog').showModal();
});
$('close-edit').addEventListener('click',()=>{if(!editBusy)$('edit-dialog').close();});
$('edit-dialog').addEventListener('cancel',event=>{if(editBusy)event.preventDefault();});
$('edit-item').addEventListener('submit',async event=>{
  event.preventDefault();if(!selected || editBusy) return;
  editBusy=true;$('edit-fields').disabled=true;$('close-edit').disabled=true;$('edit-error').hidden=true;
  $('save-item').textContent='正在保存…';
  try {
    const id=selected.id;
    const requestedDays=$('item-days').value===''?null:Number($('item-days').value);
    const saved=await api(`/api/items/${id}`,{method:'PATCH',body:{name:$('item-name').value,notes:$('item-notes').value,kind:selected.kind,days:requestedDays,route_id:selected.kind==='record' && $('item-parent').value ? Number($('item-parent').value) : null}});
    if((saved.track.days ?? null)!==requestedDays)throw new Error('天数未保存：请重启后端服务后重试。');
    await refreshLibrary();if(selected?.id===id) await selectItem(id);
    $('edit-dialog').close();notify('修改已保存');
  } catch(error) {$('edit-error').textContent=error.message;$('edit-error').hidden=false;}
  finally {editBusy=false;$('edit-fields').disabled=false;$('close-edit').disabled=false;$('save-item').textContent='保存修改';}
});
$('delete-item').addEventListener('click',async()=>{
  if(!selected || !confirm(`删除“${selected.name}”？${selected.kind==='route'?'关联的徒步记录会保留。':'此操作无法撤销。'}`)) return;
  editBusy=true;$('edit-fields').disabled=true;$('close-edit').disabled=true;
  try {const id=selected.id;await api(`/api/items/${id}`,{method:'DELETE'});if(selected?.id===id){history.replaceState(null,'',selectionUrl(null));showLibrary();}await refreshLibrary();notify('已删除');}
  catch(error){$('edit-error').textContent=error.message;$('edit-error').hidden=false;}
  finally{editBusy=false;$('edit-fields').disabled=false;$('close-edit').disabled=false;}
});
$('export-item').addEventListener('click',()=>{
  if(!route) return;
  const url=URL.createObjectURL(new Blob([exportGPX(route)],{type:'application/gpx+xml'}));
  const link=document.createElement('a');link.href=url;link.download=route.name+'.gpx';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
$('open-share').addEventListener('click',()=>{
  if (!selected) return;
  $('share-name').textContent=selected.name;
  $('share-days').value=7;
  $('share-result').hidden=true;
  $('share-dialog').showModal();
});
$('close-share').addEventListener('click',()=>$('share-dialog').close());
$('create-share').addEventListener('submit',async event=>{
  event.preventDefault();if (!selected) return;
  $('generate-share').disabled=true;
  try {
    const result=await api(`/api/items/${selected.id}/shares`,{method:'POST',body:{expires_in_days:Number($('share-days').value)}});
    $('share-url').value=new URL(result.url,location.origin).href;
    $('share-expiry').textContent=`有效期至 ${new Date(result.expires_at*1000).toLocaleString('zh-CN')}`;
    $('share-result').hidden=false;
    $('share-url').focus();$('share-url').select();
  } catch(error) {notify(error.message);} finally {$('generate-share').disabled=false;}
});
$('copy-share').addEventListener('click',async()=>{
  try {await navigator.clipboard.writeText($('share-url').value);notify('分享链接已复制');}
  catch {$('share-url').focus();$('share-url').select();notify('请复制已选中的分享链接');}
});
$('logout').addEventListener('click',async()=>{try {await api('/api/logout',{method:'POST'});location.replace('/login.html');}catch(error){notify(error.message);}});
const metricRanges={ele:[0,2000,'m'],hr:[80,180,'bpm'],speed:[0,8,'km/h'],slope:[-30,30,'%'],cad:[0,120,'步/分']};
function updateColors() {
  const key=colorMetric, [low,high]=metricRanges[key];
  if(mapReady) map.setPaintProperty('route-line','line-color',['case',['==',['get','value'],null],'#a2a8a3',['interpolate',['linear'],['get','value'],low,'#3e9cdd',(low+high)/2,'#fff386',high,'#ef5549']]);
}
$('color-metrics').addEventListener('click',event=>{
  const node=event.target.closest('button[data-metric]');if(!node)return;
  colorMetric=node.dataset.metric;
  for(const btn of $('color-metrics').querySelectorAll('button')){btn.classList.toggle('active',btn===node);btn.setAttribute('aria-pressed',btn===node);}
  if(mapReady && stats)drawRoute();else updateColors();
});
$('back-library').addEventListener('click',()=>{
  if(history.state?.fromLibrary)history.back();
  else{history.pushState(null,'',selectionUrl(null));showLibrary();}
});
window.addEventListener('popstate',async()=>{
  if(sharePrefix)return;
  const id=Number(new URLSearchParams(location.search).get('item'));
  if(id>0){try{await selectItem(id,{historyUpdate:false});}catch(error){history.replaceState(null,'',selectionUrl(null));showLibrary();notify(error.message);}}
  else showLibrary();
});
$('parent-route').addEventListener('click',()=>{if(selected?.route_id)selectItem(selected.route_id).catch(error=>notify(error.message));});
$('open-import').addEventListener('click',()=>{
  $('import-form').reset();$('import-kind').value=tab;renderParentOptions();
  $('import-parent-field').hidden=tab!=='record';chooseImportFile(null);$('import-dialog').showModal();
});
$('close-import').addEventListener('click',()=>$('import-dialog').close());
for(const layout of ['cards','table'])$(layout+'-view').addEventListener('click',()=>{
  libraryLayout=layout;try {localStorage.setItem('library-layout',layout);}catch {}
  renderLibrary();
});
$('sort').addEventListener('change',()=>{ascending=$('sort').value==='name';renderLibrary();});
$('sort-direction').addEventListener('click',()=>{ascending=!ascending;renderLibrary();});
$('filter-status-all').addEventListener('click', () => {
  filterStatus = 'all';
  updateStatusButtons();
  renderLibrary();
});
$('filter-status-unhiked').addEventListener('click', () => {
  filterStatus = filterStatus === 'unhiked' ? 'all' : 'unhiked';
  updateStatusButtons();
  renderLibrary();
});
$('filter-status-hiked').addEventListener('click', () => {
  filterStatus = filterStatus === 'hiked' ? 'all' : 'hiked';
  updateStatusButtons();
  renderLibrary();
});
$('filter-province').addEventListener('change', () => {
  $('filter-city').value = '';
  $('filter-district').value = '';
  updateLocationOptions();
  renderLibrary();
});
$('filter-city').addEventListener('change', () => {
  const chosenCity = $('filter-city').value;
  if (chosenCity && !$('filter-province').value) {
    const routes = items.filter(item => item.kind === 'route');
    for (const item of routes) {
      if (!item.location) continue;
      const { province, city } = parseLocation(item.location);
      if (city === chosenCity && province) {
        $('filter-province').value = province;
        break;
      }
    }
  }
  $('filter-district').value = '';
  updateLocationOptions();
  renderLibrary();
});
$('filter-district').addEventListener('change', () => {
  const chosenDistrict = $('filter-district').value;
  if (chosenDistrict && (!$('filter-city').value || !$('filter-province').value)) {
    const routes = items.filter(item => item.kind === 'route');
    for (const item of routes) {
      if (!item.location) continue;
      const { province, city, district } = parseLocation(item.location);
      if (district === chosenDistrict) {
        if (province && !$('filter-province').value) $('filter-province').value = province;
        if (city && !$('filter-city').value) $('filter-city').value = city;
        break;
      }
    }
    updateLocationOptions();
  }
  renderLibrary();
});
$('filter-difficulty').addEventListener('change', () => renderLibrary());
$('filter-days-all').addEventListener('click', () => {
  filterDays = 'all';
  updateDaysButtons();
  renderLibrary();
});
$('filter-days-single').addEventListener('click', () => {
  filterDays = filterDays === 'single' ? 'all' : 'single';
  updateDaysButtons();
  renderLibrary();
});
$('filter-days-multi').addEventListener('click', () => {
  filterDays = filterDays === 'multi' ? 'all' : 'multi';
  updateDaysButtons();
  renderLibrary();
});
$('reset-filters').addEventListener('click', () => {
  $('search').value = '';
  $('sort').value = 'default';
  ascending = false;
  filterStatus = 'all';
  updateStatusButtons();
  $('filter-province').value = '';
  $('filter-city').value = '';
  $('filter-district').value = '';
  $('filter-difficulty').value = '';
  filterDays = 'all';
  updateDaysButtons();
  updateLocationOptions();
  renderLibrary();
});
document.addEventListener('click',event=>{if(!$('map-options').contains(event.target))$('map-options').open=false;});
document.addEventListener('keydown',event=>{if(event.key==='Escape' && $('map-options').open){$('map-options').open=false;$('map-options').querySelector('summary').focus();}});
document.querySelector('.more-stats').addEventListener('toggle',()=>{
  if(stats && !$('detail-view').hidden){renderProfile();showPoint(Number($('route-position').value));if(mapReady)map.resize();}
});
let resizeTimer;
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(stats && !$('detail-view').hidden){updateHudMetrics();renderProfile();showPoint(Number($('route-position').value));}fitRoute();},150);});
if (sharePrefix) {
  for (const id of ['library-view','header-library','back-library','open-share','system-settings','logout','open-edit','edit-item','related-records','elevation-actions']) $(id).hidden=true;
  $('header-detail').hidden=false;
  document.querySelector('.photo-upload-label').hidden=true;
  $('photo-file-input').disabled=true;
  $('share-status').hidden=false;
  $('share-status').textContent='正在加载分享…';
  try {
    const data=await api('/api/share');
    $('share-status').textContent=`分享内容 · 只读 · 有效期至 ${new Date(data.expires_at*1000).toLocaleString('zh-CN')}`;
    await selectItem(data.item_id);
  } catch(error) {$('share-status').textContent=error.message;}
} else {
  try {
    await refreshLibrary();
    const id=Number(new URLSearchParams(location.search).get('item'));
    const entry=items.find(item=>item.id===id);
    if(entry) {tab=entry.kind;await selectItem(id);}
  } catch(error){notify(error.message);$('library-list').textContent='加载失败，请刷新重试。';}
}

function updateOverlays() {
  $('labels-toggle').classList.toggle('active',labels);
  $('labels-toggle').setAttribute('aria-pressed',labels);
  if(mapReady) for(const kind of ['satellite','street'])map.setLayoutProperty(kind+'-labels','visibility',labels && mode===kind?'visible':'none');
}
$('labels-toggle').addEventListener('click',()=>{labels=!labels;updateOverlays();});
$('photos-toggle').addEventListener('click', () => {
  showPhotos = !showPhotos;
  $('photos-toggle').classList.toggle('active', showPhotos);
  $('photos-toggle').setAttribute('aria-pressed', showPhotos);
  renderPhotoMarkers();
});

$('photo-file-input').addEventListener('change', e => uploadPhotos(e.target.files));
const photosSection = $('photos-section');
for (const ev of ['dragenter', 'dragover']) {
  photosSection.addEventListener(ev, e => {
    e.preventDefault();
    photosSection.classList.add('drag-over');
  });
}
for (const ev of ['dragleave', 'drop']) {
  photosSection.addEventListener(ev, e => {
    e.preventDefault();
    photosSection.classList.remove('drag-over');
  });
}
photosSection.addEventListener('drop', e => {
  if (e.dataTransfer?.files?.length) {
    uploadPhotos(e.dataTransfer.files);
  }
});
setupPhotoPreview();
$('lightbox-close').addEventListener('click', () => $('photo-lightbox').close());
$('photo-lightbox').addEventListener('close', () => lightboxPlayer?.stop());

function replayClock(ms) {
  const seconds = Math.floor(ms / 1000);
  return `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function createReplay({ getMap, prepareMap, restoreMap, showPoint, resource, notify }) {
  const $ = id => document.getElementById(id);
  let points = [], timeline, photos = [], active = false, playing = false;
  let elapsed = 0, last = null, frameId, marker, camera, pointIndex = -1, photoId;
  let following = true, heading;
  let scrubPointer = null, resumeAfterScrub = false;
  const livePlayer = createLivePlayer($('replay-photo-media'), resource, () => {
    const next = photos[photos.findIndex(photo => photo.id === photoId) + 1];
    return playing && next ? Math.max(0, next.elapsed - elapsed) / Number($('replay-rate').value) / 1000 : Infinity;
  });

  function follow(enabled) {
    following = enabled;
    $('replay-follow').textContent = enabled ? '跟随路线' : '恢复跟随';
    $('replay-follow').setAttribute('aria-pressed', String(enabled));
    if (enabled) {
      const bearing = getMap().getBearing();
      heading = {bearing, from: bearing, target: bearing, sampledAt: -Infinity, startedAt: performance.now()};
    }
  }

  function pause() {
    playing = false; last = null; cancelAnimationFrame(frameId); livePlayer.pause();
    $('replay-play').textContent = elapsed >= (timeline?.duration || Infinity) ? '重新播放' : '继续播放';
    $('replay-play').setAttribute('aria-pressed', 'false');
  }
  function close() {
    scrubPointer = null; resumeAfterScrub = false;
    pause(); livePlayer.stop();
    if (!active) return;
    active = false; marker?.remove(); marker = null;
    document.body.classList.remove('replay-active');
    $('replay-panel').hidden = true; $('replay-photo').hidden = true; $('replay-memories').hidden = true;
    $('replay-open').setAttribute('aria-expanded', 'false');
    updateHudMetrics();
    getMap()?.resize();
    restoreMap();
    getMap()?.jumpTo(camera);
    renderProfile(); showPoint(Number($('route-position').value));
  }
  function render(selectedPhoto) {
    if (!active) return;
    const p = replayFrame(points, timeline, elapsed), map = getMap();
    if (pointIndex !== p.index) { showPoint(p.index); pointIndex = p.index; }
    marker.setLngLat([p.lng, p.lat]);
    if (scrubPointer == null) $('replay-progress').value = elapsed;
    $('replay-progress').setAttribute('aria-valuetext', `${replayClock(elapsed)}，共 ${replayClock(timeline.duration)}`);
    $('replay-time').textContent = `${replayClock(elapsed)} / ${replayClock(timeline.duration)}`;
    $('replay-data').textContent = `${(p.distance / 1000).toFixed(2)} km · 速度 ${p.speed == null ? '—' : p.speed.toFixed(1)} km/h · 海拔 ${p.ele == null ? '—' : Math.round(p.ele)} m${p.hr == null ? '' : ` · 心率 ${p.hr} bpm`}${p.time == null ? '' : ' · ' + new Date(p.time).toLocaleTimeString('zh-CN')}`;
    // Keep the last reached photo visible until the next one, including at high playback speeds.
    const photo = selectedPhoto || photos.findLast(p => p.elapsed <= elapsed);
    const photoWasHidden = $('replay-photo').hidden;
    $('replay-photo').hidden = !photo;
    const photoChanged = (photo?.id ?? null) !== photoId;
    if (photo && (photoChanged || selectedPhoto)) {
      $('replay-photo-image').src = resource(`/api/photos/${photo.id}?v=3`);
      $('replay-photo-image').alt = photo.filename;
      $('replay-photo-caption').textContent = `${photo.filename}${photo.taken_at ? ' · ' + new Date(photo.taken_at).toLocaleString('zh-CN') : ''}`;
    }
    if (photoChanged || selectedPhoto) {
      photoId = photo?.id ?? null;
      livePlayer.show(photo);
      for (const button of $('replay-photos').children) button.setAttribute('aria-current', String(button.dataset.photoId === String(photoId)));
    }
    if (photoWasHidden !== !photo) map.resize();
    if (following) map.jumpTo({ center: [p.lng, p.lat],
      bearing: replayCameraBearing(heading, points, p.index, performance.now()) });
  }
  function tick(now) {
    if (!playing) return;
    elapsed = last == null ? elapsed : Math.min(timeline.duration, elapsed + (now - last) * Number($('replay-rate').value));
    last = now; render();
    if (elapsed >= timeline.duration && !livePlayer.busy) pause(); else frameId = requestAnimationFrame(tick);
  }
  function play() {
    if (elapsed >= timeline.duration && !livePlayer.busy) { elapsed = 0; photoId = undefined; }
    playing = true; last = null;
    livePlayer.resume();
    $('replay-play').textContent = '暂停'; $('replay-play').setAttribute('aria-pressed', 'true');
    render();
    frameId = requestAnimationFrame(tick);
  }
  $('replay-open').addEventListener('click', () => {
    if (active) { close(); return; }
    const map = getMap();
    if (!map || points.length < 2) { notify('请等待地图和轨迹加载完成后再回放。'); return; }
    camera = { center: map.getCenter(), zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() };
    prepareMap(); map.stop();
    active = true; elapsed = 0; pointIndex = -1; photoId = undefined;
    document.body.classList.add('replay-active');
    $('map-options').open = false;
    map.jumpTo({zoom: 16, pitch: 65, bearing: replayHeading(points, 0) ?? camera.bearing});
    follow(true);
    $('replay-panel').hidden = false;
    $('replay-memories').hidden = !photos.length;
    updateHudMetrics();
    map.resize();
    $('replay-open').setAttribute('aria-expanded', 'true');
    const el = document.createElement('div'); el.className = 'replay-walker'; el.textContent = '●'; el.setAttribute('aria-label', '当前徒步位置');
    marker = new maplibregl.Marker({ element: el }).setLngLat([points[0].lng, points[0].lat]).addTo(map);
    play(); $('replay-play').focus();
  });
  $('replay-play').addEventListener('click', () => playing ? pause() : play());
  $('replay-follow').addEventListener('click', () => { follow(!following); render(); });
  // Yield before map gestures so playback never overwrites a manual camera adjustment.
  for (const event of ['pointerdown', 'wheel', 'dblclick']) $('map').addEventListener(event, () => {
    if (active) follow(false);
  }, {capture: true, passive: true});
  $('map').addEventListener('keydown', e => {
    if (active && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '-', '='].includes(e.key)) follow(false);
  }, true);
  $('replay-close').addEventListener('click', () => { close(); $('replay-open').focus(); });
  function seek(value) {
    livePlayer.stop(); photoId = undefined;
    elapsed = value; last = null; render();
    if (elapsed >= timeline.duration) pause();
  }
  function dragProgress(e) {
    if (e.pointerId !== scrubPointer) return;
    const slider = $('replay-progress'), bounds = slider.getBoundingClientRect();
    slider.value = Math.round(Math.max(0, Math.min(1, (e.clientX - bounds.left) / bounds.width)) * timeline.duration);
    seek(Number(slider.value));
  }
  // Capture the gesture so dragging works from anywhere on the track, including outside its bounds.
  $('replay-progress').addEventListener('pointerdown', e => {
    if (!active || e.button !== 0 || !e.isPrimary) return;
    e.preventDefault();
    scrubPointer = e.pointerId; resumeAfterScrub = playing;
    pause();
    e.currentTarget.setPointerCapture(e.pointerId);
    e.currentTarget.focus({preventScroll: true});
    dragProgress(e);
  });
  $('replay-progress').addEventListener('pointermove', dragProgress);
  for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) window.addEventListener(event, e => {
    if (e.pointerId !== scrubPointer) return;
    scrubPointer = null;
    const resume = resumeAfterScrub; resumeAfterScrub = false;
    if (resume && active && !document.hidden && elapsed < timeline.duration) play();
  });
  $('replay-progress').addEventListener('input', e => seek(Number(e.target.value)));
  $('replay-rate').addEventListener('change', () => { last = null; });
  document.addEventListener('visibilitychange', () => { if (document.hidden && active) { resumeAfterScrub = false; pause(); } });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && active && !$('photo-lightbox').open) { close(); $('replay-open').focus(); } });
  return {
    close, pause,
    get active() { return active; },
    setRoute(next) {
      close(); points = next; timeline = replayTimeline(points); elapsed = 0;
      $('replay-progress').max = timeline.duration;
      $('replay-help-title').textContent = timeline.estimated ? '含估算时间' : '回放说明';
      $('replay-note').textContent = timeline.estimated ? '部分时间缺失或异常：该部分按 4 km/h 估算，速度 — 表示未知。分段间跳转。' : '按记录时间还原快慢与停留，分段间跳转。速度为轨迹点间均速。';
      this.setPhotos([]);
    },
    setPhotos(next) {
      if (!timeline) return;
      livePlayer.stop(); photos = replayPhotos(next, timeline); photoId = null;
      $('replay-memories').hidden = !active || !photos.length;
      $('replay-photos').replaceChildren(...photos.map(photo => {
        const button = document.createElement('button'); button.type = 'button'; button.dataset.photoId = photo.id;
        button.title = `${replayClock(photo.elapsed)} · ${photo.filename}`; button.setAttribute('aria-label', `跳到照片：${photo.filename}`);
        const img = document.createElement('img'); img.src = resource(`/api/photos/${photo.id}?v=3`); img.alt = photo.filename; img.loading = 'lazy';
        button.append(img); button.addEventListener('click', () => { pause(); elapsed = photo.elapsed; last = null; render(photo); });
        return button;
      }));
      $('replay-photo-note').textContent = `${photos.length} 张`;
      $('replay-photo-note').title = next.length ? `${photos.length} 张沿途照片${next.length > photos.length ? `，${next.length - photos.length} 张无匹配位置` : ''}` : '暂无沿途照片';
      if (active) getMap()?.resize();
      render();
    }
  };
}
