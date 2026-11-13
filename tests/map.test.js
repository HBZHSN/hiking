import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createContext,runInContext} from 'node:vm';

const app = readFileSync(new URL('../js/app.js',import.meta.url),'utf8');
const code = app.slice(app.indexOf("$('map-size').addEventListener"),app.indexOf('function fitRoute()'));

test('compact toggle preserves the map and explored point without a dialog',()=>{
  const nodes = new Map(), calls=[];
  const $ = id => {
    if (!nodes.has(id)) {
      const classes=new Set();
      nodes.set(id,{attributes:{},events:{},value:'2',
        classList:{toggle(name){if(classes.delete(name))return false;classes.add(name);return true;}},
        setAttribute(key,value){this.attributes[key]=String(value);},
        addEventListener(key,handler){this.events[key]=handler;}});
    }
    return nodes.get(id);
  };
  const context=createContext({$,mapReady:true,map:{resize:()=>calls.push('resize')},
    renderProfile:()=>calls.push('profile'),showPoint:index=>calls.push(index)});
  runInContext(code,context);
  for (const compact of [true,false,true,false]) {
    calls.length=0;
    $('map-size').events.click();
    assert.equal($('map-size').textContent,compact?'展开地图':'缩小地图');
    assert.equal($('map-size').attributes['aria-expanded'],String(!compact));
    assert.deepEqual(calls,['profile',2,'resize']);
  }
  assert.doesNotMatch(readFileSync(new URL('../index.html',import.meta.url),'utf8'),/id="map-dialog"|id="map-expand"/);
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
