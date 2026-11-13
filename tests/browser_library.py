"""Isolated library display regression with fictional records and mocked map resources."""
import safety

from pathlib import Path
import sys
import re
from tempfile import TemporaryDirectory
from threading import Thread
from wsgiref.simple_server import make_server, WSGIRequestHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from backend.server import App, Server
from backend.storage import connect, save


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def exercise_workflows(page, origin, width):
    print(f'Checking navigation, editing and import at {width}px', flush=True)
    link = page.locator('.card-open').last
    link.scroll_into_view_if_needed()
    scroll = page.evaluate('scrollY')
    link.click()
    expect(page.locator('#detail-view')).to_be_visible()
    expect(page).to_have_url(re.compile(r'\?item=\d+$'))
    detail_url = page.url
    page.go_back()
    expect(page.locator('#library-view')).to_be_visible()
    page.wait_for_function('(y) => Math.abs(scrollY - y) < 2', arg=scroll)
    page.go_forward()
    expect(page.locator('#detail-view')).to_be_visible()
    page.reload()
    expect(page.locator('#detail-view')).to_be_visible()
    assert page.url == detail_url
    name = page.locator('#route-name').inner_text()
    page.locator('#map-options > summary').click()
    box = page.locator('.map-options-panel').bounding_box()
    assert 0 <= box['x'] and box['x'] + box['width'] <= width
    page.locator('#photos-toggle').click()
    expect(page.locator('#photos-toggle')).to_have_attribute('aria-pressed', 'false')
    expect(page.locator('#photos-toggle')).to_have_text('沿途照片')
    page.locator('#photos-toggle').click()
    page.keyboard.press('Escape')
    assert not page.locator('#map-options').evaluate('e => e.open')
    page.locator('.more-stats > summary').click()
    expect(page.locator('#stat-min')).to_be_visible()
    page.locator('.more-stats > summary').click()

    page.locator('#open-edit').click()
    page.locator('#item-name').fill('取消时不应保存')
    page.keyboard.press('Escape')
    expect(page.locator('#route-name')).to_have_text(name)
    page.locator('#open-edit').click()
    expect(page.locator('#item-name')).to_have_value(name)
    page.locator('#item-notes').fill('虚构测试备注')
    page.locator('#item-days').fill('2')
    item_url = origin + '/api/items/' + detail_url.split('item=')[1]
    page.route(item_url, lambda r: r.fulfill(status=500, json={'error': '模拟保存失败'})
               if r.request.method == 'PATCH' else r.continue_())
    page.locator('#save-item').click()
    expect(page.locator('#edit-error')).to_have_text('模拟保存失败')
    expect(page.locator('#item-notes')).to_have_value('虚构测试备注')
    expect(page.locator('#save-item')).to_be_enabled()
    page.unroute(item_url)
    page.locator('#save-item').click()
    expect(page.locator('#edit-dialog')).not_to_be_visible()
    expect(page.locator('#shared-notes')).to_have_text('虚构测试备注')
    expect(page.locator('#stat-days')).to_have_text('2天')
    page.locator('#open-edit').click()
    page.locator('#item-notes').fill('')
    page.locator('#item-days').fill('')
    page.locator('#save-item').click()
    expect(page.locator('#edit-dialog')).not_to_be_visible()
    expect(page.locator('#stat-days')).to_have_text('1天')
    page.go_back()
    expect(page.locator('#library-view')).to_be_visible()
    page.go_forward()
    expect(page.locator('#detail-view')).to_be_visible()

    page.locator('#open-share').click()
    page.locator('#generate-share').click()
    expect(page.locator('#share-url')).to_be_visible()
    share_url = page.locator('#share-url').input_value()
    anonymous = page.context.browser.new_context(viewport={'width': width, 'height': 1000})
    try:
        anonymous.route('**/*', lambda r: r.continue_() if r.request.url.startswith(origin + '/') else r.abort())
        shared = anonymous.new_page()
        shared.goto(share_url)
        expect(shared.locator('#route-name')).to_have_text(name)
        expect(shared.locator('#open-edit')).not_to_be_visible()
        expect(shared.locator('.photo-upload-label')).not_to_be_visible()
        assert shared.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        anonymous.close()
    page.locator('#close-share').click()
    sync = page.context.new_page()
    try:
        sync.route('**/photo-sync/scan', lambda r: r.fulfill(json={
            'start': 1760000000000, 'end': 1760000120000, 'skipped': 0,
            'index': {'running': False, 'error': '', 'checked': 2},
            'photos': [dict(token=f'fixture-{i}', filename=f'虚构照片{i}.png',
                            taken_at='2025-10-09T08:54:00Z', synced=False) for i in range(2)]}))
        sync.route('**/photo-sync/preview?*', lambda r: r.fulfill(content_type='image/svg+xml', body=thumbnail))
        sync.goto(origin + '/' + page.locator('#open-photo-sync').get_attribute('href'))
        expect(sync.locator('.sync-photo-preview').first).to_be_enabled()
        sync.locator('#select-all-photos').click()
        expect(sync.locator('#sync-selection-count')).to_have_text('已选择 2 张')
        sync.locator('#clear-photo-selection').click()
        expect(sync.locator('#sync-selected-photos')).to_be_disabled()
        sync.locator('.sync-photo-preview').first.click()
        expect(sync.locator('#photo-lightbox')).to_be_visible()
        sync.locator('#lightbox-close').click()
        assert sync.evaluate('document.documentElement.scrollWidth <= innerWidth')
        sync.screenshot(path=f'/tmp/hiking-sync-layout-{width}.png', full_page=True)
    finally:
        sync.close()
    page.locator('#back-library').click()
    expect(page.locator('#library-view')).to_be_visible()
    page.locator('#sort').select_option('name')
    expect(page.locator('#sort-direction')).to_have_attribute('aria-label', '切换为降序')
    page.locator('#sort-direction').click()
    expect(page.locator('#sort-direction')).to_have_attribute('aria-label', '切换为升序')
    page.locator('#reset-filters').click()
    expect(page.locator('#reset-filters')).not_to_be_visible()

    imports = []
    page.on('request', lambda request: imports.append(request) if request.url == origin + '/api/items' and request.method == 'POST' else None)
    page.locator('#open-import').click()
    expect(page.locator('#confirm-import')).to_be_disabled()
    page.locator('#file-input').set_input_files({'name': 'invalid.gpx', 'mimeType': 'application/gpx+xml', 'buffer': b'invalid'})
    assert not imports
    page.locator('#confirm-import').click()
    expect(page.locator('#import-status')).to_be_visible()
    assert not imports
    payload = b'<gpx><trk><name>Workflow fixture</name><trkseg><trkpt lat="30" lon="119"><ele>100</ele></trkpt><trkpt lat="30.01" lon="119.01"><ele>200</ele></trkpt></trkseg></trk></gpx>'
    page.locator('#file-input').set_input_files({'name': 'fixture.gpx', 'mimeType': 'application/gpx+xml', 'buffer': payload})
    page.locator('#import-kind').select_option('route')
    expect(page.locator('#import-parent-field')).not_to_be_visible()
    page.locator('#import-kind').select_option('record')
    expect(page.locator('#import-parent-field')).to_be_visible()
    page.locator('#import-parent').select_option('none')
    page.screenshot(path=f'/tmp/hiking-import-{width}.png', full_page=True)
    assert not imports
    page.locator('#confirm-import').click()
    expect(page.locator('#import-dialog')).not_to_be_visible()
    expect(page.locator('#route-name')).to_have_text('Workflow fixture')
    assert len(imports) == 1
    rows = page.evaluate('fetch("/api/items").then(r => r.json())')
    assert len([row for row in rows if row['kind'] == 'route']) == 1
    assert next(row for row in rows if row['name'] == 'Workflow fixture')['route_id'] is None
    page.locator('#open-edit').click()
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.locator('#delete-item').click()
    expect(page.locator('#edit-dialog')).to_be_visible()
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('#delete-item').click()
    expect(page.locator('#library-view')).to_be_visible()
    expect(page.locator('.library-card')).to_have_count(3)


