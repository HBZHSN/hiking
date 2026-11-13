/**
 * EXIF extraction, photo-to-track matching, and client-side compression.
 */

/**
 * Parse EXIF metadata from ArrayBuffer of JPEG image.
 * Safely returns { lat, lng, timestamp, isoTime, orientation, altitude } or empty values.
 */
export function parseExif(buffer) {
  const result = {
    lat: null,
    lng: null,
    timestamp: null,
    isoTime: null,
    orientation: 1,
    altitude: null
  };

  try {
    const view = new DataView(buffer);
    if (view.byteLength < 4 || view.getUint16(0) !== 0xffd8) {
      return result; // Not a standard JPEG
    }

    let offset = 2;
    let tiffStart = -1;

    while (offset + 4 <= view.byteLength) {
      const marker = view.getUint16(offset);
      offset += 2;

      // Standalone markers without payload
      if (marker === 0xffd8 || marker === 0xffd9 || (marker >= 0xffd0 && marker <= 0xffd7)) {
        continue;
      }

      if (offset + 2 > view.byteLength) break;
      const length = view.getUint16(offset);

      if (marker === 0xffe1) {
        // APP1 marker - check for 'Exif\0\0'
        if (offset + 2 + 6 <= view.byteLength) {
          const header = String.fromCharCode(
            view.getUint8(offset + 2),
            view.getUint8(offset + 3),
            view.getUint8(offset + 4),
            view.getUint8(offset + 5)
          );
          if (header === 'Exif' && view.getUint16(offset + 6) === 0x0000) {
            tiffStart = offset + 8;
            break;
          }
        }
      }

      offset += length;
    }

    if (tiffStart === -1 || tiffStart + 8 > view.byteLength) {
      return result;
    }

    // TIFF Header
    const byteOrder = view.getUint16(tiffStart);
    const littleEndian = byteOrder === 0x4949; // 'II'
    if (!littleEndian && byteOrder !== 0x4d4d) { // 'MM'
      return result;
    }

    if (view.getUint16(tiffStart + 2, littleEndian) !== 0x002a) {
      return result;
    }

    const firstIFDOffset = view.getUint32(tiffStart + 4, littleEndian);
    if (tiffStart + firstIFDOffset >= view.byteLength) {
      return result;
    }

    const readValue = (entryOffset) => {
      const tag = view.getUint16(entryOffset, littleEndian);
      const type = view.getUint16(entryOffset + 2, littleEndian);
      const count = view.getUint32(entryOffset + 4, littleEndian);

      const typeSizes = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8 };
      const size = (typeSizes[type] || 1) * count;
      const dataOffset = size <= 4 ? entryOffset + 8 : tiffStart + view.getUint32(entryOffset + 8, littleEndian);

      if (dataOffset + size > view.byteLength) return null;

      if (type === 2) { // ASCII
        let str = '';
        for (let i = 0; i < count; i++) {
          const c = view.getUint8(dataOffset + i);
          if (c === 0) break;
          str += String.fromCharCode(c);
        }
        return str.trim();
      }

      if (type === 3) { // SHORT
        return count === 1 ? view.getUint16(dataOffset, littleEndian) : null;
      }

      if (type === 4) { // LONG
        return count === 1 ? view.getUint32(dataOffset, littleEndian) : null;
      }

      if (type === 5) { // RATIONAL
        if (count === 1) {
          const num = view.getUint32(dataOffset, littleEndian);
          const den = view.getUint32(dataOffset + 4, littleEndian);
          return den ? num / den : 0;
        }
        const vals = [];
        for (let i = 0; i < count; i++) {
          const num = view.getUint32(dataOffset + i * 8, littleEndian);
          const den = view.getUint32(dataOffset + i * 8 + 4, littleEndian);
          vals.push(den ? num / den : 0);
        }
        return vals;
      }

      return null;
    };

    const readIFD = (ifdOffset) => {
      const tags = {};
      if (tiffStart + ifdOffset + 2 > view.byteLength) return tags;
      const count = view.getUint16(tiffStart + ifdOffset, littleEndian);
      let entryOffset = tiffStart + ifdOffset + 2;

      for (let i = 0; i < count && entryOffset + 12 <= view.byteLength; i++, entryOffset += 12) {
        const tag = view.getUint16(entryOffset, littleEndian);
        tags[tag] = readValue(entryOffset);
      }
      return tags;
    };

    const ifd0 = readIFD(firstIFDOffset);
    if (ifd0[0x0112]) result.orientation = ifd0[0x0112];

    let exifIFD = {};
    if (ifd0[0x8769]) exifIFD = readIFD(ifd0[0x8769]);

    let gpsIFD = {};
    if (ifd0[0x8825]) gpsIFD = readIFD(ifd0[0x8825]);

    // Parse timestamp (DateTimeOriginal 0x9003, DateTimeDigitized 0x9004, DateTime 0x0132)
    const timeStr = exifIFD[0x9003] || exifIFD[0x9004] || ifd0[0x0132];
    const offsetStr = exifIFD[0x9011] || exifIFD[0x9010] || '';

    if (timeStr && typeof timeStr === 'string') {
      const match = timeStr.match(/^(\d{4})[:/-](\d{2})[:/-](\d{2})\s+(\d{2}):(\d{2}):(\d{2})/);
      if (match) {
        const [_, Y, M, D, h, m, s] = match;
        if (offsetStr && /^[+-]\d{2}:\d{2}$/.test(offsetStr)) {
          const iso = `${Y}-${M}-${D}T${h}:${m}:${s}${offsetStr}`;
          result.timestamp = new Date(iso).getTime();
          result.isoTime = new Date(result.timestamp).toISOString();
        } else {
          const date = new Date(Number(Y), Number(M) - 1, Number(D), Number(h), Number(m), Number(s));
          result.timestamp = date.getTime();
          result.isoTime = date.toISOString();
        }
      }
    }

    // Parse GPS
    const latRef = gpsIFD[0x0001]; // 'N' or 'S'
    const latVal = gpsIFD[0x0002]; // array of 3 numbers
    const lngRef = gpsIFD[0x0003]; // 'E' or 'W'
    const lngVal = gpsIFD[0x0004]; // array of 3 numbers

    if (Array.isArray(latVal) && latVal.length >= 3) {
      let lat = latVal[0] + latVal[1] / 60 + latVal[2] / 3600;
      if (latRef === 'S') lat = -lat;
      if (lat >= -90 && lat <= 90) result.lat = lat;
    }

    if (Array.isArray(lngVal) && lngVal.length >= 3) {
      let lng = lngVal[0] + lngVal[1] / 60 + lngVal[2] / 3600;
      if (lngRef === 'W') lng = -lng;
      if (lng >= -180 && lng <= 180) result.lng = lng;
    }

    if (gpsIFD[0x0006] != null) {
      const altRef = gpsIFD[0x0005] || 0;
      result.altitude = altRef === 1 ? -gpsIFD[0x0006] : gpsIFD[0x0006];
    }
  } catch {
    // Return whatever was parsed safely
  }

  return result;
}

