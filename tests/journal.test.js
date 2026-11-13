import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeRoute,analyze,coloredGeoJSON,exportGPX} from '../js/route.js';

test('record metrics preserve sensor values and never bridge separate segments',()=>{
  const route=normalizeRoute({name:'A & <B>',segments:[[[119,30,100,0,100,30],[119.001,30,120,60000,140,40]],[[120,31,null,900000,120,null],[120.001,31,null,960000,null,20]]]});
  const stats=analyze(route);
  assert.equal(stats.hrMean,120);assert.equal(stats.hrMax,140);assert.equal(stats.cadMean,30);
  assert.equal(stats.duration,120000);assert.equal(stats.moving,120000);assert.equal(stats.ascent,20);
  assert.equal(stats.points[2].speed,null);assert.equal(stats.points[2].slope,null);
  assert.equal(stats.points[0].hr,100);
  const geo=coloredGeoJSON(stats,'hr');assert.equal(geo.features.length,2);assert.equal(geo.features[1].properties.value,null);
  const xml=exportGPX(route);assert.match(xml,/<name>A &amp; &lt;B&gt;<\/name>/);assert.match(xml,/<gpxtpx:hr>140/);
  assert.equal((xml.match(/<trkseg>/g)||[]).length,2);
});
test('missing health and time data remain missing',()=>{
  const stats=analyze(normalizeRoute({segments:[[[119,30],[119.01,30]]]}));
  assert.equal(stats.hrMean,null);assert.equal(stats.cadMean,null);assert.equal(stats.duration,null);assert.equal(stats.speed,null);
});
