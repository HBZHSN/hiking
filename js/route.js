import { toWgs } from './coordinates.js';

export const MAX_POINTS = 50000;
const finite = value => typeof value === 'number' && Number.isFinite(value);
export const numberOrNull = value => value == null || String(value).trim() === '' ? null : (Number.isFinite(Number(value)) ? Number(value) : null);

export function normalizeRoute(raw, system = 'WGS84') {
  let count = 0;
  const segments = raw.segments.filter(s => s.length).map(segment => {
    if (segment.length < 2) throw new Error('每个路线分段至少需要两个轨迹点。');
    return segment.map(point => {
      if (++count > MAX_POINTS) throw new Error('最多支持 50,000 个轨迹点，请先简化路线。');
      const [lng, lat, elevation] = point;
      if (!finite(lng) || !finite(lat) || Math.abs(lng) > 180 || Math.abs(lat) > 85) {
        throw new Error('坐标无效：请使用 [经度, 纬度]，纬度范围为 -85°～85°。不支持米制投影坐标。');
      }
      if (elevation != null && (!finite(elevation) || elevation < -12000 || elevation > 100000)) {
        throw new Error('海拔无效：海拔必须是以米为单位的有限数值。');
      }
      const pos = toWgs([lng, lat], system);
      return { lng: pos[0], lat: pos[1], ele: elevation ?? null, time: point[3] ?? null, hr: numberOrNull(point[4]), cad: numberOrNull(point[5]) };
    });
  });
  if (!segments.length) throw new Error('文件中没有可用的线路。');
  return { name: String(raw.name || '导入的徒步线路').slice(0,160), segments, system };
}

export function parseGeoJSON(text) {
  const json = JSON.parse(text);
  if (json.crs && !/CRS84|4326/.test(JSON.stringify(json.crs))) throw new Error('请先将投影坐标转换为经纬度后再导入。');
  const segments = [];
  let name = json.name;
  function read(value) {
    if (!value || typeof value !== 'object') throw new Error('GeoJSON 结构无效。');
    if (value.type === 'FeatureCollection') value.features.forEach(read);
    else if (value.type === 'Feature') {
      name ||= value.properties?.name;
      if (value.geometry) read(value.geometry);
    } else if (value.type === 'GeometryCollection') value.geometries.forEach(read);
    else if (value.type === 'LineString') segments.push(value.coordinates);
    else if (value.type === 'MultiLineString') segments.push(...value.coordinates);
  }
  read(json);
  return { name, segments };
}

export function parseGPX(text) {
  if (/<!DOCTYPE|<!ENTITY/i.test(text)) throw new Error('不支持带 DTD 或外部实体的 GPX。');
  const doc = new DOMParser().parseFromString(text, 'application/xml');
  if (doc.getElementsByTagName('parsererror').length || doc.documentElement.localName !== 'gpx') throw new Error('GPX XML 格式无效。');
  const children = (node, tag) => [...node.children].filter(e => e.localName === tag);
  const childText = (node, tag) => children(node,tag)[0]?.textContent;
  const tracks = [...doc.getElementsByTagNameNS('*', 'trk')];
  const routes = [...doc.getElementsByTagNameNS('*', 'rte')];
  const groups = tracks.length ? tracks.flatMap(t => children(t,'trkseg').map(s => children(s,'trkpt'))) : routes.map(r => children(r,'rtept'));
  return {
    name: childText(tracks[0] || routes[0] || doc.documentElement, 'name'),
    segments: groups.map(points => points.map(p => {
      const elevationText = childText(p,'ele');
      const ele = numberOrNull(elevationText);
      if (elevationText?.trim() && ele === null) throw new Error('GPX 中存在无效海拔。');
      const time = Date.parse(childText(p,'time'));
      return [numberOrNull(p.getAttribute('lon')), numberOrNull(p.getAttribute('lat')), ele, Number.isFinite(time) ? time : null, numberOrNull(p.getElementsByTagNameNS('*','hr')[0]?.textContent), numberOrNull(p.getElementsByTagNameNS('*','cad')[0]?.textContent)];
    }))
  };
}

