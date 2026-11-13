"""The Shortcuts client sends a GPX file, then individual JPEG files."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import parse_qs

from backend.import_gpx import parse_gpx
from backend.storage import ensure_route_for_record, fields, save, validate_track


def read_file(env):
    size = int(env.get('CONTENT_LENGTH') or 0)
    if not 0 < size <= 15 * 1024 * 1024:
        raise ValueError('文件为空或超过 15 MB')
    content = env['wsgi.input'].read(size)
    if len(content) != size:
        raise ValueError('文件传输不完整，请重试')
    return content


def time_range(track):
    timestamps = [p['time'] for s in track['segments'] for p in s if p.get('time') is not None]
    if not timestamps:
        raise ValueError('GPX 没有轨迹点时间，无法筛选照片；请在网页中单独导入')
    return min(timestamps), max(timestamps)


def import_gpx(db, env, preview=False):
    content = read_file(env)
    query = parse_qs(env.get('QUERY_STRING', ''), keep_blank_values=True, errors='strict')
    kind = query.get('kind', ['record'])[-1]
    header_kind = env.get('HTTP_X_HIKING_KIND', '')
    if header_kind:
        kind = header_kind
    if kind in ('route', '路线'):
        kind = 'route'
    elif kind in ('record', '记录'):
        kind = 'record'
    else:
        raise ValueError('无效的条目类型，必须为路线或记录')

    track = parse_gpx(content)
    clean = validate_track(track)
    name = track['name'].strip() or '导入记录'
    if preview:
        return {'name': name}
    name = fields({'name': query.get('name', [name])[-1], 'kind': kind})[0]
    iso = lambda stamp: datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    start_iso, end_iso = None, None
    if kind == 'record':
        start, end = time_range(clean)
        start_iso, end_iso = iso(start), iso(end)
    else:
        try:
            start, end = time_range(clean)
            start_iso, end_iso = iso(start), iso(end)
        except ValueError:
            pass

    digest = hashlib.sha256(content if kind == 'record' else (content + b'\0' + kind.encode())).hexdigest()
    db.execute('BEGIN IMMEDIATE')
    row = db.execute('SELECT items.id, items.name, items.kind, items.route_id FROM items JOIN shortcut_imports ON items.id=shortcut_imports.item_id WHERE shortcut_imports.digest=?', (digest,)).fetchone()
    if row:
        item_id = row['id']
        if 'name' in query:
            route_id = row['route_id']
            if kind == 'record' and route_id is None:
                route_id = ensure_route_for_record(db, name, clean)
            db.execute('UPDATE items SET name=?, route_id=? WHERE id=?', (name, route_id, item_id))
            if route_id is not None:
                db.execute('UPDATE items SET name=? WHERE id=?', (name, route_id))
        else:
            name = row['name']
        reused = True
    else:
        route_id = None
        if kind == 'record':
            route_id = ensure_route_for_record(db, name, clean)
        item_id = save(db, {'name': name, 'kind': kind, 'route_id': route_id, 'track': clean})
        db.execute('INSERT OR REPLACE INTO shortcut_imports VALUES(?,?)', (digest, item_id))
        reused = False
    return {'item_id': item_id, 'name': name, 'kind': kind, 'start': start_iso, 'end': end_iso,
            'photo_path': f'/api/shortcut/items/{item_id}/photos', 'reused': reused}


def prepare_photo(db, env, item_id=None):
    if item_id is not None:
        row = db.execute("SELECT items.id, items.name, items.track FROM items WHERE items.id=? AND items.kind='record'", (item_id,)).fetchone()
        if not row:
            raise LookupError('未找到指定的徒步记录')

    taken_at = env.get('HTTP_X_PHOTO_TAKEN_AT', '')
    try:
        taken = datetime.fromisoformat(taken_at.replace('Z', '+00:00'))
        if taken.utcoffset() is None:
            raise ValueError()
        stamp = taken.timestamp() * 1000
    except (ValueError, OverflowError):
        raise ValueError('照片拍摄时间必须为带时区的 ISO 8601 时间')

    if item_id is not None:
        track = json.loads(row['track'])
        start, end = time_range(track)
        if not start <= stamp <= end:
            raise ValueError('照片拍摄时间不在 GPX 时间区间内')
        record_id = item_id
    else:
        rows = db.execute("SELECT items.id, items.name, items.track FROM items WHERE items.kind='record' ORDER BY items.id DESC").fetchall()
        matched_row, track = None, None
        for r in rows:
            t = json.loads(r['track'])
            try:
                s, e = time_range(t)
            except ValueError:
                continue
            if s <= stamp <= e:
                matched_row, track = r, t
                break
        if not matched_row:
            margin = 30 * 60 * 1000
            for r in rows:
                t = json.loads(r['track'])
                try:
                    s, e = time_range(t)
                except ValueError:
                    continue
                if (s - margin) <= stamp <= (e + margin):
                    matched_row, track = r, t
                    break
        if not matched_row:
            content = read_file(env)
            if item_id is None:
                return None, {'id': 0, 'item_id': 0, 'skipped': True, 'match_type': 'none',
                              'message': f'未找到与照片拍摄时间（{taken_at}）匹配的徒步记录，已自动跳过'}, None
            raise LookupError(f'未找到与照片拍摄时间（{taken_at}）匹配的徒步记录')
        record_id = matched_row['id']

    content = read_file(env)
    if not content.startswith(b'\xff\xd8\xff') or not content.endswith(b'\xff\xd9'):
        raise ValueError('请先将照片转换为 JPEG 再上传')
    digest = hashlib.sha256(taken_at.encode() + b'\0' + content).hexdigest()
    points = [p for s in track['segments'] for p in s]
    index = min((i for i, p in enumerate(points) if p['time'] is not None), key=lambda i: abs(points[i]['time'] - stamp))
    point = points[index]
    return record_id, {'name': f'IMG_{taken.strftime("%Y%m%d_%H%M%S")}.jpg',
                       'data': base64.b64encode(content).decode(), 'taken_at': taken_at,
                       'lat': point['lat'], 'lng': point['lng'], 'point_index': index,
                       'match_type': 'time'}, digest
