"""Read-only album scanning and small JPEG copies for a hiking record."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import shutil
import stat as stat_module
import struct
import subprocess
import tempfile
import time
from threading import BoundedSemaphore, Lock, Thread

from backend.resize_existing_photos import get_image_info
from backend.shortcut_upload import time_range
from backend.storage import connect

DEFAULT_SOURCE = Path('.local/photo-source')
EXTENSIONS = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.webp'}
LOCAL_TIMEZONE = timezone(timedelta(hours=8))
CONVERSIONS = BoundedSemaphore(2)
# Fixed locks prevent duplicate conversions without retaining a lock for every album photo.
CONVERSION_LOCKS = tuple(Lock() for _ in range(32))
IGNORED_FOLDERS = {'@eaDir', '#recycle', '.snapshot', '.snapshots', '@Recycle'}
CACHE_VERSION = 'photo-sync-v3'


def record_track(db, item_id):
    row = db.execute('SELECT kind, track FROM items WHERE id=?', (item_id,)).fetchone()
    if not row:
        raise LookupError('记录不存在')
    if row['kind'] != 'record':
        raise ValueError('只有徒步记录可以同步照片')
    track = json.loads(row['track'])
    time_range(track)
    return track


def taken_time(metadata):
    for tag, offset in (('SubSecDateTimeOriginal', 'OffsetTimeOriginal'),
                        ('DateTimeOriginal', 'OffsetTimeOriginal'),
                        ('SubSecCreateDate', 'OffsetTimeDigitized'),
                        ('CreateDate', 'OffsetTimeDigitized')):
        value = metadata.get(tag)
        if not isinstance(value, str):
            continue
        try:
            taken = datetime.fromisoformat(value[:10].replace(':', '-') + value[10:])
            if taken.utcoffset() is None:
                zone = metadata.get(offset)
                taken = (datetime.fromisoformat(taken.isoformat() + zone) if zone
                         else taken.replace(tzinfo=LOCAL_TIMEZONE))
            return taken.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
    return None


def source_path(source, relative):
    root = Path(source).resolve()
    path = root / relative
    if (not isinstance(relative, str) or Path(relative).is_absolute()
            or '..' in Path(relative).parts or path.resolve() != path
            or path.suffix.lower() not in EXTENSIONS or not path.is_file()):
        raise ValueError('照片已移走或不可读取，请重新扫描')
    return path


def candidate_token(item_id, relative, size, modified, taken_at, sign):
    payload = json.dumps([item_id, relative, size, modified, taken_at], ensure_ascii=False).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip('=')
    return encoded + '.' + sign('photo-sync:' + encoded)


def candidate(source, token, item_id, track, sign):
    if not isinstance(token, str) or len(token) > 16000:
        raise ValueError('请选择扫描结果中的照片')
    try:
        encoded, signature = token.rsplit('.', 1)
        if not hmac.compare_digest(signature, sign('photo-sync:' + encoded)):
            raise ValueError()
        record_id, relative, size, modified, taken_at = json.loads(
            base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if record_id != item_id:
            raise ValueError()
        path = source_path(source, relative)
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (size, modified):
            raise ValueError('照片已更改，请重新扫描')
        stamp = datetime.fromisoformat(taken_at).timestamp() * 1000
        start, end = time_range(track)
        if not start <= stamp <= end:
            raise ValueError('照片拍摄时间不在当前记录时间内，请重新扫描')
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('请选择扫描结果中的照片') from exc
    digest = 'album:' + hashlib.sha256(encoded.encode()).hexdigest()
    return path, taken_at, stamp, digest


def read_metadata(paths, fast):
    if not shutil.which('exiftool'):
        raise ValueError('服务器需要 exiftool 才能读取照片拍摄时间')
    try:
        result = subprocess.run(['exiftool', '-j', '-n', fast, '-charset', 'filename=UTF8',
                                 '-SubSecDateTimeOriginal', '-DateTimeOriginal', '-OffsetTimeOriginal',
                                 '-SubSecCreateDate', '-CreateDate', '-OffsetTimeDigitized', '-@', '-'],
                                input='\n'.join(map(str, paths)), capture_output=True, text=True,
                                encoding='utf-8', timeout=180)
        metadata = json.loads(result.stdout)
        if not isinstance(metadata, list) or not metadata:
            raise ValueError()
        return {entry['SourceFile']: entry for entry in metadata}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        raise ValueError('照片扫描失败或超时，请重试并检查目录权限') from exc


def refresh_index(db, source, progress=lambda **values: None):
    source_key = os.path.abspath(source)
    root = Path(source).resolve()
    if not root.is_dir():
        raise ValueError('照片备份目录不存在或未挂载')
    cached = {row['path']: row for row in db.execute('SELECT * FROM album_metadata WHERE source=?', (source_key,))}
    paths, changed, dates = {}, [], {}
    def inaccessible(error):
        raise ValueError('无法读取照片备份目录，请检查服务用户权限') from error
    for directory, folders, files in os.walk(root, onerror=inaccessible):
        folders[:] = [name for name in folders if name not in IGNORED_FOLDERS and not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if path.suffix.lower() not in EXTENSIONS or '\n' in str(path) or '\r' in str(path):
                continue
            try:
                stat = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat_module.S_ISREG(stat.st_mode):
                continue
            relative = str(path.relative_to(root))
            paths[path] = stat
            old = cached.get(relative)
            if old and (old['size'], old['modified'], old['changed']) == (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
                dates[path] = old['taken_at']
            else:
                changed.append(path)
            if len(paths) % 256 == 0:
                progress(stage='discover', checked=len(paths))
    progress(stage='metadata', checked=len(paths), total=len(changed), processed=0)
    for offset in range(0, len(changed), 256):
        batch = changed[offset:offset + 256]
        metadata = read_metadata(batch, '-fast2')
        # fast2 can miss timestamps after PNG/HEIF image data; retry these before caching.
        slow = [path for path in batch if str(path) in metadata and not metadata[str(path)].get('Error')
                and taken_time(metadata[str(path)]) is None]
        if slow:
            metadata.update(read_metadata(slow, '-fast'))
        updates = []
        for path in batch:
            entry = metadata.get(str(path), {})
            taken = taken_time(entry)
            dates[path] = taken.isoformat() if taken and not entry.get('Error') else None
            stat = paths[path]
            if entry and not entry.get('Error'):
                updates.append((source_key, str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, dates[path]))
            else:
                db.execute('DELETE FROM album_metadata WHERE source=? AND path=?', (source_key, str(path.relative_to(root))))
        db.executemany('INSERT OR REPLACE INTO album_metadata VALUES(?,?,?,?,?,?)', updates)
        # Keep completed batches when a long first scan is interrupted or retried.
        db.commit()
        progress(processed=min(offset + 256, len(changed)))
    present = {str(path.relative_to(root)) for path in paths}
    db.executemany('DELETE FROM album_metadata WHERE source=? AND path=?',
                   ((source_key, relative) for relative in cached.keys() - present))
    skipped = sum(date is None for date in dates.values())
    db.execute('INSERT OR REPLACE INTO album_index VALUES(?,?,?,?,?,?)',
               (source_key, len(paths), skipped, len(paths) - len(changed), len(changed), time.time()))


def scan(db, source, item_id, track, sign):
    source_key = os.path.abspath(source)
    start, end = time_range(track)
    iso = lambda stamp: datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
    start_at, end_at = iso(start), iso(end)
    synced = {row['digest'] for row in db.execute('SELECT digest FROM shortcut_photos WHERE item_id=?', (item_id,))}
    candidates = []
    for row in db.execute('SELECT path,size,modified,taken_at FROM album_metadata '
                          'WHERE source=? AND taken_at>=? AND taken_at<=? ORDER BY taken_at,path',
                          (source_key, start_at, end_at)):
        token = candidate_token(item_id, row['path'], row['size'], row['modified'], row['taken_at'], sign)
        digest = 'album:' + hashlib.sha256(token.split('.')[0].encode()).hexdigest()
        candidates.append({'token': token, 'filename': Path(row['path']).name, 'taken_at': row['taken_at'],
                           'synced': digest in synced})
    state = db.execute('SELECT scanned,skipped,cached,updated FROM album_index WHERE source=?', (source_key,)).fetchone()
    counts = dict(state) if state else dict(scanned=0, skipped=0, cached=0, updated=0)
    return {'start': start_at, 'end': end_at, 'photos': candidates, **counts}


class AlbumIndex:
    def __init__(self, database):
        self.database = database
        self.lock = Lock()
        self.thread = None
        self.last_attempt = float('-inf')
        self.state = {'running': False, 'error': None}

    def status(self, db, source, refresh=False):
        with self.lock:
            saved = db.execute('SELECT completed_at FROM album_index WHERE source=?', (os.path.abspath(source),)).fetchone()
            stale = not saved or time.time() - saved['completed_at'] >= 300
            if not self.state['running'] and (refresh or (stale and time.monotonic() - self.last_attempt >= 300)):
                self.last_attempt = time.monotonic()
                self.state = dict(running=True, error=None, stage='discover', checked=0, processed=0, total=0)
                self.thread = Thread(target=self.update, args=(source,), daemon=True, name='album-index')
                self.thread.start()
            return {**self.state, 'ready': bool(saved)}

    def progress(self, **values):
        with self.lock:
            self.state.update(values)

    def update(self, source):
        error = None
        try:
            with connect(self.database) as db:
                refresh_index(db, source, self.progress)
        except Exception as exc:
            logging.exception('Album index update failed')
            error = str(exc) if isinstance(exc, ValueError) else '无法更新照片目录，请检查读取权限后重试'
        finally:
            with self.lock:
                self.state.update(running=False, error=error)
                self.last_attempt = time.monotonic()


def image_orientation(metadata):
    orientation = metadata.get('Orientation', 1)
    return orientation if type(orientation) is int and 1 <= orientation <= 8 else 1


def heif_primary(path):
    # An unqualified Rotation tag can belong to a thumbnail or tile, not the primary image.
    def boxes(data):
        offset = 0
        while offset < len(data):
            if len(data) - offset < 8:
                raise ValueError('HEIC 图像属性不完整')
            size, kind = struct.unpack_from('>I4s', data, offset)
            header = 8
            if size == 1:
                if len(data) - offset < 16:
                    raise ValueError('HEIC 图像属性不完整')
                size = struct.unpack_from('>Q', data, offset + 8)[0]
                header = 16
            elif size == 0:
                size = len(data) - offset
            if size < header or offset + size > len(data):
                raise ValueError('HEIC 图像属性长度无效')
            yield kind, data[offset + header:offset + size]
            offset += size

    with path.open('rb') as image:
        end = os.fstat(image.fileno()).st_size
        while image.tell() < end:
            start = image.tell()
            header = image.read(8)
            if len(header) != 8:
                raise ValueError('HEIC 文件头不完整')
            size, kind = struct.unpack('>I4s', header)
            if size == 1:
                header += image.read(8)
                if len(header) != 16:
                    raise ValueError('HEIC 文件头不完整')
                size = struct.unpack_from('>Q', header, 8)[0]
            elif size == 0:
                size = end - start
            if size < len(header) or start + size > end:
                raise ValueError('HEIC 文件长度无效')
            if kind == b'meta':
                if size > 32 * 1024 * 1024:
                    raise ValueError('HEIC 图像属性过大')
                metadata = dict(boxes(image.read(size - len(header))[4:]))
                break
            image.seek(start + size)
        else:
            raise ValueError('HEIC 缺少主图信息')
    primary = metadata[b'pitm']
    if primary[0] not in (0, 1):
        raise ValueError('HEIC 主图标记版本不支持')
    primary_id = struct.unpack_from('>H' if primary[0] == 0 else '>I', primary, 4)[0]
    containers = list(boxes(metadata[b'iprp']))
    properties = list(boxes(dict(containers)[b'ipco']))
    transforms = []
    for kind, associations in containers:
        if kind != b'ipma':
            continue
        if associations[0] not in (0, 1):
            raise ValueError('HEIC 图像属性版本不支持')
        item_size = 2 if associations[0] == 0 else 4
        property_size = 2 if int.from_bytes(associations[1:4], 'big') & 1 else 1
        offset = 8
        for _ in range(struct.unpack_from('>I', associations, 4)[0]):
            item_id = struct.unpack_from('>H' if item_size == 2 else '>I', associations, offset)[0]
            offset += item_size
            count = associations[offset]
            offset += 1
            for _ in range(count):
                number = struct.unpack_from('>H' if property_size == 2 else '>B', associations, offset)[0]
                number &= 0x7fff if property_size == 2 else 0x7f
                offset += property_size
                if item_id != primary_id or number == 0:
                    continue
                kind, value = properties[number - 1]
                if kind == b'irot':
                    transforms.append(('', 'transpose=cclock', 'hflip,vflip', 'transpose=clock')[value[0] & 3])
                elif kind == b'imir':
                    transforms.append(('vflip', 'hflip')[value[0] & 1])
    transform = ','.join(transform for transform in transforms if transform) if transforms else None
    return primary_id, transform


def ffmpeg_image_filter(path, edge):
    scale = f"scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease"
    heic = path.suffix.lower() in {'.heic', '.heif'}
    primary_id, transform = heif_primary(path) if heic else (None, None)
    if transform is None:
        result = subprocess.run(['exiftool', '-j', '-n', '-IFD0:Orientation', str(path)],
                                capture_output=True, check=True, text=True, timeout=10)
        orientation = image_orientation(json.loads(result.stdout)[0])
        transform = {2: 'hflip', 3: 'hflip,vflip', 4: 'vflip', 5: 'transpose=clock,hflip',
                     6: 'transpose=clock', 7: 'transpose=clock,vflip', 8: 'transpose=cclock'}.get(orientation)
    resize = (transform + ',' if transform else '') + scale
    if not heic:
        return ['-noautorotate'], ['-map', '0:v:0', '-vf', resize]
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_stream_groups', '-show_streams', '-of', 'json', str(path)],
                            capture_output=True, check=True, text=True, timeout=10)
    metadata = json.loads(result.stdout)
    groups = [group for group in metadata.get('stream_groups', []) if group.get('type') == 'Tile Grid'
              and int(group.get('id', '0'), 0) == primary_id]
    if groups:
        group = groups[0]
        grid = group['components'][0]
        tiles = grid['subcomponents']
        inputs = ''.join(f"[0:{group['streams'][tile['stream_index']]['index']}]" for tile in tiles)
        layout = '|'.join(f"{tile['tile_horizontal_offset']}_{tile['tile_vertical_offset']}" for tile in tiles)
        # A HEIC grid stores separate video streams; selecting its first stream shows just one corner.
        filters = f"{inputs}xstack=inputs={len(tiles)}:layout={layout}," if len(tiles) > 1 else inputs
        filters += f"crop={grid['width']}:{grid['height']}:{grid['horizontal_offset']}:{grid['vertical_offset']}:exact=1"
        return ['-noautorotate'], ['-filter_complex', filters + ',' + resize + '[photo]', '-map', '[photo]']
    streams = [stream for stream in metadata.get('streams', []) if stream.get('codec_type') == 'video'
               and not stream.get('disposition', {}).get('dependent') and int(stream.get('id', '0'), 0) == primary_id]
    if not streams:
        raise ValueError('无法读取 HEIC 完整图像的分块信息，请检查 FFmpeg 版本')
    stream = streams[0]
    return ['-noautorotate'], ['-map', f"0:{stream['index']}", '-vf', resize]


def preview(database, path, digest, small=False):
    cache = Path(database).parent / 'remote-cache' / CACHE_VERSION
    cache.mkdir(parents=True, exist_ok=True)
    full = cache / (hashlib.sha256(digest.encode()).hexdigest() + '.jpg')
    target = full.with_name('small-' + full.name) if small else full
    if target.is_file():
        return target
    # Every display size comes from the same normalized image, decoded only once.
    if small:
        preview(database, path, digest)
    source = full if small else path
    vips, ffmpeg = shutil.which('vipsthumbnail'), shutil.which('ffmpeg')
    if source.suffix.lower() in {'.heic', '.heif'}:
        vips = None
        if not ffmpeg:
            raise ValueError('服务器需要支持 HEIC 的 FFmpeg 才能正确处理照片方向和完整图像')
    if not vips and not ffmpeg:
        raise ValueError('服务器需要 vipsthumbnail 或 ffmpeg 才能处理照片')
    with CONVERSION_LOCKS[int(full.stem[:8], 16) % len(CONVERSION_LOCKS)], CONVERSIONS:
        if target.is_file():
            return target
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            temporary = Path(directory) / 'preview.jpg'
            try:
                if vips:
                    command = ['vipsthumbnail', '--size', '320x320>' if small else '720x720>', '--vips-concurrency=2',
                               '--output', str(temporary) + '[Q=85,strip]', '--', str(source)]
                    subprocess.run(command, capture_output=True, check=True, timeout=10 if small else 30)
                else:
                    raise FileNotFoundError('vipsthumbnail')
            except (OSError, subprocess.SubprocessError) as exc:
                if not ffmpeg:
                    raise ValueError('照片解码失败，服务器缺少 FFmpeg 备用解码器') from exc
                edge = 320 if small else 720
                try:
                    inputs, filters = ffmpeg_image_filter(source, edge)
                    subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-threads', '2', '-filter_complex_threads', '2',
                                    *inputs, '-i', str(source), *filters, '-map_metadata', '-1', '-frames:v', '1',
                                    '-q:v', '3', '-threads', '2', '-update', '1', '-y', str(temporary)],
                                   capture_output=True, check=True, timeout=30)
                except subprocess.TimeoutExpired as error:
                    raise ValueError('照片解码超时，请重试') from error
                except (KeyError, IndexError, TypeError, struct.error, json.JSONDecodeError) as error:
                    raise ValueError('无法读取 HEIC 完整图像的分块信息，请检查照片文件及解码器版本') from error
                except (OSError, subprocess.SubprocessError) as error:
                    logging.warning('Photo decoding failed: vips=%s; ffmpeg=%s',
                                    getattr(exc, 'stderr', exc), getattr(error, 'stderr', error))
                    raise ValueError('照片解码失败，请检查解码器版本或文件是否完整') from error
            try:
                with temporary.open('rb') as image:
                    if image.read(3) != b'\xff\xd8\xff':
                        raise ValueError('照片解码器未生成有效 JPEG，请重试')
                os.replace(temporary, target)
            except OSError as exc:
                raise ValueError('照片预览保存失败，请检查服务器存储空间和权限') from exc
    return target


def photo_data(path, taken_at, stamp, track, image_path):
    try:
        width, height = get_image_info(image_path)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError('照片尺寸读取失败，请检查 ffprobe') from exc
    points = [point for segment in track['segments'] for point in segment]
    index = min((i for i, point in enumerate(points) if point['time'] is not None),
                key=lambda i: abs(points[i]['time'] - stamp))
    return {'name': path.stem + '.jpg', 'width': width, 'height': height, 'taken_at': taken_at,
            'lat': points[index]['lat'], 'lng': points[index]['lng'], 'point_index': index, 'match_type': 'time'}


def stored_preview(db, database, source, photo, sign):
    linked = db.execute('SELECT digest FROM shortcut_photos WHERE photo_id=?', (photo['id'],)).fetchone()
    if not linked or not linked['digest'].startswith('album:'):
        return None
    digest = linked['digest']
    name = hashlib.sha256(digest.encode()).hexdigest() + '.jpg'
    cache = Path(database).parent / 'remote-cache'
    corrected = cache / CACHE_VERSION / name
    if corrected.is_file():
        return corrected
    try:
        track = record_track(db, photo['item_id'])
        for entry in scan(db, source, photo['item_id'], track, sign)['photos']:
            encoded = entry['token'].split('.')[0]
            if 'album:' + hashlib.sha256(encoded.encode()).hexdigest() == digest:
                path, _, _, _ = candidate(source, entry['token'], photo['item_id'], track, sign)
                return preview(database, path, digest)
    except (OSError, ValueError, LookupError):
        # A missing or changed backup must not hide the existing stored photo.
        pass
    return None
