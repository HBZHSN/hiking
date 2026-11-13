import safety  # Install protection before importing application code.
import copy
import math
import unittest
from backend.metrics import summarize
from backend.storage import validate_track
import test_backend
from test_backend import TRACK


class MetricsTests(unittest.TestCase):
    def test_elevation_noise_and_continuous_climbs(self):
        cases = [
            ([[100, 101, 99, 102, 100] * 100], 0, 0),
            ([list(range(100, 183))], 82, 0),
            ([list(range(182, 99, -1))], 0, 82),
            ([[100, 105, 110, 115, 120, 119, 120, 115, 110, 105, 100, 110, 120]], 40, 20),
            ([[100, 109, None, 500, 503, 502]], 11, 0),
            ([[100, 109], [500, 509]], 18, 0),
            ([[100, None, 500]], None, None),
        ]
        for case, (segments, ascent, descent) in enumerate(cases):
            with self.subTest(case=case):
                track = dict(system='WGS84', segments=[
                    [dict(lng=119 + i / 1000, lat=30, ele=height) for i, height in enumerate(segment)]
                    for segment in segments])
                original = copy.deepcopy(track)
                stats = summarize(track)
                self.assertEqual(stats['ascent'], ascent)
                self.assertEqual(stats['descent'], descent)
                self.assertEqual(track, original)

    def test_elevation_smoothing_removes_short_distance_spikes(self):
        track = dict(system='WGS84', segments=[[
            dict(lng=119 + i / 10000, lat=30, ele=height)
            for i, height in enumerate([100] * 10 + [140] + [100] * 10)]])
        stats = summarize(track)
        self.assertEqual(stats['ascent'], 0)
        self.assertEqual(stats['descent'], 0)
        self.assertEqual(stats['max'], 140)

    def test_units_gaps_missing_elevation(self):
        track=copy.deepcopy(TRACK)
        track['segments'].append([dict(track['segments'][0][0], time=1760172800000), dict(track['segments'][0][1], time=1760173400000)])
        stats=summarize(track)
        self.assertEqual(stats['days'],3)
        dist_km = stats['distance'] / 1000
        expected = (math.sqrt((dist_km * 0.621371 / 3) * (100 * 3.28084 / 3) * 2)
                    * (1 + math.log(3) * 0.2) * (1 + math.floor((100 / 3) / 500) * 0.15))
        self.assertAlmostEqual(stats['difficulty'], expected)
        track['segments'][0][0]['ele']=None
        self.assertIsNone(summarize(track)['difficulty'])

    def test_days_validation(self):
        for days in [0,-1,1.5,True,'2',3651]:
            with self.subTest(days=days), self.assertRaises(ValueError):
                validate_track(dict(TRACK,days=days))
        self.assertEqual(validate_track(dict(TRACK,days=2))['days'],2)


class LibraryApiTests(unittest.TestCase):
    setUp = test_backend.BackendTests.setUp
    tearDown = test_backend.BackendTests.tearDown
    request = test_backend.BackendTests.request
    login = test_backend.BackendTests.login

    def test_summary_difficulty_uses_filtered_ascent(self):
        self.login()
        track = dict(system='WGS84', segments=[[
            dict(lng=119 + i / 10000, lat=30, ele=height)
            for i, height in enumerate([100, 101, 99, 102, 100] * 100)]])
        self.assertEqual(self.request('/api/items', 'POST', dict(name='Noisy flat route', kind='route', track=track))[0], 201)
        summary = self.request('/api/items')[1][0]['summary']
        self.assertEqual(summary['ascent'], 0)
        self.assertEqual(summary['descent'], 0)
        self.assertEqual(summary['difficulty'], 0)

    def test_summary_and_persist_days(self):
        self.login()
        payload=dict(name='Route',kind='route',track=TRACK)
        status,item=self.request('/api/items','POST',payload)
        self.assertEqual(status,201)
        status,listing=self.request('/api/items')
        self.assertNotIn('track',listing[0])
        self.assertIsNotNone(listing[0]['summary']['difficulty'])
        payload.pop('track');payload['days']=2
        status,item=self.request(f"/api/items/{item['id']}",'PATCH',payload)
        self.assertEqual(status,200)
        self.assertEqual(item['track']['days'],2)
        self.assertEqual(self.request('/api/items')[1][0]['summary']['days'],2)
        payload['days']=None
        self.assertEqual(self.request(f"/api/items/{item['id']}",'PATCH',payload)[1]['track']['days'],None)

    def test_tianditu_config_requires_login_and_returns_four_sources(self):
        self.app.tianditu_key = 'test-key&encoded'
        self.assertEqual(self.request('/api/map-config')[0], 401)
        self.login()
        status, config = self.request('/api/map-config')
        self.assertEqual(status, 200)
        expected = {'satellite': 'img_w', 'satellite-labels': 'cia_w', 'street': 'vec_w', 'street-labels': 'cva_w'}
        self.assertEqual(set(config['sources']), set(expected))
        for name, layer in expected.items():
            source = config['sources'][name]
            self.assertEqual(source['tileSize'], 256)
            self.assertEqual(source['maxzoom'], 18)
            self.assertEqual(source['tiles'], [f'/api/tiles/tianditu/{layer}/{{z}}/{{x}}/{{y}}'])
        self.app.tianditu_key = ''
        self.assertEqual(self.request('/api/map-config')[0], 503)

    def test_external_tile_cache_reuses_local_file(self):
        from unittest.mock import patch
        from backend.server import urllib

        class Headers:
            def get_content_type(self): return 'image/png'
        class Response:
            headers = Headers()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'cached tile'
        opener = type('Opener', (), {'open': lambda self, request, timeout: Response()})()
        self.login()
        self.app.tianditu_key = 'test-key'
        with patch.object(urllib.request, 'build_opener', return_value=opener) as build:
            first = self.request('/api/tiles/tianditu/img_w/10/850/422')
            second = self.request('/api/tiles/tianditu/img_w/10/850/422')
        self.assertEqual(first[0], 200); self.assertEqual(first[1], b'cached tile')
        self.assertEqual(second[0], 200); self.assertEqual(second[1], b'cached tile')
        self.assertEqual(build.call_count, 1)