export function distance(a,b) {
  const rad = Math.PI/180;
  const h = Math.sin((b.lat-a.lat)*rad/2)**2 + Math.cos(a.lat*rad)*Math.cos(b.lat*rad)*Math.sin((b.lng-a.lng)*rad/2)**2;
  return 6371008.8*2*Math.asin(Math.sqrt(Math.min(1,h)));
}

function elevationGain(points) {
  // Keep the 100 m smoothing window and 10 m threshold in sync with backend/metrics.py.
  let ascent=0, descent=0, start=0;
  while (start < points.length) {
    if (points[start].ele == null) { start++; continue; }
    let end=start+1;
    while (end < points.length && points[end].ele != null && !points[end].start) end++;
    let left=start, right=start, heightSum=0, baseline=points[start].ele;
    for (let i=start; i<end; i++) {
      const p=points[i];
      while (right < end && points[right].distance <= p.distance+50) heightSum+=points[right++].ele;
      while (points[left].distance < p.distance-50) heightSum-=points[left++].ele;
      const smoothed=i===start || i===end-1 ? p.ele : heightSum/(right-left);
      const rise=smoothed-baseline;
      if (Math.abs(rise)>=10 || i===end-1) {
        ascent+=Math.max(0,rise); descent+=Math.max(0,-rise);
        baseline=smoothed;
      }
    }
    start=end;
  }
  return {ascent,descent};
}

export function analyze(route) {
  let moving = 0;
  let total = 0, timed = 0, timedDistance = 0;
  let elevationEdges = 0, edges = 0, known = 0, min = Infinity, max = -Infinity;
  const points = [];
  route.segments.forEach((segment, segmentIndex) => segment.forEach((p,i) => {
    let slope = null, speed = null;
    if (i) {
      const prev = segment[i-1];
      const d = distance(prev,p);
      total += d;
      edges++;
      if (p.ele != null && prev.ele != null) {
        const rise = p.ele-prev.ele;
        elevationEdges++;
        if (d >= 1) slope = rise/d*100;
      }
      if (finite(p.time) && finite(prev.time) && p.time > prev.time) {
        timed += p.time-prev.time; timedDistance += d;
        speed = d / (p.time-prev.time) * 3600;
        if (speed >= 1) moving += p.time-prev.time;
      }
    }
    if (p.ele != null) { min = Math.min(min,p.ele); max = Math.max(max,p.ele); known++; }
    points.push({...p, distance:total, slope, speed, segment:segmentIndex, start:i === 0});
  }));
  const {ascent,descent}=elevationGain(points);
  const heart = points.map(p => p.hr).filter(v => finite(v) && v > 0);
  const cadence = points.map(p => p.cad).filter(v => finite(v) && v >= 0);
  return { points, moving, hrMean:heart.length ? heart.reduce((a,b)=>a+b,0)/heart.length : null, hrMax:heart.length ? Math.max(...heart) : null, cadMean:cadence.length ? cadence.reduce((a,b)=>a+b,0)/cadence.length : null, distance:total, ascent:elevationEdges ? ascent : null, descent:elevationEdges ? descent : null,
    min:known ? min : null, max:known ? max : null, known, count:points.length,
    partialElevation:elevationEdges < edges, duration:timed || null,
    speed:timed ? timedDistance/(timed/3600000)/1000 : null };
}

export function routeGeoJSON(route) {
  return { type:'Feature', properties:{name:route.name}, geometry:{type:'MultiLineString',
    coordinates:route.segments.map(s => s.map(p => [p.lng,p.lat]))} };
}

export function coloredGeoJSON(stats, key) {
  const features=[];
  for(let i=1;i<stats.points.length;i++) {
    const p=stats.points[i], prev=stats.points[i-1];
    if(p.start) continue;
    features.push({type:'Feature',properties:{value:p[key] ?? null},geometry:{type:'LineString',coordinates:[[prev.lng,prev.lat],[p.lng,p.lat]]}});
  }
  return {type:'FeatureCollection',features};
}

