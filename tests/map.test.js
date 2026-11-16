import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createContext,runInContext} from 'node:vm';

const app = readFileSync(new URL('../js/app.js',import.meta.url),'utf8');
const code = app.slice(app.indexOf('function updateHudMetrics()'),app.indexOf('function detailCameraPadding('));

test('floating detail and profile toggles preserve explored point and refit map',()=>{
  const nodes = new Map(), bodyClasses = new Set(), calls=[];
  const $ = id => {
    if (!nodes.has(id)) {
      const classes=new Set();
      nodes.set(id,{attributes:{},events:{},value:'2',
        classList:{
          toggle(name,force){const has=classes.has(name);const next=force===undefined?!has:Boolean(force);if(next)classes.add(name);else classes.delete(name);return next;},
          contains(name){return classes.has(name);}
        },
        setAttribute(key,value){this.attributes[key]=String(value);},
        addEventListener(key,handler){this.events[key]=handler;}});
    }
    return nodes.get(id);
  };
  const document = {
    body: { classList: {
      toggle(name,force){const has=bodyClasses.has(name);const next=force===undefined?!has:Boolean(force);if(next)bodyClasses.add(name);else bodyClasses.delete(name);return next;},
      contains(name){return bodyClasses.has(name);}
    }},
    documentElement: { style: { setProperty(){} } },
    querySelector: () => null
  };
  const context=createContext({$,document,window:{innerWidth:1440},
    renderProfile:()=>calls.push('profile'),showPoint:index=>calls.push(index),fitRoute:()=>calls.push('fit')});
  runInContext(code,context);

  // Detail drawer toggle
  $('detail-toggle').events.click();
  assert.equal(bodyClasses.has('detail-collapsed'), true);
  assert.equal($('detail-toggle').textContent, '展开详情');
  assert.equal($('detail-toggle').attributes['aria-expanded'], 'false');
  assert.deepEqual(calls, ['profile', 2, 'fit']);

  calls.length = 0;
  $('detail-toggle').events.click();
  assert.equal(bodyClasses.has('detail-collapsed'), false);
  assert.equal($('detail-toggle').textContent, '收起');
  assert.equal($('detail-toggle').attributes['aria-expanded'], 'true');
  assert.deepEqual(calls, ['profile', 2, 'fit']);

  // Profile visibility and expand toggles
  calls.length = 0;
  $('profile-toggle').events.click();
  assert.equal(bodyClasses.has('profile-collapsed'), true);
  assert.equal($('profile-toggle').attributes['aria-expanded'], 'false');
  assert.deepEqual(calls, ['profile', 2, 'fit']);

  calls.length = 0;
  $('profile-expand').events.click();
  assert.equal($('profile-card').classList.contains('expanded'), true);
  assert.equal($('profile-expand').textContent, '精简曲线');
  assert.deepEqual(calls, ['profile', 2, 'fit']);

  assert.doesNotMatch(readFileSync(new URL('../index.html',import.meta.url),'utf8'),/id="map-dialog"|id="map-expand"|id="map-size"/);
});

test('turning off 3D terrain clears false terrain error alert',()=>{
  const nodes = new Map(), calls = [];
  const statusEl = { textContent: '地形加载失败，可关闭 3D 地形。' };
  nodes.set('map-status', statusEl);
  const $ = id => {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, { attributes: {}, classList: { toggle(name) { if (classes.delete(name)) return false; classes.add(name); return true; } }, setAttribute(k, v) { this.attributes[k] = String(v); } });
    }
    return nodes.get(id);
  };
  const status = (msg) => { statusEl.textContent = msg; calls.push(msg); };
  const updateMapModeCode = app.slice(app.indexOf('function updateMapMode()'), app.indexOf("$('fit-route').addEventListener"));
  const context = createContext({
    $, status, terrain: false, mode: 'satellite', mapReady: false,
    updateOverlays: () => {}, fitRoute: () => {}
  });
  runInContext(updateMapModeCode + 'updateMapMode();', context);
  assert.equal(statusEl.textContent, '');
  assert.deepEqual(calls, ['']);
});

test('3D terrain error is suppressed when dem tiles load or when map becomes idle',()=>{
  const handlers = {};
  const statusEl = { textContent: '' };
  const $ = id => id === 'map-status' ? statusEl : { textContent: '' };
  const status = (msg) => { statusEl.textContent = msg; };
  const map = {
    on(event, handler) { handlers[event] = handler; },
    once() {},
    isSourceLoaded: (src) => src === 'dem',
    areTilesLoaded: () => true
  };
  const mapEventsCode = app.slice(app.indexOf('let demLoaded = false'), app.indexOf("map.on('load', () =>"));
  const context = createContext({
    $, status, map, terrain: true, setTimeout: fn => fn(), clearTimeout: () => {}
  });
  runInContext(mapEventsCode, context);

  // 1. 模拟 DEM 瓦片成功加载
  handlers.sourcedata({ sourceId: 'dem', tile: { state: 'loaded' } });

  // 2. 模拟偶发单个 DEM 瓦片报错（由于 demLoaded 已为 true，不应报警）
  handlers.error({ sourceId: 'dem', error: new Error('502 Bad Gateway') });
  assert.equal(statusEl.textContent, '');

  // 3. 模拟中止请求，直接忽略
  handlers.error({ sourceId: 'dem', error: { name: 'AbortError' } });
  assert.equal(statusEl.textContent, '');

  // 4. 假设之前存在错误文本，idle 触发时清空
  statusEl.textContent = '地形加载失败，可关闭 3D 地形。';
  handlers.idle();
  assert.equal(statusEl.textContent, '');
});
