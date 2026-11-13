import safety  # Install protection before importing application code.
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from backend.build_shortcut import build, build_gpx, build_photos
from backend.server import App
from backend.storage import connect

GPX = b'''<gpx><trk><name>Phone hike</name><trkseg>
<trkpt lat="30" lon="119"><time>2026-01-01T10:00:00+08:00</time></trkpt>
<trkpt lat="30.1" lon="119.1"><time>2026-01-01T01:00:00Z</time></trkpt>
</trkseg></trk></gpx>'''
JPEG = b'\xff\xd8\xff\xe0test\xff\xd9'


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'hiking.sqlite3'
        self.app = App(self.database, 'test-password')
        self.cookie = ''
        self.token = ''

    def request(self, path, method='GET', body=b'', **headers):
        p, _, qs = path.partition('?')
        env = {'PATH_INFO': p, 'QUERY_STRING': qs, 'REQUEST_METHOD': method, 'CONTENT_LENGTH': str(len(body)),
               'wsgi.input': io.BytesIO(body), 'HTTP_HOST': 'localhost',
               'HTTP_COOKIE': self.cookie, **headers}
        result = {}
        def start(status, response_headers):
            result['status'] = int(status.split()[0])
            result['headers'] = dict(response_headers)
        with patch('backend.server.shutil.which', return_value=None):
            raw = b''.join(self.app(env, start))
        if 'Set-Cookie' in result['headers']:
            self.cookie = result['headers']['Set-Cookie'].split(';')[0]
        data = json.loads(raw) if result['headers']['Content-Type'].startswith('application/json') else raw
        return result['status'], data

    def login(self):
        self.assertEqual(self.request('/api/login', 'POST', b'{"password":"test-password"}',
                                      CONTENT_TYPE='application/json', HTTP_X_HIKING_REQUEST='1')[0], 200)

    def credential(self):
        self.login()
        status, data = self.request('/api/settings/shortcut-token', 'POST', HTTP_X_HIKING_REQUEST='1')
        self.assertEqual(status, 200)
        self.token = data['token']
        self.cookie = ''

    def upload(self, path='/api/shortcut/gpx', body=GPX, **headers):
        return self.request(path, 'POST', body, HTTP_AUTHORIZATION='Bearer ' + self.token, **headers)

    def test_upload_requires_scoped_token_even_with_browser_session(self):
        self.assertEqual(self.upload()[0], 401)
        self.login()
        self.assertEqual(self.upload()[0], 401)
        self.assertEqual(self.request('/api/settings/shortcut-token', 'POST')[0], 403)
        self.credential()
        self.assertEqual(self.upload()[0], 201)
        self.assertEqual(self.request('/api/items', HTTP_AUTHORIZATION='Bearer ' + self.token)[0], 401)
        self.assertEqual(self.upload('/api/shortcut/items/1')[0], 404)
        self.assertEqual(self.request('/api/shortcut/gpx', HTTP_AUTHORIZATION='Bearer ' + self.token)[0], 404)
        with connect(self.database) as db:
            self.assertNotEqual(db.execute('SELECT digest FROM shortcut_token').fetchone()[0], self.token)

    def test_upload_gpx_as_route_and_record(self):
        self.credential()
        # Upload as record
        status, record_data = self.upload('/api/shortcut/gpx?kind=record')
        self.assertEqual(status, 201)
        self.assertEqual(record_data['kind'], 'record')
        self.assertEqual(record_data['start'], '2026-01-01T01:00:00.000Z')
        self.assertEqual(record_data['end'], '2026-01-01T02:00:00.000Z')

        # Upload as route
        status, route_data = self.upload('/api/shortcut/gpx?kind=route')
        self.assertEqual(status, 201)
        self.assertEqual(route_data['kind'], 'route')

        with connect(self.database) as db:
            items = db.execute('SELECT id, kind, route_id FROM items ORDER BY id ASC').fetchall()
            self.assertEqual(len(items), 3)
            self.assertEqual(items[0]['kind'], 'route')
            self.assertEqual(items[1]['kind'], 'record')
            self.assertEqual(items[1]['route_id'], items[0]['id'])
            self.assertEqual(items[2]['kind'], 'route')

        # Header support: X-Hiking-Kind: 路线 / 记录
        status, h_data = self.upload('/api/shortcut/gpx', HTTP_X_HIKING_KIND='路线')
        self.assertEqual(status, 201)
        self.assertEqual(h_data['kind'], 'route')

        # Untimed GPX as route succeeds
        untimed = GPX.replace(b'<time>2026-01-01T10:00:00+08:00</time>', b'').replace(b'<time>2026-01-01T01:00:00Z</time>', b'')
        status, u_route = self.upload('/api/shortcut/gpx?kind=route', body=untimed)
        self.assertEqual(status, 201)
        self.assertEqual(u_route['kind'], 'route')
        self.assertIsNone(u_route['start'])

        # Untimed GPX as record fails
        status, u_record = self.upload('/api/shortcut/gpx?kind=record', body=untimed)
        self.assertEqual(status, 400)

    def test_gpx_preview_reads_name_without_saving(self):
        path = '/api/shortcut/gpx/preview'
        self.assertEqual(self.upload(path)[0], 401)
        self.credential()
        self.assertEqual(self.request(path, HTTP_AUTHORIZATION='Bearer ' + self.token)[0], 404)
        for body, name in (
            (GPX, 'Phone hike'),
            (GPX.replace(b'<gpx>', b'<gpx xmlns="http://www.topografix.com/GPX/1/1">')
                .replace(b'Phone hike', '  山路 &amp; 北线  '.encode()), '山路 & 北线'),
            (GPX.replace(b'Phone hike', '<![CDATA[山路 <北线>]]>'.encode()), '山路 <北线>'),
            (GPX.replace(b'<name>Phone hike</name>', b''), '导入记录'),
            (GPX.replace(b'Phone hike', b'  '), '导入记录'),
            (GPX.replace(b'Phone hike', b'x' * 161), 'x' * 161),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.upload(path, body), (200, {'name': name}))
        self.assertEqual(self.upload(path, b'not XML')[0], 400)
        self.assertEqual(self.upload(path, GPX.replace(b'lat="30"', b'lat="999"'))[0], 400)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM shortcut_imports').fetchone()[0], 0)

    def test_renamed_uploads_and_retries_keep_route_and_record_names_in_sync(self):
        self.credential()
        for kind in ('route', 'record'):
            with self.subTest(kind=kind):
                name = f'{kind} 示例山径 & 北线 + 50% / 🚶? #=环线'
                path = f'/api/shortcut/gpx?kind={kind}'
                status, data = self.upload(path + '&name=' + quote('  ' + name + '  ', safe=''))
                self.assertEqual(status, 201)
                self.assertEqual(data['name'], name)
                self.assertFalse(data['reused'])
                with connect(self.database) as db:
                    before = dict(db.execute('SELECT * FROM items WHERE id=?', (data['item_id'],)).fetchone())
                    self.assertEqual(before['name'], name)
                    if kind == 'record':
                        route = db.execute('SELECT name FROM items WHERE id=?', (before['route_id'],)).fetchone()
                        self.assertEqual(route['name'], name)
                renamed = name + ' 新名称'
                status, again = self.upload(path + '&name=' + quote(renamed, safe=''))
                self.assertEqual(status, 201)
                self.assertTrue(again['reused'])
                self.assertEqual(again['item_id'], data['item_id'])
                self.assertEqual(again['name'], renamed)
                self.assertEqual(self.upload(path)[1]['name'], renamed)
                with connect(self.database) as db:
                    after = dict(db.execute('SELECT * FROM items WHERE id=?', (data['item_id'],)).fetchone())
                    self.assertEqual(after, {**before, 'name': renamed})
                    if kind == 'record':
                        route = db.execute('SELECT name FROM items WHERE id=?', (after['route_id'],)).fetchone()
                        self.assertEqual(route['name'], renamed)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE name='Phone hike'").fetchone()[0], 0)

    def test_renamed_record_uses_existing_route_with_the_new_name(self):
        self.credential()
        name = quote('正确路线名', safe='')
        route = self.upload('/api/shortcut/gpx?kind=route&name=' + name)[1]
        record = self.upload('/api/shortcut/gpx?kind=record&name=' + name)[1]
        with connect(self.database) as db:
            saved = db.execute('SELECT name, route_id FROM items WHERE id=?', (record['item_id'],)).fetchone()
            self.assertEqual(saved['name'], '正确路线名')
            self.assertEqual(saved['route_id'], route['item_id'])
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 2)

    def test_rename_of_existing_record_preserves_photos_and_notes_and_restores_deleted_route(self):
        self.credential()
        record = self.upload()[1]
        photo_path = record['photo_path']
        photo = self.upload(photo_path, JPEG, HTTP_X_PHOTO_TAKEN_AT=record['start'])[1]
        with connect(self.database) as db:
            db.execute('UPDATE items SET notes=? WHERE id=?', ('保留备注', record['item_id']))
        status, renamed = self.upload('/api/shortcut/gpx?name=' + quote('新路线名', safe=''))
        self.assertEqual(status, 201)
        self.assertEqual(renamed['item_id'], record['item_id'])
        self.assertEqual(self.upload(photo_path, JPEG, HTTP_X_PHOTO_TAKEN_AT=record['start'])[1]['id'], photo['id'])
        with connect(self.database) as db:
            saved = db.execute('SELECT name, notes, route_id FROM items WHERE id=?', (record['item_id'],)).fetchone()
            self.assertEqual(saved['name'], '新路线名')
            self.assertEqual(saved['notes'], '保留备注')
            self.assertEqual(db.execute('SELECT name FROM items WHERE id=?', (saved['route_id'],)).fetchone()[0], '新路线名')
            db.execute('DELETE FROM items WHERE id=?', (saved['route_id'],))
        self.assertEqual(self.upload('/api/shortcut/gpx?name=' + quote('恢复路线名', safe=''))[0], 201)
        with connect(self.database) as db:
            saved = db.execute('SELECT route_id FROM items WHERE id=?', (record['item_id'],)).fetchone()
            self.assertEqual(db.execute('SELECT name FROM items WHERE id=?', (saved['route_id'],)).fetchone()[0], '恢复路线名')
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 2)

    def test_invalid_rename_does_not_save_or_change_existing_names(self):
        self.credential()
        for existing in (False, True):
            if existing:
                self.upload()
            for name in ('', ' \n\t ', 'x' * 161):
                with self.subTest(existing=existing, name=name):
                    self.assertEqual(self.upload('/api/shortcut/gpx?name=' + quote(name, safe=''))[0], 400)
            with connect(self.database) as db:
                names = [row['name'] for row in db.execute('SELECT name FROM items')]
                self.assertEqual(names, ['Phone hike', 'Phone hike'] if existing else [])
        status, corrected = self.upload('/api/shortcut/gpx?name=' + quote('修正名称', safe=''),
                                        body=GPX.replace(b'Phone hike', b'x' * 161))
        self.assertEqual(status, 201)
        self.assertEqual(corrected['name'], '修正名称')

    def test_upload_record_auto_creates_route_and_associates_existing(self):
        self.credential()
        status, rec1 = self.upload('/api/shortcut/gpx?kind=record')
        self.assertEqual(status, 201)
        with connect(self.database) as db:
            route1 = db.execute("SELECT id, name, kind FROM items WHERE kind='route' AND name='Phone hike'").fetchone()
            self.assertIsNotNone(route1)
            record1 = db.execute("SELECT id, name, kind, route_id FROM items WHERE id=?", (rec1['item_id'],)).fetchone()
            self.assertEqual(record1['route_id'], route1['id'])

        another_gpx = b'''<gpx><trk><name>Phone hike</name><trkseg>
        <trkpt lat="31" lon="120"><time>2026-01-02T10:00:00+08:00</time></trkpt>
        <trkpt lat="31.1" lon="120.1"><time>2026-01-02T01:00:00Z</time></trkpt>
        </trkseg></trk></gpx>'''
        status, rec2 = self.upload('/api/shortcut/gpx?kind=record', body=another_gpx)
        self.assertEqual(status, 201)
        with connect(self.database) as db:
            routes = db.execute("SELECT id FROM items WHERE kind='route' AND name='Phone hike'").fetchall()
            self.assertEqual(len(routes), 1)
            record2 = db.execute("SELECT id, name, kind, route_id FROM items WHERE id=?", (rec2['item_id'],)).fetchone()
            self.assertEqual(record2['route_id'], route1['id'])

    def test_time_range_is_utc_sorted_and_retries_reuse_record(self):
        self.credential()
        status, data = self.upload()
        self.assertEqual(status, 201)
        self.assertEqual(data['start'], '2026-01-01T01:00:00.000Z')
        self.assertEqual(data['end'], '2026-01-01T02:00:00.000Z')
        status, again = self.upload()
        self.assertEqual(again['item_id'], data['item_id'])
        self.assertTrue(again['reused'])
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 2)

    def test_invalid_or_untimed_gpx_does_not_create_records(self):
        self.credential()
        for body in (b'', b'not XML', b'<gpx>', b'<!DOCTYPE x>' + GPX,
                     GPX.replace(b'T10:00:00+08:00', b'T10:00:00'),
                     GPX.replace(b'<time>2026-01-01T10:00:00+08:00</time>', b'').replace(b'<time>2026-01-01T01:00:00Z</time>', b''),
                     GPX.replace(b'lat="30"', b'lat="999"')):
            with self.subTest(body=body):
                self.assertEqual(self.upload(body=body)[0], 400)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 0)
        self.assertEqual(self.upload(CONTENT_LENGTH=str(16 * 1024 * 1024))[0], 400)

    def test_photo_auto_association(self):
        self.credential()
        # Upload record first
        status, gpx_res = self.upload()
        self.assertEqual(status, 201)
        item_id = gpx_res['item_id']

        # Photo upload to /api/shortcut/photos without item_id: auto matches record!
        status, photo = self.upload('/api/shortcut/photos', JPEG, HTTP_X_PHOTO_TAKEN_AT=gpx_res['start'])
        self.assertEqual(status, 201)
        self.assertEqual(photo['item_id'], item_id)
        self.assertEqual(photo['match_type'], 'time')
        self.assertEqual(photo['point_index'], 1)
        self.assertEqual(photo['lat'], 30.1)

        # Re-uploading same photo deduplicates
        status, again = self.upload('/api/shortcut/photos', JPEG, HTTP_X_PHOTO_TAKEN_AT=gpx_res['start'])
        self.assertEqual(status, 200)
        self.assertEqual(again['id'], photo['id'])

        # Photo outside all records returns 200 with skipped indicator
        status, skipped = self.upload('/api/shortcut/photos', JPEG, HTTP_X_PHOTO_TAKEN_AT='2020-01-01T00:00:00Z')
        self.assertEqual(status, 200)
        self.assertTrue(skipped['skipped'])
        self.assertEqual(skipped['id'], 0)
        self.assertEqual(skipped['match_type'], 'none')

    def test_photo_upload_match_dedup_and_boundary_validation(self):
        self.credential()
        data = self.upload()[1]
        path = data['photo_path']
        self.assertEqual(self.upload(path, JPEG)[0], 400)
        for date in ('invalid', '2026-01-01T01:30:00', '2026-01-01T00:59:59Z', '2026-01-01T02:00:01Z'):
            self.assertEqual(self.upload(path, JPEG, HTTP_X_PHOTO_TAKEN_AT=date)[0], 400)
        self.assertEqual(self.upload(path, b'not JPEG', HTTP_X_PHOTO_TAKEN_AT=data['start'])[0], 400)
        status, photo = self.upload(path, JPEG, HTTP_X_PHOTO_TAKEN_AT=data['start'])
        self.assertEqual(status, 201)
        self.assertEqual(photo['match_type'], 'time')
        self.assertEqual(photo['point_index'], 1)
        self.assertEqual(photo['lat'], 30.1)
        status, again = self.upload(path, JPEG, HTTP_X_PHOTO_TAKEN_AT=data['start'])
        self.assertEqual(status, 200)
        self.assertEqual(again['id'], photo['id'])
        self.assertEqual(self.upload(path, JPEG, HTTP_X_PHOTO_TAKEN_AT=data['end'])[0], 201)
        self.assertEqual(self.upload('/api/shortcut/items/999/photos', JPEG)[0], 404)
        self.login()
        self.request(f'/api/items/{data["item_id"]}', 'DELETE', HTTP_X_HIKING_REQUEST='1')
        self.assertEqual(self.upload(path, JPEG)[0], 404)
        with connect(self.database) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shortcut_photos').fetchone()[0], 0)
        self.assertFalse(self.upload()[1]['reused'])

    def test_revoke_rotate_and_password_change_invalidate_token(self):
        self.credential()
        first = self.token
        self.credential()
        second = self.token
        self.token = first
        self.assertEqual(self.upload()[0], 401)
        self.token = second
        self.assertEqual(self.upload()[0], 201)
        self.app = App(self.database, 'changed-password')
        self.assertEqual(self.upload()[0], 401)
        self.app = App(self.database, 'test-password')
        self.login()
        self.assertEqual(self.request('/api/settings/shortcut-token', 'DELETE', HTTP_X_HIKING_REQUEST='1')[0], 200)
        self.assertEqual(self.upload()[0], 401)

    def test_gpx_template_references_and_actions(self):
        template = build_gpx()
        path = Path(__file__).resolve().parent.parent / 'shortcuts/hiking-gpx-unsigned.shortcut'
        self.assertEqual(plistlib.loads(path.read_bytes()), template)
        seen, groups = set(), []
        def check(value):
            if isinstance(value, dict):
                if value.get('Type') == 'ActionOutput':
                    self.assertIn(value['OutputUUID'], seen)
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        menus, menu_branches, uploads = [], [], []
        ask_action = None
        positions = {}
        for index, action in enumerate(template['WFWorkflowActions']):
            params = action['WFWorkflowActionParameters']
            check(params)
            seen.add(params['UUID'])
            ident = action['WFWorkflowActionIdentifier']
            if ident.endswith('.choosefrommenu'):
                mode = params.get('WFControlFlowMode')
                if mode == 0:
                    menus.append(params)
                    positions['rename_menu' if len(menus) == 2 else 'kind_menu'] = index
                elif mode == 1:
                    menu_branches.append(params.get('WFMenuItemTitle'))
            elif ident.endswith('.ask'):
                ask_action = params
                positions['ask'] = index
                self.assertEqual(groups[-1], menus[-1]['GroupingIdentifier'])
                self.assertEqual(menu_branches[-1], '重命名')
            elif ident.endswith('.urlencode'):
                positions['encode'] = index
                self.assertEqual(params['WFEncodeMode'], 'Encode')
                self.assertEqual(params['WFInput']['Value']['attachmentsByRange']['{0, 1}']['OutputUUID'], ask_action['UUID'])
            elif ident.endswith('.downloadurl'):
                uploads.append(params)
                positions['preview' if len(uploads) == 1 else 'upload'] = index
            if 'WFControlFlowMode' in params:
                if params['WFControlFlowMode'] == 0:
                    groups.append(params['GroupingIdentifier'])
                elif params['WFControlFlowMode'] == 2:
                    self.assertEqual(groups.pop(), params['GroupingIdentifier'])

        self.assertEqual(len(menus), 2)
        self.assertEqual(menus[0]['WFMenuItems'], ['路线', '记录'])
        self.assertIn('选择上传类型', menus[0]['WFMenuPrompt'])
        self.assertEqual(menus[1]['WFMenuItems'], ['保留原名', '重命名'])
        prompt = menus[1]['WFMenuPrompt']['Value']
        self.assertIn('当前读取的名称', prompt['string'])
        self.assertIn({'Type': 'Variable', 'VariableName': '当前名称'}, prompt['attachmentsByRange'].values())
        self.assertEqual(menu_branches, ['路线', '记录', '保留原名', '重命名'])
        self.assertEqual(ask_action['WFInputType'], 'Text')
        self.assertFalse(ask_action['WFAllowsMultilineText'])
        self.assertEqual(ask_action['WFAskActionDefaultAnswer']['Value']['attachmentsByRange']['{0, 1}'],
                         {'Type': 'Variable', 'VariableName': '当前名称'})
        self.assertEqual(len(uploads), 2)
        self.assertIn('/api/shortcut/gpx/preview?kind=', uploads[0]['WFURL']['Value']['string'])
        self.assertIn('/api/shortcut/gpx?kind=', uploads[1]['WFURL']['Value']['string'])
        self.assertIn({'Type': 'Variable', 'VariableName': '名称参数'},
                      uploads[1]['WFURL']['Value']['attachmentsByRange'].values())
        for upload in uploads:
            self.assertEqual(upload['WFHTTPMethod'], 'POST')
            self.assertEqual(upload['WFHTTPBodyType'], 'File')
            self.assertEqual(upload['WFRequestVariable']['Value']['VariableName'], 'GPX文件')
        order = ['kind_menu', 'preview', 'rename_menu', 'ask', 'encode', 'upload']
        self.assertEqual([positions[key] for key in order], sorted(positions.values()))
        self.assertEqual(groups, [])

    def test_photos_template_references_and_actions(self):
        template = build_photos()
        path = Path(__file__).resolve().parent.parent / 'shortcuts/hiking-photos-unsigned.shortcut'
        self.assertEqual(plistlib.loads(path.read_bytes()), template)
        seen, groups = set(), []
        def check(value):
            if isinstance(value, dict):
                if value.get('Type') == 'ActionOutput':
                    self.assertIn(value['OutputUUID'], seen)
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        has_select_photos = False
        has_repeat = False
        upload_action = None
        for action in template['WFWorkflowActions']:
            params = action['WFWorkflowActionParameters']
            check(params)
            seen.add(params['UUID'])
            ident = action['WFWorkflowActionIdentifier']
            if ident.endswith('.selectphotos'):
                has_select_photos = True
                self.assertTrue(params['WFSelectMultiple'])
            elif ident.endswith('.repeat.each') and params.get('WFControlFlowMode') == 0:
                has_repeat = True
            elif ident.endswith('.downloadurl'):
                upload_action = params
            if 'WFControlFlowMode' in params:
                if params['WFControlFlowMode'] == 0:
                    groups.append(params['GroupingIdentifier'])
                elif params['WFControlFlowMode'] == 2:
                    self.assertEqual(groups.pop(), params['GroupingIdentifier'])

        self.assertTrue(has_select_photos)
        self.assertTrue(has_repeat)
        self.assertIsNotNone(upload_action)
        self.assertIn('/api/shortcut/photos', upload_action['WFURL']['Value']['string'])
        self.assertEqual(groups, [])

    def test_shortcut_downloads_are_private_and_signed_files_are_optional(self):
        for path in ('/api/settings/shortcut',
                     '/shortcuts/hiking-gpx.shortcut', '/shortcuts/hiking-gpx-unsigned.shortcut',
                     '/shortcuts/hiking-photos.shortcut', '/shortcuts/hiking-photos-unsigned.shortcut'):
            self.assertEqual(self.request(path)[0], 401)
        self.login()
        status, gpx_src = self.request('/shortcuts/hiking-gpx-unsigned.shortcut')
        self.assertEqual(status, 200)
        self.assertEqual(plistlib.loads(gpx_src), build_gpx())

        status, photos_src = self.request('/shortcuts/hiking-photos-unsigned.shortcut')
        self.assertEqual(status, 200)
        self.assertEqual(plistlib.loads(photos_src), build_photos())

        root = Path(self.directory.name)
        (root / 'shortcuts').mkdir()
        for kind in ('gpx', 'photos'):
            with self.subTest(kind=kind), patch('backend.server.ROOT', root):
                path = f'shortcuts/hiking-{kind}.shortcut'
                self.assertFalse(self.request('/api/settings/shortcut')[1]['download_available'])
                self.assertEqual(self.request('/' + path)[0], 404)
                signed = root / path
                signed.write_bytes(b'AEA1 synthetic signed fixture')
                res = self.request('/api/settings/shortcut')[1]
                self.assertTrue(res['download_available'])
                self.assertTrue(res[f'{kind}_available'])
                self.assertEqual(self.request('/' + path), (200, b'AEA1 synthetic signed fixture'))
                signed.unlink()


if __name__ == '__main__':
    unittest.main()