export function exportGPX(route) {
  const escape = value => String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
  return '<?xml version="1.0" encoding="UTF-8"?>'+
    '<gpx version="1.1" creator="Hiking" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1"><trk><name>'+escape(route.name)+'</name>'+route.segments.map(segment=>'<trkseg>'+segment.map(p=>
    `<trkpt lat="${p.lat}" lon="${p.lng}">${p.ele == null ? '' : `<ele>${p.ele}</ele>`}${p.time == null ? '' : `<time>${new Date(p.time).toISOString()}</time>`}<extensions><gpxtpx:TrackPointExtension>${p.hr == null ? '' : `<gpxtpx:hr>${p.hr}</gpxtpx:hr>`}${p.cad == null ? '' : `<gpxtpx:cad>${p.cad}</gpxtpx:cad>`}</gpxtpx:TrackPointExtension></extensions></trkpt>`).join('')+'</trkseg>').join('')+'</trk></gpx>';
}

export function difficulty(distance, ascent, days) {
  if (![distance, ascent, days].every(Number.isFinite) || distance < 0 || ascent < 0 || days < 1) return null;
  const distanceKm = distance / 1000;
  return Math.sqrt(
    (distanceKm * 0.621371 / days) *
    (ascent * 3.28084 / days) * 2
  ) *
  (1 + Math.log(days) * 0.2) *
  (1 + Math.floor((ascent / days) / 500) * 0.15);
}
export function difficultyLevel(score) {
  if (score == null) return '难度暂缺';
  if (score === 0) return '⚫ 无效路线';
  if (score <= 100) return '⚪ 极简';
  if (score <= 200) return '🟢 简单';
  if (score <= 300) return '🟡 一般';
  if (score <= 400) return '🟠 困难';
  return '🔴 极难';
}
export function routeDays(route) {
  if (route.days) return {days:route.days, source:'手动设置'};
  const times=route.segments.flat().map(p=>p.time).filter(Number.isFinite);
  return times.length ? {days:Math.max(1,Math.ceil((Math.max(...times)-Math.min(...times))/86400000)),source:'按起止时间估算'} : {days:1,source:'无时间数据，按 1 天'};
}
function compareRecordDefault(a, b, ascending) {
  const da = a.summary?.date, db = b.summary?.date;
  if (da == null || db == null) return da == null ? (db == null ? b.id - a.id : 1) : -1;
  const comparison = da - db;
  return (ascending ? comparison : -comparison) || b.id - a.id;
}

function compareRouteDefault(a, b, hikeCounts, ascending) {
  const countA = a.hikeCount ?? hikeCounts?.get(a.id) ?? 0;
  const countB = b.hikeCount ?? hikeCounts?.get(b.id) ?? 0;

  const unhikedA = countA === 0 ? 1 : 0;
  const unhikedB = countB === 0 ? 1 : 0;
  if (unhikedA !== unhikedB) {
    const comparison = unhikedA - unhikedB;
    return (ascending ? comparison : -comparison) || b.id - a.id;
  }

  if (countA !== countB) {
    const comparison = countA - countB;
    return (ascending ? comparison : -comparison) || b.id - a.id;
  }

  const diffA = a.summary?.difficulty;
  const diffB = b.summary?.difficulty;
  if (diffA == null || diffB == null) {
    if (diffA == null && diffB == null) return b.id - a.id;
    return diffA == null ? 1 : -1;
  }
  const comparison = diffA - diffB;
  return (ascending ? comparison : -comparison) || b.id - a.id;
}

