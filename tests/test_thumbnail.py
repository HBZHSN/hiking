import safety  # Install protection before importing application code.
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from backend.thumbnail import HEIGHT, WIDTH, PADDING, layout, thumbnail

TRACK = {'segments': [[{'lng': 119, 'lat': 30}, {'lng': 119.01, 'lat': 30.02}],
                      [{'lng': 119.02, 'lat': 30.01}, {'lng': 119.03, 'lat': 30}]]}


class ThumbnailTests(unittest.TestCase):
    def test_north_up_fit_segments_and_dateline(self):
        paths, tiles = layout(TRACK['segments'])
        self.assertEqual(len(paths), 2)
        self.assertGreater(paths[0][0][1], paths[0][1][1])
        self.assertLess(paths[0][0][0], paths[0][1][0])
        for path in paths:
            for x, y in path:
                self.assertTrue(PADDING - 0.01 <= x <= WIDTH - PADDING + 0.01)
                self.assertTrue(PADDING - 0.01 <= y <= HEIGHT - PADDING + 0.01)
        self.assertLessEqual(len(tiles), 12)
        paths, tiles = layout([[{'lng': 179.99, 'lat': 30}, {'lng': -179.99, 'lat': 30.01}]])
        self.assertGreater(tiles[0][0], 10)
        self.assertLess(paths[0][0][0], paths[0][1][0])
        for z, x, y, *_ in tiles:
            self.assertTrue(0 <= x < 2**z and 0 <= y < 2**z)
        paths, _ = layout([[{'lng': 0, 'lat': 0}] * 2])
        self.assertEqual(paths[0][0], (WIDTH / 2, HEIGHT / 2))
        # World-scale and polar tracks still fit; invalid tile rows are omitted.
        paths, tiles = layout([[{'lng': -170, 'lat': -85}, {'lng': 0, 'lat': 85}]])
        for path in paths:
            for x, y in path:
                self.assertTrue(0 <= x <= WIDTH and 0 <= y <= HEIGHT)
        self.assertLessEqual(len(tiles), 12)

    def test_embedded_tiles_cached_and_coordinates_invalidate(self):
        tile = Mock(return_value=(200, b'example image', [('Content-Type', 'image/png')]))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '1.svg'
            first = thumbnail(TRACK, path, 'source', tile)
            root = ET.fromstring(first)
            ns = {'s': 'http://www.w3.org/2000/svg'}
            self.assertTrue(root.findall('s:image', ns))
            self.assertTrue(all(img.get('href').startswith('data:image/png;base64,') for img in root.findall('s:image', ns)))
            self.assertEqual(root.find('s:path', ns).get('d').count('M'), 2)
            self.assertEqual(path.read_bytes(), first)
            calls = tile.call_count
            self.assertEqual(thumbnail(TRACK, path, 'source', tile), first)
            self.assertEqual(tile.call_count, calls)
            changed = copy.deepcopy(TRACK)
            changed['segments'][0][0]['lat'] += 0.001
            self.assertNotEqual(thumbnail(changed, path, 'source', tile), first)
            self.assertGreater(tile.call_count, calls)
            calls = tile.call_count
            thumbnail(changed, path, 'new source', tile)
            self.assertGreater(tile.call_count, calls)

    def test_failed_satellite_is_not_cached_and_recovers(self):
        tile = Mock(return_value=(502, {'error': 'offline'}, []))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '1.svg'
            result = thumbnail(TRACK, path, 'source', tile)
            self.assertIn('卫星底图暂不可用'.encode(), result)
            self.assertFalse(path.exists())
            tile.return_value = (200, b'image', [('Content-Type', 'image/jpg')])
            self.assertNotIn('暂不可用'.encode(), thumbnail(TRACK, path, 'source', tile))
            self.assertTrue(path.exists())
            self.assertIn(b'data:image/jpeg;base64,', path.read_bytes())
