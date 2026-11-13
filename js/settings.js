const $ = id => document.getElementById(id);

async function request(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'X-Hiking-Request': '1', ...options.headers}});
  if (response.status === 401) {
    location.replace('/login.html');
    throw new Error('登录已过期');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}

function bytes(value) {
  const unit = value >= 1024 ** 3 ? 3 : value >= 1024 ** 2 ? 2 : value >= 1024 ? 1 : 0;
  return `${(value / 1024 ** unit).toLocaleString('zh-CN', {maximumFractionDigits: 1})} ${['B', 'KiB', 'MiB', 'GiB'][unit]}`;
}

async function refreshStorage() {
  $('refresh-storage').disabled = true;
  $('settings-status').textContent = '正在统计存储占用…';
  try {
    const data = await request('/api/settings/storage');
    $('cache-bytes').textContent = bytes(data.cache.bytes);
    $('cache-files').textContent = `${data.cache.files} 个文件（含缓存元数据）`;
    $('photo-bytes').textContent = bytes(data.photos.bytes);
    $('photo-files').textContent = `${data.photos.files} 个文件`;
    $('database-bytes').textContent = bytes(data.database_bytes);
    $('disk-free').textContent = bytes(data.disk_free_bytes);
    $('disk-total').textContent = `磁盘总容量 ${bytes(data.disk_total_bytes)}`;
    $('settings-status').textContent = `已更新：${new Date().toLocaleTimeString('zh-CN')}`;
  } catch (error) {
    $('settings-status').textContent = error.message;
  } finally {
    $('refresh-storage').disabled = false;
  }
}

$('refresh-storage').addEventListener('click', refreshStorage);
refreshStorage();

$('shortcut-server').value = location.origin;
request('/api/settings/shortcut').then(data => {
  if ($('download-gpx-shortcut')) $('download-gpx-shortcut').hidden = !data.gpx_available;
  if ($('download-photos-shortcut')) $('download-photos-shortcut').hidden = !data.photos_available;
  if ($('download-shortcut')) $('download-shortcut').hidden = !data.download_available;
  $('shortcut-availability').textContent = data.download_available
    ? '可下载已签名版本；添加后填写地址和凭证。'
    : '暂未提供已签名版本。可下载源文件；没有 Mac 时，可展开下方说明在 iPhone 上手动创建。';
}).catch(error => { $('shortcut-availability').textContent = error.message; });

async function updateCredential(method) {
  const creating = method === 'POST';
  if (!confirm(creating ? '生成新凭证后，所有使用旧凭证的快捷指令将停止上传。继续？' : '撤销后，现有快捷指令将无法上传。继续？')) return;
  $('create-shortcut-token').disabled = true;
  $('revoke-shortcut-token').disabled = true;
  $('shortcut-status').textContent = '正在更新上传凭证…';
  try {
    const data = await request('/api/settings/shortcut-token', {method});
    $('shortcut-token').value = data.token || '';
    $('shortcut-token-panel').hidden = !creating;
    $('shortcut-status').textContent = creating ? '新凭证已生成。复制后填写到快捷指令；刷新页面后不会再次显示。' : '上传凭证已撤销。';
  } catch (error) {
    $('shortcut-status').textContent = error.message;
  } finally {
    $('create-shortcut-token').disabled = false;
    $('revoke-shortcut-token').disabled = false;
  }
}

$('create-shortcut-token').addEventListener('click', () => updateCredential('POST'));
$('revoke-shortcut-token').addEventListener('click', () => updateCredential('DELETE'));
$('copy-shortcut-token').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('shortcut-token').value);
    $('shortcut-status').textContent = '凭证已复制。';
  } catch {
    $('shortcut-token').focus();
    $('shortcut-token').select();
    $('shortcut-status').textContent = '请长按选中的凭证并复制。';
  }
});
