import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import time
import tempfile
import urllib.error
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit, quote, parse_qs
from wsgiref.simple_server import WSGIServer, make_server

from backend.thumbnail import thumbnail
from backend.metrics import summarize
from backend.storage import connect, ensure_route_for_record, initialize, item, photo_item, save
from backend.shortcut_upload import import_gpx, prepare_photo
from backend import photo_sync, live_photos

ROOT = Path(__file__).resolve().parent.parent
LIMIT = 20 * 1024 * 1024


def config():
    values = {}
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, sep, value = line.partition('=')
                if sep:
                    values[key.strip()] = value.strip().strip('\"\'')
    values.update(os.environ)
    if len(values.get('HIKING_PASSWORD', '')) < 6 or values['HIKING_PASSWORD'] == 'replace-with-a-long-private-password':
        raise RuntimeError('请在 .env 中设置至少 6 位的 HIKING_PASSWORD')
    values['PHOTO_SOURCE'] = values.get('PHOTO_SOURCE') or str(ROOT / photo_sync.DEFAULT_SOURCE)
    return values


class App:
    def __init__(self, database, password, secure=False, tianditu_key="", photo_source=None):
        self.tianditu_key = tianditu_key
        self.photo_source = (Path(photo_source) if photo_source is not None
                             else Path(database).parent / 'photo-source').expanduser().resolve()
        self.database = database
        self.password = hashlib.sha256(password.encode()).digest()
        self.secure = secure
        initialize(database)
        self.album_index = photo_sync.AlbumIndex(database)

    def resolve_item_location(self, db, item_id, track):
        if not self.tianditu_key:
            return ''
        try:
            from backend.geocoder import get_item_location
            return get_item_location(db, item_id, track, self.tianditu_key, self.remote_cache)
        except Exception:
            return ''

    def remote_cache(self, url, content_type='application/octet-stream'):
        """Fetch an external resource directly once, then serve the disk copy."""
        cache_dir = Path(self.database).parent / 'remote-cache'
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode()).hexdigest()
        body_path, type_path = cache_dir / digest, cache_dir / f'{digest}.type'
        if body_path.is_file():
            return body_path.read_bytes(), type_path.read_text() if type_path.exists() else content_type
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url, headers={'User-Agent': 'hiking-map-cache/1.0'})
        with opener.open(request, timeout=25) as response:
            body = response.read()
            kind = response.headers.get_content_type() or content_type
        with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as temporary:
            temporary.write(body)
        try:
            os.replace(temporary.name, body_path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
        type_path.write_text(kind)
        return body, kind

    def tile(self, path):
        match = re.fullmatch(r'/api/tiles/(tianditu|dem)/([^/]+)/(\d+)/(\d+)/(\d+)', path)
        if not match:
            return None
        kind, layer, zoom, x, y = match.groups()
        if kind == 'dem':
            if layer != 'terrarium':
                return 404, {'error': '未知地形图层'}, []
            url, content_type = f'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{zoom}/{x}/{y}.png', 'image/png'
        else:
            if layer not in {'img_w', 'cia_w', 'vec_w', 'cva_w'} or not self.tianditu_key:
                return 404, {'error': '未知天地图图层'}, []
            host = int(x) % 4
            url = (f'https://t{host}.tianditu.gov.cn/DataServer?T={layer}'
                   f'&x={x}&y={y}&l={zoom}&tk={quote(self.tianditu_key, safe="")}')
            content_type = 'image/png'
        try:
            body, kind = self.remote_cache(url, content_type)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 502, {'error': f'外部瓦片请求失败：{exc}'}, []
        return 200, body, [('Content-Type', kind), ('Cache-Control', 'public, max-age=31536000, immutable')]

    def __call__(self, env, start_response):
        try:
            status, payload, headers = self.handle(env)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            status, payload, headers = 400, {'error': str(exc) or '请求无效'}, []
        except LookupError as exc:
            status, payload, headers = 404, {'error': str(exc)}, []
        except Exception:
            logging.exception('Request failed')
            status, payload, headers = 500, {'error': '服务器错误，请查看服务日志'}, []
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode()
            headers.append(('Content-Type', 'application/json; charset=utf-8'))
        if env.get('PATH_INFO', '').startswith('/share/'):
            headers = [(name, value) for name, value in headers if name.lower() != 'cache-control']
        headers += [('Content-Length', str(len(payload))), ('Cache-Control', 'no-store'),
                    ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'),
                    ('Referrer-Policy', 'no-referrer' if env.get('PATH_INFO', '').startswith('/share/') else 'strict-origin-when-cross-origin')]
        start_response(f'{status} {HTTPStatus(status).phrase}', headers)
        return [payload]

    def cookie(self, token, age=604800):
        return ('Set-Cookie', f'hiking_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}' + ('; Secure' if self.secure else ''))

    def body(self, env):
        if env.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
            raise ValueError('请使用 JSON 请求')
        size = int(env.get('CONTENT_LENGTH') or 0)
        if not 0 < size <= LIMIT:
            raise ValueError('请求为空或超过 20 MB')
        value = json.loads(env['wsgi.input'].read(size))
        if not isinstance(value, dict):
            raise ValueError('请求必须为对象')
        return value

    def handle(self, env):
        raw_path = env.get('PATH_INFO', '')
        if '?' in raw_path:
            path, qs = raw_path.split('?', 1)
            if not env.get('QUERY_STRING'):
                env['QUERY_STRING'] = qs
        else:
            path = raw_path
        method = env['REQUEST_METHOD']
        share_match = re.fullmatch(r'/share/([A-Za-z0-9_-]{43})(/.*)?', path)
        share_prefix = ''
        if path.startswith('/share/') and method != 'GET':
            return 405, {'error': '分享链接仅支持查看'}, []
        if path.startswith('/share/') and not share_match:
            return 404, {'error': '分享链接已过期或失效'}, []
        shortcut_request = path.startswith('/api/shortcut/')
        shortcut_data, photo_digest = None, None
        live_video = None
        if method not in ('GET', 'POST', 'PATCH', 'DELETE'):
            return 405, {'error': '不支持的方法'}, []
        if method != 'GET' and not shortcut_request:
            origin = env.get('HTTP_ORIGIN')
            if env.get('HTTP_X_HIKING_REQUEST') != '1' or (origin and urlsplit(origin).netloc != env.get('HTTP_HOST')):
                return 403, {'error': '请求来源无效'}, []
        if path == '/api/login' and method == 'POST':
            data = self.body(env)
            address, now = env.get('REMOTE_ADDR', ''), time.time()
            with connect(self.database) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM attempts WHERE reset < ?', (now,))
                attempt = db.execute('SELECT * FROM attempts WHERE address=?', (address,)).fetchone()
                if attempt and attempt['count'] >= 10:
                    return 429, {'error': '尝试过多，请在 15 分钟后重试'}, []
                supplied = data.get('password', '')
                if not isinstance(supplied, str) or not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), self.password):
                    db.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(address) DO UPDATE SET count=count+1', (address, now + 900))
                    return 401, {'error': '密码错误'}, []
                db.execute('DELETE FROM attempts WHERE address=?', (address,))
                db.execute('DELETE FROM sessions WHERE expires < ?', (now,))
                token = secrets.token_urlsafe(32)
                db.execute('INSERT INTO sessions VALUES(?,?)', (self.digest(token), now + 604800))
            return 200, {'ok': True}, [self.cookie(token)]
        cookies = SimpleCookie()
        try:
            cookies.load(env.get('HTTP_COOKIE', ''))
        except Exception:
            pass
        token = cookies.get('hiking_session')
        digest = self.digest(token.value if token else '')
        with connect(self.database) as db:
            authenticated = db.execute('SELECT 1 FROM sessions WHERE token=? AND expires>?', (digest, time.time())).fetchone()
            if share_match:
                share_token, suffix = share_match.groups()
                share_prefix = f'/share/{share_token}'
                path = suffix or '/'
                share = db.execute('SELECT * FROM shares WHERE token=? AND expires>?',
                                   (self.digest(share_token), time.time())).fetchone()
                if not share:
                    if path in ('/', '/index.html'):
                        body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,minimum-scale=1,maximum-scale=1,user-scalable=no"><title>分享已失效 · Hiking</title><link rel="stylesheet" href="/style.css"><body class="login-page"><main class="login-card"><h1>分享链接已过期或失效</h1><p>请联系分享者获取新的链接。</p></main></body></html>'
                        return 404, body.encode(), [('Content-Type', 'text/html; charset=utf-8')]
                    return 404, {'error': '分享链接已过期或失效'}, []
                if path in ('/', '/index.html'):
                    status, body, headers = self.static('index.html')
                    body = body.replace(b'<head>', f'<head><base href="{share_prefix}/">'.encode(), 1)
                    return status, body, headers
                if path == '/api/share':
                    return 200, {'item_id': share['item_id'], 'expires_at': share['expires']}, []
                allowed = {'/style.css', '/credits.html', '/js/app.js', '/js/route.js',
                           '/js/coordinates.js', '/js/elevation.js', '/js/exif.js', '/api/map-config',
                           '/api/maplibre/maplibre-gl.js', '/api/maplibre/maplibre-gl.css',
                           f"/api/items/{share['item_id']}", f"/api/items/{share['item_id']}/photos",
                           f"/api/items/{share['item_id']}/thumbnail"}
                shared_photo = re.fullmatch(r'/api/photos/(\d+)(?:/live)?', path)
                if shared_photo and db.execute('SELECT 1 FROM photos WHERE id=? AND item_id=?',
                                               (int(shared_photo.group(1)), share['item_id'])).fetchone():
                    allowed.add(path)
                if path not in allowed and not re.fullmatch(r'/api/tiles/(tianditu|dem)/[^/]+/\d+/\d+/\d+', path):
                    return 404, {'error': '此链接仅可查看分享的路线或记录'}, []
                authenticated = True
            if shortcut_request:
                authorization = env.get('HTTP_AUTHORIZATION', '')
                upload_token = authorization[7:] if authorization.startswith('Bearer ') else ''
                credential = db.execute('SELECT digest FROM shortcut_token WHERE id=1').fetchone()
                if not upload_token or not credential or not hmac.compare_digest(credential['digest'], self.digest(upload_token)):
                    return 401, {'error': '上传凭证无效，请在系统设置中重新生成'}, []
                if path in ('/api/shortcut/gpx', '/api/shortcut/gpx/preview') and method == 'POST':
                    preview = path.endswith('/preview')
                    return (200 if preview else 201), import_gpx(db, env, preview=preview), []
                upload = re.fullmatch(r'/api/shortcut/items/(\d+)/photos', path)
                if path == '/api/shortcut/photos' or upload:
                    if method != 'POST':
                        return 404, {'error': '快捷指令仅可导入 GPX 和照片'}, []
                    specified_id = int(upload.group(1)) if upload else None
                    db.execute('BEGIN IMMEDIATE')
                    item_id, shortcut_data, photo_digest = prepare_photo(db, env, specified_id)
                    if item_id is None and shortcut_data.get('skipped'):
                        return 200, shortcut_data, []
                    existing = db.execute('SELECT photos.* FROM photos JOIN shortcut_photos ON photos.id=shortcut_photos.photo_id WHERE shortcut_photos.item_id=? AND digest=?', (item_id, photo_digest)).fetchone()
                    if existing:
                        return 200, photo_item(existing), []
                    path = f'/api/items/{item_id}/photos'
                    authenticated = True
                else:
                    return 404, {'error': '快捷指令仅可导入 GPX 和照片'}, []
            if path == '/api/session':
                return (200 if authenticated else 401), {'authenticated': bool(authenticated)}, []
            if path == '/api/logout' and method == 'POST':
                db.execute('DELETE FROM sessions WHERE token=?', (digest,))
                return 200, {'ok': True}, [self.cookie('', 0)]
            if not authenticated:
                if path in ('/', '/login.html', '/style.css') and method == 'GET':
                    return self.static('login.html' if path == '/' else path[1:])
                return 401, {'error': '请先登录'}, []
            if path == '/api/settings/shortcut-token':
                if method == 'POST':
                    upload_token = secrets.token_urlsafe(32)
                    db.execute('INSERT OR REPLACE INTO shortcut_token VALUES(1,?)', (self.digest(upload_token),))
                    return 200, {'token': upload_token}, []
                if method == 'DELETE':
                    db.execute('DELETE FROM shortcut_token')
                    return 200, {'ok': True}, []
            if path == '/api/settings/shortcut' and method == 'GET':
                gpx_avail = (ROOT / 'shortcuts/hiking-gpx.shortcut').is_file()
                photos_avail = (ROOT / 'shortcuts/hiking-photos.shortcut').is_file()
                return 200, {'download_available': gpx_avail or photos_avail,
                             'gpx_available': gpx_avail,
                             'photos_available': photos_avail}, []
            if path.startswith('/shortcuts/') and path.endswith('.shortcut') and method == 'GET':
                shortcut = ROOT / 'shortcuts' / Path(path).name
                if not shortcut.is_file():
                    raise LookupError('可安装版本尚未生成，请查看设置页的安装说明')
                return 200, shortcut.read_bytes(), [('Content-Type', 'application/octet-stream'),
                    ('Content-Disposition', f'attachment; filename="{shortcut.name}"')]
            if path == '/api/settings/storage' and method == 'GET':
                directory = Path(self.database).parent
                def usage(folder):
                    sizes = []
                    for p in folder.rglob('*') if folder.exists() else []:
                        try:
                            if p.is_file() and not p.is_symlink():
                                sizes.append(p.stat().st_size)
                        except FileNotFoundError:
                            pass  # A concurrent upload or cache refresh can rename a file.
                    return {'bytes': sum(sizes), 'files': len(sizes)}
                database_bytes = 0
                for suffix in ('', '-wal', '-shm'):
                    try:
                        database_bytes += Path(str(self.database) + suffix).stat().st_size
                    except FileNotFoundError:
                        pass
                disk = shutil.disk_usage(directory)
                return 200, {'cache': usage(directory / 'remote-cache'),
                             'photos': usage(directory / 'photos'),
                             'database_bytes': database_bytes,
                             'disk_free_bytes': disk.free, 'disk_total_bytes': disk.total}, []
            tile_response = self.tile(path)
            if tile_response is not None:
                return tile_response
            if path in ('/api/maplibre/maplibre-gl.js', '/api/maplibre/maplibre-gl.css') and method == 'GET':
                filename = path.rsplit('/', 1)[-1]
                url = f'https://cdn.jsdelivr.net/npm/maplibre-gl@5.6.1/dist/{filename}'
                try:
                    body, kind = self.remote_cache(url, 'text/css' if filename.endswith('.css') else 'application/javascript')
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    return 502, {'error': f'地图引擎资源请求失败：{exc}'}, []
                return 200, body, [('Content-Type', kind), ('Cache-Control', 'public, max-age=31536000, immutable')]
            if path == '/api/map-config' and method == 'GET':
                if not self.tianditu_key:
                    return 503, {'error': '请配置 TIANDITU_KEY 并重启服务'}, []
                sources = {}
                for name, layer in [('satellite', 'img_w'), ('satellite-labels', 'cia_w'),
                                    ('street', 'vec_w'), ('street-labels', 'cva_w')]:
                    sources[name] = {
                        'type': 'raster', 'tileSize': 256, 'minzoom': 1, 'maxzoom': 18,
                        # DataServer is the documented XYZ endpoint and avoids
                        # WMTS matrix template differences between MapLibre versions.
                        'tiles': [f'{share_prefix}/api/tiles/tianditu/{layer}/{{z}}/{{x}}/{{y}}'],
                        'attribution': '<a href="https://www.tianditu.gov.cn/">天地图</a> © 国家地理信息公共服务平台'}
                return 200, {'sources': sources}, []
            if path == '/api/items':
                if method == 'GET':
                    rows = db.execute('''
                        SELECT items.*, item_locations.location
                        FROM items
                        LEFT JOIN item_locations ON items.id = item_locations.item_id
                        ORDER BY items.id DESC
                    ''').fetchall()
                    summaries = []
                    for row in rows:
                        entry = dict(row)
                        track_data = json.loads(entry.pop('track'))
                        loc = entry.get('location')
                        if not loc and self.tianditu_key:
                            loc = self.resolve_item_location(db, entry['id'], track_data)
                        entry['location'] = loc or ''
                        entry['summary'] = summarize(track_data)
                        summaries.append(entry)
                    return 200, summaries, []
                if method == 'POST':
                    data = self.body(env)
                    auto_route = bool(data.pop('auto_route', False))
                    db.execute('BEGIN IMMEDIATE')
                    if data.get('kind') == 'record' and auto_route and not data.get('route_id'):
                        route_id = ensure_route_for_record(db, data.get('name', ''), data.get('track'))
                        data['route_id'] = route_id
                    identifier = save(db, data)
                    if self.tianditu_key:
                        self.resolve_item_location(db, identifier, data.get('track'))
                    created_row = db.execute('''
                        SELECT items.*, item_locations.location
                        FROM items
                        LEFT JOIN item_locations ON items.id = item_locations.item_id
                        WHERE items.id=?
                    ''', (identifier,)).fetchone()
                    return 201, item(created_row), []
            photo_match = re.fullmatch(r'/api/photos/(\d+)(/live)?', path)
            if photo_match:
                photo_id = int(photo_match.group(1))
                row = db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
                if not row:
                    raise LookupError('照片不存在')
                if photo_match.group(2):
                    if method != 'GET':
                        return 405, {'error': '不支持的方法'}, []
                    if not row['live_path'] or not Path(row['live_path']).is_file():
                        raise LookupError('Live 视频不存在')
                    video = Path(row['live_path'])
                    size = video.stat().st_size
                    headers = [('Content-Type', 'video/mp4'), ('Accept-Ranges', 'bytes')]
                    requested = env.get('HTTP_RANGE')
                    start, end = 0, size - 1
                    if requested:
                        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                        if not match or not any(match.groups()):
                            return 416, b'', headers + [('Content-Range', f'bytes */{size}')]
                        first, last = match.groups()
                        start = int(first) if first else max(0, size - int(last))
                        end = min(size - 1, int(last)) if first and last else size - 1
                        if start > end:
                            return 416, b'', headers + [('Content-Range', f'bytes */{size}')]
                        headers.append(('Content-Range', f'bytes {start}-{end}/{size}'))
                    with video.open('rb') as stream:
                        stream.seek(start)
                        return 206 if requested else 200, stream.read(end - start + 1), headers
                if method == 'GET':
                    p = photo_sync.stored_preview(db, self.database, self.photo_source, row, self.digest) or Path(row['file_path'])
                    if not p.is_file():
                        raise LookupError('照片文件不存在')
                    content_type = mimetypes.guess_type(row['filename'])[0] or 'image/jpeg'
                    return 200, p.read_bytes(), [('Content-Type', content_type), ('Cache-Control', 'public, max-age=31536000, immutable')]
                if method == 'DELETE':
                    p = Path(row['file_path'])
                    if p.is_file():
                        p.unlink(missing_ok=True)
                    if row['live_path']:
                        Path(row['live_path']).unlink(missing_ok=True)
                    db.execute('DELETE FROM photos WHERE id=?', (photo_id,))
                    return 200, {'ok': True}, []

            sync_match = re.fullmatch(r'/api/items/(\d+)/photo-sync/(scan|preview|import)', path)
            if sync_match:
                item_id, action = int(sync_match.group(1)), sync_match.group(2)
                track = photo_sync.record_track(db, item_id)
                if action == 'scan' and method == 'POST':
                    refresh = self.body(env).get('refresh') is True
                    index = self.album_index.status(db, self.photo_source, refresh)
                    return 200, {**photo_sync.scan(db, self.photo_source, item_id, track, self.digest), 'index': index}, []
                if action == 'preview' and method == 'GET':
                    query = parse_qs(env.get('QUERY_STRING', ''))
                    token = query.get('token', [''])[-1]
                    source, taken_at, stamp, photo_digest = photo_sync.candidate(
                        self.photo_source, token, item_id, track, self.digest)
                    image = photo_sync.preview(self.database, source, photo_digest, small=query.get('size', [''])[0] != 'large')
                    return 200, image.read_bytes(), [('Content-Type', 'image/jpeg')]
                if action == 'import' and method == 'POST':
                    token = self.body(env).get('token')
                    source, taken_at, stamp, photo_digest = photo_sync.candidate(
                        self.photo_source, token, item_id, track, self.digest)
                    image = photo_sync.preview(self.database, source, photo_digest)
                    live_video = live_photos.preview(self.database, source)
                    db.execute('BEGIN IMMEDIATE')
                    track = photo_sync.record_track(db, item_id)
                    photo_sync.candidate(self.photo_source, token, item_id, track, self.digest)
                    existing = db.execute('SELECT photos.* FROM photos JOIN shortcut_photos ON photos.id=shortcut_photos.photo_id WHERE shortcut_photos.item_id=? AND digest=?', (item_id, photo_digest)).fetchone()
                    if existing:
                        live_photos.attach(db, existing, live_video)
                        return 200, photo_item(db.execute('SELECT * FROM photos WHERE id=?', (existing['id'],)).fetchone()), []
                    shortcut_data = photo_sync.photo_data(source, taken_at, stamp, track, image)
                    shortcut_data['data'] = base64.b64encode(image.read_bytes()).decode()
                    path = f'/api/items/{item_id}/photos'
                else:
                    return 405, {'error': '不支持的方法'}, []

            photos_match = re.fullmatch(r'/api/items/(\d+)/photos', path)
            if photos_match:
                item_id = int(photos_match.group(1))
                item_row = db.execute('SELECT id FROM items WHERE id=?', (item_id,)).fetchone()
                if not item_row:
                    raise LookupError('条目不存在')
                if method == 'GET':
                    rows = db.execute('SELECT * FROM photos WHERE item_id=? ORDER BY id ASC', (item_id,)).fetchall()
                    return 200, [photo_item(r) for r in rows], []
                if method == 'POST':
                    data = shortcut_data if shortcut_data is not None else self.body(env)
                    raw_data = data.get('data', '')
                    if ',' in raw_data:
                        raw_data = raw_data.split(',', 1)[1]
                    try:
                        image_bytes = base64.b64decode(raw_data)
                    except Exception:
                        raise ValueError('无效的图片数据')
                    if len(image_bytes) == 0 or len(image_bytes) > 15 * 1024 * 1024:
                        raise ValueError('图片数据为空或超过 15 MB')

                    filename = Path(data.get('name', 'photo.jpg')).name.strip() or 'photo.jpg'
                    photos_dir = Path(self.database).parent / 'photos' / str(item_id)
                    photos_dir.mkdir(parents=True, exist_ok=True)
                    ext = '.jpg'
                    if filename.lower().endswith(('.png', '.webp', '.jpeg')):
                        ext = Path(filename).suffix.lower()
                    stored_name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}{ext}"
                    file_path = photos_dir / stored_name
                    file_path.write_bytes(image_bytes)

                    width = data.get('width')
                    if width is not None and (not isinstance(width, int) or width <= 0):
                        width = None
                    height = data.get('height')
                    if height is not None and (not isinstance(height, int) or height <= 0):
                        height = None

                    # If ffmpeg is available and photo exceeds 720, optimize on server
                    if shutil.which('ffmpeg') and shutil.which('ffprobe') and (
                        (width and width > 720) or (height and height > 720) or (width is None and height is None)
                    ):
                        try:
                            temp_p = file_path.with_name(f"{file_path.stem}_opt.tmp.jpg")
                            subprocess.run([
                                'ffmpeg', '-y', '-i', str(file_path),
                                '-vf', "scale='if(gt(iw,ih),min(720,iw),-2)':'if(gt(iw,ih),-2,min(720,ih))'",
                                '-q:v', '3', '-update', '1', '-frames:v', '1', str(temp_p)
                            ], capture_output=True, check=True, timeout=10)
                            probe = subprocess.run([
                                'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', str(temp_p)
                            ], capture_output=True, text=True, check=True, timeout=5)
                            w_str, h_str = probe.stdout.strip().split('x')[:2]
                            width, height = int(w_str), int(h_str)
                            os.replace(temp_p, file_path)
                            image_bytes = file_path.read_bytes()
                        except Exception:
                            pass
                    taken_at = data.get('taken_at')
                    if taken_at is not None and not isinstance(taken_at, str):
                        taken_at = None
                    lat = data.get('lat')
                    if lat is not None and (not isinstance(lat, (int, float)) or not -85 <= lat <= 85):
                        lat = None
                    lng = data.get('lng')
                    if lng is not None and (not isinstance(lng, (int, float)) or not -180 <= lng <= 180):
                        lng = None
                    match_type = data.get('match_type', 'none')
                    if match_type not in ('time', 'gps', 'none'):
                        match_type = 'none'
                    point_index = data.get('point_index')
                    if point_index is not None and (not isinstance(point_index, int) or point_index < 0):
                        point_index = None

                    cursor = db.execute(
                        '''INSERT INTO photos (item_id, filename, file_path, size, width, height, taken_at, lat, lng, match_type, point_index)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (item_id, filename, str(file_path), len(image_bytes), width, height, taken_at, lat, lng, match_type, point_index)
                    )
                    photo_id = cursor.lastrowid
                    if photo_digest:
                        db.execute('INSERT INTO shortcut_photos VALUES(?,?,?)', (item_id, photo_digest, photo_id))
                    created_row = db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
                    live_photos.attach(db, created_row, live_video)
                    created_row = db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
                    return 201, photo_item(created_row), []

            thumbnail_match = re.fullmatch(r'/api/items/(\d+)/thumbnail', path)
            if thumbnail_match and method == 'GET':
                identifier = int(thumbnail_match.group(1))
                row = db.execute('SELECT track FROM items WHERE id=?', (identifier,)).fetchone()
                if not row:
                    raise LookupError('条目不存在')
                cache_path = Path(self.database).parent / 'remote-cache' / 'thumbnails' / f'{identifier}.svg'
                body = thumbnail(json.loads(row['track']), cache_path, self.tianditu_key, self.tile)
                return 200, body, [('Content-Type', 'image/svg+xml')]

            create_share = re.fullmatch(r'/api/items/(\d+)/shares', path)
            if create_share and method == 'POST':
                identifier = int(create_share.group(1))
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('SELECT 1 FROM items WHERE id=?', (identifier,)).fetchone():
                    raise LookupError('条目不存在')
                days = self.body(env).get('expires_in_days', 7)
                if type(days) is not int or not 1 <= days <= 365:
                    raise ValueError('分享有效期须为 1～365 天的整数')
                share_token = secrets.token_urlsafe(32)
                expires = time.time() + days * 86400
                db.execute('INSERT INTO shares VALUES(?,?,?)', (self.digest(share_token), identifier, expires))
                return 201, {'url': f'/share/{share_token}', 'expires_at': expires}, []

            loc_match = re.fullmatch(r'/api/items/(\d+)/location', path)
            if loc_match and method == 'GET':
                identifier = int(loc_match.group(1))
                row = db.execute('''
                    SELECT items.track, item_locations.location
                    FROM items
                    LEFT JOIN item_locations ON items.id = item_locations.item_id
                    WHERE items.id=?
                ''', (identifier,)).fetchone()
                if not row:
                    raise LookupError('条目不存在')
                loc = row['location']
                if not loc:
                    track_data = json.loads(row['track'])
                    loc = self.resolve_item_location(db, identifier, track_data) or ''
                return 200, {'location': loc or ''}, []

            item_match = re.fullmatch(r'/api/items/(\d+)', path)
            if item_match:
                identifier = int(item_match.group(1))
                row = db.execute('''
                    SELECT items.*, item_locations.location
                    FROM items
                    LEFT JOIN item_locations ON items.id = item_locations.item_id
                    WHERE items.id=?
                ''', (identifier,)).fetchone()
                if not row:
                    raise LookupError('条目不存在')
                if method == 'GET':
                    res = item(row)
                    if not res.get('location') and self.tianditu_key:
                        res['location'] = self.resolve_item_location(db, identifier, res['track']) or ''
                        res['track']['location'] = res['location']
                    return 200, res, []
                if method == 'PATCH':
                    patch_body = self.body(env)
                    save(db, patch_body, identifier)
                    if 'track' in patch_body and self.tianditu_key:
                        db.execute('DELETE FROM item_locations WHERE item_id=?', (identifier,))
                        self.resolve_item_location(db, identifier, patch_body['track'])
                    updated_row = db.execute('''
                        SELECT items.*, item_locations.location
                        FROM items
                        LEFT JOIN item_locations ON items.id = item_locations.item_id
                        WHERE items.id=?
                    ''', (identifier,)).fetchone()
                    return 200, item(updated_row), []
                if method == 'DELETE':
                    db.execute('DELETE FROM items WHERE id=?', (identifier,))
                    db.execute('DELETE FROM item_locations WHERE item_id=?', (identifier,))
                    photos_dir = Path(self.database).parent / 'photos' / str(identifier)
                    shutil.rmtree(photos_dir, ignore_errors=True)
                    (Path(self.database).parent / 'remote-cache' / 'thumbnails' / f'{identifier}.svg').unlink(missing_ok=True)
                    return 200, {'ok': True}, []
        if method == 'GET':
            return self.static('index.html' if path == '/' else path[1:])
        return 404, {'error': '页面不存在'}, []

    def digest(self, token):
        # Changing the password invalidates existing sessions on restart.
        return hmac.new(self.password, token.encode(), hashlib.sha256).hexdigest()

    def static(self, name):
        allowed = {'index.html', 'login.html', 'style.css', 'credits.html', 'settings.html', 'js/settings.js',
                   'photo-sync.html', 'js/photo-sync.js',
                   'js/app.js', 'js/route.js', 'js/coordinates.js', 'js/elevation.js', 'js/exif.js'}
        if name not in allowed:
            return 404, {'error': '页面不存在'}, []
        content_type = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        return 200, (ROOT / name).read_bytes(), [('Content-Type', content_type + '; charset=utf-8')]


class Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(30)
        return sock, address


def main():
    os.umask(0o077)
    values = config()
    app = App(ROOT / 'data/hiking.sqlite3', values['HIKING_PASSWORD'], values.get('COOKIE_SECURE') == '1',
              values.get('TIANDITU_KEY', ''), photo_source=values['PHOTO_SOURCE'])
    with make_server(values.get('HOST', '0.0.0.0'), int(values.get('PORT', '8010')), app, server_class=Server) as http:
        print(f'Hiking listening on {http.server_address}', flush=True)
        http.serve_forever()


if __name__ == '__main__':
    main()
