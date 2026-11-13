import safety  # Install protection before importing application code.
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from backend.server import App, config
from backend.storage import connect
from backend.import_gpx import import_file, parse_gpx

TRACK = {'system': 'WGS84', 'segments': [[
    {'lng':119.1, 'lat':30.1, 'ele':100, 'time':1760000000000, 'hr':108, 'cad':32},
    {'lng':119.2, 'lat':30.2, 'ele':150, 'time':1760000600000, 'hr':135, 'cad':40}]]}
GPX = b'''<gpx xmlns="http://www.topografix.com/GPX/1/1" xmlns:t="http://www.garmin.com/xmlschemas/TrackPointExtension/v1"><trk><name>test</name><trkseg><trkpt lat="30.1" lon="119.1"><ele>100</ele><time>2026-01-01T00:00:00Z</time><extensions><t:hr>100</t:hr><t:cad>32</t:cad></extensions></trkpt><trkpt lat="30.2" lon="119.2"><ele>150</ele></trkpt></trkseg></trk></gpx>'''


class ConfigTests(unittest.TestCase):
    def test_file_configuration_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory, patch('backend.server.ROOT', Path(directory)), patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / '.env'
            env.write_text('HIKING_PASSWORD=file-test-password\nPHOTO_SOURCE=\n')
            self.assertEqual(config()['HIKING_PASSWORD'], 'file-test-password')
            self.assertEqual(config()['PHOTO_SOURCE'], str(Path(directory) / '.local/photo-source'))
            album = str(Path(directory) / 'album')
            env.write_text(f'HIKING_PASSWORD=file-test-password\nPHOTO_SOURCE={album}\n')
            self.assertEqual(config()['PHOTO_SOURCE'], album)
            override = str(Path(directory) / 'override')
            with patch.dict(os.environ, {'HIKING_PASSWORD': 'changed-password', 'PHOTO_SOURCE': override}):
                self.assertEqual(config()['HIKING_PASSWORD'], 'changed-password')
                self.assertEqual(config()['PHOTO_SOURCE'], override)

    def test_missing_short_and_placeholder_passwords_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch('backend.server.ROOT', Path(directory)), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                config()
            for password in ('', '12345', 'replace-with-a-long-private-password'):
                with self.subTest(password=password):
                    (Path(directory) / '.env').write_text(f'HIKING_PASSWORD={password}\n')
                    with self.assertRaises(RuntimeError):
                        config()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'test.sqlite3'
        self.app = App(self.db, 'correct-password')
        self.cookie = ''

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, path, method='GET', body=None, origin=None, marker='1'):
        raw = json.dumps(body).encode() if body is not None else b''
        env = {'PATH_INFO':path, 'REQUEST_METHOD':method, 'CONTENT_TYPE':'application/json',
               'CONTENT_LENGTH':str(len(raw)), 'wsgi.input':io.BytesIO(raw),
               'HTTP_COOKIE':self.cookie, 'HTTP_HOST':'localhost:8010', 'REMOTE_ADDR':'127.0.0.1',
               'HTTP_X_HIKING_REQUEST':marker}
        if origin:
            env['HTTP_ORIGIN'] = origin
        captured = {}
        def start(status, headers):
            captured.update(status=int(status.split()[0]), headers=dict(headers))
        result = b''.join(self.app(env, start))
        self.headers = captured['headers']
        if 'Set-Cookie' in captured['headers']:
            self.cookie = captured['headers']['Set-Cookie'].split(';')[0]
        if captured['headers'].get('Content-Type', '').startswith('application/json'):
            result = json.loads(result)
        return captured['status'], result

    def login(self):
        self.assertEqual(self.request('/api/login', 'POST', {'password':'correct-password'})[0], 200)

    def test_thumbnails_are_private_cached_and_deleted_with_item(self):
        self.assertEqual(self.request('/api/items/1/thumbnail')[0], 401)
        self.login()
        self.app.tianditu_key = 'test-key'
        route = self.create()[1]
        record = self.create(kind='record', route_id=route['id'])[1]
        for entry in (route, record):
            path = f"/api/items/{entry['id']}/thumbnail"
            with patch.object(self.app, 'remote_cache', return_value=(b'image', 'image/png')) as tile:
                status, image = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn(b'<svg', image)
                calls = tile.call_count
                self.assertGreater(calls, 0)
                self.assertEqual(self.request(path)[1], image)
                self.assertEqual(tile.call_count, calls)
            cached = self.db.parent / 'remote-cache' / 'thumbnails' / f"{entry['id']}.svg"
            self.assertTrue(cached.exists())
            self.request(f"/api/items/{entry['id']}", 'DELETE')
            self.assertFalse(cached.exists())
            self.assertEqual(self.request(path)[0], 404)
        self.request('/api/logout', 'POST')
        self.assertEqual(self.request('/api/items/1/thumbnail')[0], 401)

    def test_storage_settings_are_private_and_count_actual_files(self):
        self.assertEqual(self.request('/api/settings/storage')[0], 401)
        self.assertEqual(self.request('/settings.html')[0], 401)
        self.login()
        self.assertEqual(self.request('/settings.html')[0], 200)
        self.assertEqual(self.request('/js/settings.js')[0], 200)
        data = self.request('/api/settings/storage')[1]
        self.assertEqual(data['cache'], {'bytes': 0, 'files': 0})
        self.assertEqual(data['photos'], {'bytes': 0, 'files': 0})
        for folder in ('remote-cache', 'photos/1'):
            path = self.db.parent / folder
            path.mkdir(parents=True)
            (path / 'sample').write_bytes(b'12345')
        (self.db.parent / 'photos/link').symlink_to(self.db)
        data = self.request('/api/settings/storage')[1]
        self.assertEqual(data['cache'], {'bytes': 5, 'files': 1})
        self.assertEqual(data['photos'], {'bytes': 5, 'files': 1})
        self.assertGreaterEqual(data['database_bytes'], self.db.stat().st_size)
        self.assertGreater(data['disk_total_bytes'], data['disk_free_bytes'])

    def test_initial_six_character_password_login(self):
        self.app = App(self.db, '123456')
        self.assertEqual(self.request('/api/login', 'POST', {'password': 'wrong'})[0], 401)
        self.assertEqual(self.request('/api/login', 'POST', {'password': '123456'})[0], 200)
        self.assertEqual(self.request('/api/items')[0], 200)

    def create(self, kind='route', route_id=None, **extra):
        payload = {'name':'示例山径', 'notes':'test', 'kind':kind, 'route_id':route_id, 'track':copy.deepcopy(TRACK)}
        payload.update(extra)
        return self.request('/api/items', 'POST', payload)

    def test_password_boundary_and_static_allowlist(self):
        for path in ['/api/items','/index.html','/js/app.js','/.env','/data/hiking.sqlite3']:
            self.assertEqual(self.request(path)[0], 401)
        self.assertIn(b'password', self.request('/')[1])
        self.assertEqual(self.request('/api/login','POST',{'password':'wrong'})[0],401)
        self.login()
        for path in ['/.env','/data/hiking.sqlite3','/../backend/server.py','/backend/server.py']:
            self.assertEqual(self.request(path)[0],404)
        self.assertEqual(self.request('/')[0],200)
        self.request('/api/logout','POST')
        self.assertEqual(self.request('/api/items')[0],401)

    def test_share_creation_requires_login_csrf_and_valid_expiry(self):
        self.assertEqual(self.request('/api/items/1/shares', 'POST', {})[0], 401)
        self.login()
        entry = self.create()[1]
        path = f"/api/items/{entry['id']}/shares"
        self.assertEqual(self.request(path, 'POST', {}, marker='')[0], 403)
        self.assertEqual(self.request(path, 'POST', {}, origin='https://evil.example')[0], 403)
        for days in (0, -1, 366, 1.5, True, None, '7'):
            with self.subTest(days=days):
                self.assertEqual(self.request(path, 'POST', {'expires_in_days': days})[0], 400)
        self.assertEqual(self.request('/api/items/999/shares', 'POST', {})[0], 404)
        with patch('backend.server.time.time', return_value=1760000000):
            status, default = self.request(path, 'POST', {})
            self.assertEqual(status, 201)
            self.assertEqual(default['expires_at'], 1760000000 + 7 * 86400)
            for days in (1, 30, 365):
                status, custom = self.request(path, 'POST', {'expires_in_days': days})
                self.assertEqual(status, 201)
                self.assertEqual(custom['expires_at'], 1760000000 + days * 86400)
                self.assertNotEqual(default['url'], custom['url'])
        token = default['url'].rsplit('/', 1)[1]
        self.assertEqual(len(token), 43)
        with connect(self.db) as db:
            rows = db.execute('SELECT token, item_id FROM shares').fetchall()
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row['token'] != token and row['item_id'] == entry['id'] for row in rows))
            self.assertIn(self.app.digest(token), [row['token'] for row in rows])

    def test_share_is_anonymous_read_only_and_scoped_to_one_item(self):
        self.login()
        route = self.create()[1]
        record = self.create('record', route['id'])[1]
        unrelated = self.create()[1]
        links = [self.request(f"/api/items/{entry['id']}/shares", 'POST', {})[1]['url']
                 for entry in (route, record)]
        self.cookie = ''
        for entry, link in zip((route, record), links):
            with self.subTest(kind=entry['kind']):
                status, html = self.request(link, marker='')
                self.assertEqual(status, 200)
                self.assertIn(f'<base href="{link}/">'.encode(), html)
                self.assertNotIn('Set-Cookie', self.headers)
                self.assertEqual(self.headers['Referrer-Policy'], 'no-referrer')
                self.assertEqual(self.headers['Cache-Control'], 'no-store')
                self.assertEqual(self.request(link + '/api/share')[1]['item_id'], entry['id'])
                self.assertEqual(self.request(link + f"/api/items/{entry['id']}")[1], entry)
                for suffix in ('/', '/index.html', '/style.css', '/js/app.js', '/js/route.js',
                               '/js/coordinates.js', '/js/elevation.js', '/js/exif.js', '/credits.html',
                               f"/api/items/{entry['id']}/photos", f"/api/items/{entry['id']}/thumbnail"):
                    self.assertEqual(self.request(link + suffix, marker='')[0], 200, suffix)
                for suffix in ('/api/items', f"/api/items/{unrelated['id']}",
                               f"/api/items/{unrelated['id']}/photos", f"/api/items/{unrelated['id']}/thumbnail",
                               '/api/session', '/api/settings/storage', '/api/settings/shortcut-token',
                               '/settings.html', '/js/settings.js', '/.env', '/backend/server.py',
                               '/api/shortcut/gpx', '/shortcuts/hiking-gpx.shortcut'):
                    self.assertEqual(self.request(link + suffix)[0], 404, suffix)
                other = record if entry['kind'] == 'route' else route
                self.assertEqual(self.request(link + f"/api/items/{other['id']}")[0], 404)
                for method, suffix in (('PATCH', f"/api/items/{entry['id']}"),
                                       ('DELETE', f"/api/items/{entry['id']}"),
                                       ('POST', f"/api/items/{entry['id']}/photos"),
                                       ('POST', f"/api/items/{entry['id']}/shares"),
                                       ('POST', '/api/login'), ('POST', '/api/logout')):
                    self.assertEqual(self.request(link + suffix, method, {})[0], 405)
        for path in ('/api/items', f"/api/items/{route['id']}", '/api/session', '/js/app.js'):
            self.assertEqual(self.request(path)[0], 401)
        self.login()
        self.assertEqual(self.request(links[0] + f"/api/items/{unrelated['id']}")[0], 404)
        self.assertEqual(self.request(links[0] + f"/api/items/{route['id']}", 'DELETE')[0], 405)
        self.assertEqual(len(self.request('/api/items')[1]), 3)

    def test_shared_photos_and_map_resources_require_a_valid_scoped_link(self):
        self.login()
        entry = self.create('record')[1]
        unrelated = self.create('record')[1]
        photo_data = {'name': 'test.jpg', 'data': 'aGVsbG8=', 'width': 10, 'height': 10}
        photo = self.request(f"/api/items/{entry['id']}/photos", 'POST', photo_data)[1]
        other_photo = self.request(f"/api/items/{unrelated['id']}/photos", 'POST', photo_data)[1]
        link = self.request(f"/api/items/{entry['id']}/shares", 'POST', {})[1]['url']
        self.cookie = ''
        self.app.tianditu_key = 'test-key'
        self.assertEqual(self.request(link + f"/api/photos/{photo['id']}")[1], b'hello')
        self.assertEqual(self.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request(link + f"/api/photos/{other_photo['id']}")[0], 404)
        self.assertEqual(self.request(link + f"/api/photos/{photo['id']}", 'DELETE')[0], 405)
        self.assertNotIn('file_path', self.request(link + f"/api/items/{entry['id']}/photos")[1][0])
        status, data = self.request(link + '/api/map-config')
        self.assertEqual(status, 200)
        self.assertTrue(all(source['tiles'][0].startswith(link + '/api/tiles/') for source in data['sources'].values()))
        with patch.object(self.app, 'remote_cache', return_value=(b'resource', 'application/octet-stream')):
            for suffix in ('/api/maplibre/maplibre-gl.js', '/api/maplibre/maplibre-gl.css',
                           '/api/tiles/dem/terrarium/12/3400/1700', '/api/tiles/tianditu/img_w/12/3400/1700'):
                self.assertEqual(self.request(link + suffix)[0], 200)
                self.assertEqual(self.headers['Cache-Control'], 'no-store')
            with connect(self.db) as db:
                db.execute('UPDATE shares SET expires=0')
            for suffix in ('/api/share', f"/api/items/{entry['id']}", f"/api/items/{entry['id']}/photos",
                           f"/api/photos/{photo['id']}", '/api/map-config', '/api/maplibre/maplibre-gl.js',
                           '/api/tiles/dem/terrarium/12/3400/1700'):
                self.assertEqual(self.request(link + suffix)[0], 404)

    def test_share_persistence_expiry_password_rotation_and_deletion(self):
        self.login()
        entry = self.create()[1]
        shared = self.request(f"/api/items/{entry['id']}/shares", 'POST', {})[1]
        link = shared['url']
        self.cookie = ''
        self.app = App(self.db, 'correct-password')
        self.assertEqual(self.request(link)[0], 200)
        for offset, status in ((-1, 200), (0, 404), (1, 404)):
            with patch('backend.server.time.time', return_value=shared['expires_at'] + offset):
                self.assertEqual(self.request(link)[0], status)
                self.assertEqual(self.request(link + '/api/share')[0], status)
        self.app = App(self.db, 'different-password')
        self.assertEqual(self.request(link)[0], 404)
        self.app = App(self.db, 'correct-password')
        self.assertEqual(self.request('/share/' + 'x' * 43)[0], 404)
        self.assertEqual(self.request('/share/invalid')[0], 404)
        self.login()
        self.assertEqual(self.request(f"/api/items/{entry['id']}", 'DELETE')[0], 200)
        self.cookie = ''
        status, html = self.request(link)
        self.assertEqual(status, 404)
        self.assertIn('分享链接已过期或失效'.encode(), html)
        self.assertNotIn('password'.encode(), html)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shares').fetchone()[0], 0)

    def test_association_persistence_and_delete_preserves_records(self):
        self.login()
        status, route = self.create()
        self.assertEqual(status,201)
        status, record = self.create('record', route['id'])
        self.assertEqual(status,201)
        self.assertEqual(record['track']['segments'][0][0]['hr'],108)
        self.app = App(self.db, 'correct-password')
        self.assertEqual(len(self.request('/api/items')[1]),2)
        self.assertEqual(self.request(f"/api/items/{record['id']}",'PATCH',
            {'kind':'record','name':'new','notes':'saved','route_id':None})[0],200)
        self.assertEqual(self.request(f"/api/items/{record['id']}",'PATCH',
            {'kind':'record','name':'new','notes':'saved','route_id':route['id']})[0],200)
        self.assertEqual(self.request(f"/api/items/{route['id']}",'DELETE')[0],200)
        row = self.request(f"/api/items/{record['id']}")[1]
        self.assertEqual(row['name'],'new')
        self.assertIsNone(row['route_id'])
        self.assertEqual(self.request('/api/items/999')[0],404)

    def test_validation_and_csrf(self):
        self.login()
        self.assertEqual(self.request('/api/items','POST',{},origin='https://evil.example')[0],403)
        self.assertEqual(self.request('/api/items','POST',{},marker='')[0],403)
        self.assertEqual(self.create(name=' ')[0],400)
        self.assertEqual(self.create('record',999)[0],400)
        status, record = self.create('record')
        self.assertEqual(self.create('record',record['id'])[0],400)
        for key, value in [('lng',999), ('hr',-1), ('lat',True), ('time',float('inf'))]:
            invalid = copy.deepcopy(TRACK)
            invalid['segments'][0][0][key] = value
            self.assertEqual(self.create(track=invalid)[0],400)
        self.assertEqual(self.create(track={'system':'WGS84','segments':[]})[0],400)
        self.assertEqual(len(self.request('/api/items')[1]),1)

    def test_post_items_auto_route_for_record(self):
        self.login()
        track_data = copy.deepcopy(TRACK)
        # 1. auto_route=True without route_id -> auto creates route and associates
        status, rec = self.request('/api/items', 'POST', {
            'name': '西湖群山',
            'kind': 'record',
            'auto_route': True,
            'track': track_data
        })
        self.assertEqual(status, 201)
        self.assertIsNotNone(rec['route_id'])
        with connect(self.db) as db:
            route = db.execute("SELECT id, name, kind FROM items WHERE id=?", (rec['route_id'],)).fetchone()
            self.assertIsNotNone(route)
            self.assertEqual(route['name'], '西湖群山')
            self.assertEqual(route['kind'], 'route')

        # 2. auto_route=True when route already exists -> reuses existing route
        status, rec2 = self.request('/api/items', 'POST', {
            'name': '西湖群山',
            'kind': 'record',
            'auto_route': True,
            'track': track_data
        })
        self.assertEqual(status, 201)
        self.assertEqual(rec2['route_id'], rec['route_id'])
        with connect(self.db) as db:
            routes = db.execute("SELECT id FROM items WHERE kind='route' AND name='西湖群山'").fetchall()
            self.assertEqual(len(routes), 1)

        # 3. Explicit route_id provided -> respects explicit route_id without auto creating new route
        custom_route_id = rec['route_id']
        status, rec3 = self.request('/api/items', 'POST', {
            'name': '不同名字的记录',
            'kind': 'record',
            'route_id': custom_route_id,
            'auto_route': False,
            'track': track_data
        })
        self.assertEqual(status, 201)
        self.assertEqual(rec3['route_id'], custom_route_id)
        with connect(self.db) as db:
            route_none = db.execute("SELECT id FROM items WHERE kind='route' AND name='不同名字的记录'").fetchone()
            self.assertIsNone(route_none)

    def test_login_throttle_expiry_and_password_rotation(self):
        for _ in range(10):
            self.assertEqual(self.request('/api/login','POST',{'password':'wrong'})[0],401)
        self.assertEqual(self.request('/api/login','POST',{'password':'correct-password'})[0],429)
        with connect(self.db) as db:
            db.execute('UPDATE attempts SET reset=0')
        self.login()
        self.app = App(self.db, 'different-password')
        self.assertEqual(self.request('/api/items')[0],401)
        self.app = App(self.db, 'correct-password')
        with connect(self.db) as db:
            db.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.request('/api/items')[0],401)

    def test_import_idempotent_extensions_and_entities(self):
        path = Path(self.tmp.name) / 'sample.gpx'
        path.write_bytes(GPX)
        self.assertTrue(import_file(path,self.db))
        self.assertFalse(import_file(path,self.db))
        self.login()
        self.assertEqual(len(self.request('/api/items')[1]),2)
        parsed = parse_gpx(GPX)
        self.assertEqual(parsed['segments'][0][0]['hr'],100)
        self.assertEqual(parsed['segments'][0][0]['cad'],32)
        with self.assertRaises(ValueError):
            parse_gpx(b'<!DOCTYPE x>'+GPX)

    def test_photo_lifecycle_upload_fetch_delete(self):
        self.assertEqual(self.request('/api/items/1/photos')[0], 401)
        self.login()
        status, item = self.create('record')
        self.assertEqual(status, 201)
        item_id = item['id']

        # Empty photo list
        status, photos = self.request(f'/api/items/{item_id}/photos')
        self.assertEqual(status, 200)
        self.assertEqual(photos, [])

        # Upload a photo
        dummy_jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00'
        b64_data = 'data:image/jpeg;base64,' + io.BytesIO(dummy_jpeg).getvalue().hex() # wait, hex is not base64
        import base64
        b64_str = 'data:image/jpeg;base64,' + base64.b64encode(dummy_jpeg).decode()
        payload = {
            'name': 'summit.jpg',
            'data': b64_str,
            'width': 1920,
            'height': 1080,
            'taken_at': '2026-05-01T10:00:00Z',
            'lat': 30.2,
            'lng': 119.2,
            'match_type': 'time',
            'point_index': 1
        }
        status, photo = self.request(f'/api/items/{item_id}/photos', 'POST', payload)
        self.assertEqual(status, 201)
        self.assertEqual(photo['filename'], 'summit.jpg')
        self.assertEqual(photo['match_type'], 'time')
        self.assertEqual(photo['point_index'], 1)
        self.assertNotIn('file_path', photo)

        photo_id = photo['id']

        # Get photo file
        status, content = self.request(f'/api/photos/{photo_id}')
        self.assertEqual(status, 200)
        self.assertEqual(content, dummy_jpeg)

        # Get list
        status, photos = self.request(f'/api/items/{item_id}/photos')
        self.assertEqual(status, 200)
        self.assertEqual(len(photos), 1)

        # Delete photo
        status, resp = self.request(f'/api/photos/{photo_id}', 'DELETE')
        self.assertEqual(status, 200)
        self.assertEqual(self.request(f'/api/photos/{photo_id}')[0], 404)

        # Re-upload and test item cascade deletion
        status, photo2 = self.request(f'/api/items/{item_id}/photos', 'POST', payload)
        self.assertEqual(status, 201)
        self.assertEqual(self.request(f'/api/items/{item_id}', 'DELETE')[0], 200)
        self.assertEqual(self.request(f'/api/photos/{photo2["id"]}')[0], 404)


if __name__ == '__main__':
    unittest.main()
