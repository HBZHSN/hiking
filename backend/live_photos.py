"""Read-only Live Photo pairing and browser-compatible copies with original audio."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from backend import photo_sync


def companion(path):
    candidates = [p for p in path.parent.iterdir()
                  if p.stem.casefold() == path.stem.casefold() and p.suffix.lower() in {'.mov', '.mp4'}
                  and p.resolve() == p and p.is_file()]
    if not candidates:
        return None
    try:
        result = subprocess.run(['exiftool', '-j', '-ContentIdentifier', '--', str(path), *map(str, candidates)],
                                capture_output=True, text=True, check=True, timeout=30)
        metadata = json.loads(result.stdout)
        ids = {entry['SourceFile']: entry.get('ContentIdentifier') for entry in metadata}
        identifier = ids.get(str(path))
        matches = [p for p in candidates if identifier and ids.get(str(p)) == identifier]
        return matches[0] if len(matches) == 1 else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('无法确认 Live 配对，请检查 exiftool 或备份文件') from exc


def preview(database, path):
    movie = companion(path)
    if movie is None:
        return None
    stat = movie.stat()
    signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    digest = hashlib.sha256(f'{movie}:{signature}'.encode()).hexdigest()
    cache = Path(database).parent / 'remote-cache' / 'live-v1'
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = cache / (digest + '.mp4')
    with photo_sync.CONVERSION_LOCKS[int(digest[:8], 16) % len(photo_sync.CONVERSION_LOCKS)], photo_sync.CONVERSIONS:
        if target.is_file():
            return target
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            temporary = Path(directory) / 'live.mp4'
            try:
                probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                        '-of', 'json', str(movie)], capture_output=True, text=True, check=True, timeout=15)
                duration = float(json.loads(probe.stdout)['format']['duration'])
                if not 0 < duration <= 30:
                    raise ValueError('Live 视频时长无效或超过 30 秒')
                subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '2', '-i', str(movie),
                                '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1',
                                '-vf', "scale=w='min(720,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p',
                                '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', '-threads', '2',
                                '-y', str(temporary)], capture_output=True, check=True, timeout=120)
                current = movie.stat()
                if movie.resolve() != movie or (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != signature:
                    raise ValueError('Live 备份已变化，请重新同步')
                os.replace(temporary, target)
            except (OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
                raise ValueError('Live 转换失败，请检查 FFmpeg 和存储空间后重试') from exc
    return target


def attach(db, photo, video):
    if video is None or (photo['live_path'] and Path(photo['live_path']).is_file()):
        return
    target = Path(photo['file_path']).with_suffix('.mp4')
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.mp4', delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copyfile(video, temporary_path)
        os.replace(temporary_path, target)
        db.execute('UPDATE photos SET live_path=? WHERE id=?', (str(target), photo['id']))
    finally:
        temporary_path.unlink(missing_ok=True)


def original(db, source, photo):
    if not photo['taken_at']:
        return None
    linked = db.execute('SELECT digest FROM shortcut_photos WHERE photo_id=?', (photo['id'],)).fetchone()
    matches = []
    # The index narrows recovery to the original capture time; never guess by filename alone.
    for row in db.execute('SELECT * FROM album_metadata WHERE source=? AND '
                          'julianday(taken_at)=julianday(?)', (os.path.abspath(source), photo['taken_at'])):
        token = photo_sync.candidate_token(photo['item_id'], row['path'], row['size'], row['modified'], row['taken_at'], lambda _: '')
        digest = 'album:' + hashlib.sha256(token.split('.')[0].encode()).hexdigest()
        if linked and linked['digest'].startswith('album:'):
            if linked['digest'] != digest:
                continue
        elif (not re.fullmatch(r'IMG_\d{8}_\d{6}', Path(photo['filename']).stem)
              and Path(row['path']).stem.casefold() != Path(photo['filename']).stem.casefold()):
            continue
        try:
            path = photo_sync.source_path(source, row['path'])
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) == (row['size'], row['modified']):
                matches.append(path)
        except (OSError, ValueError):
            continue
    return matches[0] if len(matches) == 1 else None


def backfill(database, source, progress=print):
    from backend.storage import connect, initialize
    initialize(database)
    counts = dict(added=0, existing=0, no_live=0, unmatched=0, failed=0)
    with connect(database) as db:
        photos = db.execute('SELECT * FROM photos ORDER BY id').fetchall()
    for photo in photos:
        try:
            if photo['live_path'] and Path(photo['live_path']).is_file():
                counts['existing'] += 1
                continue
            with connect(database) as db:
                path = original(db, source, photo)
            if path is None:
                counts['unmatched'] += 1
                continue
            video = preview(database, path)
            if video is None:
                counts['no_live'] += 1
                continue
            with connect(database) as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT * FROM photos WHERE id=?', (photo['id'],)).fetchone()
                if current:
                    attach(db, current, video)
                    counts['added'] += 1
        except (OSError, ValueError) as exc:
            counts['failed'] += 1
            progress(f'photo {photo["id"]}: {exc}')
        finally:
            progress(json.dumps(counts))
    return counts


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='为现有照片补同步 Live；备份原片只读')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] / photo_sync.DEFAULT_SOURCE)
    args = parser.parse_args()
    if not args.database.is_file() or not args.source.is_dir():
        parser.error('数据库和备份目录必须已存在')
    print(json.dumps(backfill(args.database, args.source.resolve())))
