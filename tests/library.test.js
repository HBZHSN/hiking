import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {difficulty,difficultyLevel,routeDays,filterLibrary,routeView,routeCamera,parseLocation} from '../js/route.js';
test('returning to the library focuses a tab on touch devices and search on desktop',()=>{
  const app=readFileSync(new URL('../js/app.js',import.meta.url),'utf8');
  const handler=app.slice(app.indexOf('function showLibrary()'),app.indexOf('function selectionUrl('))+'showLibrary();';
  for(const touch of [true,false])for(const tab of ['route','record']) {
    const calls=[];
    runInNewContext(handler,{
      $:id=>({addEventListener:(event,callback)=>callback(),focus:()=>calls.push(id)}),
      clearSelection:()=>calls.push('clear'),renderLibrary:()=>calls.push('render'),
      restoreLibraryScroll:()=>{},tab,matchMedia:query=>{assert.equal(query,'(max-width: 600px), (pointer: coarse)');return {matches:touch};}
    });
    assert.deepEqual(calls,['clear','render',touch?(tab==='route'?'routes-tab':'records-tab'):'search']);
  }
});
test('returning to the library restores previous scroll position without scrolling on focus',()=>{
  const app=readFileSync(new URL('../js/app.js',import.meta.url),'utf8');
  const handler=app.slice(app.indexOf('function showLibrary()'),app.indexOf('function selectionUrl('))+'showLibrary();';

  const focusOpts=[];
  const scrollCalls=[];
  runInNewContext(handler,{
    $:id=>({addEventListener:(event,callback)=>callback(),focus:opts=>focusOpts.push({id,opts})}),
    clearSelection:()=>{},renderLibrary:()=>{},
    tab:'route',matchMedia:()=>({matches:false}),
    restoreLibraryScroll:()=>scrollCalls.push('restored')
  });
  assert.equal(focusOpts[0]?.id, 'search');
  assert.equal(focusOpts[0]?.opts?.preventScroll, true);
  assert.deepEqual(scrollCalls, ['restored']);

  // Verify app.js records scroll on selectItem and restores on return
  assert.match(app,/if\s*\(!\$\('library-view'\)\.hidden[^)]*\)\s*\{[\s\S]*libraryScrollY\s*=\s*window\.scrollY/);
  assert.match(app,/function restoreLibraryScroll\(\)\s*\{[\s\S]*window\.scrollTo\(libraryScrollX,\s*libraryScrollY\)/);
});
test('difficulty follows miles, feet, natural log and floor at daily ascent boundaries',()=>{
  const expectedBase = Math.sqrt((10 * 0.621371 / 1) * (1000 * 3.28084 / 1) * 2) * (1 + 0) * (1 + 2 * 0.15);
  assert.ok(Math.abs(difficulty(10000, 1000, 1) - expectedBase) < 1e-10);
  for(const ascent of [499,500,999,1000,1500])for(const days of [1,2,3]) {
    const expected = Math.sqrt(((18000 / 1000) * 0.621371 / days) * (ascent * 3.28084 / days) * 2)
      * (1 + Math.log(days) * 0.2) * (1 + Math.floor((ascent / days) / 500) * 0.15);
    assert.equal(difficulty(18000, ascent, days), expected);
  }
  assert.equal(difficulty(1000,null,1),null);assert.equal(difficulty(1000,10,0),null);assert.equal(difficulty(1000,0,1),0);
});
test('difficulty levels distinguish missing and zero scores and include every upper boundary',()=>{
  for(const [score,level] of [[null,'难度暂缺'],[0,'⚫ 无效路线'],[0.01,'⚪ 极简'],[100,'⚪ 极简'],
    [100.01,'🟢 简单'],[200,'🟢 简单'],[200.01,'🟡 一般'],[300,'🟡 一般'],
    [300.01,'🟠 困难'],[400,'🟠 困难'],[400.01,'🔴 极难'],[1000,'🔴 极难'],[1200,'🔴 极难']]) {
    assert.equal(difficultyLevel(score),level,`score ${score}`);
  }
});
test('difficulty display progress bar uses score / 500', async () => {
  const { readFile } = await import('node:fs/promises');
  const app = await readFile(new URL('../js/app.js', import.meta.url), 'utf8');

  assert.match(app, /progress\.max=500;/);
  assert.match(app, /难度分数 \$\{format\(score,'',1\)\} \/ 500/);
  assert.match(app, /进度为分数 \/ 500/);
  assert.doesNotMatch(app, /progress\.max=1000;/);
  assert.doesNotMatch(app, /难度分数 \$\{format\(score,'',1\)\} \/ 1000/);
});
test('days include overnight gaps, manual overrides and missing timestamps',()=>{
  const route={segments:[[{time:0},{time:3600000}],[{time:172800000},{time:176400000}]]};
  assert.equal(routeDays(route).days,3);assert.equal(routeDays({...route,days:2}).days,2);
  assert.equal(routeDays({segments:[[{time:null},{time:null}]]}).days,1);
});
test('card days omit the estimate label and retain the default indicator',()=>{
  const app=readFileSync(new URL('../js/app.js',import.meta.url),'utf8');
  const assignment=app.split('\n').find(line=>line.trim().startsWith('meta.textContent='));
  for(const daysSource of ['manual','time','default']) {
    const meta={};
    runInNewContext(assignment,{meta,s:{date:null,days:3,daysSource},item:{kind:'route'},related:[],items:[]});
    assert.equal(meta.textContent,`日期未知 · 3 天${daysSource==='default'?'（默认）':''} · 0 次记录`);
  }
});
test('record card metadata omits parent routes and independent-record labels',()=>{
  const app=readFileSync(new URL('../js/app.js',import.meta.url),'utf8');
  const assignment=app.split('\n').find(line=>line.trim().startsWith('meta.textContent='));
  for(const route_id of [null,1]) {
    const meta={};
    runInNewContext(assignment,{meta,s:{date:null,days:2,daysSource:'time'},item:{kind:'record',route_id},related:[],items:[{id:1,name:'父路线'}]});
    assert.equal(meta.textContent,'日期未知 · 2 天');
  }
});
test('library searches notes and sorts missing values last',()=>{
  const items=[{id:1,name:'甲',notes:'山',location:'浙江省杭州市临安区',kind:'route',summary:{distance:5000,difficulty:10}},
    {id:2,name:'乙',notes:'',location:'安徽省宣城市宁国市',kind:'route',summary:{distance:2000,difficulty:null}},
    {id:3,name:'丙',notes:'山',location:'',kind:'record',summary:{distance:6000,difficulty:20}}];
  assert.deepEqual(filterLibrary(items,{kind:'route',query:'山'}).map(i=>i.id),[1]);
  assert.deepEqual(filterLibrary(items,{kind:'route',query:'杭州'}).map(i=>i.id),[1]);
  assert.deepEqual(filterLibrary(items,{kind:'route',query:'宁国'}).map(i=>i.id),[2]);
  assert.deepEqual(filterLibrary(items,{kind:'route',sort:'difficulty',ascending:true}).map(i=>i.id),[1,2]);
});
test('route and record displays start location beside or below route name', async () => {
  const { readFile } = await import('node:fs/promises');
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const app = await readFile(new URL('../js/app.js', import.meta.url), 'utf8');
  const css = await readFile(new URL('../style.css', import.meta.url), 'utf8');

  // Verify header-detail and overview contain route-location elements
  assert.match(html, /id="route-location"/);
  assert.match(html, /id="overview-location"/);

  // Cards, table and detail show the address without a prefix.
  assert.equal((app.match(/loc\.textContent=item\.location/g)||[]).length,2);
  assert.match(app, /const locText = route\.location \|\| ''/);
  assert.match(app, /\$\('route-location'\)\.textContent=res\.location/);
  assert.match(app, /\$\('overview-location'\)\.textContent=res\.location/);

  // Verify flex wrapping allows location to sit beside or below route name
  assert.match(css, /\.card-title-group\{[^}]*display:flex[^}]*flex-wrap:wrap/);
  assert.match(css, /\.route-location\{/);
});
test('record default sorting sorts by activity date descending with missing date last',()=>{
  const records=[
    {id:1,name:'A',kind:'record',summary:{date:1000}},
    {id:2,name:'B',kind:'record',summary:{date:3000}},
    {id:3,name:'C',kind:'record',summary:{date:null}},
    {id:4,name:'D',kind:'record',summary:{date:3000}},
  ];
  assert.deepEqual(filterLibrary(records,{kind:'record'}).map(r=>r.id),[4,2,1,3]);
  assert.deepEqual(filterLibrary(records,{kind:'record',sort:'default'}).map(r=>r.id),[4,2,1,3]);
  assert.deepEqual(filterLibrary(records,{kind:'record',ascending:true}).map(r=>r.id),[1,4,2,3]);
});
test('route default sorting prioritizes unhiked, then most hiked, then difficulty descending',()=>{
  const items=[
    {id:1,name:'R1',kind:'route',summary:{difficulty:50}},
    {id:2,name:'R2',kind:'route',summary:{difficulty:90}},
    {id:3,name:'R3',kind:'route',summary:{difficulty:null}},
    {id:4,name:'R4',kind:'route',summary:{difficulty:30}},
    {id:5,name:'R5',kind:'route',summary:{difficulty:70}},
    {id:6,name:'R6',kind:'route',summary:{difficulty:60}},
    {id:7,name:'R7',kind:'route',summary:{difficulty:50}},
    {id:101,kind:'record',route_id:4,summary:{}},
    {id:102,kind:'record',route_id:4,summary:{}},
    {id:103,kind:'record',route_id:5,summary:{}},
    {id:104,kind:'record',route_id:5,summary:{}},
    {id:105,kind:'record',route_id:6,summary:{}},
  ];

  // Unhiked routes (0 records): R2 (90), R7 (50, id 7), R1 (50, id 1), R3 (null)
  // Most hiked routes (2 records): R5 (70), R4 (30)
  // Less hiked routes (1 record): R6 (60)
  assert.deepEqual(filterLibrary(items,{kind:'route'}).map(r=>r.id),[2,7,1,3,5,4,6]);
  assert.deepEqual(filterLibrary(items,{kind:'route',sort:'default'}).map(r=>r.id),[2,7,1,3,5,4,6]);

  const standaloneRoutes=[
    {id:10,name:'SA',kind:'route',hikeCount:0,summary:{difficulty:20}},
    {id:20,name:'SB',kind:'route',hikeCount:3,summary:{difficulty:50}},
    {id:30,name:'SC',kind:'route',hikeCount:0,summary:{difficulty:40}},
  ];
  assert.deepEqual(filterLibrary(standaloneRoutes,{kind:'route'}).map(r=>r.id),[30,10,20]);
});
test('parseLocation parses provinces, municipalities, cities, and districts correctly',()=>{
  assert.deepEqual(parseLocation('浙江省杭州市临安区'),{province:'浙江省',city:'杭州市',district:'临安区'});
  assert.deepEqual(parseLocation('安徽省宣城市宁国市'),{province:'安徽省',city:'宣城市',district:'宁国市'});
  assert.deepEqual(parseLocation('北京市怀柔区'),{province:'北京市',city:'北京市',district:'怀柔区'});
  assert.deepEqual(parseLocation('上海市浦东新区'),{province:'上海市',city:'上海市',district:'浦东新区'});
  assert.deepEqual(parseLocation('内蒙古自治区呼伦贝尔市海拉尔区'),{province:'内蒙古自治区',city:'呼伦贝尔市',district:'海拉尔区'});
  assert.deepEqual(parseLocation('四川省阿坝藏族羌族自治州理县'),{province:'四川省',city:'阿坝藏族羌族自治州',district:'理县'});
  assert.deepEqual(parseLocation('虚构省虚构市虚构区'),{province:'虚构省',city:'虚构市',district:'虚构区'});
  assert.deepEqual(parseLocation(''),{province:'',city:'',district:''});
  assert.deepEqual(parseLocation(null),{province:'',city:'',district:''});
});
test('filterLibrary quick filters by hike status, region, difficulty, and days',()=>{
  const items=[
    {id:1,name:'浙东大峡谷',kind:'route',location:'浙江省宁波市宁海县',hikeCount:0,summary:{difficulty:180,days:1}},
    {id:2,name:'临安大明山',kind:'route',location:'浙江省杭州市临安区',hikeCount:2,summary:{difficulty:280,days:1}},
    {id:3,name:'黄山莲花峰',kind:'route',location:'安徽省黄山市徽州区',hikeCount:0,summary:{difficulty:350,days:2}},
    {id:4,name:'箭扣长城',kind:'route',location:'北京市怀柔区',hikeCount:1,summary:{difficulty:320,days:1}},
  ];

  // Unhiked routes filter
  assert.deepEqual(filterLibrary(items,{kind:'route',hikeStatus:'unhiked'}).map(i=>i.id),[3,1]);
  // Hiked routes filter: 2 (hikeCount 2) before 4 (hikeCount 1)
  assert.deepEqual(filterLibrary(items,{kind:'route',hikeStatus:'hiked'}).map(i=>i.id),[2,4]);

  // Province filter
  assert.deepEqual(filterLibrary(items,{kind:'route',province:'浙江省'}).map(i=>i.id),[1,2]);
  assert.deepEqual(filterLibrary(items,{kind:'route',province:'北京市'}).map(i=>i.id),[4]);

  // City filter
  assert.deepEqual(filterLibrary(items,{kind:'route',city:'杭州市'}).map(i=>i.id),[2]);

  // District filter
  assert.deepEqual(filterLibrary(items,{kind:'route',district:'临安区'}).map(i=>i.id),[2]);

  // Combined: unhiked AND Zhejiang
  assert.deepEqual(filterLibrary(items,{kind:'route',hikeStatus:'unhiked',province:'浙江省'}).map(i=>i.id),[1]);

  // Difficulty filter
  assert.deepEqual(filterLibrary(items,{kind:'route',difficulty:'easy'}).map(i=>i.id),[1]);
  assert.deepEqual(filterLibrary(items,{kind:'route',difficulty:'moderate'}).map(i=>i.id),[2]);
  assert.deepEqual(filterLibrary(items,{kind:'route',difficulty:'hard'}).map(i=>i.id),[3,4]);

  // Days filter: 1 (unhiked), then 2 (hikeCount 2), then 4 (hikeCount 1)
  assert.deepEqual(filterLibrary(items,{kind:'route',days:'single'}).map(i=>i.id),[1,2,4]);
  assert.deepEqual(filterLibrary(items,{kind:'route',days:'multi'}).map(i=>i.id),[3]);
});
test('camera unwraps dateline and adapts orientation to viewport',()=>{
  const p=[{lng:179.9,lat:30},{lng:-179.9,lat:30.1}];
  const wide=routeView(p,1000,600,true),tall=routeView(p,360,600,false);
  assert.ok(wide.bounds[1][0]-wide.bounds[0][0]<1);assert.equal(wide.pitch,35);assert.equal(tall.pitch,0);
  assert.ok(Math.abs(Math.abs(wide.bearing-tall.bearing)-90)<.001);
});
test('routeCamera calculates optimal scale fitting viewport snugly without defaulting to zoom 10',()=>{
  const routePoints=[
    {lng:-138,lat:26},
    {lng:-137.985,lat:26.027}
  ];
  const cam2D=routeCamera(routePoints,1000,600,false);
  // Optimal zoom for a ~3km hike on 1000x600 viewport should be around 14~16, not 10
  assert.ok(cam2D.zoom >= 14 && cam2D.zoom <= 16, `zoom ${cam2D.zoom} should be between 14 and 16`);
  assert.ok(cam2D.center[0] > -138 && cam2D.center[0] < -137.98);
  assert.ok(cam2D.center[1] > 26 && cam2D.center[1] < 26.03);
  assert.equal(cam2D.bearing, 0, 'strictly north-up');

  // Single point and empty fallback
  const singleCam=routeCamera([{lng:120,lat:30}],1000,600,false);
  assert.equal(singleCam.zoom, 15);
  assert.equal(singleCam.bearing, 0);
  const emptyCam=routeCamera([],1000,600,false);
  assert.deepEqual(emptyCam.center, [104,35]);
  assert.equal(emptyCam.zoom, 3);
  assert.equal(emptyCam.bearing, 0);
});

test('page includes every literal element referenced by app initialization',async()=>{
  const {readFile}=await import('node:fs/promises');
  const html=await readFile(new URL('../index.html',import.meta.url),'utf8');
  const app=await readFile(new URL('../js/app.js',import.meta.url),'utf8');
  const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
  assert.equal(new Set(ids).size,ids.length,'duplicate DOM IDs');
  for(const match of app.matchAll(/\$\('([^']+)'\)/g))assert.ok(ids.includes(match[1]),`Missing #${match[1]}`);
  assert.match(html, /id="workspace" hidden/);
  for(const id of ['cards-view','table-view']) assert.ok(ids.includes(id));
  assert.doesNotMatch(html, /id="(?:min|max)-(?:distance|ascent|difficulty)"/);
  assert.doesNotMatch(html, /id="profile-metric"|id="color-metric"/);
});

test('library exposes search, explicit sorting, tabs and both views',()=>{
  const html=readFileSync(new URL('../index.html',import.meta.url),'utf8');
  for(const id of ['routes-tab','records-tab','search','sort','sort-direction','cards-view','table-view','reset-filters','result-count',
    'quick-filters','filter-status-all','filter-status-unhiked','filter-status-hiked','filter-province','filter-city','filter-district','filter-difficulty','filter-days-all','filter-days-single','filter-days-multi'])assert.ok(html.includes(`id="${id}"`));
  assert.match(html,/搜索名称、地区或备注/);
  assert.match(html,/名称 · A → Z/);
  assert.match(html,/未爬过/);
  assert.match(html,/全部省份/);
});

test('map interface conforms to user requirements: removed exaggeration, help, scale, color-legend and added photos toggle', async () => {
  const { readFile } = await import('node:fs/promises');
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const app = await readFile(new URL('../js/app.js', import.meta.url), 'utf8');

  // Removed exaggeration slider and help
  assert.doesNotMatch(html, /id="exaggeration"/);
  assert.doesNotMatch(html, /id="exaggeration-value"/);
  assert.doesNotMatch(html, /拖动平移 · 滚轮缩放/);
  assert.doesNotMatch(app, /\$\('exaggeration'\)/);

  // Removed scale control
  assert.doesNotMatch(app, /ScaleControl/);

  // Removed color legend text
  assert.doesNotMatch(html, /id="color-legend"/);
  assert.doesNotMatch(app, /\$\('color-legend'\)/);
  assert.doesNotMatch(app, /灰色为缺失/);

  // Added photos toggle button
  assert.match(html, /id="photos-toggle"/);
  assert.match(html, /id="photos-toggle"[^>]*aria-pressed="true"[^>]*>沿途照片/);
  assert.match(app, /\$\('photos-toggle'\)/);
});

test('detail provides overview, map controls and optional editing without duplicate IDs',()=>{
  const html=readFileSync(new URL('../index.html',import.meta.url),'utf8');
  const header=html.slice(html.indexOf('id="header-detail"'),html.indexOf('</header>'));
  for(const id of ['back-library','route-name','open-edit','export-item','open-share'])assert.ok(header.includes(`id="${id}"`));
  assert.ok(!header.includes('id="color-metrics"'));
  assert.ok(html.indexOf('class="route-overview"') < html.indexOf('id="workspace" hidden'));
  assert.ok(html.indexOf('<dialog id="edit-dialog"') < html.indexOf('<form id="edit-item"'));
  assert.ok(html.includes('id="map-options"'));
});
