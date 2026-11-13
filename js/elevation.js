export const DEM_TILES = '/api/tiles/dem/terrarium/{z}/{x}/{y}';
export function tilePosition(lng, lat, zoom = 12) {
  const size = 2**zoom;
  const x = ((lng+180)/360*size) % size;
  const y = (1-Math.asinh(Math.tan(lat*Math.PI/180))/Math.PI)/2*size;
  return { x:Math.floor(x), y:Math.floor(y), px:Math.min(255,Math.floor((x%1)*256)), py:Math.min(255,Math.floor((y%1)*256)), zoom };
}
export const decodeTerrarium = (r,g,b) => r*256+g+b/256-32768;

export async function fillElevation(route, onProgress, signal) {
  const groups = new Map();
  const result = {...route, segments:route.segments.map(s => s.map(p => ({...p})))};
  for (const p of result.segments.flat()) {
    if (p.ele !== null) continue;
    const t = tilePosition(p.lng,p.lat);
    const key = `${t.zoom}/${t.x}/${t.y}`;
    if (!groups.has(key)) groups.set(key,[]);
    groups.get(key).push({point:p,...t});
  }
  if (groups.size > 150) throw new Error('缺失海拔的路线范围过大（超过 150 张 DEM 瓦片），请分段导入。');
  const queue = [...groups.values()];
  let done = 0;
  await Promise.all(Array.from({length:Math.min(4,queue.length)},async () => {
    while (queue.length) {
      signal?.throwIfAborted();
      const items = queue.shift(), t = items[0];
      const url = DEM_TILES.replace('{z}',t.zoom).replace('{x}',t.x).replace('{y}',t.y);
      const response = await fetch(url,{signal:AbortSignal.any([signal,AbortSignal.timeout(20000)].filter(Boolean))});
      if (!response.ok) throw new Error(`DEM 下载失败（HTTP ${response.status}）`);
      const bitmap = await createImageBitmap(await response.blob());
      if (bitmap.width !== 256 || bitmap.height !== 256) { bitmap.close(); throw new Error('DEM 瓦片尺寸无效。'); }
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 256;
      const ctx = canvas.getContext('2d',{willReadFrequently:true});
      ctx.drawImage(bitmap,0,0); bitmap.close();
      const pixels = ctx.getImageData(0,0,256,256).data;
      for (const item of items) {
        const i = (item.py*256+item.px)*4;
        if (pixels[i+3] === 0) throw new Error('此区域缺少有效 DEM 数据。');
        item.point.ele = decodeTerrarium(pixels[i],pixels[i+1],pixels[i+2]);
      }
      onProgress(++done,groups.size);
    }
  }));
  return result;
}