/**
 * Match photo metadata to track points.
 * Rule: Priority on Time, fallback to GPS location.
 *
 * @param {Object} exif - { timestamp, lat, lng }
 * @param {Array} trackPoints - array of points [{ lng, lat, ele, time, distance }, ...]
 * @returns {Object} - { matchType: 'time'|'gps'|'none', pointIndex, lat, lng }
 */
export function matchPhotoToTrack(exif, trackPoints) {
  if (!Array.isArray(trackPoints) || trackPoints.length === 0) {
    return { matchType: 'none', pointIndex: null, lat: exif?.lat ?? null, lng: exif?.lng ?? null };
  }

  // 1. Priority: Time match
  if (exif?.timestamp != null) {
    const timedIndices = [];
    for (let i = 0; i < trackPoints.length; i++) {
      if (trackPoints[i].time != null) timedIndices.push(i);
    }

    if (timedIndices.length > 0) {
      const firstTime = trackPoints[timedIndices[0]].time;
      const lastTime = trackPoints[timedIndices[timedIndices.length - 1]].time;
      const minTrackTime = Math.min(firstTime, lastTime);
      const maxTrackTime = Math.max(firstTime, lastTime);

      // Allow 30 minutes tolerance around hike duration
      const tolerance = 30 * 60 * 1000;
      if (exif.timestamp >= minTrackTime - tolerance && exif.timestamp <= maxTrackTime + tolerance) {
        let bestIndex = timedIndices[0];
        let minDiff = Math.abs(trackPoints[bestIndex].time - exif.timestamp);

        for (const idx of timedIndices) {
          const diff = Math.abs(trackPoints[idx].time - exif.timestamp);
          if (diff < minDiff) {
            minDiff = diff;
            bestIndex = idx;
          }
        }

        const point = trackPoints[bestIndex];
        return {
          matchType: 'time',
          pointIndex: bestIndex,
          lat: point.lat,
          lng: point.lng,
          timeDiffMs: minDiff
        };
      }
    }
  }

  // 2. Secondary: GPS match
  if (exif?.lat != null && exif?.lng != null) {
    let bestIndex = 0;
    let minDistanceSq = Infinity;

    // Find nearest point on route (using spherical approximation)
    const rad = Math.PI / 180;
    const cosLat = Math.cos(exif.lat * rad);

    for (let i = 0; i < trackPoints.length; i++) {
      const p = trackPoints[i];
      const dLng = (p.lng - exif.lng) * cosLat;
      const dLat = p.lat - exif.lat;
      const distSq = dLng * dLng + dLat * dLat;
      if (distSq < minDistanceSq) {
        minDistanceSq = distSq;
        bestIndex = i;
      }
    }

    const nearestPoint = trackPoints[bestIndex];
    // Distance in rough meters: approx 111,320 meters per degree
    const approxDistMeters = Math.sqrt(minDistanceSq) * 111320;

    // Snap to route point if within 1500m, otherwise place at photo's GPS with reference to closest point
    if (approxDistMeters <= 1500) {
      return {
        matchType: 'gps',
        pointIndex: bestIndex,
        lat: nearestPoint.lat,
        lng: nearestPoint.lng,
        distanceMeters: approxDistMeters
      };
    }

    return {
      matchType: 'gps',
      pointIndex: bestIndex,
      lat: exif.lat,
      lng: exif.lng,
      distanceMeters: approxDistMeters
    };
  }

  // 3. Neither time nor GPS match
  return {
    matchType: 'none',
    pointIndex: null,
    lat: null,
    lng: null
  };
}

