import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createContext,runInContext} from 'node:vm';

const app = readFileSync(new URL('../js/app.js',import.meta.url),'utf8');
const code = app.slice(app.indexOf('function collapsePhoto()'),app.indexOf('function createLivePlayer('));

test('inline preview toggles, switches photos, and dismisses without blocking other actions',()=>{
  const events = {};
  const document = {addEventListener(name,handler){events[name]=handler;}};
  const context = createContext({document,window:document,expandedPhoto:null});
  runInContext(code+'setupPhotoPreview();',context);
  function photo() {
    const classes = new Set();
    return {classes,attributes:{},classList:{add:v=>classes.add(v),remove:v=>classes.delete(v)},
      setAttribute(k,v){this.attributes[k]=v;},querySelector(){return null;},contains(target){return target===this;}};
  }
  const a=photo(), b=photo();
  for (const event of ['wheel','scroll','input','change','resize','focusin']) assert.equal(events[event], undefined);
  context.togglePhoto(a);
  assert.equal(a.attributes['aria-expanded'],'true');
  events.pointerdown({target:a});
  assert.ok(a.classes.has('photo-expanded'));
  context.togglePhoto(b);
  assert.equal(a.attributes['aria-expanded'],'false');
  assert.equal(b.attributes['aria-expanded'],'true');
  context.togglePhoto(b);
  assert.equal(b.attributes['aria-expanded'],'false');
  for (const event of ['pointerdown','click','keydown']) {
    context.togglePhoto(a);
    events[event]({target:b,key:'Escape'});
    assert.equal(a.attributes['aria-expanded'],'false',event);
  }
});

test('a photo batch keeps its original record and matching points when navigation changes',async()=>{
  const uploadCode=app.slice(app.indexOf('async function uploadPhotos('),app.indexOf('function collapsePhoto()'));
  const nodes=new Map(),paths=[],matched=[],loaded=[];
  const points=[{time:1000}];
  const context=createContext({
    sharePrefix:'',selected:{id:12,name:'原记录'},stats:{points},photoUploadBusy:false,
    $:id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);},
    parseExif:()=>({}),matchPhotoToTrack:(_exif,received)=>{matched.push(received);return {};},
    compressImage:async()=>({data:'fixture',width:1,height:1}),
    api:async path=>paths.push(path),loadPhotos:async id=>loaded.push(id),notify:()=>{}
  });
  runInContext(uploadCode,context);
  const file={name:'fixture.png',arrayBuffer:async()=>{
    context.selected={id:99,name:'另一记录'};context.stats={points:[]};return new ArrayBuffer(0);
  }};
  const upload=context.uploadPhotos([file,file]);
  await context.uploadPhotos([file]);
  await upload;
  assert.deepEqual(paths,['/api/items/12/photos','/api/items/12/photos']);
  assert.ok(matched.every(value=>value===points));
  assert.deepEqual(loaded,[12]);
  assert.equal(context.photoUploadBusy,false);
  assert.equal(nodes.get('photo-file-input').disabled,false);
});

test('Live plays with sound once, returns to the still, and ignores stale playback failures',async()=>{
  const elements=[];
  function element(tag) {
    const events={};
    const e={tag,events,hidden:false,paused:true,attributes:{},
      addEventListener(name,fn){events[name]=fn;},
      getAttribute(name){return name==='src'?this.src:this.attributes[name];},
      removeAttribute(name){delete this[name];},load(){},
      play(){this.paused=false;return this.promise || Promise.resolve();},pause(){this.paused=true;}};
    elements.push(e);return e;
  }
  const context=createContext({document:{createElement:element},activeLivePlayer:null,setTimeout:()=>1,clearTimeout(){}});
  runInContext(app.slice(app.indexOf('function createLivePlayer('),app.indexOf('let lightboxPlayer;')),context);
  const player=context.createLivePlayer({append(){}},path=>'/shared'+path),[video,retry]=elements;
  player.show({id:1,live:true});
  assert.equal(video.src,'/shared/api/photos/1/live');
  assert.equal(video.muted,false);assert.equal(video.loop,false);assert.equal(video.playsInline,true);
  assert.equal(player.busy,true);
  video.events.playing();assert.equal(video.hidden,false);
  player.pause();assert.equal(video.paused,true);
  player.resume();assert.equal(video.src,'/shared/api/photos/1/live');
  video.events.ended();assert.equal(video.hidden,true);assert.equal(player.busy,false);assert.equal(video.paused,true);
  let reject;
  video.promise=new Promise((resolve,fail)=>{reject=fail;});
  player.show({id:2,live:true});player.stop();
  reject({name:'NotAllowedError'});await Promise.resolve();
  assert.equal(retry.hidden,true);assert.equal(player.busy,false);
  video.promise=Promise.reject({name:'NotAllowedError'});
  player.show({id:3,live:true});await Promise.resolve();
  assert.equal(retry.hidden,false);assert.equal(player.busy,true);
  video.promise=Promise.resolve();retry.events.click({stopPropagation(){}});
  assert.equal(video.src,'/shared/api/photos/3/live');
  player.show({id:4,live:false});assert.equal(player.busy,false);assert.equal(video.hidden,true);
});

test('replay Live waits for readiness and skips clips that cannot finish before the next photo',()=>{
  const elements=[];
  const context=createContext({document:{createElement(){
    const element={events:{},readyState:0,duration:1.5,currentTime:0,plays:0,
      addEventListener(name,fn){this.events[name]=fn;},getAttribute(){return this.src;},
      removeAttribute(name){delete this[name];},load(){},pause(){},
      play(){this.plays++;return Promise.resolve();}};
    elements.push(element);return element;
  }},activeLivePlayer:null,setTimeout:()=>1,clearTimeout(){}});
  runInContext(app.slice(app.indexOf('function createLivePlayer('),app.indexOf('let lightboxPlayer;')),context);
  let remaining=2;
  const player=context.createLivePlayer({append(){}},path=>path,()=>remaining);
  const [video,retry]=elements;
  player.show({id:1,live:true});
  assert.equal(video.plays,0);
  remaining=1;video.readyState=3;video.events.canplay();
  assert.equal(video.plays,0);assert.equal(player.busy,false);
  assert.equal(video.hidden,true);assert.equal(retry.hidden,true);
  remaining=1.5;player.show({id:2,live:true});
  assert.equal(video.plays,1,'exactly enough time permits playback');
  player.pause();video.currentTime=.5;remaining=.9;player.resume();
  assert.equal(video.plays,1,'resume checks the remaining clip against the new window');
  video.readyState=0;player.show({id:3,live:true});player.pause();
  remaining=Infinity;video.readyState=3;video.events.canplay();
  assert.equal(video.plays,1,'loading must not resume a paused replay');
  player.resume();assert.equal(video.plays,2,'the final photo has no deadline');
  video.readyState=0;player.show({id:4,live:true});player.stop();
  video.readyState=3;video.events.canplay();
  assert.equal(video.plays,2,'switching photos cancels pending playback');
});
