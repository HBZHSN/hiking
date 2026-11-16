import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createContext,runInContext} from 'node:vm';

const app = readFileSync(new URL('../js/app.js',import.meta.url),'utf8');
const profileCode = app.slice(app.indexOf('const profileMetrics ='),app.indexOf('function notify('))
  + app.slice(app.indexOf('function renderProfile()'),app.indexOf('function cancelElevation()'));

function profile(points, saved) {
  const nodes = new Map(), storage = new Map(saved === undefined ? [] : [['profile-metrics',saved]]);
  function element() {
    return {children:[],attributes:{},events:{},classList:{contains:()=>false},style:{setProperty(){}},clientWidth:600,value:'1',
      append(...children){this.children.push(...children);},
      replaceChildren(...children){this.children=children;},
      setAttribute(key,value){this.attributes[key]=String(value);},
      addEventListener(key,handler){this.events[key]=handler;},
      getBoundingClientRect(){return {top:150,left:0,width:600};}};
  }
  const $ = id => {if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  const context = createContext({$,stats:{points,distance:1000},mapReady:false,
    window:{scrollY:0,innerHeight:800},
    document:{createElement:element,createElementNS:element,querySelector:()=> $('map-pane'),querySelectorAll:()=>[]},
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value)}});
  runInContext(profileCode,context);
  runInContext('renderProfile();showPoint(1);',context);
  return {$,storage,context,svg:()=> $('profiles').children[0].children[0]};
}

const points = [
  {distance:0,lng:119,lat:30,hr:100,ele:200,speed:0,cad:50,slope:-10,start:true},
  {distance:500,lng:119.01,lat:30,hr:150,ele:250,speed:4,cad:60,slope:5},
  {distance:1000,lng:119.02,lat:30,hr:120,ele:220,speed:2,cad:55,slope:-5}
];

test('one profile keeps real-unit curves without min/max text after checkboxes',()=>{
  const p = profile(points);
  assert.equal(p.$('profiles').children.length,1);
  assert.equal(p.svg().attributes['aria-label'],'心率、海拔、速度沿途曲线');
  assert.equal((p.svg().innerHTML.match(/<path /g)||[]).length,3);
  for (const label of p.$('profile-metrics').children) {
    assert.equal(label.children.length,2);
    assert.equal(label.children[0].type,'checkbox');
    assert.equal(label.children[1].className,'profile-metric-name');
  }
  assert.match(p.svg().innerHTML,/data-metric="hr" d="M20.00,139.58 L300.00,35.42 L580.00,97.92"/);
  assert.match(p.svg().innerHTML,/data-metric="ele" d="M20.00,139.58 L300.00,35.42 L580.00,97.92"/);
  assert.match(p.svg().innerHTML,/class="profile-area" points="20\.00,150 20\.00,139\.58 300\.00,35\.42 580\.00,97\.92 580\.00,150"/);
  assert.match(p.$('point-info').textContent,/心率 150 bpm · 海拔 250 m · 速度 4 km\/h/);
  assert.doesNotMatch(p.$('point-info').textContent,/步频|坡度/);
});

test('parameter changes persist and preserve the explored point; all parameters can be hidden',()=>{
  const p = profile(points);
  p.$('profile-metrics').events.change({target:{type:'checkbox',value:'cad',checked:true}});
  assert.match(p.svg().innerHTML,/data-metric="cad"/);
  assert.match(p.$('point-info').textContent,/步频 60 步\/分/);
  assert.ok(JSON.parse(p.storage.get('profile-metrics')).includes('cad'));
  const restored = profile(points,p.storage.get('profile-metrics'));
  assert.match(restored.svg().innerHTML,/data-metric="cad"/);
  for(const key of ['hr','ele','speed','cad'])p.$('profile-metrics').events.change({target:{type:'checkbox',value:key,checked:false}});
  assert.doesNotMatch(p.svg().innerHTML,/<path /);
  assert.match(p.svg().innerHTML,/勾选上方参数/);
  assert.equal(p.$('point-info').textContent,'里程 0.50 km');
  assert.equal(p.storage.get('profile-metrics'),'[]');
});

