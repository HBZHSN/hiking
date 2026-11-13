import safety

import io
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from backend import live_photos
from backend.storage import connect, initialize
import test_photo_sync as fixtures


class LivePhotoTests(unittest.TestCase):
    setUp = fixtures.PhotoSyncTests.setUp
    request = fixtures.PhotoSyncTests.request
    create = fixtures.PhotoSyncTests.create
    scan = fixtures.PhotoSyncTests.scan
    refresh = fixtures.PhotoSyncTests.refresh
    token = fixtures.PhotoSyncTests.token
    import_token = fixtures.PhotoSyncTests.import_token

    def video(self):
        video = self.root / 'live.mp4'
        video.write_bytes(b'0123456789')
        return video

    def test_pair_requires_matching_identifier_and_rejects_symlinks_and_ambiguity(self):
        movie = self.source.with_suffix('.MOV')
        movie.write_bytes(b'movie')
        for identifier, expected in [('same', movie), ('other', None), (None, None)]:
            metadata = [{'SourceFile': str(self.source), 'ContentIdentifier': 'same'},
                        {'SourceFile': str(movie), 'ContentIdentifier': identifier}]
            with patch('backend.live_photos.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata))):
                self.assertEqual(live_photos.companion(self.source), expected)
        movie.unlink()
        movie.symlink_to(self.video())
        with patch('backend.live_photos.subprocess.run') as run:
            self.assertIsNone(live_photos.companion(self.source))
            run.assert_not_called()

    def test_import_repeat_upgrade_list_and_delete(self):
        token = self.token()
        _, photo = self.import_token(token)
        self.assertFalse(photo['live'])
        with patch('backend.live_photos.preview', return_value=self.video()):
            status, upgraded = self.import_token(token)
            self.assertEqual(status, 200)
            self.assertEqual(upgraded['id'], photo['id'])
            self.assertTrue(upgraded['live'])
            status, repeated = self.import_token(token)
        self.assertEqual(repeated, upgraded)
        self.assertNotIn('live_path', upgraded)
        self.assertEqual(self.request(f"/api/items/{photo['item_id']}/photos")[1], [upgraded])
        self.assertEqual(self.request(f"/api/photos/{photo['id']}/live"), (200, b'0123456789'))
        with connect(self.app.database) as db:
            video = Path(db.execute('SELECT live_path FROM photos WHERE id=?', (photo['id'],)).fetchone()[0])
        self.request(f"/api/photos/{photo['id']}", 'DELETE')
        self.assertFalse(video.exists())
        self.assertTrue(self.source.exists())

    def test_live_range_auth_and_share_scope(self):
        token = self.token()
        with patch('backend.live_photos.preview', return_value=self.video()):
            _, photo = self.import_token(token)
        path = f"/api/photos/{photo['id']}/live"
        for requested, status, body in [('bytes=0-1', 206, b'01'), ('bytes=3-', 206, b'3456789'),
                                        ('bytes=-3', 206, b'789'), ('bytes=0-999', 206, b'0123456789'),
                                        ('bytes=20-', 416, b''), ('bytes=-0', 416, b''),
                                        ('bytes=5-2', 416, b''), ('bytes=0-1,3-4', 416, b'')]:
            env = {'PATH_INFO': path, 'REQUEST_METHOD': 'GET', 'HTTP_COOKIE': self.cookie, 'HTTP_RANGE': requested}
            code, data, headers = self.app.handle(env)
            self.assertEqual((code, data), (status, body), requested)
            self.assertIn(('Accept-Ranges', 'bytes'), headers)
        _, share = self.request(f"/api/items/{photo['item_id']}/shares", 'POST', {})
        other = self.create('record')
        _, other_share = self.request(f"/api/items/{other['id']}/shares", 'POST', {})
        self.cookie = ''
        self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request(share['url'] + path)[0], 200)
        self.assertEqual(self.request(other_share['url'] + path)[0], 404)

    def test_backfill_idempotent_missing_backup_and_conversion_failure(self):
        token = self.token()
        _, photo = self.import_token(token)
        with patch('backend.live_photos.preview', side_effect=ValueError('conversion failed')):
            counts = live_photos.backfill(self.app.database, self.album, lambda _: None)
        self.assertEqual(counts['failed'], 1)
        self.assertFalse(self.request(f"/api/items/{photo['item_id']}/photos")[1][0]['live'])
        with patch('backend.live_photos.preview', return_value=self.video()) as convert:
            counts = live_photos.backfill(self.app.database, self.album, lambda _: None)
            self.assertEqual(counts['added'], 1)
            counts = live_photos.backfill(self.app.database, self.album, lambda _: None)
            self.assertEqual(counts['existing'], 1)
            self.assertEqual(convert.call_count, 1)
        initialize(self.app.database)
        self.assertTrue(self.request(f"/api/items/{photo['item_id']}/photos")[1][0]['live'])

    def test_failed_live_import_can_retry_without_creating_static_duplicate(self):
        token = self.token()
        with patch('backend.live_photos.preview', side_effect=ValueError('bad video')):
            self.assertEqual(self.import_token(token)[0], 400)
        self.assertEqual(self.request(f"/api/items/{self.record['id']}/photos")[1], [])
        with patch('backend.live_photos.preview', return_value=self.video()):
            self.assertTrue(self.import_token(token)[1]['live'])

    def test_timestamp_named_upload_requires_unique_time_and_existing_backup(self):
        _, photo = self.import_token(self.token())
        with connect(self.app.database) as db:
            db.execute('DELETE FROM shortcut_photos WHERE photo_id=?', (photo['id'],))
            db.execute('UPDATE photos SET filename=? WHERE id=?', ('IMG_20260101_103000.jpg', photo['id']))
            row = db.execute('SELECT * FROM photos WHERE id=?', (photo['id'],)).fetchone()
            self.assertEqual(live_photos.original(db, self.album, row), self.source)
            duplicate = self.source.with_name('another.HEIC')
            duplicate.write_bytes(self.source.read_bytes())
            stat = duplicate.stat()
            db.execute('INSERT INTO album_metadata VALUES(?,?,?,?,?,?)',
                       (str(self.album), str(duplicate.relative_to(self.album)), stat.st_size,
                        stat.st_mtime_ns, stat.st_ctime_ns, row['taken_at']))
            self.assertIsNone(live_photos.original(db, self.album, row))
            self.source.unlink()
            duplicate.unlink()
            self.assertIsNone(live_photos.original(db, self.album, row))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg unavailable')
    def test_real_conversion_preserves_audio_and_cache(self):
        movie = self.root / 'fixture.mov'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=96x64:r=12',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '0.5',
                        '-c:v', 'libx264', '-c:a', 'aac', '-threads', '1', str(movie)], check=True, timeout=20)
        original = movie.read_bytes()
        with patch('backend.live_photos.companion', return_value=movie):
            video = live_photos.preview(self.app.database, self.source)
            with patch('backend.live_photos.subprocess.run', side_effect=AssertionError('cached conversion')):
                self.assertEqual(live_photos.preview(self.app.database, self.source), video)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(video)],
                               capture_output=True, text=True, check=True, timeout=10)
        streams = json.loads(probe.stdout)['streams']
        self.assertEqual({s['codec_name'] for s in streams}, {'h264', 'aac'})
        self.assertEqual(movie.read_bytes(), original)
