"""Validated, private SQLite storage for planned routes and hiking records."""
import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('route','record')),
 name TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', route_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
 track TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS shares (
 token TEXT PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (address TEXT PRIMARY KEY, count INTEGER NOT NULL, reset REAL NOT NULL);
CREATE TABLE IF NOT EXISTS imports (digest TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS shortcut_token (id INTEGER PRIMARY KEY CHECK(id=1), digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shortcut_imports (
 digest TEXT PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS shortcut_photos (
 item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 digest TEXT NOT NULL, photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
 PRIMARY KEY(item_id, digest)
);
CREATE TABLE IF NOT EXISTS photos (
 id INTEGER PRIMARY KEY,
 item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 filename TEXT NOT NULL,
 file_path TEXT NOT NULL,
 size INTEGER NOT NULL,
 width INTEGER,
 height INTEGER,
 taken_at TEXT,
 lat REAL,
 lng REAL,
 match_type TEXT NOT NULL DEFAULT 'none',
 point_index INTEGER,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_photos_item_id ON photos(item_id);
CREATE TABLE IF NOT EXISTS album_metadata (
 source TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, modified INTEGER NOT NULL, changed INTEGER NOT NULL,
 taken_at TEXT, PRIMARY KEY(source, path)
);
CREATE INDEX IF NOT EXISTS idx_album_time ON album_metadata(source, taken_at);
CREATE TABLE IF NOT EXISTS album_index (
 source TEXT PRIMARY KEY, scanned INTEGER NOT NULL, skipped INTEGER NOT NULL,
 cached INTEGER NOT NULL, updated INTEGER NOT NULL, completed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS item_locations (
 item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
 location TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS geocache (
 key TEXT PRIMARY KEY,
 location TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        if 'live_path' not in {row['name'] for row in db.execute('PRAGMA table_info(photos)')}:
            db.execute('ALTER TABLE photos ADD COLUMN live_path TEXT')


def numeric(value, low, high, optional=False):
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('轨迹数值无效或超出范围')
    return value


def validate_track(track):
    if not isinstance(track, dict) or track.get('system') not in ('WGS84', 'GCJ02', 'BD09'):
        raise ValueError('轨迹必须声明原始坐标系，坐标值统一使用 WGS84')
    segments = track.get('segments')
    if not isinstance(segments, list) or not segments:
        raise ValueError('轨迹不能为空')
    count = 0
    clean = []
    for segment in segments:
        if not isinstance(segment, list) or len(segment) < 2:
            raise ValueError('每段至少需要两个点')
        count += len(segment)
        if count > 50000:
            raise ValueError('最多支持 50,000 个点')
        points = []
        for p in segment:
            if not isinstance(p, dict):
                raise ValueError('无效轨迹点')
            points.append({k: numeric(p.get(k), lo, hi, optional) for k, lo, hi, optional in (
                ('lng', -180, 180, False), ('lat', -85, 85, False), ('ele', -12000, 100000, True),
                ('time', -2208988800000, 4102444800000, True), ('hr', 1, 300, True), ('cad', 0, 400, True))})
        clean.append(points)
    days = track.get('days')
    if days is not None and (type(days) is not int or not 1 <= days <= 3650):
        raise ValueError('天数须为 1～3650 的整数')
    return {'days': days, 'segments': clean, 'system': track['system'], 'demFilled': bool(track.get('demFilled'))}


def fields(data):
    if not isinstance(data, dict):
        raise ValueError('请求必须为对象')
    name, notes = data.get('name'), data.get('notes', '')
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError('名称须为 1～160 字符')
    if not isinstance(notes, str) or len(notes) > 10000:
        raise ValueError('备注最多 10,000 字符')
    kind = data.get('kind')
    if kind not in ('route', 'record'):
        raise ValueError('请选择路线或记录')
    route_id = data.get('route_id')
    if route_id is not None and (type(route_id) is not int or kind != 'record'):
        raise ValueError('只有徒步记录可以关联路线')
    return name.strip(), notes, kind, route_id


def save(db, data, item_id=None):
    name, notes, kind, route_id = fields(data)
    if route_id is not None:
        parent = db.execute("SELECT id FROM items WHERE id=? AND kind='route'", (route_id,)).fetchone()
        if not parent:
            raise ValueError('关联路线不存在')
    if item_id is None:
        track = json.dumps(validate_track(data.get('track')), ensure_ascii=False, allow_nan=False)
        return db.execute('INSERT INTO items(name,notes,kind,route_id,track) VALUES(?,?,?,?,?)',
                          (name, notes, kind, route_id, track)).lastrowid
    old = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
    if not old:
        raise LookupError('条目不存在')
    if kind != old['kind']:
        raise ValueError('不能更改条目类型')
    track = json.dumps(validate_track(data['track']), ensure_ascii=False) if 'track' in data else old['track']
    if 'days' in data:
        updated = json.loads(track)
        updated['days'] = data['days']
        track = json.dumps(validate_track(updated), ensure_ascii=False)
    db.execute('UPDATE items SET name=?,notes=?,route_id=?,track=? WHERE id=?', (name, notes, route_id, track, item_id))
    return item_id


def item(row):
    result = dict(row)
    result['track'] = json.loads(result['track'])
    result['track']['name'] = result['name']
    loc = result.get('location') or ''
    result['location'] = loc
    result['track']['location'] = loc
    return result


def photo_item(row):
    result = dict(row)
    result.pop('file_path', None)
    result['live'] = bool(result.pop('live_path', None))
    return result


def ensure_route_for_record(db, name, track):
    clean_name = name.strip() if isinstance(name, str) else ''
    route = db.execute("SELECT id FROM items WHERE kind='route' AND name=? ORDER BY id DESC LIMIT 1", (clean_name,)).fetchone()
    if route:
        return route['id']
    return save(db, {'name': clean_name, 'kind': 'route', 'track': track})