test('redrawing profile preserves the explored point without mutating full-screen workspace height',()=>{
  const p = profile(points);
  p.$('profile-card').classList.contains=()=>true;
  runInContext('renderProfile();showPoint(Number($("route-position").value));',p.context);
  assert.equal(p.$('workspace').style?.height, undefined);
  assert.match(p.$('point-info').textContent,/里程 0.50 km/);
  p.$('profile-card').classList.contains=()=>false;
  runInContext('renderProfile();',p.context);
  assert.equal(p.$('workspace').style?.height, undefined);
});

test('missing values and segment starts break lines; zero, negatives and flat series stay finite',()=>{
  const p = profile(points.map((point,i)=>({...point,hr:i===1?null:100,ele:0,start:i===2})),JSON.stringify(['hr','ele','slope']));
  assert.equal((p.svg().innerHTML.match(/data-metric="hr"/g)||[]).length,2);
  assert.equal((p.svg().innerHTML.match(/data-metric="ele"/g)||[]).length,2);
  assert.match(p.svg().innerHTML,/data-metric="ele" d="M20.00,87.50 L300.00,87.50"/);
  assert.match(p.svg().innerHTML,/data-metric="slope" d="M20.00,125.00 L300.00,50.00"/);
  assert.match(p.$('point-info').textContent,/心率 — bpm/);
  assert.doesNotMatch(p.svg().innerHTML,/NaN|Infinity/);
  const missing = profile(points.map(point=>({...point,hr:null,speed:null})));
  assert.match(missing.svg().innerHTML,/心率、速度无数据/);
});

test('invalid or unavailable saved preferences fall back to the defaults',()=>{
  for(const saved of ['invalid','null','{}','["unknown"]']) {
    assert.equal(profile(points,saved).svg().attributes['aria-label'],'心率、海拔、速度沿途曲线');
  }
});

test('opening record defaults to hr, ele and speed, opening route defaults to ele only',()=>{
  const p = profile(points);
  const inputs = p.$('profile-metrics').children.map(label => label.children[0]);

  // 打开路线：默认只勾选海拔
  runInContext("applyProfileMetrics(defaultProfileMetrics('route')); renderProfile(); showPoint(1);", p.context);
  assert.equal(p.svg().attributes['aria-label'], '海拔沿途曲线');
  assert.equal((p.svg().innerHTML.match(/<path /g)||[]).length, 1);
  assert.match(p.svg().innerHTML, /data-metric="ele"/);
  assert.doesNotMatch(p.svg().innerHTML, /data-metric="hr"/);
  assert.doesNotMatch(p.svg().innerHTML, /data-metric="speed"/);
  assert.match(p.$('point-info').textContent, /海拔 250 m/);
  assert.doesNotMatch(p.$('point-info').textContent, /心率|速度/);
  assert.deepEqual(inputs.filter(i => i.checked).map(i => i.value), ['ele']);
  assert.deepEqual(JSON.parse(p.storage.get('profile-metrics')), ['ele']);

  // 打开记录：默认勾选心率、海拔、速度
  runInContext("applyProfileMetrics(defaultProfileMetrics('record')); renderProfile(); showPoint(1);", p.context);
  assert.equal(p.svg().attributes['aria-label'], '心率、海拔、速度沿途曲线');
  assert.equal((p.svg().innerHTML.match(/<path /g)||[]).length, 3);
  assert.match(p.svg().innerHTML, /data-metric="hr"/);
  assert.match(p.svg().innerHTML, /data-metric="ele"/);
  assert.match(p.svg().innerHTML, /data-metric="speed"/);
  assert.match(p.$('point-info').textContent, /心率 150 bpm · 海拔 250 m · 速度 4 km\/h/);
  assert.deepEqual(inputs.filter(i => i.checked).map(i => i.value), ['hr', 'ele', 'speed']);
  assert.deepEqual(JSON.parse(p.storage.get('profile-metrics')), ['hr', 'ele', 'speed']);
});
