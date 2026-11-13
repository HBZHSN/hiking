"""Isolated browser regression; uses the existing cached map engine, never production data."""
import safety

import hashlib
import subprocess
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
    movie = Path(directory) / 'live.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=320x240:r=12',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '2',
                    '-c:v', 'libx264', '-c:a', 'aac', '-threads', '1', str(movie)], check=True, timeout=20)
    video_bytes = movie.read_bytes()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=['--enable-unsafe-swiftshader'])
            for width in (1440, 390):
                context = browser.new_context(viewport={'width': width, 'height': 1000}, has_touch=width <= 600)
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/api/items/*/photos', lambda r: r.fulfill(json=[
                    dict(id=1, filename='虚构Live照片.png', point_index=1, lat=30.001, lng=119.001, live=True),
                    dict(id=2, filename='同一时刻Live照片.png', point_index=1, lat=None, lng=None, live=True),
                    dict(id=3, filename='终点Live照片.png', point_index=5, lat=None, lng=None, live=True),
                    dict(id=4, filename='静态照片.png', point_index=4, lat=30.004, lng=119.004, live=False)]))
                page.route('**/api/photos/*', lambda r: r.fulfill(content_type='image/png', body=png(320, 240)))
                page.route('**/api/photos/*/live', lambda r: r.fulfill(content_type='video/mp4', body=video_bytes))
                page.goto(origin)
                page.locator('#password').fill('browser-test-password')
                page.get_by_role('button', name='登录', exact=True).click()
                page.locator('#records-tab').click()
                page.get_by_role('heading', name='虚构徒步回放', exact=True).click()
                page.wait_for_function('window.testMap && document.querySelector(".photo-marker")')
                page.locator('.photo-thumb-card .photo-preview').first.click()
                page.wait_for_function('document.querySelector("#lightbox-media video")?.currentTime > 0')
                assert page.locator('#lightbox-media video').evaluate('v => !v.muted && !v.loop && v.volume > 0')
                page.mouse.wheel(0, 100)
                page.mouse.move(1, 1)
                assert page.locator('#photo-lightbox').evaluate('d => d.open')
                page.wait_for_function('document.querySelector("#lightbox-media video").hidden')
                assert page.locator('#lightbox-img').is_visible()
                page.locator('#lightbox-close').click()
                page.wait_for_function('document.querySelector("#lightbox-media video").paused && !document.querySelector("#lightbox-media video").getAttribute("src")')
                card = page.locator('.photo-thumb-card').first
                image_box = card.locator('.photo-preview').bounding_box()
                delete_box = card.locator('.photo-del-btn').bounding_box()
                assert delete_box['y'] >= image_box['y'] + image_box['height'] - 1
                page.once('dialog', lambda d: d.dismiss())
                card.locator('.photo-del-btn').click()
                assert page.locator('.photo-thumb-card').count() == 4
                print(f'{width}px: dialog and deletion passed', flush=True)
                page.evaluate('testMap.jumpTo({center:[119.001,30.001],zoom:16,pitch:0})')
                marker = page.locator('.photo-marker').first
                marker.locator('.photo-preview').click()
                page.wait_for_function('document.querySelector(".photo-marker video")?.currentTime > 0')
                assert marker.get_attribute('aria-expanded') == 'true'
                page.mouse.wheel(0, 20)
                assert marker.get_attribute('aria-expanded') == 'true'
                page.locator('#route-name').click()
                assert marker.get_attribute('aria-expanded') == 'false'
                assert marker.locator('video').evaluate('v => v.paused')
                print(f'{width}px: map playback passed', flush=True)
                page.locator('#replay-rate').evaluate("e => e.value='30'")
                page.evaluate("window.liveEnds=[]; document.addEventListener('ended',e=>{if(e.target.closest('#replay-photo-media')) liveEnds.push(e.target.src)},true); window.liveStarts=[]; document.addEventListener('playing',e=>{if(e.target.closest('#replay-photo-media')) liveStarts.push(e.target.src)},true)")
                page.locator('#replay-open').click()
                page.wait_for_function('liveStarts.length >= 1')
                assert page.locator('#replay-photo-image').get_attribute('alt') == '同一时刻Live照片.png'
                page.locator('#replay-follow').dispatch_event('click')
                position = float(page.locator('#replay-progress').input_value())
                page.wait_for_function('(before) => Number(document.querySelector("#replay-progress").value) > before && !document.querySelector("#replay-photo-media video").paused && !document.querySelector("#replay-photo-media video").muted', arg=position)
                page.locator('#replay-play').click()
                assert page.locator('#replay-photo-media video').evaluate('v => v.paused')
                paused = page.locator('#replay-photo-media video').evaluate('v => v.currentTime')
                page.wait_for_timeout(150)
                assert page.locator('#replay-photo-media video').evaluate('v => v.currentTime') == paused
                page.locator('#replay-play').click()
                page.wait_for_function("liveStarts.some(s=>s.endsWith('/2/live'))")
                page.wait_for_function("liveStarts.some(s=>s.endsWith('/3/live'))")
                page.wait_for_function("document.querySelector('#replay-play').getAttribute('aria-pressed')==='false'")
                assert page.locator('#replay-photo-media video').evaluate('v => v.hidden && v.paused')
                assert page.locator('#replay-photo-image').get_attribute('alt') == '终点Live照片.png'
                assert not page.evaluate("liveStarts.some(s=>s.endsWith('/1/live'))"), 'duplicate timestamps must not queue old Live photos'
                for photo_id in (2, 3):
                    assert page.evaluate('(id) => liveEnds.filter(s=>s.endsWith(`/${id}/live`)).length', photo_id) == 1
                # At 120x the 1.5-second window to the next still cannot fit a 2-second clip.
                page.locator('#replay-rate').select_option('120')
                page.locator('#replay-close').click()
                page.evaluate("""() => {
                    liveStarts=[]; liveEnds=[]; window.fastPhotos=[];
                    window.photoObserver=new MutationObserver(() => {
                        const image=document.querySelector('#replay-photo-image');
                        const video=document.querySelector('#replay-photo-media video');
                        fastPhotos.push({name:image.alt, still:video.hidden && video.paused && image.checkVisibility()});
                    });
                    photoObserver.observe(document.querySelector('#replay-photo-image'), {attributes:true, attributeFilter:['alt']});
                }""")
                page.locator('#replay-open').dispatch_event('click')
                page.wait_for_function("liveEnds.some(s=>s.endsWith('/3/live'))")
                for name in ('同一时刻Live照片.png', '静态照片.png'):
                    assert page.evaluate('(name) => fastPhotos.some(p=>p.name===name && p.still)', name), page.evaluate('fastPhotos')
                assert not page.evaluate("liveStarts.some(s=>s.endsWith('/1/live') || s.endsWith('/2/live'))")
                page.evaluate('photoObserver.disconnect()')
                page.locator('#replay-photos button').first.click()
                page.wait_for_function('document.querySelector("#replay-photo-media video").currentTime > 0')
                page.locator('#replay-close').click()
                assert page.locator('#replay-photo-media video').evaluate('v => v.paused && !v.getAttribute("src")')
                # An explicit retry grants sound when automatic playback is blocked.
                page.evaluate("""() => { window.originalPlay=HTMLMediaElement.prototype.play;
                    HTMLMediaElement.prototype.play=function(){return Promise.reject(new DOMException('blocked','NotAllowedError'))}; }""")
                page.locator('.photo-thumb-card .photo-preview').first.click()
                page.wait_for_function('!document.querySelector("#lightbox-media .photo-live-play").hidden')
                page.evaluate('() => {HTMLMediaElement.prototype.play=window.originalPlay;}')
                page.locator('#lightbox-media .photo-live-play').click()
                page.wait_for_function('document.querySelector("#lightbox-media video").currentTime > 0')
                page.keyboard.press('Escape')
                page.wait_for_function('document.querySelector("#lightbox-media video").paused')
                assert not errors, errors
                print(f'{width}px: photo dialog, separate deletion, map collapse, sound, once-only Live, continuous replay/duplicates/end/pause/cleanup and autoplay retry passed.', flush=True)
                context.close()
            browser.close()
    finally:
        http.shutdown()
        http.server_close()
