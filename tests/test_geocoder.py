import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import safety  # noqa: F401 - Must be imported first for test isolation
from backend.geocoder import extract_start_point, format_location_component, geocode, get_item_location
from backend.server import App
from backend.storage import connect, initialize, save

SAMPLE_TRACK = {
    'system': 'WGS84',
    'segments': [[
        {'lng': -138, 'lat': 26, 'ele': 200},
        {'lng': -137.99, 'lat': 26.01, 'ele': 250}
    ]]
}


class GeocoderTests(unittest.TestCase):
    def test_extract_start_point(self):
        self.assertEqual(extract_start_point(SAMPLE_TRACK), (-138, 26))
        self.assertIsNone(extract_start_point({}))
        self.assertIsNone(extract_start_point({'segments': []}))
        self.assertIsNone(extract_start_point({'segments': [[]]}))
        self.assertIsNone(extract_start_point({'segments': [[{'lng': 'invalid', 'lat': 30}]]}))

    def test_format_location_component(self):
        # Standard prefecture
        self.assertEqual(
            format_location_component({'province': '浙江省', 'city': '杭州市', 'county': '临安区'}),
            '浙江省杭州市临安区'
        )
        # Municipality with empty city
        self.assertEqual(
            format_location_component({'province': '北京市', 'city': '', 'county': '海淀区'}),
            '北京市海淀区'
        )
        # Municipality with duplicate city
        self.assertEqual(
            format_location_component({'province': '上海市', 'city': '上海市', 'county': '黄浦区'}),
            '上海市黄浦区'
        )
        # Province-administered county-level city
        self.assertEqual(
            format_location_component({'province': '湖北省', 'city': '省直辖县级行政区划', 'county': '仙桃市'}),
            '湖北省仙桃市'
        )
        # Autonomous prefecture
        self.assertEqual(
            format_location_component({'province': '四川省', 'city': '阿坝藏族羌族自治州', 'county': '松潘县'}),
            '四川省阿坝藏族羌族自治州松潘县'
        )
        # Overseas
        self.assertEqual(
            format_location_component({'nation': '日本', 'address': '富士吉田', 'province': '', 'city': '', 'county': ''}),
            '日本 富士吉田'
        )

    def test_geocode_caching_and_item_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'test.sqlite3'
            initialize(db_path)
            app = App(db_path, 'test-password-123', tianditu_key='test-key')

            tianditu_response = {
                'status': '0',
                'msg': 'ok',
                'result': {
                    'addressComponent': {
                        'province': '安徽省',
                        'city': '宣城市',
                        'county': '宁国市'
                    }
                }
            }

            with connect(app.database) as db:
                with patch.object(app, 'remote_cache', return_value=(json.dumps(tianditu_response).encode(), 'application/json')) as mock_cache:
                    loc1 = get_item_location(db, 1, SAMPLE_TRACK, tianditu_key='test-key', remote_cache=app.remote_cache)
                    self.assertEqual(loc1, '安徽省宣城市宁国市')
                    self.assertEqual(mock_cache.call_count, 1)

                    # Second call with same item hits item_locations table without remote_cache
                    loc2 = get_item_location(db, 1, SAMPLE_TRACK, tianditu_key='test-key', remote_cache=app.remote_cache)
                    self.assertEqual(loc2, '安徽省宣城市宁国市')
                    self.assertEqual(mock_cache.call_count, 1)

                    # Another item with identical coordinates hits geocache table without remote_cache
                    loc3 = get_item_location(db, 2, SAMPLE_TRACK, tianditu_key='test-key', remote_cache=app.remote_cache)
                    self.assertEqual(loc3, '安徽省宣城市宁国市')
                    self.assertEqual(mock_cache.call_count, 1)


class ServerLocationApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.sqlite3'
        self.app = App(self.db, 'correct-password', tianditu_key='test-key')
        self.cookie = ''

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, path, method='GET', body=None):
        raw = json.dumps(body).encode() if body is not None else b''
        env = {
            'PATH_INFO': path, 'REQUEST_METHOD': method, 'CONTENT_TYPE': 'application/json',
            'CONTENT_LENGTH': str(len(raw)), 'wsgi.input': io.BytesIO(raw),
            'HTTP_HOST': 'localhost:8010', 'REMOTE_ADDR': '127.0.0.1',
            'HTTP_COOKIE': self.cookie,
            'HTTP_X_HIKING_REQUEST': '1'
        }
        captured = {}
        def start(status, headers):
            captured.update(status=int(status.split()[0]), headers=dict(headers))
        result = b''.join(self.app(env, start))
        if 'Set-Cookie' in captured.get('headers', {}):
            self.cookie = captured['headers']['Set-Cookie'].split(';')[0]
        if captured.get('headers', {}).get('Content-Type', '').startswith('application/json'):
            result = json.loads(result)
        return captured['status'], result

    def login(self):
        status, _ = self.request('/api/login', 'POST', {'password': 'correct-password'})
        self.assertEqual(status, 200)

    def test_api_items_returns_location_and_supports_location_endpoint(self):
        self.login()
        tianditu_response = {
            'status': '0',
            'result': {
                'addressComponent': {
                    'province': '浙江省',
                    'city': '杭州市',
                    'county': '临安区'
                }
            }
        }
        with patch.object(self.app, 'remote_cache', return_value=(json.dumps(tianditu_response).encode(), 'application/json')):
            with connect(self.app.database) as db:
                item_id = save(db, {'name': '示例山径', 'kind': 'route', 'track': copy.deepcopy(SAMPLE_TRACK)})
                self.app.resolve_item_location(db, item_id, SAMPLE_TRACK)

            status, items = self.request('/api/items')
            self.assertEqual(status, 200)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['location'], '浙江省杭州市临安区')

            status, detail = self.request(f'/api/items/{item_id}')
            self.assertEqual(status, 200)
            self.assertEqual(detail['location'], '浙江省杭州市临安区')
            self.assertEqual(detail['track']['location'], '浙江省杭州市临安区')

            status, loc_resp = self.request(f'/api/items/{item_id}/location')
            self.assertEqual(status, 200)
            self.assertEqual(loc_resp['location'], '浙江省杭州市临安区')
