"""Idempotently import a WGS84 GPX as a planned route plus its first record."""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import xml.etree.ElementTree as ET
from backend.storage import connect, initialize, save


def parse_gpx(content):
    if len(content) > 15 * 1024 * 1024:
        raise ValueError('GPX 超过 15 MB')
    content = content.decode('utf-8-sig')
    if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
        raise ValueError('GPX 过大或包含不支持的实体')
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError('GPX XML 格式无效') from exc
    tag = lambda node: node.tag.rsplit('}', 1)[-1]
    if tag(root) != 'gpx':
        raise ValueError('不是 GPX 文件')
    def value(node, name):
        return next((x.text for x in node.iter() if tag(x) == name), None)
    segments = []
    for group in root.iter():
        if tag(group) not in ('trkseg', 'rte'):
            continue
        points = []
        for p in group:
            if tag(p) not in ('trkpt', 'rtept'):
                continue
            point = {'lng': float(p.attrib['lon']), 'lat': float(p.attrib['lat'])}
            for key in ('ele', 'hr', 'cad'):
                v = value(p, key)
                point[key] = float(v) if v and v.strip() else None
            timestamp = value(p, 'time')
            point['time'] = None
            if timestamp:
                date = datetime.fromisoformat(timestamp.strip().replace('Z', '+00:00'))
                if date.utcoffset() is None:
                    raise ValueError('GPX 轨迹点时间缺少时区，无法确定照片时间区间')
                point['time'] = date.timestamp() * 1000
            points.append(point)
        if points:
            segments.append(points)
    return {'name': value(root, 'name') or '导入记录', 'system': 'WGS84', 'segments': segments}


def import_file(path, database):
    content = Path(path).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    track = parse_gpx(content)
    initialize(database)
    with connect(database) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM imports WHERE digest=?', (digest,)).fetchone():
            return False
        route_id = save(db, {'name': track['name'], 'kind': 'route', 'track': track, 'notes': '从实测记录提取的路线'})
        save(db, {'name': track['name'], 'kind': 'record', 'track': track, 'route_id': route_id, 'notes': '原始 GPX 导入'})
        db.execute('INSERT INTO imports VALUES(?)', (digest,))
    return True


if __name__ == '__main__':
    from backend.server import ROOT
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    print('已导入路线及关联记录' if import_file(args.file, ROOT / 'data/hiking.sqlite3') else '相同文件已导入，未重复写入')
