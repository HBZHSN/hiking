import { test } from 'node:test';
import assert from 'node:assert/strict';
import { wgsToGcj, gcjToWgs, toWgs } from '../js/coordinates.js';
import { normalizeRoute, parseGeoJSON, analyze, distance, findMaxMetricPoint } from '../js/route.js';
import { tilePosition, decodeTerrarium } from '../js/elevation.js';

const near = (a,b,tolerance=1e-6) => assert.ok(Math.abs(a-b)<tolerance,`${a} is not near ${b}`);

test('Beijing coordinate known fixture and iterative inverse',() => {
  const original=[116.404,39.915];
  const gcj=wgsToGcj(original);
  near(gcj[0],116.41024449916938);near(gcj[1],39.91640428150164);
  const restored=gcjToWgs(gcj);restored.forEach((v,i) => near(v,original[i],1e-8));
  const bd=toWgs([116.41662724378733,39.922699552216216],'BD09');
  bd.forEach((v,i) => near(v,original[i],2e-6));
});
test('China regional conversions roundtrip and overseas coordinates unchanged',() => {
  for(const point of [[114.18,27.47],[121.47,31.23],[113.26,23.13],[87.62,43.82],[91.13,29.65]]) {
    const restored=gcjToWgs(wgsToGcj(point));restored.forEach((v,i) => near(v,point[i],1e-7));
  }
  assert.deepEqual(toWgs([-122.4,37.8],'GCJ02'),[-122.4,37.8]);
  assert.throws(() => toWgs([0,0],'AUTO'));
});
test('segment breaks do not add phantom distance, ascent or duration',() => {
  const route=normalizeRoute({segments:[[[0,0,10,0],[.01,0,30,60000]],[[100,30,1000,3600000],[100.01,30,900,3660000]]]});
  const stats=analyze(route);
  assert.ok(stats.distance>2000 && stats.distance<2100);
  assert.equal(stats.ascent,20);assert.equal(stats.descent,100);assert.equal(stats.duration,120000);
  assert.equal(stats.points[2].distance,stats.points[1].distance);
});
test('missing elevations stay unknown and do not create fake climb',() => {
  const stats=analyze(normalizeRoute({segments:[[[0,0,100],[.01,0],[.02,0,200],[.03,0,220]]]}));
  assert.equal(stats.ascent,20);assert.equal(stats.min,100);assert.equal(stats.known,3);
  assert.equal(stats.partialElevation,true);assert.equal(stats.points[2].slope,null);
  const empty=analyze(normalizeRoute({segments:[[[0,0],[.01,0]]]}));
  assert.equal(empty.ascent,null);assert.equal(empty.min,null);
});
test('elevation gain filters jitter, retains gradual climbs and resets at gaps',() => {
  const cases = [
    [[Array.from({length:100}, () => [100,101,99,102,100]).flat()],0,0],
    [[Array.from({length:83}, (_,i) => 100+i)],82,0],
    [[Array.from({length:83}, (_,i) => 182-i)],0,82],
    [[[100,105,110,115,120,119,120,115,110,105,100,110,120]],40,20],
    [[[100,109,null,500,503,502]],11,0],
    [[[100,109],[500,509]],18,0],
    [[[100,null,500]],null,null]
  ];
  for (const [segments,ascent,descent] of cases) {
    const route=normalizeRoute({segments:segments.map(s => s.map((ele,i) => [119+i/1000,30,ele]))});
    const original=structuredClone(route);
    const stats=analyze(route);
    assert.equal(stats.ascent,ascent);assert.equal(stats.descent,descent);
    assert.deepEqual(route,original);
    assert.deepEqual(stats.points.map(p => p.ele),segments.flat());
  }
});
test('distance smoothing removes a brief elevation spike without changing recorded heights',() => {
  const heights=[...Array(10).fill(100),140,...Array(10).fill(100)];
  const route=normalizeRoute({segments:[heights.map((ele,i) => [119+i/10000,30,ele])]});
  const stats=analyze(route);
  assert.equal(stats.ascent,0);assert.equal(stats.descent,0);assert.equal(stats.max,140);
  assert.deepEqual(stats.points.map(p => p.ele),heights);
});
test('reject malformed coordinates, projected data and oversized tracks',() => {
  for (const point of [[null,0],['114',27],[NaN,27],[114,91],[114,27,'800'],[114,27,Infinity]]) {
    assert.throws(() => normalizeRoute({segments:[[point,[114,27,100]]]}));
  }
  assert.throws(() => normalizeRoute({segments:[]}));
  assert.throws(() => normalizeRoute({segments:[[[114,27]]]}));
  assert.throws(() => normalizeRoute({segments:[Array.from({length:50001},() => [114,27])]}));
  assert.throws(() => parseGeoJSON(JSON.stringify({type:'LineString',crs:{name:'EPSG:3857'},coordinates:[[100,30],[101,31]]})));
});
test('GeoJSON collection and MultiLineString retain separate segments',() => {
  const parsed=parseGeoJSON(JSON.stringify({type:'FeatureCollection',features:[
    {type:'Feature',properties:{name:'中国路线'},geometry:{type:'MultiLineString',coordinates:[[[114,27,0],[114.01,27,10]],[[115,28],[115.01,28]]]}},
    {type:'Feature',geometry:{type:'Point',coordinates:[114,27]}}
  ]}));
  const route=normalizeRoute(parsed);assert.equal(route.name,'中国路线');assert.equal(route.segments.length,2);assert.equal(route.segments[0][0].ele,0);
});
test('distance handles identical points and dateline crossing',() => {
  assert.equal(distance({lng:0,lat:0},{lng:0,lat:0}),0);
  near(distance({lng:179.99,lat:0},{lng:-179.99,lat:0}),2223.9016,.01);
});
test('Terrarium decoding preserves negative elevation and tile positions',() => {
  assert.equal(decodeTerrarium(128,0,0),0);assert.equal(decodeTerrarium(127,255,128),-.5);
  assert.equal(decodeTerrarium(131,232,0),1000);
  assert.deepEqual(tilePosition(0,0,1),{x:1,y:1,px:0,py:0,zoom:1});
  assert.equal(tilePosition(180,0,1).x,0);
});

