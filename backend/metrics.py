"""Small library summaries; track points stay on the detail endpoint."""
import math


def elevation_gain(points):
    # shortcut: unknown sensor quality uses fixed 100 m/10 m filtering; use accuracy metadata when available.
    ascent = descent = 0
    start = 0
    while start < len(points):
        if points[start][1] is None:
            start += 1
            continue
        end = start + 1
        while end < len(points) and points[end][1] is not None and not points[end][2]:
            end += 1
        left = right = start
        height_sum = 0
        baseline = points[start][1]
        for i in range(start, end):
            distance, height, _ = points[i]
            while right < end and points[right][0] <= distance + 50:
                height_sum += points[right][1]
                right += 1
            while points[left][0] < distance - 50:
                height_sum -= points[left][1]
                left += 1
            smoothed = height if i in (start, end - 1) else height_sum / (right - left)
            rise = smoothed - baseline
            if abs(rise) >= 10 or i == end - 1:
                ascent += max(0, rise)
                descent += max(0, -rise)
                baseline = smoothed
        start = end
    return ascent, descent


def summarize(track):
    distance = duration = moving = timed_distance = 0
    heights, times, heart, cadence = [], [], [], []
    profile = []
    edges = known_edges = count = 0
    for segment in track['segments']:
        for i, p in enumerate(segment):
            count += 1
            for key, values in [('ele', heights), ('time', times), ('hr', heart), ('cad', cadence)]:
                if p.get(key) is not None:
                    values.append(p[key])
            if not i:
                profile.append((distance, p.get('ele'), True))
                continue
            prev = segment[i - 1]
            edges += 1
            lat1, lat2 = math.radians(prev['lat']), math.radians(p['lat'])
            h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(math.radians(p['lng']-prev['lng'])/2)**2
            length = 6371008.8 * 2 * math.asin(math.sqrt(min(1, h)))
            distance += length
            profile.append((distance, p.get('ele'), False))
            if prev.get('ele') is not None and p.get('ele') is not None:
                known_edges += 1
            if prev.get('time') is not None and p.get('time') is not None and p['time'] > prev['time']:
                elapsed = p['time']-prev['time']
                duration += elapsed
                timed_distance += length
                if length / elapsed * 3600 >= 1:
                    moving += elapsed
    ascent, descent = elevation_gain(profile)
    inferred_days = max(1, math.ceil((max(times)-min(times))/86400000)) if times else 1
    days = track.get('days') or inferred_days
    complete = known_edges == edges and known_edges > 0
    dist_km = distance / 1000
    difficulty = (math.sqrt((dist_km * 0.621371 / days) * (ascent * 3.28084 / days) * 2)
                  * (1 + math.log(days) * 0.2) * (1 + math.floor((ascent / days) / 500) * 0.15)) if complete else None
    return dict(distance=distance, ascent=ascent if known_edges else None, descent=descent if known_edges else None,
                min=min(heights) if heights else None, max=max(heights) if heights else None,
                duration=duration or None, moving=moving if duration else None,
                speed=timed_distance/duration*3600 if duration else None,
                hrMean=sum(heart)/len(heart) if heart else None, hrMax=max(heart) if heart else None,
                cadMean=sum(cadence)/len(cadence) if cadence else None,
                days=days, daysSource='manual' if track.get('days') else 'time' if times else 'default',
                difficulty=difficulty, partialElevation=not complete, count=count,
                date=min(times) if times else None)
