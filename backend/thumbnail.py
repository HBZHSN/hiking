"""North-up GPX previews with embedded satellite tiles, using only the standard library."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

WIDTH, HEIGHT, PADDING = 480, 288, 28


def layout(segments):
    origin = segments[0][0]['lng']
    projected = []
    for segment in segments:
        points = []
        for point in segment:
            lng = origin + (point['lng'] - origin + 180) % 360 - 180
            lat = math.radians(point['lat'])
            points.append(((lng + 180) / 360, (1 - math.asinh(math.tan(lat)) / math.pi) / 2))
        projected.append(points)
    xs, ys = zip(*(point for segment in projected for point in segment))
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    scale = min((WIDTH - 2 * PADDING) / max(span_x, 1e-12),
                (HEIGHT - 2 * PADDING) / max(span_y, 1e-12), 256 * 2**17)
    zoom = max(1, min(17, math.floor(math.log2(scale / 256))))
    size = scale / 2**zoom
    left = (min(xs) + max(xs)) / 2 * scale - WIDTH / 2
    top = (min(ys) + max(ys)) / 2 * scale - HEIGHT / 2
    paths = [[(x * scale - left, y * scale - top) for x, y in segment] for segment in projected]
    tiles = []
    for x in range(math.floor(left / size), math.ceil((left + WIDTH) / size)):
        for y in range(max(0, math.floor(top / size)), min(2**zoom, math.ceil((top + HEIGHT) / size))):
            tiles.append((zoom, x % 2**zoom, y, x * size - left, y * size - top, size))
    return paths, tiles


def thumbnail(track, cache_path, source, tile):
    coordinates = [[{'lng': p['lng'], 'lat': p['lat']} for p in segment] for segment in track['segments']]
    digest = hashlib.sha256(json.dumps(['v1', source, coordinates], sort_keys=True).encode()).hexdigest()
    signature = f'<!--{digest}-->'.encode()
    cache_path = Path(cache_path)
    if cache_path.is_file():
        cached = cache_path.read_bytes()
        if cached.startswith(signature):
            return cached
    paths, tiles = layout(coordinates)
    images = []
    complete = True
    for zoom, x, y, dx, dy, size in tiles:
        status, body, headers = tile(f'/api/tiles/tianditu/img_w/{zoom}/{x}/{y}')
        content_type = dict(headers).get('Content-Type', '').split(';')[0]
        if content_type == 'image/jpg':
            content_type = 'image/jpeg'
        if status != 200 or content_type not in ('image/png', 'image/jpeg', 'image/webp'):
            complete = False
            images = []
            break
        data = base64.b64encode(body).decode('ascii')
        images.append(f'<image x="{dx:.3f}" y="{dy:.3f}" width="{size:.3f}" height="{size:.3f}" href="data:{content_type};base64,{data}"/>')
    route = ' '.join('M' + ' L'.join(f'{x:.1f},{y:.1f}' for x, y in segment) for segment in paths)
    markers = []
    for (x, y), color in [(paths[0][0], '#208955'), (paths[-1][-1], '#ed7044')]:
        markers.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" stroke="white" stroke-width="2"/>')
    label = '© 天地图' if complete else '卫星底图暂不可用 · 重新打开列表重试'
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">'
           '<rect width="100%" height="100%" fill="#34483f"/>' + ''.join(images) +
           f'<path d="{route}" fill="none" stroke="#183b32" stroke-width="6" stroke-linejoin="round" stroke-linecap="round"/>'
           f'<path d="{route}" fill="none" stroke="#ffdc65" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>' + ''.join(markers) +
           '<rect x="8" y="8" width="30" height="36" rx="5" fill="#17372cdd"/>'
           '<text x="23" y="23" text-anchor="middle" fill="white" font-size="13">↑</text>'
           '<text x="23" y="37" text-anchor="middle" fill="white" font-size="11">北</text>'
           f'<rect y="{HEIGHT-22}" width="{WIDTH}" height="22" fill="#17372cdd"/>'
           f'<text x="8" y="{HEIGHT-7}" fill="white" font-size="11">{label}</text></svg>').encode()
    body = signature + svg
    # Failed backgrounds are never cached, so the next visit can recover automatically.
    if complete:
        cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache_path.parent, delete=False) as temporary:
            temporary.write(body)
        try:
            os.replace(temporary.name, cache_path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    return body
