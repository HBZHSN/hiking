"""Automatic location identification for route and record starting points."""
import json
import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)


def extract_start_point(track):
    """Return (lng, lat) of the starting point of the track, or None."""
    if not isinstance(track, dict):
        return None
    segments = track.get('segments')
    if not isinstance(segments, list) or not segments:
        return None
    seg = segments[0]
    if not isinstance(seg, list) or not seg:
        return None
    p = seg[0]
    if isinstance(p, dict) and 'lng' in p and 'lat' in p:
        lng, lat = p['lng'], p['lat']
        if isinstance(lng, (int, float)) and isinstance(lat, (int, float)):
            return float(lng), float(lat)
    return None


def format_location_component(comp):
    """Format addressComponent from Tianditu into 省市区 representation."""
    if not isinstance(comp, dict):
        return ''
    province = (comp.get('province') or '').strip()
    city = (comp.get('city') or '').strip()
    county = (comp.get('county') or '').strip()
    nation = (comp.get('nation') or '').strip()

    # Normalize municipalities (e.g. 北京市, 上海市, 天津市, 重庆市)
    if province in ('北京市', '上海市', '天津市', '重庆市'):
        city = ''
    elif '直辖' in city and ('省' in city or '县' in city):
        city = ''

    parts = []
    if province:
        parts.append(province)
    if city and city != province and city not in parts:
        parts.append(city)
    if county and county != city and county not in parts:
        parts.append(county)

    if parts:
        return ''.join(parts)

    address = (comp.get('address') or comp.get('poi') or '').strip()
    if nation and nation != '中国':
        return f"{nation} {address}".strip() if address else nation
    return address or (comp.get('formatted_address') or '').strip()


def geocode_tianditu(lng, lat, tianditu_key, remote_cache=None):
    """Query Tianditu Geocoding API for coordinates."""
    if not tianditu_key:
        return ''
    post_str = json.dumps({'lon': round(lng, 5), 'lat': round(lat, 5), 'ver': 1})
    url = f'https://api.tianditu.gov.cn/geocoder?postStr={quote(post_str)}&type=geocode&tk={quote(tianditu_key, safe="")}'
    try:
        if remote_cache:
            body, _ = remote_cache(url, 'application/json')
        else:
            import urllib.request
            req = urllib.request.Request(url, headers={'User-Agent': 'hiking-geocoder/1.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read()
        data = json.loads(body.decode('utf-8'))
        if data.get('status') == '0' and 'result' in data:
            result = data['result']
            comp = result.get('addressComponent') or {}
            formatted = format_location_component(comp)
            if formatted:
                return formatted
    except Exception as exc:
        logger.debug('Tianditu geocode failed: %s', exc)
    return ''


def geocode_nominatim(lng, lat, remote_cache=None):
    """Fallback to OpenStreetMap Nominatim for overseas or unkeyed requests."""
    url = f'https://nominatim.openstreetmap.org/reverse?format=json&lat={round(lat, 5)}&lon={round(lng, 5)}&accept-language=zh'
    try:
        if remote_cache:
            body, _ = remote_cache(url, 'application/json')
        else:
            import urllib.request
            req = urllib.request.Request(url, headers={'User-Agent': 'hiking-geocoder/1.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read()
        data = json.loads(body.decode('utf-8'))
        addr = data.get('address', {})
        province = addr.get('province') or addr.get('state') or ''
        city = addr.get('city') or addr.get('city_district') or addr.get('municipality') or ''
        county = addr.get('county') or addr.get('district') or addr.get('suburb') or ''
        parts = [p for p in (province, city, county) if p]
        if parts:
            return ''.join(parts)
        return data.get('display_name', '')
    except Exception as exc:
        logger.debug('Nominatim geocode failed: %s', exc)
    return ''


def geocode(db, lng, lat, tianditu_key='', remote_cache=None):
    """Resolve coordinates to location, using database and remote cache."""
    cache_key = f"{round(lng, 4)},{round(lat, 4)}"
    if db is not None:
        try:
            row = db.execute('SELECT location FROM geocache WHERE key=?', (cache_key,)).fetchone()
            if row and row['location']:
                return row['location']
        except Exception:
            pass

    loc = ''
    if tianditu_key:
        loc = geocode_tianditu(lng, lat, tianditu_key, remote_cache)
    if not loc:
        loc = geocode_nominatim(lng, lat, remote_cache)

    if loc and db is not None:
        try:
            db.execute('INSERT OR REPLACE INTO geocache(key, location) VALUES(?, ?)', (cache_key, loc))
        except Exception:
            pass
    return loc


def get_item_location(db, item_id, track, tianditu_key='', remote_cache=None):
    """Get location for an item, checking item_locations cache first."""
    if db is not None and item_id is not None:
        try:
            row = db.execute('SELECT location FROM item_locations WHERE item_id=?', (item_id,)).fetchone()
            if row and row['location']:
                return row['location']
        except Exception:
            pass

    pt = extract_start_point(track)
    if not pt:
        return ''
    lng, lat = pt
    loc = geocode(db, lng, lat, tianditu_key, remote_cache)
    if loc and db is not None and item_id is not None:
        try:
            db.execute('INSERT OR REPLACE INTO item_locations(item_id, location) VALUES(?, ?)', (item_id, loc))
        except Exception:
            pass
    return loc