/**
 * Calculate scaled dimensions fitting within maxDimension without upscaling.
 */
export function computeDimensions(width, height, maxDimension = 720) {
  if (width <= maxDimension && height <= maxDimension) {
    return { width, height };
  }
  const ratio = Math.min(maxDimension / width, maxDimension / height);
  return {
    width: Math.max(1, Math.round(width * ratio)),
    height: Math.max(1, Math.round(height * ratio))
  };
}

/**
 * Compress an image file using browser Canvas.
 * Keeps resolution sharp (max dimension 720px), JPEG quality 0.85.
 * Returns { data, width, height, size } where data is base64 DataURL.
 */
export async function compressImage(file, maxDimension = 720, quality = 0.85) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
  } catch {
    // Fallback if createImageBitmap fails or option not supported
    bitmap = await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('无法解析图片'));
      img.src = URL.createObjectURL(file);
    });
  }

  const { width: srcWidth, height: srcHeight } = bitmap;
  const { width, height } = computeDimensions(srcWidth, srcHeight, maxDimension);

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;

  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(bitmap, 0, 0, width, height);

  if (typeof bitmap.close === 'function') {
    bitmap.close();
  }

  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob(b => b ? resolve(b) : reject(new Error('Canvas 导出失败')), 'image/jpeg', quality);
  });

  const reader = new FileReader();
  const dataUrl = await new Promise((resolve, reject) => {
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('转换图片编码失败'));
    reader.readAsDataURL(blob);
  });

  return {
    data: dataUrl,
    width,
    height,
    size: blob.size
  };
}