test('findMaxMetricPoint identifies maximum value and point location for all metrics', () => {
  const points = [
    { lng: 119.0, lat: 30.0, ele: 100, hr: 90, speed: 4.5, slope: 5.0, cad: 60 },
    { lng: 119.1, lat: 30.1, ele: 500, hr: 165, speed: 6.2, slope: 18.5, cad: 120 },
    { lng: 119.2, lat: 30.2, ele: 350, hr: 140, speed: 8.8, slope: -12.0, cad: 110 },
    { lng: 119.3, lat: 30.3, ele: 200, hr: null, speed: null, slope: null, cad: null }
  ];

  const maxEle = findMaxMetricPoint(points, 'ele');
  assert.equal(maxEle.value, 500);
  assert.equal(maxEle.point.lng, 119.1);
  assert.equal(maxEle.point.lat, 30.1);
  assert.equal(maxEle.index, 1);

  const maxHr = findMaxMetricPoint(points, 'hr');
  assert.equal(maxHr.value, 165);
  assert.equal(maxHr.index, 1);

  const maxSpeed = findMaxMetricPoint(points, 'speed');
  assert.equal(maxSpeed.value, 8.8);
  assert.equal(maxSpeed.point.lng, 119.2);
  assert.equal(maxSpeed.index, 2);

  const maxSlope = findMaxMetricPoint(points, 'slope');
  assert.equal(maxSlope.value, 18.5);
  assert.equal(maxSlope.index, 1);

  const maxCad = findMaxMetricPoint(points, 'cad');
  assert.equal(maxCad.value, 120);
  assert.equal(maxCad.index, 1);

  // Missing metric returns null
  assert.equal(findMaxMetricPoint(points, 'non_existent'), null);
  assert.equal(findMaxMetricPoint([], 'ele'), null);
  assert.equal(findMaxMetricPoint(null, 'ele'), null);

  // All negative numbers
  const negPoints = [
    { lng: 10, lat: 20, ele: -100 },
    { lng: 11, lat: 21, ele: -30 },
    { lng: 12, lat: 22, ele: -80 }
  ];
  const maxNeg = findMaxMetricPoint(negPoints, 'ele');
  assert.equal(maxNeg.value, -30);
  assert.equal(maxNeg.index, 1);
});