with TemporaryDirectory() as directory:
    app = App(Path(directory) / 'test.sqlite3', 'library-test-password',
              photo_source=Path(directory) / 'album')
    track = {'system': 'WGS84', 'segments': [[
        dict(lng=119 + i * .01, lat=30 + i * .01, ele=100 + i * 50,
             time=1760000000000 + i * 60000) for i in range(3)]]}
    names = ['虚构短名', '虚构长名称' + '山谷与山脊穿越' * 12, '虚构无地区记录']
    address = '虚构省虚构市虚构区' + '虚构地区' * 8
    with connect(app.database) as db:
        parent = save(db, {'name': '虚构所属路线', 'kind': 'route', 'track': track})
        db.execute('INSERT INTO item_locations(item_id, location) VALUES(?,?)', (parent, address))
        for name in names:
            record_track = {**track, 'segments': [[
                {**point, 'ele': None if name == names[-1] else 100 + i * (2000 if name == names[1] else 50)}
                for i, point in enumerate(track['segments'][0])]]}
            identifier = save(db, {'name': name, 'kind': 'record', 'route_id': parent, 'track': record_track})
            if name != names[-1]:
                db.execute('INSERT INTO item_locations(item_id, location) VALUES(?,?)', (identifier, address))
            else:
                missing_location_id = identifier
    safety.BROWSER_PORT = 0
    http = make_server('127.0.0.1', 0, app, server_class=Server, handler_class=Quiet)
    safety.BROWSER_PORT = http.server_port
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{http.server_port}'
    thumbnail = '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="288"><rect width="480" height="288" fill="#34483f"/><path d="M40 230L220 40L430 170" fill="none" stroke="#ffdc65" stroke-width="8"/></svg>'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width in (1440, 1024, 768, 320, 360, 390, 600):
                print(f'Checking library at {width}px', flush=True)
                context = browser.new_context(viewport={'width': width, 'height': 1000}, has_touch=width <= 600)
                context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(origin + '/') else r.abort())
                context.route('**/api/items/*/thumbnail', lambda r: r.fulfill(content_type='image/svg+xml', body=thumbnail))
                context.route('**/api/map-config', lambda r: r.fulfill(status=503, json={'error': '测试地图占位'}))
                context.route(f'**/api/items/{missing_location_id}/location', lambda r: r.fulfill(json={'location': address}))
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin)
                page.locator('#password').wait_for()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if width in (1440, 320):
                    page.screenshot(path=f'/tmp/hiking-login-{width}.png', full_page=True)
                page.locator('#password').fill('library-test-password')
                page.get_by_role('button', name='登录', exact=True).click()
                page.locator('.library-card').wait_for()
                assert page.locator('.card-meta').inner_text().endswith(' · 3 次记录')
                assert page.locator('.card-title-group .route-location').inner_text() == address
                assert page.locator('#library-summary .summary-cell').last.locator('span').inner_text().endswith('（已知部分）')
                assert '已知部分' not in page.locator('#library-summary strong').last.inner_text()
                assert page.locator('#search').bounding_box()['width'] >= 160
                assert page.locator('#sort').bounding_box()['width'] >= 80
                if width <= 600:
                    search_box = page.locator('#search').bounding_box()
                    sort_box = page.locator('#sort').bounding_box()
                    assert search_box['width'] >= width - 40
                    assert sort_box['width'] >= 160 and sort_box['y'] >= search_box['y'] + search_box['height']
                    controls = page.locator('.library-tools > :visible').all()
                    for index, control in enumerate(controls):
                        box = control.bounding_box()
                        assert 0 <= box['x'] and box['x'] + box['width'] <= width
                        for other in controls[index + 1:]:
                            b = other.bounding_box()
                            assert (box['x'] + box['width'] <= b['x'] + 1 or b['x'] + b['width'] <= box['x'] + 1
                                    or box['y'] + box['height'] <= b['y'] + 1 or b['y'] + b['height'] <= box['y'] + 1)
                page.locator('#open-import').click()
                assert page.locator('#import-dialog').is_visible()
                assert page.locator('#import-kind').is_visible()
                page.keyboard.press('Escape')
                assert page.locator('#import-dialog').is_hidden()
                page.locator('#search').fill('不存在的虚构路线')
                assert page.locator('.empty-library').is_visible()
                page.locator('#reset-filters').click()
                assert page.locator('.library-card').count() == 1
                page.locator('#table-view').click()
                assert page.locator('.library-table th').count() == 9
                assert page.locator('.library-table tbody tr').first.locator('td').count() == 9
                page.locator('#records-tab').click()
                assert page.locator('.library-table th').count() == 8
                assert page.locator('.library-table th').all_text_contents()[-1] == '时长'
                for row in page.locator('.library-table tbody tr').all():
                    assert row.locator('td').count() == 8
                    assert '虚构所属路线' not in row.inner_text()
                assert page.locator('.library-table .route-location').first.inner_text() == address
                page.locator('#cards-view').click()
                assert page.locator('.library-card').count() == len(names)
                for meta in page.locator('.card-meta').all_text_contents():
                    assert meta.endswith(' · 1 天'), meta
                    assert '估算' not in meta and '虚构所属路线' not in meta and '独立记录' not in meta
                for thumbnail_image in page.locator('.card-open > .route-thumbnail').all():
                    box = thumbnail_image.bounding_box()
                    assert abs(box['width'] / box['height'] - 5 / 3) < .01, box
                if width <= 600:
                    heading_heights = []
                    for card in page.locator('.library-card').all():
                        heading = card.locator('.card-heading').bounding_box()
                        badge = card.locator('.difficulty-badge').bounding_box()
                        name = card.locator('h2').bounding_box()
                        assert abs(badge['y'] - heading['y']) < 1
                        assert abs(badge['x'] + badge['width'] - heading['x'] - heading['width']) < 1
                        assert abs(name['y'] - heading['y']) < 1 and abs(name['height'] - 24) < 1
                        assert name['x'] + name['width'] <= badge['x'] - 7
                        location = card.locator('.route-location')
                        if location.count():
                            box = location.bounding_box()
                            assert box['y'] >= name['y'] + name['height'] and box['height'] <= 18
                            assert location.evaluate("e => getComputedStyle(e).whiteSpace") == 'nowrap'
                        preview = card.locator('.route-thumbnail').bounding_box()
                        assert preview['y'] + preview['height'] <= heading['y']
                        heading_heights.append(heading['height'])
                        assert card.locator('.card-stats > div').count() == 3
                    assert max(heading_heights) - min(heading_heights) < 1
                    assert page.get_by_role('heading', name=names[1], exact=True).evaluate('e => e.scrollWidth > e.clientWidth')
                    assert page.get_by_role('heading', name=names[1], exact=True).get_attribute('title') == names[1]
                page.get_by_role('heading', name=names[0], exact=True).click()
                page.locator('#detail-view').wait_for(state='visible')
                expect(page.locator('#route-location')).to_have_text(address)
                assert page.locator('#overview-location').text_content() == address
                assert page.locator('#stat-days').inner_text() == '1天'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#open-share').click()
                assert page.locator('#share-dialog').is_visible()
                page.locator('#close-share').click()
                if width in (1440, 320):
                    page.screenshot(path=f'/tmp/hiking-detail-{width}.png', full_page=True)
                page.locator('#back-library').click()
                page.get_by_role('heading', name=names[-1], exact=True).click()
                expect(page.locator('#route-name')).to_have_text(names[-1])
                expect(page.locator('#route-location')).to_have_text(address)
                expect(page.locator('#overview-location')).to_have_text(address)
                page.locator('#back-library').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                page.screenshot(path=f'/tmp/hiking-library-display-{width}.png', full_page=True)
                if width in (1440, 390):
                    exercise_workflows(page, origin, width)
                    assert not errors, errors
                if width in (1440, 320):
                    page.goto(origin + '/settings.html')
                    page.locator('#cache-bytes:not(:text("—"))').wait_for()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=f'/tmp/hiking-settings-{width}.png', full_page=True)
                    page.goto(origin + '/credits.html')
                    assert page.get_by_role('heading', name='开放数据与署名').is_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert not errors, errors
                context.close()
            browser.close()
            print('Library cards/table/detail and mobile alignment: plain addresses/days, hidden record parent, long/short names, long addresses and missing data passed.')
    finally:
        http.shutdown()
        http.server_close()
        thread.join()
