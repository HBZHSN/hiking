import safety  # Install protection before importing application code.
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from threading import Event, Lock
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote
import zlib

from backend import photo_sync
from backend.server import App
from backend.storage import connect


def png(width=48, height=32):
    def chunk(name, data):
        return struct.pack('!I', len(data)) + name + data + struct.pack('!I', zlib.crc32(name + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\x28\x78\x50' * width) * height)) + chunk(b'IEND', b''))


class PhotoSyncTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.album = self.root / 'album'
        self.album.mkdir()
        self.app = App(self.root / 'test.sqlite3', 'test-password', photo_source=self.album)
        self.cookie = ''
        self.request('/api/login', 'POST', {'password': 'test-password'})
        start = datetime(2026, 1, 1, 2, tzinfo=timezone.utc).timestamp() * 1000
        self.track = {'system': 'WGS84', 'segments': [[
            {'lng': 119, 'lat': 30, 'time': start}, {'lng': 119.1, 'lat': 30.1, 'time': start + 3600000}]]}
        self.record = self.create('record')
        self.endpoint = f"/api/items/{self.record['id']}/photo-sync"
        self.source = self.album / '新年' / 'IMG_0001.HEIC'
        self.source.parent.mkdir()
        self.source.write_bytes(b'fixture-heic')
        self.metadata = [{'SourceFile': str(self.source), 'DateTimeOriginal': '2026:01:01 10:30:00',
                          'OffsetTimeOriginal': '+08:00'}]
        self.preview = self.root / 'preview.jpg'
        self.preview.write_bytes(b'\xff\xd8\xffprocessed\xff\xd9')

    def request(self, path, method='GET', data=None, marker='1'):
        raw = json.dumps(data).encode() if data is not None else b''
        route, _, query = path.partition('?')
        env = {'PATH_INFO': route, 'QUERY_STRING': query, 'REQUEST_METHOD': method,
               'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(raw)),
               'wsgi.input': io.BytesIO(raw), 'HTTP_COOKIE': self.cookie,
               'HTTP_HOST': 'localhost', 'HTTP_X_HIKING_REQUEST': marker}
        captured = {}
        def start(status, headers):
            captured.update(status=int(status.split()[0]), headers=dict(headers))
        result = b''.join(self.app(env, start))
        if 'Set-Cookie' in captured['headers']:
            self.cookie = captured['headers']['Set-Cookie'].split(';')[0]
        if captured['headers']['Content-Type'].startswith('application/json'):
            result = json.loads(result)
        return captured['status'], result

    def create(self, kind, track=None):
        status, item = self.request('/api/items', 'POST', {'name': '虚构记录', 'kind': kind, 'track': track or self.track})
        self.assertEqual(status, 201)
        return item

    def scan(self, metadata=None):
        with patch('backend.photo_sync.shutil.which', return_value='/test/exiftool'), patch(
                'backend.photo_sync.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(
                    self.metadata if metadata is None else metadata), '')) as run:
            self.refresh()
            status, result = self.request(self.endpoint + '/scan', 'POST', {})
        self.assertEqual(status, 200, result)
        return result, run

    def refresh(self):
        with connect(self.app.database) as db:
            photo_sync.refresh_index(db, self.app.photo_source)

    def token(self):
        return self.scan()[0]['photos'][0]['token']

    def import_token(self, token):
        with patch('backend.photo_sync.preview', return_value=self.preview), patch(
                'backend.photo_sync.get_image_info', return_value=(48, 32)):
            return self.request(self.endpoint + '/import', 'POST', {'token': token})

    def assert_image_corners(self, image, dimensions, order):
        self.assertEqual(photo_sync.get_image_info(image), dimensions)
        colors = {'R': (220, 30, 30), 'G': (30, 210, 30), 'B': (30, 30, 220), 'Y': (220, 210, 30)}
        width, height = dimensions
        for (x, y), letter in zip(((.2, .2), (.8, .2), (.2, .8), (.8, .8)), order):
            actual = subprocess.run(['vips', 'getpoint', str(image), str(int(width * x)), str(int(height * y))],
                                    capture_output=True, check=True, text=True, timeout=10).stdout
            self.assertTrue(all(abs(float(value) - channel) < 12 for value, channel in zip(actual.split(), colors[letter])), actual)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('exiftool', 'ffmpeg', 'ffprobe', 'vipsthumbnail', 'vips')), 'local photo tools unavailable')
    def test_heic_exif_only_previews_and_full_images_have_the_same_direction(self):
        self.source.write_bytes((Path(__file__).parent / 'fixtures' / 'tiled-colors.heic').read_bytes())
        subprocess.run(['exiftool', '-overwrite_original', '-Orientation#=6', str(self.source)],
                       capture_output=True, check=True, timeout=10)
        full = photo_sync.preview(self.app.database, self.source, 'exif-only')
        small = photo_sync.preview(self.app.database, self.source, 'exif-only', small=True)
        self.assert_image_corners(full, (352, 480), 'BRYG')
        self.assert_image_corners(small, (235, 320), 'BRYG')

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('exiftool', 'ffmpeg', 'ffprobe', 'vips')), 'local photo tools unavailable')
    def test_heic_uses_primary_rotation_instead_of_another_items_rotation(self):
        self.source.write_bytes((Path(__file__).parent / 'fixtures' / 'tiled-colors-multiple-rotations.heic').read_bytes())
        misleading = subprocess.run(['exiftool', '-j', '-n', '-Rotation', str(self.source)],
                                    capture_output=True, check=True, text=True, timeout=10)
        self.assertEqual(json.loads(misleading.stdout)[0]['Rotation'], 0)
        real_which = shutil.which
        with patch('backend.photo_sync.shutil.which', side_effect=lambda tool: None if tool == 'vipsthumbnail' else real_which(tool)):
            image = photo_sync.preview(self.app.database, self.source, 'multiple-rotations')
        self.assert_image_corners(image, (352, 480), 'GYRB')

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('exiftool', 'ffmpeg', 'ffprobe', 'vipsthumbnail', 'vips')), 'local photo tools unavailable')
    def test_all_exif_directions_for_heic_and_jpeg_with_and_without_vips(self):
        fixture = Path(__file__).parent / 'fixtures' / 'tiled-colors.heic'
        seed = self.root / 'colors.jpg'
        subprocess.run(['vips', 'copy', str(fixture), str(seed)], capture_output=True, check=True, timeout=10)
        real_which = shutil.which
        for extension in ('heic', 'jpg'):
            for orientation, order in enumerate(('RGBY', 'GRYB', 'YBGR', 'BYRG', 'RBGY', 'BRYG', 'YGBR', 'GYRB'), 1):
                source = self.root / f'exif-{orientation}.{extension}'
                source.write_bytes((fixture if extension == 'heic' else seed).read_bytes())
                subprocess.run(['exiftool', '-overwrite_original', f'-Orientation#={orientation}', str(source)],
                               capture_output=True, check=True, timeout=10)
                dimensions = (352, 480) if orientation >= 5 else (480, 352)
                small_dimensions = (235, 320) if orientation >= 5 else (320, 235)
                for vips_available in (True, False):
                    with self.subTest(extension=extension, orientation=orientation, vips=vips_available), patch(
                            'backend.photo_sync.shutil.which', side_effect=lambda tool: None if tool == 'vipsthumbnail' and not vips_available else real_which(tool)):
                        digest = f'{extension}-{orientation}-{vips_available}'
                        small = photo_sync.preview(self.app.database, source, digest, small=True)
                        full = photo_sync.preview(self.app.database, source, digest)
                        self.assert_image_corners(small, small_dimensions, order)
                        self.assert_image_corners(full, dimensions, order)
                        metadata = subprocess.run(['exiftool', '-j', '-n', '-Orientation', str(full)],
                                                  capture_output=True, check=True, text=True, timeout=10)
                        self.assertNotIn('Orientation', json.loads(metadata.stdout)[0])

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('exiftool', 'ffmpeg', 'ffprobe', 'vips')), 'local photo tools unavailable')
    def test_primary_heif_rotation_overrides_conflicting_exif_including_zero_rotation(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'tiled-colors-rotated.heic').read_bytes()
        for rotation, order in enumerate(('RGBY', 'GYRB', 'YBGR', 'BRYG')):
            with self.subTest(rotation=rotation):
                content = bytearray(fixture)
                content[content.index(b'irot') + 4] = rotation
                source = self.root / f'heif-{rotation}.heic'
                source.write_bytes(content)
                subprocess.run(['exiftool', '-overwrite_original', '-Orientation#=6', str(source)],
                               capture_output=True, check=True, timeout=10)
                small = photo_sync.preview(self.app.database, source, str(rotation), small=True)
                full = photo_sync.preview(self.app.database, source, str(rotation))
                self.assert_image_corners(full, (352, 480) if rotation % 2 else (480, 352), order)
                self.assert_image_corners(small, (235, 320) if rotation % 2 else (320, 235), order)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe', 'vipsthumbnail', 'vips')), 'local photo tools unavailable')
    def test_primary_heif_mirroring_is_applied_once(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'tiled-colors-rotated.heic').read_bytes()
        for axis, order in enumerate(('BYRG', 'GRYB')):
            with self.subTest(axis=axis):
                content = bytearray(fixture.replace(b'irot', b'imir'))
                content[content.index(b'imir') + 4] = axis
                source = self.root / f'mirror-{axis}.heic'
                source.write_bytes(content)
                full = photo_sync.preview(self.app.database, source, f'mirror-{axis}')
                self.assert_image_corners(full, (480, 352), order)
                reference = self.root / f'mirror-reference-{axis}.jpg'
                subprocess.run(['vipsthumbnail', '--size', '720x720>', '--output', str(reference) + '[strip]', str(source)],
                               capture_output=True, check=True, timeout=10)
                self.assert_image_corners(reference, (480, 352), order)

    def test_heif_primary_ids_wide_associations_and_extended_box_lengths(self):
        def box(kind, data, extended=False):
            return (struct.pack('>I4sQ', 1, kind, len(data) + 16) if extended
                    else struct.pack('>I4s', len(data) + 8, kind)) + data
        for version in (0, 1):
            for wide in (0, 1):
                with self.subTest(version=version, wide=wide):
                    primary_id = 5 if version == 0 else 65537
                    item_format = '>H' if version == 0 else '>I'
                    property_format = '>B' if wide == 0 else '>H'
                    essential = 0x80 if wide == 0 else 0x8000
                    primary = bytes([version, 0, 0, 0]) + struct.pack(item_format, primary_id)
                    properties = box(b'irot', b'\3') + box(b'imir', b'\1') + box(b'irot', b'\0')
                    associations = (bytes([version, 0, 0, wide]) + struct.pack('>I', 2)
                                    + struct.pack(item_format, primary_id) + b'\2'
                                    + struct.pack(property_format, essential | 2) + struct.pack(property_format, essential | 1)
                                    + struct.pack(item_format, 1) + b'\1' + struct.pack(property_format, essential | 3))
                    metadata = (bytes(4) + box(b'pitm', primary, True)
                                + box(b'iprp', box(b'ipco', properties, True) + box(b'ipma', associations)))
                    self.source.write_bytes(box(b'ftyp', b'heic') + box(b'mdat', b'skip image data', True)
                                            + box(b'meta', metadata, True))
                    self.assertEqual(photo_sync.heif_primary(self.source), (primary_id, 'hflip,transpose=clock'))
        # A zero-size box occupies the rest of the file.
        self.source.write_bytes(struct.pack('>I4s', 0, b'meta') + metadata)
        self.assertEqual(photo_sync.heif_primary(self.source), (primary_id, 'hflip,transpose=clock'))

    def test_heic_chooses_primary_stream_even_when_another_image_has_a_grid(self):
        self.source.write_bytes((Path(__file__).parent / 'fixtures' / 'tiled-colors-rotated.heic').read_bytes())
        metadata = {'stream_groups': [{'id': '0x6', 'type': 'Tile Grid', 'disposition': {'default': 1}}],
                    'streams': [{'index': 2, 'id': '0x5', 'codec_type': 'video', 'width': 480, 'height': 352},
                                {'index': 0, 'id': '0x6', 'codec_type': 'video', 'width': 960, 'height': 640,
                                 'disposition': {'default': 1}}]}
        with patch('backend.photo_sync.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata))):
            inputs, filters = photo_sync.ffmpeg_image_filter(self.source, 320)
        self.assertEqual(inputs, ['-noautorotate'])
        self.assertEqual(filters[:2], ['-map', '0:2'])
        self.assertIn('transpose=cclock', filters[-1])

    def test_invalid_heif_properties_return_errors_without_starting_a_decoder(self):
        for content in (b'bad', struct.pack('>I4s', 2, b'meta'), struct.pack('>I4s', 99, b'meta'),
                        struct.pack('>I4s', 12, b'meta') + bytes(4),
                        struct.pack('>I4s', 20, b'meta') + bytes(4) + struct.pack('>I4s', 9, b'pitm')):
            with self.subTest(content=content):
                self.source.write_bytes(content)
                token = self.token()
                with patch('backend.photo_sync.shutil.which', side_effect=lambda tool: '/test/' + tool), patch(
                        'backend.photo_sync.subprocess.run', side_effect=AssertionError('decoder called for invalid HEIC')):
                    status, result = self.request(self.endpoint + '/preview?token=' + quote(token))
                self.assertEqual(status, 400, result)
                self.assertIn('HEIC', result['error'])

    def test_heic_requires_ffmpeg_to_keep_direction_consistent(self):
        with patch('backend.photo_sync.shutil.which', side_effect=lambda tool: None if tool == 'ffmpeg' else '/test/' + tool):
            with self.assertRaisesRegex(ValueError, 'FFmpeg'):
                photo_sync.preview(self.app.database, self.source, 'no-ffmpeg')

    def test_scan_uses_capture_time_inclusively_and_skips_symlinks_and_undated(self):
        entries = []
        for name, date in [('start.jpg', '2026:01:01 10:00:00'), ('end.PNG', '2026:01:01 11:00:00'),
                           ('before.jpeg', '2026:01:01 09:59:59'), ('after.webp', '2026:01:01 11:00:01'),
                           ('no-time.jpg', None), ('bad.jpg', 'not a date')]:
            path = self.album / name
            path.write_bytes(b'fixture')
            entries.append({'SourceFile': str(path), 'DateTimeOriginal': date})
        # Backup modification time must never replace the capture time.
        os.utime(self.source, (1, 1))
        outside = self.root / 'private.jpg'
        outside.write_bytes(b'private')
        (self.album / 'link.jpg').symlink_to(outside)
        (self.album / 'outside').symlink_to(self.root, target_is_directory=True)
        (self.album / 'movie.mov').write_bytes(b'video')
        result, run = self.scan(self.metadata + entries)
        self.assertEqual([photo['filename'] for photo in result['photos']], ['start.jpg', 'IMG_0001.HEIC', 'end.PNG'])
        self.assertEqual(result['scanned'], 7)
        self.assertEqual(result['skipped'], 2)
        self.assertTrue(all(not photo['synced'] for photo in result['photos']))
        arguments = '\n'.join(call.kwargs['input'] for call in run.call_args_list)
        self.assertIn(str(self.source), arguments)
        self.assertNotIn('link.jpg', arguments)
        self.assertNotIn('outside', arguments)
        self.assertNotIn('movie.mov', arguments)
        self.assertEqual(self.request(f"/api/items/{self.record['id']}/photos")[1], [])

    def test_timestamp_offsets_subseconds_fallback_and_invalid_values(self):
        examples = [({'SubSecDateTimeOriginal': '2026:01:01 10:30:00.123+08:00'}, '2026-01-01T02:30:00.123000+00:00'),
                    ({'DateTimeOriginal': '2026:01:01 04:30:00', 'OffsetTimeOriginal': '+02:00'}, '2026-01-01T02:30:00+00:00'),
                    ({'CreateDate': '2026:01:01 10:30:00'}, '2026-01-01T02:30:00+00:00'),
                    ({'DateTimeOriginal': 'bad', 'SubSecCreateDate': '2026:01:01 02:30:00Z'}, '2026-01-01T02:30:00+00:00')]
        for metadata, expected in examples:
            with self.subTest(metadata=metadata):
                self.assertEqual(photo_sync.taken_time(metadata).isoformat(), expected)
        for metadata in ({}, {'DateTimeOriginal': '0000:00:00 00:00:00'}, {'DateTimeOriginal': 123}):
            self.assertIsNone(photo_sync.taken_time(metadata))

    def test_scan_reuses_persistent_dates_and_only_reads_added_or_changed_files(self):
        first, run = self.scan()
        self.assertEqual(first['updated'], 1)
        self.assertEqual(run.call_count, 1)
        self.app = App(self.app.database, 'test-password', photo_source=self.album)
        second, run = self.scan()
        run.assert_not_called()
        self.assertEqual((second['cached'], second['updated']), (1, 0))
        self.assertEqual(first['photos'], second['photos'])
        added = self.album / 'new.jpg'
        added.write_bytes(b'new photo')
        changed = {'SourceFile': str(added), 'DateTimeOriginal': '2026:01:01 11:00:01'}
        third, run = self.scan([changed])
        self.assertEqual(run.call_args.kwargs['input'], str(added))
        self.assertEqual(third['photos'], second['photos'])
        added.write_bytes(b'updated photo')
        changed['DateTimeOriginal'] = '2026:01:01 10:45:00'
        fourth, run = self.scan([changed])
        self.assertEqual(run.call_args.kwargs['input'], str(added))
        self.assertEqual(len(fourth['photos']), 2)
        added.unlink()
        fifth, run = self.scan()
        run.assert_not_called()
        self.assertEqual(fifth['photos'], first['photos'])

    def test_nas_generated_thumbnail_and_recycle_directories_are_excluded(self):
        for folder in photo_sync.IGNORED_FOLDERS:
            path = self.album / folder
            path.mkdir()
            (path / 'generated.jpg').write_bytes(b'not an original')
        result, run = self.scan()
        self.assertEqual(result['scanned'], 1)
        self.assertEqual(run.call_args.kwargs['input'], str(self.source))

    def test_metadata_edits_preserving_size_and_modification_time_are_reindexed(self):
        self.scan()
        stat = self.source.stat()
        self.source.write_bytes(b'changed-heic')
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual((self.source.stat().st_size, self.source.stat().st_mtime_ns), (stat.st_size, stat.st_mtime_ns))
        changed = {**self.metadata[0], 'DateTimeOriginal': '2026:01:01 10:45:00'}
        result, run = self.scan([changed])
        self.assertEqual(run.call_args.kwargs['input'], str(self.source))
        self.assertEqual(result['photos'][0]['taken_at'], '2026-01-01T02:45:00+00:00')

    def test_fast_scan_retries_late_metadata_and_caches_undated_files(self):
        with patch('backend.photo_sync.read_metadata', side_effect=[
                {str(self.source): {'SourceFile': str(self.source)}},
                {str(self.source): self.metadata[0]}]) as read:
            self.refresh()
            status, result = self.request(self.endpoint + '/scan', 'POST', {})
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result['photos']), 1)
        self.assertEqual([call.args[1] for call in read.call_args_list], ['-fast2', '-fast'])
        self.source.write_bytes(b'no timestamp')
        result, _ = self.scan([{'SourceFile': str(self.source)}])
        self.assertEqual(result['skipped'], 1)
        result, run = self.scan()
        run.assert_not_called()
        self.assertEqual(result['skipped'], 1)

    def test_interrupted_first_scan_preserves_completed_batches(self):
        for index in range(257):
            (self.album / f'{index}.jpg').write_bytes(b'fixture')
        def metadata(paths, fast):
            return {str(path): {'SourceFile': str(path), 'DateTimeOriginal': '2026:01:01 10:30:00'} for path in paths}
        def interrupted(paths, fast):
            if len(paths) != 256:
                raise ValueError('模拟扫描中断')
            return metadata(paths, fast)
        with patch('backend.photo_sync.read_metadata', side_effect=interrupted):
            with self.assertRaisesRegex(ValueError, '模拟扫描中断'):
                self.refresh()
        with connect(self.app.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM album_metadata').fetchone()[0], 256)
        with patch('backend.photo_sync.read_metadata', side_effect=metadata) as read:
            self.refresh()
            status, result = self.request(self.endpoint + '/scan', 'POST', {})
        self.assertEqual(status, 200, result)
        self.assertEqual((result['cached'], result['updated']), (256, 2))
        self.assertEqual(len(read.call_args.args[0]), 2)

    def test_time_query_uses_sqlite_index_without_accessing_album_files(self):
        first, _ = self.scan()
        with connect(self.app.database) as db:
            db.executemany('INSERT INTO album_metadata VALUES(?,?,?,?,?,?)',
                           ((str(self.album), f'old/{index}.jpg', 1, 1, 1, '2025-01-01T00:00:00+00:00') for index in range(2000)))
            plan = db.execute('EXPLAIN QUERY PLAN SELECT path FROM album_metadata '
                              'WHERE source=? AND taken_at>=? AND taken_at<=?',
                              (str(self.album), first['start'], first['end'])).fetchall()
            self.assertTrue(any('idx_album_time' in row['detail'] for row in plan))
        original_stat, original_resolve = Path.stat, Path.resolve
        def guard(method):
            def checked(path, *args, **options):
                if path == self.album or path.is_relative_to(self.album):
                    raise AssertionError('query accessed the album')
                return method(path, *args, **options)
            return checked
        with patch('backend.photo_sync.os.walk', side_effect=AssertionError('directory walk')), patch.object(
                Path, 'stat', guard(original_stat)), patch.object(
                Path, 'resolve', guard(original_resolve)), patch(
                'backend.photo_sync.read_metadata', side_effect=AssertionError('EXIF read')):
            status, result = self.request(self.endpoint + '/scan', 'POST', {})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['photos'], first['photos'])
        self.assertFalse(result['index']['running'])

    def test_index_query_preserves_subsecond_time_boundaries(self):
        entries = []
        for index, date in enumerate(('10:00:00', '10:00:00.000001', '10:00:00.500000', '10:00:00.500001', '11:00:00')):
            path = self.album / f'{index}.jpg'
            path.write_bytes(b'fixture')
            entries.append({'SourceFile': str(path), 'DateTimeOriginal': '2026:01:01 ' + date})
        self.scan(entries + self.metadata)
        track = json.loads(json.dumps(self.track))
        track['segments'][0][1]['time'] = track['segments'][0][0]['time'] + 500
        with connect(self.app.database) as db:
            result = photo_sync.scan(db, self.album, self.record['id'], track, self.app.digest)
        self.assertEqual([photo['filename'] for photo in result['photos']], ['0.jpg', '1.jpg', '2.jpg'])
        track['segments'][0][0]['time'] += .001
        with connect(self.app.database) as db:
            result = photo_sync.scan(db, self.album, self.record['id'], track, self.app.digest)
        self.assertEqual([photo['filename'] for photo in result['photos']], ['1.jpg', '2.jpg'])

    def test_first_index_build_is_background_single_worker_and_polls_partial_results(self):
        for index in range(257):
            (self.album / f'{index}.jpg').write_bytes(b'fixture')
        blocked, release = Event(), Event()
        def metadata(paths, fast):
            if len(paths) != 256:
                blocked.set()
                if not release.wait(5):
                    raise AssertionError('background worker was not released')
            return {str(path): {'SourceFile': str(path), 'DateTimeOriginal': '2026:01:01 10:30:00'} for path in paths}
        with patch('backend.photo_sync.read_metadata', side_effect=metadata) as read:
            try:
                status, first = self.request(self.endpoint + '/scan', 'POST', {})
                self.assertEqual(status, 200, first)
                self.assertTrue(first['index']['running'])
                self.assertTrue(blocked.wait(3))
                worker = self.app.album_index.thread
                status, partial = self.request(self.endpoint + '/scan', 'POST', {'refresh': True})
                self.assertEqual(status, 200, partial)
                self.assertEqual(len(partial['photos']), 256)
                self.assertTrue(partial['index']['running'])
                self.assertIs(self.app.album_index.thread, worker)
                self.assertEqual(read.call_count, 2)
            finally:
                release.set()
                self.app.album_index.thread.join(5)
        self.assertFalse(worker.is_alive())
        status, done = self.request(self.endpoint + '/scan', 'POST', {})
        self.assertEqual(status, 200, done)
        self.assertFalse(done['index']['running'])
        self.assertTrue(done['index']['ready'])
        self.assertEqual(len(done['photos']), 258)

    def test_refresh_error_keeps_results_and_explicit_retry_recovers(self):
        first, _ = self.scan()
        with patch('backend.photo_sync.refresh_index', side_effect=ValueError('模拟权限错误')) as refresh, self.assertLogs(level='ERROR'):
            self.request(self.endpoint + '/scan', 'POST', {'refresh': True})
            self.app.album_index.thread.join(3)
            status, failed = self.request(self.endpoint + '/scan', 'POST', {})
            self.assertEqual(status, 200, failed)
            self.assertEqual(failed['photos'], first['photos'])
            self.assertEqual(failed['index']['error'], '模拟权限错误')
            self.assertFalse(failed['index']['running'])
            self.assertEqual(refresh.call_count, 1)
        with patch('backend.photo_sync.read_metadata') as read:
            self.request(self.endpoint + '/scan', 'POST', {'refresh': True})
            self.app.album_index.thread.join(3)
            read.assert_not_called()
        self.assertIsNone(self.request(self.endpoint + '/scan', 'POST', {})[1]['index']['error'])

    def test_stale_index_returns_existing_results_during_background_refresh(self):
        first, _ = self.scan()
        with connect(self.app.database) as db:
            db.execute('UPDATE album_index SET completed_at=0')
        blocked, release = Event(), Event()
        original_refresh = photo_sync.refresh_index
        def delayed(db, source, progress):
            blocked.set()
            if not release.wait(5):
                raise AssertionError('background worker was not released')
            original_refresh(db, source, progress)
        with patch('backend.photo_sync.refresh_index', side_effect=delayed) as refresh:
            try:
                status, result = self.request(self.endpoint + '/scan', 'POST', {})
                self.assertEqual(status, 200, result)
                self.assertEqual(result['photos'], first['photos'])
                self.assertTrue(result['index']['running'])
                self.assertTrue(blocked.wait(3))
                again = self.request(self.endpoint + '/scan', 'POST', {})[1]
                self.assertEqual(again['photos'], first['photos'])
                self.assertEqual(refresh.call_count, 1)
            finally:
                release.set()
                self.app.album_index.thread.join(5)

    def test_unreadable_changed_file_is_removed_from_index_and_can_recover(self):
        self.scan()
        self.source.write_bytes(b'changed fixture')
        result, _ = self.scan([{'SourceFile': str(self.source), 'Error': 'unreadable'}])
        self.assertEqual(result['photos'], [])
        self.assertEqual(result['skipped'], 1)
        recovered, run = self.scan()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(recovered['photos']), 1)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe', 'vips')), 'local photo tools unavailable')
    def test_real_heic_ffmpeg_fallback_previews_and_imports_without_vips_decoder(self):
        seed = self.root / 'seed.png'
        seed.write_bytes(png(960, 640))
        subprocess.run(['vips', 'heifsave', str(seed), str(self.source), '--bitdepth', '10', '--vips-concurrency=2'],
                       capture_output=True, check=True, timeout=30)
        self.scan()
        real_run = subprocess.run
        commands = []
        def unsupported_vips(command, **options):
            commands.append(command)
            if command[0] == 'vipsthumbnail':
                raise subprocess.CalledProcessError(1, command, stderr=b'Unsupported HEIC auxiliary image')
            return real_run(command, **options)
        token = self.request(self.endpoint + '/scan', 'POST', {})[1]['photos'][0]['token']
        with patch('backend.photo_sync.subprocess.run', side_effect=unsupported_vips):
            status, image = self.request(self.endpoint + '/preview?token=' + quote(token))
            self.assertEqual(status, 200, image)
            self.assertTrue(image.startswith(b'\xff\xd8\xff'))
            status, imported = self.request(self.endpoint + '/import', 'POST', {'token': token})
            self.assertEqual(status, 201, imported)
        self.assertEqual((imported['width'], imported['height']), (720, 480))
        path, _, _, digest = photo_sync.candidate(self.album, token, self.record['id'], self.track, self.app.digest)
        self.assertEqual(photo_sync.get_image_info(photo_sync.preview(self.app.database, path, digest, small=True)), (320, 213))
        self.assertEqual(sum(Path(command[0]).name == 'ffmpeg' for command in commands), 2)
        real_which = shutil.which
        with patch('backend.photo_sync.shutil.which', side_effect=lambda name: real_which(name) if name != 'vipsthumbnail' else None):
            self.assertEqual(photo_sync.get_image_info(photo_sync.preview(self.app.database, path, 'no-vips', small=True)), (320, 213))

    def test_normalized_thumbnail_timeout_falls_back_without_decoding_heic_again(self):
        full = photo_sync.hashlib.sha256(b'timeout-fallback').hexdigest() + '.jpg'
        cache = self.root / 'remote-cache' / photo_sync.CACHE_VERSION
        cache.mkdir(parents=True)
        normalized = cache / full
        normalized.write_bytes(self.preview.read_bytes())
        commands = []
        def decode(command, **options):
            commands.append(command)
            if command[0] == 'vipsthumbnail':
                self.assertEqual(options['timeout'], 10)
                raise subprocess.TimeoutExpired(command, 10)
            self.assertEqual(Path(command[0]).name, 'ffmpeg')
            self.assertEqual(command[command.index('-i') + 1], str(normalized))
            Path(command[-1]).write_bytes(self.preview.read_bytes())
            return subprocess.CompletedProcess(command, 0)
        with patch('backend.photo_sync.shutil.which', side_effect=lambda tool: '/test/' + tool), patch(
                'backend.photo_sync.ffmpeg_image_filter', return_value=([], ['-map', '0:v:0'])), patch(
                'backend.photo_sync.subprocess.run', side_effect=decode):
            self.assertTrue(photo_sync.preview(self.app.database, self.source, 'timeout-fallback', small=True).is_file())
        self.assertEqual(len(commands), 2)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe', 'vips')), 'local photo tools unavailable')
    def test_tiled_heic_fallback_assembles_all_four_corners_and_ignores_old_cache(self):
        self.source.write_bytes((Path(__file__).parent / 'fixtures' / 'tiled-colors.heic').read_bytes())
        token = self.token()
        path, _, _, digest = photo_sync.candidate(self.album, token, self.record['id'], self.track, self.app.digest)
        old_cache = self.root / 'remote-cache' / 'photo-sync'
        old_cache.mkdir(parents=True)
        name = photo_sync.hashlib.sha256(digest.encode()).hexdigest() + '.jpg'
        for prefix in ('', 'small-'):
            (old_cache / (prefix + name)).write_bytes(self.preview.read_bytes())
        real_which = shutil.which
        with patch('backend.photo_sync.shutil.which', side_effect=lambda tool: None if tool == 'vipsthumbnail' else real_which(tool)):
            small = photo_sync.preview(self.app.database, path, digest, small=True)
            self.assertEqual(photo_sync.get_image_info(small), (320, 235))
            status, imported = self.request(self.endpoint + '/import', 'POST', {'token': token})
            self.assertEqual(status, 201, imported)
            self.assertEqual((imported['width'], imported['height']), (480, 352))
        self.assertNotEqual(small.read_bytes(), self.preview.read_bytes())
        image = self.root / 'assembled.jpg'
        image.write_bytes(self.request(f"/api/photos/{imported['id']}")[1])
        for x, y, expected in ((80, 80, (220, 30, 30)), (360, 80, (30, 210, 30)),
                               (80, 280, (30, 30, 220)), (360, 280, (220, 210, 30))):
            actual = subprocess.run(['vips', 'getpoint', str(image), str(x), str(y)],
                                    capture_output=True, check=True, text=True, timeout=10).stdout
            self.assertTrue(all(abs(float(value) - channel) < 12 for value, channel in zip(actual.split(), expected)), actual)
        rotated = self.root / 'rotated.heic'
        rotated.write_bytes((Path(__file__).parent / 'fixtures' / 'tiled-colors-rotated.heic').read_bytes())
        with patch('backend.photo_sync.shutil.which', side_effect=lambda tool: None if tool == 'vipsthumbnail' else real_which(tool)):
            image = photo_sync.preview(self.app.database, rotated, 'rotated-grid')
        self.assertEqual(photo_sync.get_image_info(image), (352, 480))
        for x, y, expected in ((80, 80, (30, 210, 30)), (280, 80, (220, 210, 30)),
                               (80, 360, (220, 30, 30)), (280, 360, (30, 30, 220))):
            actual = subprocess.run(['vips', 'getpoint', str(image), str(x), str(y)],
                                    capture_output=True, check=True, text=True, timeout=10).stdout
            self.assertTrue(all(abs(float(value) - channel) < 12 for value, channel in zip(actual.split(), expected)), actual)

    def test_heic_without_grid_information_never_returns_a_dependent_tile_as_photo(self):
        self.source.write_bytes((Path(__file__).parent / 'fixtures' / 'tiled-colors.heic').read_bytes())
        metadata = {'stream_groups': [], 'streams': [{'index': 0, 'codec_type': 'video',
                     'width': 512, 'height': 512, 'disposition': {'dependent': 1}}]}
        with patch('backend.photo_sync.subprocess.run', side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps([{}])), subprocess.CompletedProcess([], 0, json.dumps(metadata))]):
            with self.assertRaisesRegex(ValueError, '完整图像'):
                photo_sync.ffmpeg_image_filter(self.source, 320)

    def test_invalid_exif_orientation_is_ignored(self):
        for orientation in range(1, 9):
            self.assertEqual(photo_sync.image_orientation({'Orientation': orientation}), orientation)
        for orientation in (0, 9, None, '6', True):
            self.assertEqual(photo_sync.image_orientation({'Orientation': orientation}), 1)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('exiftool', 'ffmpeg', 'ffprobe', 'vips')), 'local photo tools unavailable')
    def test_old_album_photo_is_corrected_when_viewed_and_missing_backup_keeps_stored_copy(self):
        self.source.write_bytes((Path(__file__).parent / 'fixtures' / 'tiled-colors-rotated.heic').read_bytes())
        token = self.token()
        _, _, _, digest = photo_sync.candidate(self.album, token, self.record['id'], self.track, self.app.digest)
        status, imported = self.import_token(token)
        self.assertEqual(status, 201, imported)
        old_cache = self.root / 'remote-cache' / 'photo-sync-v2'
        old_cache.mkdir(parents=True)
        (old_cache / (photo_sync.hashlib.sha256(digest.encode()).hexdigest() + '.jpg')).write_bytes(self.preview.read_bytes())
        real_which = shutil.which
        with patch('backend.photo_sync.shutil.which', side_effect=lambda tool: None if tool == 'vipsthumbnail' else real_which(tool)):
            status, image = self.request(f"/api/photos/{imported['id']}?v=3")
        self.assertEqual(status, 200, image)
        path = self.root / 'corrected.jpg'
        path.write_bytes(image)
        self.assertEqual(photo_sync.get_image_info(path), (352, 480))
        self.source.unlink()
        with patch('backend.photo_sync.preview', side_effect=AssertionError('decoded cached photo again')):
            self.assertEqual(self.request(f"/api/photos/{imported['id']}?v=3")[1], image)
        cache = self.root / 'remote-cache' / photo_sync.CACHE_VERSION
        for cached in cache.glob('*.jpg'):
            cached.unlink()
        self.assertEqual(self.request(f"/api/photos/{imported['id']}?v=3")[1], self.preview.read_bytes())
        with connect(self.app.database) as db:
            stored = db.execute('SELECT file_path FROM photos WHERE id=?', (imported['id'],)).fetchone()
        self.assertEqual(Path(stored['file_path']).read_bytes(), self.preview.read_bytes())
        self.assertEqual(len(self.request(f"/api/items/{self.record['id']}/photos")[1]), 1)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('exiftool', 'ffmpeg', 'ffprobe', 'vips')), 'local photo tools unavailable')
    def test_ffmpeg_normalizes_exif_rotation_once_for_jpeg_and_single_image_heic(self):
        seed = self.root / 'colors.png'
        subprocess.run(['vips', 'copy', str(Path(__file__).parent / 'fixtures' / 'tiled-colors.heic'), str(seed)],
                       capture_output=True, check=True, timeout=10)
        real_which = shutil.which
        for extension in ('jpg', 'heic'):
            source = self.root / ('orientation.' + extension)
            subprocess.run(['vips', 'copy', str(seed), str(source), '--vips-concurrency=2'], capture_output=True, check=True, timeout=10)
            subprocess.run(['exiftool', '-overwrite_original', '-Orientation#=6', str(source)], capture_output=True, check=True, timeout=10)
            with patch('backend.photo_sync.shutil.which', side_effect=lambda tool: None if tool == 'vipsthumbnail' else real_which(tool)):
                image = photo_sync.preview(self.app.database, source, 'orientation-' + extension)
            self.assertEqual(photo_sync.get_image_info(image), (352, 480))
            for x, y, expected in ((80, 80, (30, 30, 220)), (280, 80, (220, 30, 30)),
                                   (80, 360, (220, 210, 30)), (280, 360, (30, 210, 30))):
                actual = subprocess.run(['vips', 'getpoint', str(image), str(x), str(y)],
                                        capture_output=True, check=True, text=True, timeout=10).stdout
                self.assertTrue(all(abs(float(value) - channel) < 12 for value, channel in zip(actual.split(), expected)), actual)

    def test_preview_conversion_concurrency_and_duplicate_requests_are_bounded(self):
        source = self.root / 'source.jpg'
        source.write_bytes(self.preview.read_bytes())
        active = peak = 0
        lock = Lock()
        def convert(command, **options):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.02)
                output = command[command.index('--output') + 1].split('[')[0]
                Path(output).write_bytes(self.preview.read_bytes())
                return subprocess.CompletedProcess(command, 0)
            finally:
                with lock:
                    active -= 1
        with patch('backend.photo_sync.shutil.which', return_value='/test/vipsthumbnail'), patch(
                'backend.photo_sync.subprocess.run', side_effect=convert) as run, ThreadPoolExecutor(max_workers=6) as workers:
            futures = [workers.submit(photo_sync.preview, self.app.database, source, str(index)) for index in range(6)]
            self.assertTrue(all(future.result().is_file() for future in futures))
            self.assertEqual(peak, 2)
            run.reset_mock()
            futures = [workers.submit(photo_sync.preview, self.app.database, source, 'same-photo') for _ in range(6)]
            self.assertEqual(len({future.result() for future in futures}), 1)
            self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('exiftool', 'ffmpeg', 'ffprobe', 'vipsthumbnail', 'vips')), 'local photo tools unavailable')
    def test_heic_ignores_embedded_thumbnail_direction_and_decodes_original_once(self):
        self.source.write_bytes((Path(__file__).parent / 'fixtures' / 'tiled-colors.heic').read_bytes())
        jpeg = self.root / 'thumbnail.jpg'
        subprocess.run(['vips', 'copy', str(self.source), str(jpeg)], capture_output=True, check=True, timeout=10)
        subprocess.run(['exiftool', '-overwrite_original', '-Orientation#=8', str(jpeg)], capture_output=True, check=True, timeout=10)
        subprocess.run(['exiftool', '-overwrite_original', '-Orientation#=6', '-ThumbnailImage<=' + str(jpeg), str(self.source)],
                       capture_output=True, check=True, timeout=10)
        original = self.source.read_bytes(), self.source.stat().st_mtime_ns
        real_run = subprocess.run
        commands = []
        def convert(command, **options):
            commands.append(command)
            return real_run(command, **options)
        with patch('backend.photo_sync.subprocess.run', side_effect=convert):
            with ThreadPoolExecutor(max_workers=6) as workers:
                requests = [workers.submit(photo_sync.preview, self.app.database, self.source, 'embedded-test', small=bool(i % 2))
                            for i in range(6)]
                self.assertTrue(all(request.result().is_file() for request in requests))
            small = photo_sync.preview(self.app.database, self.source, 'embedded-test', small=True)
            full = photo_sync.preview(self.app.database, self.source, 'embedded-test')
            self.assertEqual(photo_sync.preview(self.app.database, self.source, 'embedded-test', small=True), small)
        self.assert_image_corners(small, (235, 320), 'BRYG')
        self.assert_image_corners(full, (352, 480), 'BRYG')
        decodes = [command for command in commands if '-i' in command and command[command.index('-i') + 1] == str(self.source)]
        self.assertEqual(len(decodes), 1)
        self.assertFalse(any('[thumbnail=true]' in str(command) or '-ThumbnailImage' in command for command in commands))
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), original)

    def test_import_matches_track_copies_readonly_source_deduplicates_and_allows_reimport_after_delete(self):
        token = self.token()
        original, modified = self.source.read_bytes(), self.source.stat().st_mtime_ns
        status, photo = self.import_token(token)
        self.assertEqual(status, 201, photo)
        self.assertEqual((photo['match_type'], photo['point_index'], photo['lat'], photo['lng']), ('time', 0, 30, 119))
        self.assertEqual((photo['filename'], photo['width'], photo['height']), ('IMG_0001.jpg', 48, 32))
        self.assertEqual(photo['taken_at'], '2026-01-01T02:30:00+00:00')
        self.assertNotIn('file_path', photo)
        self.assertEqual(self.request(f"/api/photos/{photo['id']}")[1], self.preview.read_bytes())
        status, again = self.import_token(token)
        self.assertEqual(status, 200)
        self.assertEqual(again['id'], photo['id'])
        self.assertEqual(len(self.request(f"/api/items/{self.record['id']}/photos")[1]), 1)
        self.assertTrue(self.scan()[0]['photos'][0]['synced'])
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), (original, modified))
        self.request(f"/api/photos/{photo['id']}", 'DELETE')
        self.assertFalse(self.scan()[0]['photos'][0]['synced'])
        self.assertEqual(self.import_token(token)[0], 201)

    def test_preview_is_jpeg_and_does_not_import(self):
        token = self.token()
        for query in ('', '&size=large'):
            with patch('backend.photo_sync.preview', return_value=self.preview) as preview:
                status, body = self.request(self.endpoint + '/preview?token=' + quote(token) + query)
                self.assertEqual(preview.call_args.kwargs['small'], not bool(query))
            self.assertEqual(status, 200)
            self.assertEqual(body, self.preview.read_bytes())
        self.assertEqual(self.request(f"/api/items/{self.record['id']}/photos")[1], [])

    def test_tampering_wrong_record_changed_source_and_changed_track_are_rejected(self):
        token = self.token()
        for invalid in (None, '', 'broken', token[:-1] + ('a' if token[-1] != 'a' else 'b')):
            self.assertEqual(self.import_token(invalid)[0], 400)
        other = self.create('record')
        self.assertEqual(self.request(f"/api/items/{other['id']}/photo-sync/import", 'POST', {'token': token})[0], 400)
        self.source.write_bytes(b'changed')
        self.assertEqual(self.import_token(token)[0], 400)
        token = self.token()
        track = json.loads(json.dumps(self.track))
        for point in track['segments'][0]:
            point['time'] += 86400000
        self.request(f"/api/items/{self.record['id']}", 'PATCH', {'name': '虚构记录', 'kind': 'record', 'track': track})
        self.assertEqual(self.import_token(token)[0], 400)

    def test_signed_paths_still_cannot_escape_album_or_follow_links(self):
        outside = self.root / 'outside.jpg'
        outside.write_bytes(b'private')
        (self.album / 'link.jpg').symlink_to(outside)
        for relative in ('../outside.jpg', str(outside), 'link.jpg'):
            payload = [self.record['id'], relative, outside.stat().st_size, outside.stat().st_mtime_ns,
                       '2026-01-01T02:30:00+00:00']
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
            token = encoded + '.' + self.app.digest('photo-sync:' + encoded)
            self.assertEqual(self.import_token(token)[0], 400)

    def test_login_csrf_record_time_and_share_boundaries(self):
        self.assertEqual(self.request('/photo-sync.html')[0], 200)
        self.assertEqual(self.request('/js/photo-sync.js')[0], 200)
        self.assertEqual(self.request(self.endpoint + '/scan', 'POST', {}, marker='')[0], 403)
        self.assertEqual(self.request(self.endpoint + '/scan')[0], 405)
        self.assertEqual(self.request('/api/items/999/photo-sync/scan', 'POST', {})[0], 404)
        route = self.create('route')
        self.assertEqual(self.request(f"/api/items/{route['id']}/photo-sync/scan", 'POST', {})[0], 400)
        no_time = {'system': 'WGS84', 'segments': [[{'lng': 119, 'lat': 30}, {'lng': 119.1, 'lat': 30.1}]]}
        record = self.create('record', no_time)
        with patch('backend.photo_sync.scan') as scan:
            self.assertEqual(self.request(f"/api/items/{record['id']}/photo-sync/scan", 'POST', {})[0], 400)
            scan.assert_not_called()
        link = self.request(f"/api/items/{self.record['id']}/shares", 'POST', {})[1]['url']
        for suffix in ('/photo-sync.html', '/js/photo-sync.js', self.endpoint + '/preview?token=abc'):
            self.assertEqual(self.request(link + suffix)[0], 404)
        self.cookie = ''
        for path, method in (('/photo-sync.html', 'GET'), ('/js/photo-sync.js', 'GET'),
                             (self.endpoint + '/scan', 'POST'), (self.endpoint + '/import', 'POST'),
                             (self.endpoint + '/preview', 'GET')):
            self.assertEqual(self.request(path, method, {} if method == 'POST' else None)[0], 401)

    def test_empty_directory_missing_tool_and_scan_timeout_are_recoverable(self):
        empty = self.root / 'empty'
        empty.mkdir()
        self.app.photo_source = empty
        with patch('backend.photo_sync.shutil.which', return_value='/test/exiftool'), patch('backend.photo_sync.subprocess.run') as run:
            self.refresh()
            self.assertEqual(self.request(self.endpoint + '/scan', 'POST', {})[1]['photos'], [])
            run.assert_not_called()
        self.app.photo_source = self.root / 'missing'
        with self.assertRaisesRegex(ValueError, '目录不存在'):
            self.refresh()
        self.app.photo_source = self.album
        with patch('backend.photo_sync.shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'exiftool'):
                self.refresh()
        with patch('backend.photo_sync.shutil.which', return_value='/test/exiftool'), patch(
                'backend.photo_sync.subprocess.run', side_effect=subprocess.TimeoutExpired('exiftool', 180)):
            with self.assertRaisesRegex(ValueError, '扫描失败或超时'):
                self.refresh()

    def test_conversion_failure_does_not_add_a_photo_and_retry_succeeds(self):
        token = self.token()
        with patch('backend.photo_sync.shutil.which', return_value='/test/vipsthumbnail'), patch(
                'backend.photo_sync.ffmpeg_image_filter', return_value=([], ['-map', '0:v:0'])), patch(
                'backend.photo_sync.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'vipsthumbnail')), self.assertLogs(level='WARNING'):
            self.assertEqual(self.request(self.endpoint + '/import', 'POST', {'token': token})[0], 400)
        self.assertEqual(self.request(f"/api/items/{self.record['id']}/photos")[1], [])
        self.assertEqual(self.import_token(token)[0], 201)

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('exiftool', 'vipsthumbnail', 'ffmpeg', 'ffprobe', 'vips')),
                         'local photo tools unavailable')
    def test_real_temporary_png_heic_heif_and_oriented_jpeg_scan_conversion_and_cache(self):
        self.source.unlink()
        for extension in ('png', 'heic', 'heif', 'jpg'):
            with self.subTest(extension=extension):
                source = self.album / f'fixture.{extension}'
                if extension == 'png':
                    source.write_bytes(png(960, 640))
                elif extension in ('heic', 'heif'):
                    subprocess.run(['vips', 'copy', str(self.album / 'fixture.png'), str(source), '--vips-concurrency=2'],
                                   capture_output=True, check=True, timeout=30)
                    if extension == 'heic':
                        thumb = self.root / 'thumb.png'
                        thumb.write_bytes(png())
                        thumb_jpeg = self.root / 'thumb.jpg'
                        subprocess.run(['vips', 'copy', str(thumb), str(thumb_jpeg)], capture_output=True, check=True, timeout=10)
                        subprocess.run(['exiftool', '-overwrite_original', '-ThumbnailImage<=' + str(thumb_jpeg), str(source)],
                                       capture_output=True, check=True, timeout=10)
                else:
                    tiny = self.root / 'tiny.png'
                    tiny.write_bytes(png())
                    subprocess.run(['vips', 'copy', str(tiny), str(source)], capture_output=True, check=True, timeout=10)
                    subprocess.run(['exiftool', '-overwrite_original', '-Orientation#=6', str(source)],
                                   capture_output=True, check=True, timeout=10)
                subprocess.run(['exiftool', '-overwrite_original', '-DateTimeOriginal=2026:01:01 10:45:00',
                                '-OffsetTimeOriginal=+08:00', str(source)], capture_output=True, check=True, timeout=10)
                original, modified = source.read_bytes(), source.stat().st_mtime_ns
                self.refresh()
                status, scanned = self.request(self.endpoint + '/scan', 'POST', {})
                self.assertEqual(status, 200, scanned)
                photo = next(photo for photo in scanned['photos'] if photo['filename'] == source.name)
                preview_url = self.endpoint + '/preview?token=' + quote(photo['token'])
                status, preview = self.request(preview_url)
                self.assertEqual(status, 200, preview)
                self.assertTrue(preview.startswith(b'\xff\xd8\xff'))
                if extension == 'heic':
                    path, _, _, digest = photo_sync.candidate(self.album, photo['token'], self.record['id'], self.track, self.app.digest)
                    self.assertEqual(photo_sync.get_image_info(photo_sync.preview(self.app.database, path, digest, small=True)), (320, 213))
                with patch('backend.photo_sync.subprocess.run') as run:
                    self.assertEqual(self.request(preview_url)[1], preview)
                    run.assert_not_called()
                status, imported = self.request(self.endpoint + '/import', 'POST', {'token': photo['token']})
                self.assertEqual(status, 201, imported)
                self.assertEqual((imported['width'], imported['height']), (32, 48) if extension == 'jpg' else (720, 480))
                self.assertEqual(imported['point_index'], 1)
                self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), (original, modified))
                self.assertTrue(self.request(f"/api/photos/{imported['id']}")[1].startswith(b'\xff\xd8\xff'))
                path, _, _, digest = photo_sync.candidate(self.album, photo['token'], self.record['id'], self.track, self.app.digest)
                self.assertLessEqual(max(photo_sync.get_image_info(photo_sync.preview(self.app.database, path, digest, small=True))), 320)
