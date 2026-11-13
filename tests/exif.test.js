import test from 'node:test';
import assert from 'node:assert/strict';
import { parseExif, matchPhotoToTrack, computeDimensions } from '../js/exif.js';

test('computeDimensions correctly scales dimensions preserving aspect ratio with default 720', () => {
  assert.deepEqual(computeDimensions(500, 300), { width: 500, height: 300 });
  assert.deepEqual(computeDimensions(1920, 1080), { width: 720, height: 405 });
  assert.deepEqual(computeDimensions(1080, 1920), { width: 405, height: 720 });
  assert.deepEqual(computeDimensions(2000, 4000, 1000), { width: 500, height: 1000 });
});

test('matchPhotoToTrack prioritizes time over GPS location', () => {
  const points = [
    { lng: 119.1, lat: 30.1, time: 1000000 },
    { lng: 119.2, lat: 30.2, time: 2000000 },
    { lng: 119.3, lat: 30.3, time: 3000000 }
  ];

  // Case 1: Photo has BOTH time (near point 1) and GPS (near point 0)
  // Time should WIN because priority is time
  const photoBoth = {
    timestamp: 1990000, // Very close to point 1 (time 2000000)
    lat: 30.1001,       // Very close to point 0
    lng: 119.1001
  };
  const match1 = matchPhotoToTrack(photoBoth, points);
  assert.equal(match1.matchType, 'time');
  assert.equal(match1.pointIndex, 1);
  assert.equal(match1.lat, 30.2);
  assert.equal(match1.lng, 119.2);

  // Case 2: Photo time is outside track time range (+30m tolerance), fallback to GPS
  const photoOutsideTime = {
    timestamp: 999999999, // Way outside
    lat: 30.1001,
    lng: 119.1001
  };
  const match2 = matchPhotoToTrack(photoOutsideTime, points);
  assert.equal(match2.matchType, 'gps');
  assert.equal(match2.pointIndex, 0); // Point 0 is closest geographically
  assert.equal(match2.lat, 30.1);

  // Case 3: Photo has no time, only GPS
  const photoOnlyGps = {
    timestamp: null,
    lat: 30.299,
    lng: 119.301
  };
  const match3 = matchPhotoToTrack(photoOnlyGps, points);
  assert.equal(match3.matchType, 'gps');
  assert.equal(match3.pointIndex, 2);

  // Case 4: Neither time nor GPS
  const photoNone = { timestamp: null, lat: null, lng: null };
  const match4 = matchPhotoToTrack(photoNone, points);
  assert.equal(match4.matchType, 'none');
  assert.equal(match4.pointIndex, null);
});

