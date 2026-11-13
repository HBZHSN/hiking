"""Isolated multi-track map check with fictional tracks and cached map-engine assets."""
import safety

import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from wsgiref.simple_server import make_server, WSGIRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from backend.server import App, Server
from backend.storage import connect, save
from test_photo_sync import png


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


with TemporaryDirectory() as directory:
    app = App(Path(directory) / 'test.sqlite3', 'map-test-password',
              tianditu_key='fixture-key', photo_source=Path(directory) / 'album')

    def fixture_resource(url, content_type='application/octet-stream'):
        if 'cdn.jsdelivr.net' in url:
            data = (Path('/tmp/hiking-browser-assets') / hashlib.sha256(url.encode()).hexdigest()).read_bytes()
            if url.endswith('.js'):
                data += b'\nconst resize=maplibregl.Map.prototype.resize; maplibregl.Map.prototype.resize=function(...args){if(this.getContainer().id==="library-map")window.testAtlas=this;return resize.apply(this,args);};'
            return data, content_type
        return png(256, 256), 'image/png'

    app.remote_cache = fixture_resource
    track = {'system': 'WGS84', 'segments': [[
        dict(lng=119 + i * .01, lat=30 + i * .01, ele=100 + i * 10)
        for i in range(3)], [dict(lng=119.05, lat=30.05, ele=130), dict(lng=119.06, lat=30.06, ele=140)]]}
    with connect(app.database) as db:
        route_id = save(db, {'name': '虚构山脊路线', 'kind': 'route', 'track': track})
        record_id = save(db, {'name': '虚构徒步记录', 'kind': 'record', 'track': track})
        for i in range(12):
            save(db, {'name': f'虚构备选路线{i}' + '长名称' * 10, 'kind': 'route', 'track': track})
    safety.BROWSER_PORT = 0
    http = make_server('127.0.0.1', 0, app, server_class=Server, handler_class=Quiet)
    safety.BROWSER_PORT = http.server_port
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{http.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=['--enable-unsafe-swiftshader'])
            for width, height in ((1440, 1000), (390, 844), (320, 640)):
                print(f'Checking map at {width} x {height}', flush=True)
                context = browser.new_context(viewport={'width': width, 'height': height}, has_touch=width <= 600)
                context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(origin + '/') else r.abort())
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin)
                page.locator('#password').fill('map-test-password')
                page.get_by_role('button', name='登录', exact=True).click()
                page.locator('#routes-tab').wait_for()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.route('**/api/map-config', lambda r: r.fulfill(status=503, json={'error': '模拟地图失败'}))
                page.locator('#map-tab').click()
                expect(page.locator('#library-map-status')).to_have_text('模拟地图失败')
                expect(page.locator('#library-map-retry')).to_be_visible()
                page.unroute('**/api/map-config')
                page.locator('#library-map-retry').click()
                page.wait_for_function('window.testAtlas && testAtlas.loaded()')
                page.locator(f'[data-map-item="{route_id}"]').check()
                expect(page.locator('#map-fit-all')).to_be_enabled()
                page.wait_for_function('testAtlas.getSource("tracks")._data.features.length === 2')
                page.locator('#map-pick-record').click()
                page.locator(f'[data-map-item="{record_id}"]').check()
                page.wait_for_function('testAtlas.getSource("tracks")._data.features.length === 4')
                expect(page.locator('#map-selection-count')).to_have_text('已选 2 条（路线 1 / 记录 1）')
                assert page.evaluate('new Set(testAtlas.getSource("tracks")._data.features.map(f=>f.properties.color)).size') == 2
                page.locator('#library-map-options summary').click()
                page.locator('#library-map-base').select_option('street')
                page.locator('#library-map-labels').uncheck()
                assert page.evaluate('testAtlas.getLayoutProperty("street", "visibility")') == 'visible'
                assert page.evaluate('testAtlas.getLayoutProperty("satellite", "visibility")') == 'none'
                assert page.evaluate('testAtlas.getLayoutProperty("street-labels", "visibility")') == 'none'
                page.locator('#library-map-options summary').click()
                page.locator('#map-pick-route').click()
                page.locator('#map-search').fill('不存在')
                expect(page.locator('#map-pick-list')).to_have_text('没有符合条件的轨迹')
                page.locator('#map-search').fill('')
                expect(page.locator(f'[data-map-item="{route_id}"]')).to_be_checked()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight')
                map_box = page.locator('#library-map').bounding_box()
                assert map_box['width'] == width and map_box['height'] >= height * .75, map_box
                for selector in ('#map-picker', '#map-clear', '.map-view-tools', '#library-map-status'):
                    box = page.locator(selector).bounding_box()
                    assert 0 <= box['x'] and box['x'] + box['width'] <= width, (selector, box)
                    assert box['y'] >= map_box['y'] and box['y'] + box['height'] <= height, (selector, box)
                page.locator('#map-fit-all').click()
                page.wait_for_timeout(600)
                point = page.evaluate('testAtlas.project([119.01, 30.01])')
                panel = page.locator('#map-picker').bounding_box()
                if width > 600:
                    assert point['x'] > panel['x'] + panel['width'], point
                else:
                    assert point['y'] + map_box['y'] < panel['y'], (point, panel)
                page.screenshot(path=f'/tmp/hiking-map-{width}.png', full_page=True)
                page.locator('#map-picker summary').click()
                expect(page.locator('#map-search')).not_to_be_visible()
                page.locator('#map-picker summary').click()
                page.locator('#records-tab').click()
                expect(page.locator('#library-list')).to_be_visible()
                expect(page.locator('#map-view')).not_to_be_visible()
                page.locator('#map-tab').click()
                expect(page.locator('#map-selection-count')).to_contain_text('已选 2 条')
                page.locator(f'[data-map-item="{route_id}"]').uncheck()
                page.wait_for_function('testAtlas.getSource("tracks")._data.features.length === 2')
                page.locator('#map-clear').click()
                page.wait_for_function('testAtlas.getSource("tracks")._data.features.length === 0')
                expect(page.locator('#map-fit-all')).to_be_disabled()
                # A late response after deselection must not draw the old track.
                page.evaluate('''() => {
                    const realFetch = window.fetch;
                    window.fetch = (url, options) => url === "/api/items/''' + str(route_id) + '''"
                        ? new Promise(resolve => { window.releaseTrack = () => resolve(realFetch(url, options)); })
                        : realFetch(url, options);
                    window.restoreFetch = () => { window.fetch = realFetch; };
                }''')
                page.locator(f'[data-map-item="{route_id}"]').check()
                page.locator(f'[data-map-item="{route_id}"]').uncheck()
                page.evaluate('releaseTrack(); restoreFetch()')
                page.wait_for_timeout(300)
                assert page.evaluate('testAtlas.getSource("tracks")._data.features.length') == 0
                page.route(f'**/api/items/{route_id}', lambda r: r.fulfill(status=500, json={'error': '模拟轨迹失败'}))
                page.locator(f'[data-map-item="{route_id}"]').click()
                expect(page.locator('#notice')).to_contain_text('模拟轨迹失败')
                expect(page.locator(f'[data-map-item="{route_id}"]')).not_to_be_checked()
                page.unroute(f'**/api/items/{route_id}')
                page.locator('#routes-tab').click()
                page.get_by_role('heading', name='虚构山脊路线', exact=True).click()
                expect(page.locator('#detail-view')).to_be_visible()
                page.locator('#back-library').click()
                expect(page.locator('#library-view')).to_be_visible()
                assert not errors, errors
                context.close()
            browser.close()
            print('Map selection, mixed tracks, segments, retry, race, settings, navigation and responsive layout passed.')
    finally:
        http.shutdown()
        http.server_close()
        thread.join()