export function parseLocation(location) {
  if (!location || typeof location !== 'string') return { province: '', city: '', district: '' };
  const loc = location.trim();
  if (!loc) return { province: '', city: '', district: '' };

  const munMatch = loc.match(/^(北京市|上海市|天津市|重庆市)(.*)/);
  if (munMatch) {
    const province = munMatch[1];
    const city = munMatch[1];
    let district = munMatch[2].trim();
    const dMatch = district.match(/^(.*?(?:区|县|市|旗|特区|林区))(.*)$/);
    if (dMatch) district = dMatch[1];
    return { province, city, district };
  }

  const pMatch = loc.match(/^(.*?(?:省|自治区|特别行政区))(.*)$/);
  let province = '', rest = loc;
  if (pMatch) {
    province = pMatch[1];
    rest = pMatch[2];
  }

  const cMatch = rest.match(/^(.*?(?:市|自治州|地区|盟))(.*)$/);
  let city = '', district = '';
  if (cMatch) {
    city = cMatch[1];
    district = cMatch[2].trim();
  } else if (!province) {
    province = loc;
  } else {
    district = rest.trim();
  }

  if (district) {
    const dMatch = district.match(/^(.*?(?:区|县|市|旗|特区|林区))(.*)$/);
    if (dMatch) district = dMatch[1];
  }

  return { province, city, district };
}

export function filterLibrary(items, {
  kind,
  query = '',
  sort = 'default',
  ascending = false,
  hikeStatus = 'all',
  province = '',
  city = '',
  district = '',
  difficulty = '',
  days = 'all'
} = {}) {
  const hikeCounts = new Map();
  for (const item of items) {
    if (item.kind === 'record' && item.route_id != null) {
      hikeCounts.set(item.route_id, (hikeCounts.get(item.route_id) || 0) + 1);
    }
  }
  const result = items.filter(item => {
    if (item.kind !== kind) return false;

    if (query.trim()) {
      const q = query.trim().toLowerCase();
      if (!`${item.name} ${item.notes} ${item.location || ''}`.toLowerCase().includes(q)) {
        return false;
      }
    }

    if (hikeStatus && hikeStatus !== 'all') {
      const count = item.hikeCount ?? hikeCounts.get(item.id) ?? 0;
      if (hikeStatus === 'unhiked' && count !== 0) return false;
      if (hikeStatus === 'hiked' && count === 0) return false;
    }

    if (province || city || district) {
      if (!item.location) return false;
      const parsed = parseLocation(item.location);
      if (province && parsed.province !== province && !item.location.includes(province)) return false;
      if (city && parsed.city !== city && !item.location.includes(city)) return false;
      if (district && parsed.district !== district && !item.location.includes(district)) return false;
    }

    if (difficulty && difficulty !== 'all') {
      const score = item.summary?.difficulty;
      if (score == null) return false;
      if (difficulty === 'easy' && score > 200) return false;
      if (difficulty === 'moderate' && (score <= 200 || score > 300)) return false;
      if (difficulty === 'hard' && score <= 300) return false;
    }

    if (days && days !== 'all') {
      const d = item.summary?.days;
      if (d == null) return false;
      if (days === 'single' && d !== 1) return false;
      if (days === 'multi' && d <= 1) return false;
    }

    return true;
  });

  return result.sort((a,b)=>{
    if (sort === 'default') {
      if (kind === 'record') return compareRecordDefault(a, b, ascending);
      if (kind === 'route') return compareRouteDefault(a, b, hikeCounts, ascending);
      return b.id - a.id;
    }
    const av=sort==='newest'?a.id:sort==='name'?a.name:a.summary[sort];
    const bv=sort==='newest'?b.id:sort==='name'?b.name:b.summary[sort];
    if(av==null || bv==null) return av==null ? (bv==null?b.id-a.id:1) : -1;
    const comparison=typeof av==='string'?av.localeCompare(bv,'zh-CN'):av-bv;
    return (ascending?comparison:-comparison) || b.id-a.id;
  });
}
// Mercator principal axis fits long routes to the long side of the viewport.
export function routeView(points, width, height, terrain) {
  const origin=points[0].lng;
  const coords=points.map(p=>[origin+((p.lng-origin+540)%360)-180,p.lat]);
  const xy=coords.map(([lng,lat])=>[lng*Math.PI/180,Math.log(Math.tan(Math.PI/4+lat*Math.PI/360))]);
  const mean=xy.reduce((s,p)=>[s[0]+p[0]/xy.length,s[1]+p[1]/xy.length],[0,0]);
  let xx=0, yy=0, cross=0;
  for(const [x,y] of xy) {xx+=(x-mean[0])**2;yy+=(y-mean[1])**2;cross+=(x-mean[0])*(y-mean[1]);}
  const angle=Math.atan2(2*cross,xx-yy)/2*180/Math.PI;
  let bearing=(width>=height ? -angle : 90-angle);
  if(bearing>90) bearing-=180;
  if(bearing< -90) bearing+=180;
  if(Math.abs(xx-yy)+Math.abs(cross)<1e-12) bearing=0;
  const lngs=coords.map(p=>p[0]), lats=coords.map(p=>p[1]);
  return {coords,bounds:[[Math.min(...lngs),Math.min(...lats)],[Math.max(...lngs),Math.max(...lats)]],bearing,pitch:terrain?35:0};
}