test('parseExif parses TIFF structure and extracts DateTime and GPS', () => {
  // Empty or invalid inputs
  assert.deepEqual(parseExif(new ArrayBuffer(0)).lat, null);
  assert.deepEqual(parseExif(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]).buffer).timestamp, null);

  // Construct a minimal valid JPEG with APP1 EXIF segment (Little Endian 'II')
  const buffer = new ArrayBuffer(512);
  const view = new DataView(buffer);

  // JPEG SOI
  view.setUint16(0, 0xffd8);
  // APP1 marker
  view.setUint16(2, 0xffe1);
  // APP1 length: 2 + 6 + TIFF... = 250
  view.setUint16(4, 250);
  // "Exif\0\0"
  view.setUint8(6, 0x45); // E
  view.setUint8(7, 0x78); // x
  view.setUint8(8, 0x69); // i
  view.setUint8(9, 0x66); // f
  view.setUint16(10, 0x0000);

  const tiffStart = 12;
  // TIFF Header: 'II', 0x002A, offset to IFD0 (8)
  view.setUint16(tiffStart, 0x4949, true);
  view.setUint16(tiffStart + 2, 0x002a, true);
  view.setUint32(tiffStart + 4, 8, true);

  // IFD0 at tiffStart + 8:
  // 3 entries: Orientation (0x0112), ExifIFD (0x8769), GPSIFD (0x8825)
  const ifd0 = tiffStart + 8;
  view.setUint16(ifd0, 3, true);

  // Entry 1: Orientation = 6 (SHORT, count 1)
  view.setUint16(ifd0 + 2, 0x0112, true);
  view.setUint16(ifd0 + 4, 3, true); // SHORT
  view.setUint32(ifd0 + 6, 1, true); // count 1
  view.setUint16(ifd0 + 10, 6, true); // value 6

  // Entry 2: ExifIFDPointer (0x8769, LONG, count 1, offset 60)
  view.setUint16(ifd0 + 14, 0x8769, true);
  view.setUint16(ifd0 + 16, 4, true); // LONG
  view.setUint32(ifd0 + 18, 1, true);
  view.setUint32(ifd0 + 22, 60, true); // offset from tiffStart

  // Entry 3: GPSInfoIFDPointer (0x8825, LONG, count 1, offset 110)
  view.setUint16(ifd0 + 26, 0x8825, true);
  view.setUint16(ifd0 + 28, 4, true); // LONG
  view.setUint32(ifd0 + 30, 1, true);
  view.setUint32(ifd0 + 34, 110, true);

  // ExifIFD at tiffStart + 60:
  // 1 entry: DateTimeOriginal (0x9003, ASCII, count 20, offset 80)
  const exifIfd = tiffStart + 60;
  view.setUint16(exifIfd, 1, true);
  view.setUint16(exifIfd + 2, 0x9003, true);
  view.setUint16(exifIfd + 4, 2, true); // ASCII
  view.setUint32(exifIfd + 6, 20, true); // count
  view.setUint32(exifIfd + 10, 80, true); // offset from tiffStart

  // Write "2026:10:08 14:30:00\0" at tiffStart + 80
  const dateStr = "2026:10:08 14:30:00\0";
  for (let i = 0; i < dateStr.length; i++) {
    view.setUint8(tiffStart + 80 + i, dateStr.charCodeAt(i));
  }

  // GPSIFD at tiffStart + 110:
  // 4 entries:
  // GPSLatitudeRef (0x0001, ASCII, 2, 'N')
  // GPSLatitude (0x0002, RATIONAL, 3, offset 170)
  // GPSLongitudeRef (0x0003, ASCII, 2, 'E')
  // GPSLongitude (0x0004, RATIONAL, 3, offset 194)
  const gpsIfd = tiffStart + 110;
  view.setUint16(gpsIfd, 4, true);

  view.setUint16(gpsIfd + 2, 0x0001, true);
  view.setUint16(gpsIfd + 4, 2, true);
  view.setUint32(gpsIfd + 6, 2, true);
  view.setUint8(gpsIfd + 10, 0x4e); // 'N'

  view.setUint16(gpsIfd + 14, 0x0002, true);
  view.setUint16(gpsIfd + 16, 5, true); // RATIONAL
  view.setUint32(gpsIfd + 18, 3, true);
  view.setUint32(gpsIfd + 22, 170, true); // offset

  view.setUint16(gpsIfd + 26, 0x0003, true);
  view.setUint16(gpsIfd + 28, 2, true);
  view.setUint32(gpsIfd + 30, 2, true);
  view.setUint8(gpsIfd + 34, 0x45); // 'E'

  view.setUint16(gpsIfd + 38, 0x0004, true);
  view.setUint16(gpsIfd + 40, 5, true);
  view.setUint32(gpsIfd + 42, 3, true);
  view.setUint32(gpsIfd + 46, 194, true);

  // Lat: 30 deg, 15 min, 30 sec = 30 + 15/60 + 30/3600 = 30.258333...
  // 3 rationals at tiffStart + 170 (3 * 8 = 24 bytes)
  const latOff = tiffStart + 170;
  view.setUint32(latOff, 30, true); view.setUint32(latOff + 4, 1, true);
  view.setUint32(latOff + 8, 15, true); view.setUint32(latOff + 12, 1, true);
  view.setUint32(latOff + 16, 30, true); view.setUint32(latOff + 20, 1, true);

  // Lng: 119 deg, 30 min, 0 sec = 119.5
  const lngOff = tiffStart + 194;
  view.setUint32(lngOff, 119, true); view.setUint32(lngOff + 4, 1, true);
  view.setUint32(lngOff + 8, 30, true); view.setUint32(lngOff + 12, 1, true);
  view.setUint32(lngOff + 16, 0, true); view.setUint32(lngOff + 20, 1, true);

  const parsed = parseExif(buffer);
  assert.equal(parsed.orientation, 6);
  assert.ok(Math.abs(parsed.lat - 30.258333) < 0.0001);
  assert.ok(Math.abs(parsed.lng - 119.5) < 0.0001);
  assert.ok(parsed.timestamp > 0);
  assert.equal(new Date(parsed.timestamp).getFullYear(), 2026);
});
