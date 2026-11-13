import test from 'node:test';
import assert from 'node:assert/strict';
import { analyze } from '../js/route.js';
import { replayTimeline, replayFrame, replayPhotos, replayHeading, replayCameraBearing } from '../js/route.js';

const points = analyze({segments: [[
  {lng:119,lat:30,ele:100,time:0},
  {lng:119.001,lat:30,ele:120,time:10000},
  {lng:119.001,lat:30,ele:120,time:70000},
  {lng:119.002,lat:30,ele:140,time:90000}
]]}).points;

test('replay preserves speed changes and a full stationary minute', () => {
  const timeline = replayTimeline(points);
  assert.deepEqual(timeline, {times:[0,10000,70000,90000],duration:90000,estimated:false});
  const middle = replayFrame(points,timeline,5000);
  assert.ok(Math.abs(middle.lng - 119.0005) < 1e-10);
  assert.equal(middle.ele,110);
  assert.equal(middle.time,5000);
  assert.equal(middle.speed,points[1].speed);
  assert.equal(replayFrame(points,timeline,40000).lng,119.001);
  assert.equal(replayFrame(points,timeline,40000).speed,0);
  assert.equal(replayFrame(points,timeline,80000).speed,points[3].speed);
  assert.equal(replayFrame(points,timeline,-100).index,0);
  assert.equal(replayFrame(points,timeline,100000).index,3);
});

test('missing, duplicate, reversed times remain seekable and explicitly estimated', () => {
  const sample = points.map((p,i) => ({...p,time:[null,100,100,50][i]}));
  const timeline = replayTimeline(sample);
  assert.equal(timeline.estimated,true);
  assert.ok(timeline.times.every((v,i,a) => i === 0 || v > a[i-1]));
  assert.equal(replayFrame(sample,timeline,500).time,null);
});

test('segment gaps hold then jump without connecting distant points', () => {
  const sample = points.map((p,i) => ({...p,start:i===0 || i===2}));
  sample[2].lng=120;
  const timeline = replayTimeline(sample);
  assert.equal(replayFrame(sample,timeline,50000).lng,sample[1].lng);
  assert.equal(replayFrame(sample,timeline,50000).speed,null);
  assert.equal(replayFrame(sample,timeline,70000).lng,120);
});

test('photo ordering, duplicate positions, unmatched photos and backward seeking', () => {
  const timeline = replayTimeline(points);
  const photos = replayPhotos([{id:3,point_index:3},{id:2,point_index:1},{id:1,point_index:1},
    {id:4,point_index:null},{id:5,point_index:-1},{id:6,point_index:999}],timeline);
  assert.deepEqual(photos.map(p=>p.id),[1,2,3]);
  assert.equal(photos.findLast(p=>p.elapsed<=90000).id,3);
  assert.equal(photos.findLast(p=>p.elapsed<=10000).id,2);
  assert.equal(photos.findLast(p=>p.elapsed<=0),undefined);
});

test('dateline interpolation takes the short path', () => {
  const sample = [{...points[0],lng:179},{...points[1],lng:-179}];
  assert.equal(replayFrame(sample,replayTimeline(sample),5000).lng,180);
});

const trail = angle => analyze({segments: [Array.from({length: 30}, (_, i) => ({
  lng: i * .0001 * Math.sin(angle * Math.PI / 180),
  lat: i * .0001 * Math.cos(angle * Math.PI / 180)
}))]}).points;
const headingState = bearing => ({bearing, from: bearing, target: bearing, sampledAt: 0, startedAt: 0});

test('heading filters small zigzags using a longer stretch and respects segment gaps', () => {
  const zigzag = analyze({segments: [Array.from({length: 40}, (_, i) => ({
    lng: (i % 2) * .00004, lat: i * .0001
  }))]}).points;
  for (let i = 0; i < zigzag.length; i++) assert.ok(Math.abs(replayHeading(zigzag, i)) < 5);
  const split = analyze({segments: [[{lng:0,lat:0},{lng:0,lat:.001}],
    [{lng:1,lat:0},{lng:1.001,lat:0}]]}).points;
  assert.equal(replayHeading(split, 1), 0);
  assert.equal(replayHeading(split, 2), 90);
  assert.equal(replayHeading([{lng:0,lat:0,distance:0,start:true}], 0), null);
});

test('camera samples every four wall-clock seconds, eases for two, and ignores small turns', () => {
  const state = headingState(0), east = trail(90);
  assert.equal(replayCameraBearing(state, east, 0, 3999), 0);
  assert.equal(replayCameraBearing(state, east, 0, 4000), 0);
  assert.ok(Math.abs(replayCameraBearing(state, east, 0, 5000) - 45) < .01);
  assert.ok(Math.abs(replayCameraBearing(state, east, 0, 6000) - 90) < .01);
  assert.ok(Math.abs(replayCameraBearing(state, trail(100), 0, 8000) - 90) < .01);
  assert.ok(Math.abs(replayCameraBearing(state, trail(180), 0, 9000) - 90) < .01);
  replayCameraBearing(state, trail(180), 0, 12000);
  assert.ok(Math.abs(replayCameraBearing(state, trail(180), 0, 14000) - 180) < .01);
});

test('camera crosses north/south on the short arc instead of spinning around', () => {
  const state = headingState(170), southwest = trail(-150);
  replayCameraBearing(state, southwest, 0, 4000);
  assert.ok(Math.abs(replayCameraBearing(state, southwest, 0, 5000) - 190) < .01);
  assert.ok(Math.abs(replayCameraBearing(state, southwest, 0, 6000) - 210) < .01);
});