export function routeCamera(points, width, height, terrain, padding = {top:35,bottom:90,left:30,right:55}) {
  if (!points?.length) return {center:[104,35],zoom:3,bearing:0,pitch:0};
  const view = routeView(points, width, height, terrain);
  if (points.length === 1) {
    return {center:[view.coords[0][0],view.coords[0][1]],zoom:15,bearing:0,pitch:view.pitch,bounds:view.bounds};
  }
  // Strictly north-up (上北下南): bearing is always 0.
  const pitch = view.pitch;
  const mercator = view.coords.map(([lng, lat]) => {
    const x = (lng + 180) / 360;
    const sinLat = Math.sin(lat * Math.PI / 180);
    const safeSin = Math.max(-0.9999, Math.min(0.9999, sinLat));
    const y = 0.5 - Math.log((1 + safeSin) / (1 - safeSin)) / (4 * Math.PI);
    return [x, y];
  });
  const xs = mercator.map(p => p[0]), ys = mercator.map(p => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const spanX = Math.max(1e-9, maxX - minX), spanY = Math.max(1e-9, maxY - minY);
  const midX = (minX + maxX) / 2, midY = (minY + maxY) / 2;

  const w = width || 800, h = height || 600;
  const targetW = w - padding.left - padding.right;
  const targetH = h - padding.top - padding.bottom;
  const usableW = Math.max(40, targetW - 30);
  const usableH = Math.max(40, targetH - 30);

  const pitchFactor = pitch ? Math.cos(pitch * Math.PI / 180) : 1;
  const zoomX = Math.log2(usableW / (256 * spanX));
  const zoomY = Math.log2((usableH * pitchFactor) / (256 * spanY));
  const zoom = Math.max(1, Math.min(18, Math.min(zoomX, zoomY)));

  const screenDx = (padding.left - padding.right) / 2;
  const screenDy = (padding.top - padding.bottom) / 2;
  const scale = 256 * Math.pow(2, zoom);
  const camX = midX - screenDx / scale;
  const camY = midY - screenDy / scale;

  const centerLng = camX * 360 - 180;
  const safeY = Math.max(0, Math.min(1, camY));
  const centerLat = (2 * Math.atan(Math.exp(Math.PI * (1 - 2 * safeY))) - Math.PI / 2) * 180 / Math.PI;

  return {center:[centerLng, centerLat], zoom, bearing:0, pitch, bounds:view.bounds};
}

export function findMaxMetricPoint(points, key) {
  if (!points || !points.length) return null;
  let maxVal = -Infinity;
  let bestPoint = null;
  let bestIndex = -1;
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const val = p[key];
    if (val != null && Number.isFinite(val)) {
      if (val > maxVal) {
        maxVal = val;
        bestPoint = p;
        bestIndex = i;
      }
    }
  }
  return bestIndex !== -1 ? { point: bestPoint, index: bestIndex, value: maxVal } : null;
}

