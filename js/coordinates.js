// Common approximate GCJ transform; this is not a survey-grade conversion.
const PI = Math.PI;
const A = 6378245;
const EE = 0.006693421622965943;
const outside = (lng, lat) => lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271;

export function wgsToGcj([lng, lat]) {
  // ponytail: the conventional bounding box is not a national boundary polygon.
  // Only use this transform when the source is explicitly identified as GCJ-02.
  if (outside(lng, lat)) return [lng, lat];
  const x = lng - 105, y = lat - 35;
  let dLat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*Math.sqrt(Math.abs(x));
  let dLng = 300 + x + 2*y + .1*x*x + .1*x*y + .1*Math.sqrt(Math.abs(x));
  const common = (20*Math.sin(6*x*PI) + 20*Math.sin(2*x*PI))*2/3;
  dLat += common + (20*Math.sin(y*PI) + 40*Math.sin(y/3*PI))*2/3
    + (160*Math.sin(y/12*PI) + 320*Math.sin(y*PI/30))*2/3;
  dLng += common + (20*Math.sin(x*PI) + 40*Math.sin(x/3*PI))*2/3
    + (150*Math.sin(x/12*PI) + 300*Math.sin(x/30*PI))*2/3;
  const rad = lat/180*PI;
  const magic = 1 - EE*Math.sin(rad)**2;
  dLat = dLat*180 / ((A*(1-EE))/(magic*Math.sqrt(magic))*PI);
  dLng = dLng*180 / (A/Math.sqrt(magic)*Math.cos(rad)*PI);
  return [lng+dLng, lat+dLat];
}

export function gcjToWgs(point) {
  if (outside(...point)) return [...point];
  let guess = [...point];
  for (let i = 0; i < 10; i++) {
    const forward = wgsToGcj(guess);
    const dx = forward[0]-point[0], dy = forward[1]-point[1];
    guess = [guess[0]-dx, guess[1]-dy];
    if (Math.max(Math.abs(dx), Math.abs(dy)) < 1e-9) break;
  }
  return guess;
}

export function bdToGcj([lng, lat]) {
  const x = lng-.0065, y = lat-.006;
  const z = Math.sqrt(x*x+y*y) - .00002*Math.sin(y*PI*3000/180);
  const theta = Math.atan2(y,x) - .000003*Math.cos(x*PI*3000/180);
  return [z*Math.cos(theta), z*Math.sin(theta)];
}

export function toWgs(point, system = 'WGS84') {
  switch (system) {
    case 'WGS84': return [...point];
    case 'GCJ02': return gcjToWgs(point);
    case 'BD09': return gcjToWgs(bdToGcj(point));
    default: throw new Error('不支持的坐标系，请使用 WGS84 / GCJ-02 / BD-09 经纬度。');
  }
}
