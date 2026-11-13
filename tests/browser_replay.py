"""Isolated browser regression; uses the existing cached map engine, never production data."""
import safety

import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from wsgiref.simple_server import make_server, WSGIRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from backend.server import App, Server
from backend.storage import connect, save
from test_photo_sync import png


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


with TemporaryDirectory() as directory:
    app = App(Path(directory) / 'test.sqlite3', 'browser-test-password',
              tianditu_key='fixture-key', photo_source=Path(directory) / 'album')

    def fixture_resource(url, content_type='application/octet-stream'):
        if 'cdn.jsdelivr.net' in url:
            data = (Path('/tmp/hiking-browser-assets') / hashlib.sha256(url.encode()).hexdigest()).read_bytes()
            if url.endswith('.js'):
                data += b'\nconst resize=maplibregl.Map.prototype.resize; maplibregl.Map.prototype.resize=function(...args){window.testMap=this;return resize.apply(this,args);};'
            return data, content_type
        return png(256, 256), 'image/png'

    app.remote_cache = fixture_resource
    track = {'system': 'WGS84', 'segments': [[
        dict(lng=119 + i * .001, lat=30 + i * .001, ele=100 + i * 10,
             time=1760000000000 + i * 60000, hr=110 + i) for i in range(6)]]}
    with connect(app.database) as db:
        item_id = save(db, {'name': '虚构徒步回放', 'kind': 'record', 'track': track})
    safety.BROWSER_PORT = 0
    http = make_server('127.0.0.1', 0, app, server_class=Server, handler_class=Quiet)
    safety.BROWSER_PORT = http.server_port
    Thread(target=http.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{http.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=['--enable-unsafe-swiftshader'])
            for width in (1440, 390):
                context = browser.new_context(viewport={'width': width, 'height': 1000}, has_touch=width <= 600)
                print(f'Checking replay at {width}px', flush=True)
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/api/items/*/photos', lambda r: r.fulfill(json=[
                    dict(id=1, filename='虚构山间照片.png', point_index=1, lat=30.001, lng=119.001),
                    dict(id=2, filename='虚构山顶照片.png', point_index=4, lat=30.004, lng=119.004)] + [
                    dict(id=i, filename=f'虚构沿途照片{i}.png', point_index=5, lat=None, lng=None) for i in range(3, 61)]))
                page.route('**/api/photos/*', lambda r: r.fulfill(content_type='image/png', body=png(320, 240)))
                page.goto(origin)
                page.locator('#password').fill('browser-test-password')
                page.get_by_role('button', name='登录', exact=True).click()
                page.locator('#records-tab').click()
                page.get_by_role('heading', name='虚构徒步回放', exact=True).click()
                page.wait_for_function('window.testMap && testMap.loaded()')
                page.locator('#replay-photos button').first.wait_for(state='attached')
                camera = page.evaluate('({zoom:testMap.getZoom(),pitch:testMap.getPitch(),bearing:testMap.getBearing()})')
                page.locator('#replay-open').click()
                page.wait_for_function('Number(document.querySelector("#replay-progress").value) > 1000')
                assert page.evaluate('testMap.getPitch()') == 65
                assert 35 < page.evaluate('testMap.getBearing()') < 50
                page.locator('#replay-rate').select_option('1')
                slider = page.locator('#replay-progress')
                slider.scroll_into_view_if_needed()
                box = slider.bounding_box()
                y = box['y'] + box['height'] / 2
                page.mouse.move(box['x'] + box['width'] * .2, y)
                page.mouse.down()
                assert page.locator('#replay-play').get_attribute('aria-pressed') == 'false', box
                page.mouse.move(box['x'] + box['width'] * .7, y, steps=6)
                forward = float(slider.input_value())
                assert forward > 180000, forward
                page.mouse.move(box['x'] + box['width'] * .35, y, steps=6)
                backward = float(slider.input_value())
                assert 75000 < backward < 135000 and backward < forward, backward
                page.wait_for_timeout(200)
                assert float(slider.input_value()) == backward, 'playback must not overwrite an active drag'
                assert page.locator('#replay-photo-image').get_attribute('alt') == '虚构山间照片.png'
                page.mouse.move(box['x'] + box['width'] * .35, y + 70)
                page.mouse.up()
                assert page.locator('#replay-play').get_attribute('aria-pressed') == 'true'
                page.locator('#replay-play').click()
                value = page.locator('#replay-progress').input_value()
                page.wait_for_timeout(150)
                assert page.locator('#replay-progress').input_value() == value
                if width <= 600:
                    cdp = context.new_cdp_session(page)
                    slider.scroll_into_view_if_needed()
                    box = slider.bounding_box()
                    y = box['y'] + box['height'] / 2
                    cdp.send('Input.dispatchTouchEvent', {'type':'touchStart', 'touchPoints':[{'x':box['x'] + box['width'] * .25, 'y':y}]})
                    cdp.send('Input.dispatchTouchEvent', {'type':'touchMove', 'touchPoints':[{'x':box['x'] + box['width'] * .9, 'y':y}]})
                    assert float(slider.input_value()) > 250000
                    assert page.locator('#replay-photo-image').get_attribute('alt') == '虚构山顶照片.png'
                    cdp.send('Input.dispatchTouchEvent', {'type':'touchMove', 'touchPoints':[{'x':box['x'] - 5, 'y':y}]})
                    assert slider.input_value() == '0'
                    assert page.locator('#replay-photo').is_hidden()
                    cdp.send('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':[]})
                    assert page.locator('#replay-play').get_attribute('aria-pressed') == 'false'
                    cdp.detach()
                page.locator('#replay-progress').evaluate("e => {e.value=150000;e.dispatchEvent(new Event('input'));}")
                assert '0:02:30' in page.locator('#replay-time').inner_text()
                assert 35 < page.evaluate('testMap.getBearing()') < 50
                assert page.locator('#replay-photo-image').get_attribute('alt') == '虚构山间照片.png'
                assert page.locator('#replay-photos button').first.get_attribute('aria-current') == 'true'
                if width > 1000:
                    map_box = page.locator('#map').bounding_box()
                    photo_box = page.locator('#replay-photo-image').bounding_box()
                    assert photo_box['x'] >= map_box['x'] + map_box['width'], 'photo must not cover map'
                    assert photo_box['width'] >= 210 and photo_box['height'] >= 200, photo_box
                    assert page.locator('#replay-panel').bounding_box()['height'] < 90
                    rail = page.locator('#replay-photos')
                    assert rail.evaluate('e => e.scrollHeight > e.clientHeight'), rail.evaluate('e => ({scroll:e.scrollHeight,client:e.clientHeight,rows:getComputedStyle(e).gridTemplateRows})')
                    rail.hover()
                    page.mouse.wheel(0, 240)
                    page.wait_for_function('document.querySelector("#replay-photos").scrollTop > 0')
                    assert rail.evaluate('e => getComputedStyle(e).scrollbarWidth') == 'thin'
                    rail.evaluate('e => e.scrollTop = 0')
                    assert page.locator('#map .photo-marker:visible').count() == 2
                    canvas_width = page.locator('#map canvas').evaluate('e => e.clientWidth')
                    assert abs(canvas_width - map_box['width']) < 2
                    first = page.locator('#replay-photos button').first.bounding_box()
                    second = page.locator('#replay-photos button').nth(1).bounding_box()
                    assert abs(first['y'] - second['y']) < 1 and second['x'] > first['x']
                    preview = page.locator('#replay-photo').bounding_box()
                    album = page.locator('.replay-album').bounding_box()
                    assert 0 <= album['y'] - preview['y'] - preview['height'] <= 12
                    assert page.locator('#replay-memories').bounding_box()['height'] <= map_box['height'] + 1
                preview = page.locator('#replay-photo').bounding_box()
                assert page.locator('.replay-album').bounding_box()['y'] >= preview['y'] + preview['height']
                page.locator('#map .photo-marker').first.click()
                assert page.locator('#map .photo-marker').first.get_attribute('aria-expanded') == 'true'
                assert page.locator('#replay-follow').get_attribute('aria-pressed') == 'false'
                page.locator('#replay-rate').select_option('1')
                page.locator('#replay-play').click()
                before = page.evaluate('testMap.getBearing()')
                page.locator('#map canvas').scroll_into_view_if_needed()
                box = page.locator('#map canvas').bounding_box()
                x, y = box['x'] + box['width'] * .65, box['y'] + box['height'] * .55
                page.mouse.move(x, y)
                page.mouse.down(button='right')
                page.mouse.move(x + 70, y + 15, steps=8)
                page.mouse.up(button='right')
                page.wait_for_timeout(1000)
                manual = page.evaluate('({bearing:testMap.getBearing(),pitch:testMap.getPitch()})')
                assert abs(manual['bearing'] - before) > 5, (before, manual)
                value = float(page.locator('#replay-progress').input_value())
                page.wait_for_function('(value) => Number(document.querySelector("#replay-progress").value) > value + 100', arg=value)
                assert abs(page.evaluate('testMap.getBearing()') - manual['bearing']) < .01
                assert abs(page.evaluate('testMap.getPitch()') - manual['pitch']) < .01
                page.locator('#replay-play').click()
                page.locator('#replay-follow').click()
                assert page.locator('#replay-follow').get_attribute('aria-pressed') == 'true'
                assert page.locator('#replay-panel').bounding_box()['height'] < (90 if width > 1000 else 125)
                page.locator('#replay-help-title').click()
                assert page.locator('#replay-note').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#replay-help-title').click()
                page.screenshot(path=f'/tmp/hiking-replay-photo-{width}.png', full_page=True)
                page.locator('#replay-photos button').nth(1).click()
                assert page.locator('#replay-photo-image').get_attribute('alt') == '虚构山顶照片.png'
                page.locator('#replay-progress').evaluate("e => {e.value=0;e.dispatchEvent(new Event('input'));}")
                assert page.locator('#replay-photo').is_hidden()
                if width > 1000:
                    assert page.locator('#replay-memories').bounding_box()['width'] == 84
                    assert page.locator('#map').bounding_box()['width'] > map_box['width']
                assert page.locator('#replay-photos button[aria-current="true"]').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=f'/tmp/hiking-replay-{width}.png', full_page=True)
                page.locator('#replay-rate').select_option('600')
                page.locator('#replay-play').click()
                page.wait_for_function('document.querySelector("#replay-play").textContent === "重新播放"')
                assert page.locator('#replay-progress').input_value() == '300000'
                page.locator('#replay-rate').select_option('1')
                page.locator('#replay-play').click()
                page.wait_for_function('Number(document.querySelector("#replay-progress").value) < 300000')
                page.locator('#replay-close').click()
                assert page.locator('#replay-panel').is_hidden()
                assert page.locator('#replay-memories').is_hidden()
                assert page.locator('#map .photo-marker:visible').count() == 2
                assert page.evaluate('testMap.getPitch()') == camera['pitch']
                assert abs(page.evaluate('testMap.getZoom()') - camera['zoom']) < .001
                page.locator('#replay-open').click()
                page.locator('#back-library').click()
                assert page.locator('.replay-walker').count() == 0
                assert not errors, errors
                context.close()
            browser.close()
            print('Desktop/mobile replay: playback, pause, seek, photos, rate, end, restart, cleanup passed.')
    finally:
        http.shutdown()
        http.server_close()