// Preserve recorded intervals, including stops; estimate only missing/invalid intervals at 4 km/h.
export function replayTimeline(points) {
  const times = [0];
  let estimated = false;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i];
    let dt = b.time - a.time;
    if (!Number.isFinite(a.time) || !Number.isFinite(b.time) || dt <= 0) {
      estimated = true;
      dt = b.start ? 1000 : Math.max(1000, (b.distance - a.distance) * 900);
    }
    times.push(times[i - 1] + dt);
  }
  return { times, duration: times.at(-1), estimated };
}

export function replayFrame(points, timeline, elapsed) {
  const times = timeline.times;
  elapsed = Math.max(0, Math.min(timeline.duration, elapsed));
  let lo = 0, hi = points.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (times[mid] <= elapsed) lo = mid; else hi = mid - 1;
  }
  const a = points[lo], b = points[Math.min(lo + 1, points.length - 1)];
  const fraction = b === a || b.start ? 0 : (elapsed - times[lo]) / (times[lo + 1] - times[lo]);
  const mix = key => Number.isFinite(a[key]) && Number.isFinite(b[key]) ? a[key] + (b[key] - a[key]) * fraction : a[key] ?? null;
  const deltaLng = ((b.lng - a.lng + 540) % 360) - 180;
  const rad = Math.PI / 180;
  const y = Math.sin(deltaLng * rad) * Math.cos(b.lat * rad);
  const x = Math.cos(a.lat * rad) * Math.sin(b.lat * rad) - Math.sin(a.lat * rad) * Math.cos(b.lat * rad) * Math.cos(deltaLng * rad);
  return { index: lo, lng: a.lng + deltaLng * fraction, lat: mix('lat'), ele: mix('ele'),
    distance: mix('distance'), time: timeline.estimated ? null : mix('time'), hr: a.hr,
    speed: b === a ? a.speed : b.start ? null : b.speed,
    bearing: b.start || (x === 0 && y === 0) ? null : Math.atan2(y, x) / rad };
}

export function replayPhotos(photos, timeline) {
  return photos.filter(p => Number.isInteger(p.point_index) && p.point_index >= 0 && p.point_index < timeline.times.length)
    .map(p => ({ ...p, elapsed: timeline.times[p.point_index] })).sort((a, b) => a.elapsed - b.elapsed || a.id - b.id);
}

export function replayHeading(points, index) {
  const p = points[index];
  let left = index, right = index;
  // Use a stretch of trail, rather than each GPS edge; never look across a track gap.
  while (left > 0 && !points[left].start && p.distance - points[left].distance < 50) left--;
  while (right < points.length - 1 && !points[right + 1].start && points[right].distance - p.distance < 150) right++;
  const a = points[left], b = points[right], rad = Math.PI / 180;
  if (distance(a, b) < 10) return null;
  const lng = (b.lng - a.lng) * rad;
  return Math.atan2(Math.sin(lng) * Math.cos(b.lat * rad),
    Math.cos(a.lat * rad) * Math.sin(b.lat * rad) - Math.sin(a.lat * rad) * Math.cos(b.lat * rad) * Math.cos(lng)) / rad;
}

export function replayCameraBearing(state, points, index, now) {
  if (now - state.sampledAt >= 4000) {
    state.sampledAt = now;
    const heading = replayHeading(points, index);
    const delta = ((heading - state.bearing + 540) % 360) - 180;
    if (heading != null && Math.abs(delta) >= 25) {
      state.from = state.bearing;
      state.target = state.bearing + delta;
      state.startedAt = now;
    }
  }
  const t = Math.max(0, Math.min(1, (now - state.startedAt) / 2000));
  state.bearing = state.from + (state.target - state.from) * t * t * (3 - 2 * t);
  return state.bearing;
}
