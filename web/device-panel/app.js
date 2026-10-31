'use strict';

/* 复果AI · 设备管理面板
 * 纯前端，无构建步骤。调用既有 HTTP API（8002）+ 尽力而为的 events WS。
 * - admin token  → GET /api/v1/admin/devices（全部设备总览）
 * - 用户 token   → GET /api/v1/devices（名下设备，含详情/动作全功能）
 */

const LS_BASE = 'fuguoai_panel_base';
const LS_TOKEN = 'fuguoai_panel_token';

const state = {
  base: localStorage.getItem(LS_BASE) || '',
  token: localStorage.getItem(LS_TOKEN) || '',
  mode: null,          // 'admin' | 'user' | null
  devices: [],
  ws: null,
  current: null,       // 当前打开详情的设备
  timer: null,
};

const $ = (id) => document.getElementById(id);

function api(path, opts = {}) {
  const url = state.base.replace(/\/$/, '') + path;
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  return fetch(url, { ...opts, headers });
}

function toast(msg, kind = '') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), 2600);
}

function relTime(ts) {
  if (!ts) return '';
  const d = Math.floor(Date.now() / 1000) - ts;
  if (d < 60) return d + '秒前';
  if (d < 3600) return Math.floor(d / 60) + '分钟前';
  if (d < 86400) return Math.floor(d / 3600) + '小时前';
  return Math.floor(d / 86400) + '天前';
}

function statusPill(s) {
  const cls = (s === 'active') ? 'active' : (s === 'guest' ? 'guest' : (s === 'unbound' ? 'unbound' : ''));
  return `<span class="pill ${cls}">${s || '—'}</span>`;
}

/* ---------- 连接 & 模式探测 ---------- */
async function connect() {
  state.base = $('apiBase').value.trim();
  state.token = $('token').value.trim();
  if (!state.base) return toast('请填写 API Base', 'err');
  localStorage.setItem(LS_BASE, state.base);
  localStorage.setItem(LS_TOKEN, state.token);

  // 先试 admin 总览接口
  try {
    const r = await api('/api/v1/admin/devices');
    if (r.ok) {
      state.mode = 'admin';
      setMode('admin');
      await loadDevices();
      connectWS();
      return;
    }
  } catch (e) { /* fallthrough */ }

  // 再试用户态列表
  try {
    const r = await api('/api/v1/devices');
    if (r.ok) {
      state.mode = 'user';
      setMode('user');
      await loadDevices();
      connectWS();
      return;
    }
    const j = await r.json().catch(() => ({}));
    toast('鉴权失败：' + (j.error || r.status), 'err');
  } catch (e) {
    toast('连接失败：' + e.message, 'err');
  }
  setMode('unknown');
}

function setMode(m) {
  state.mode = m;
  const b = $('modeBadge');
  b.className = 'badge mode-' + m;
  b.textContent = m === 'admin' ? '运营总览(admin)' : m === 'user' ? '用户态' : '未连接';
  const on = m !== 'unknown';
  $('refreshBtn').disabled = !on;
  $('search').disabled = !on;
  $('connState').className = 'conn-state ' + (on ? 'live' : '');
}

/* ---------- 设备列表 ---------- */
async function loadDevices() {
  const path = state.mode === 'admin' ? '/api/v1/admin/devices' : '/api/v1/devices';
  const r = await api(path);
  const j = await r.json();
  let list = [];
  if (state.mode === 'admin') {
    list = (j.data || []).map(d => ({
      device_id: d.device_id, device_name: d.device_name, status: d.status,
      user_id: d.user_id, online: d.online, last_seen: d.last_seen,
    }));
  } else {
    // 用户态：listDevices 解包 body.data
    list = (j.data || []).map(d => ({
      device_id: d.device_id, device_name: d.device_name || d.alias,
      status: d.status, user_id: d.owner_uid || '', online: d.online,
      last_seen: d.last_active_ts,
    }));
  }
  state.devices = list;
  renderTable();
}

function renderTable() {
  const q = $('search').value.trim().toLowerCase();
  const rows = state.devices.filter(d =>
    !q || (d.device_id || '').toLowerCase().includes(q) ||
    (d.device_name || '').toLowerCase().includes(q) ||
    (d.user_id || '').toLowerCase().includes(q));
  const body = $('devBody');
  body.innerHTML = '';
  $('empty').style.display = rows.length ? 'none' : 'block';
  for (const d of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span class="dot ${d.online ? 'on' : 'off'}"></span>${d.online ? '在线' : '离线'}</td>
      <td class="mono">${esc(d.device_id)}</td>
      <td>${esc(d.device_name || '—')}</td>
      <td>${statusPill(d.status)}</td>
      <td class="mono">${esc(d.user_id || '—')}</td>
      <td>${relTime(d.last_seen) || '—'}</td>
      <td class="row-actions">
        <button data-act="view">查看</button>
        <button data-act="cmd">指令</button>
        <button data-act="intercom">对讲</button>
        <button data-act="unbind" style="color:#ef4444">解绑</button>
      </td>`;
    tr.querySelector('[data-act="view"]').onclick = () => openDrawer(d);
    tr.querySelector('[data-act="cmd"]').onclick = () => doCommand(d);
    tr.querySelector('[data-act="intercom"]').onclick = () => doIntercom(d);
    tr.querySelector('[data-act="unbind"]').onclick = () => doUnbind(d);
    body.appendChild(tr);
  }
  const on = state.devices.filter(d => d.online).length;
  $('summary').textContent = `共 ${state.devices.length} 台 · 在线 ${on}`;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------- 详情抽屉 ---------- */
function openDrawer(d) {
  state.current = d;
  $('drawerTitle').textContent = (d.device_name || '设备') + ' · ' + d.device_id;
  $('drawer').classList.remove('hidden');
  $('overlay').classList.remove('hidden');
  $('tab-overview').innerHTML = renderOverview(d);
  // 重置 tab
  switchTab('overview');
}

function renderOverview(d) {
  return `<div class="kv">
    <div class="k">设备ID</div><div class="v mono">${esc(d.device_id)}</div>
    <div class="k">昵称</div><div class="v">${esc(d.device_name || '—')}</div>
    <div class="k">状态</div><div class="v">${statusPill(d.status)}</div>
    <div class="k">在线</div><div class="v"><span class="dot ${d.online ? 'on' : 'off'}"></span>${d.online ? '在线' : '离线'}</div>
    <div class="k">归属用户</div><div class="v mono">${esc(d.user_id || '—')}</div>
    <div class="k">最近活跃</div><div class="v">${relTime(d.last_seen) || '—'}</div>
  </div>
  <p class="summary">用量/记忆/留言 走用户态接口；若当前为 admin token，这些标签页可能返回 403，属正常（需设备归属 token）。</p>`;
}

async function loadTab(tab) {
  const d = state.current;
  if (!d) return;
  const paths = {
    usage: `/api/v1/devices/${encodeURIComponent(d.device_id)}/usage?days=7`,
    memory: `/api/v1/devices/${encodeURIComponent(d.device_id)}/memory`,
    reminders: `/api/v1/devices/${encodeURIComponent(d.device_id)}/reminders`,
  };
  const el = $('tab-' + tab);
  if (tab === 'events') return; // 事件页为活动日志
  el.innerHTML = '加载中…';
  try {
    const r = await api(paths[tab]);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      el.innerHTML = `<pre class="err">${r.status} ${esc(j.error || '')}\n（用户态接口需设备归属 token；当前若为 admin token 会 403）</pre>`;
      return;
    }
    const j = await r.json();
    el.innerHTML = `<pre>${esc(JSON.stringify(j, null, 2))}</pre>`;
  } catch (e) {
    el.innerHTML = `<pre class="err">请求失败：${esc(e.message)}</pre>`;
  }
}

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + tab));
  if (tab !== 'events') loadTab(tab);
}

/* ---------- 动作 ---------- */
async function doCommand(d) {
  const preset = JSON.stringify({ name: 'DeviceControl', method: 'SetMainLight', parameters: { on: true } }, null, 2);
  const input = prompt('下发 IoT 指令（JSON）：\n允许 method: SetTimerTask/CancelTimerTasks/ListTimerTasks/SetMainLight/SetDevice/SetAmbientLight', preset);
  if (!input) return;
  let body;
  try { body = JSON.parse(input); } catch (e) { return toast('JSON 解析失败：' + e.message, 'err'); }
  try {
    const r = await api(`/api/v1/devices/${encodeURIComponent(d.device_id)}/command`, {
      method: 'POST', body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    logEvent(`[command] ${d.device_id} → ${r.status} ${esc(JSON.stringify(j))}`);
    toast(r.ok ? '指令已下发' : ('失败 ' + (j.error || r.status)), r.ok ? 'ok' : 'err');
  } catch (e) { toast('请求失败：' + e.message, 'err'); }
}

async function doIntercom(d) {
  const peer = prompt('对讲对端 device_id（或 loopback 自环调试）：', 'loopback');
  if (!peer) return;
  const action = confirm('选择动作：确定=start，取消=stop') ? 'start' : 'stop';
  try {
    const r = await api(`/api/v1/devices/${encodeURIComponent(d.device_id)}/intercom`, {
      method: 'POST', body: JSON.stringify({ action, peer }),
    });
    const j = await r.json().catch(() => ({}));
    logEvent(`[intercom] ${d.device_id} ${action} ${peer} → ${r.status}`);
    toast(r.ok ? '对讲指令已发' : ('失败 ' + (j.error || r.status)), r.ok ? 'ok' : 'err');
  } catch (e) { toast('请求失败：' + e.message, 'err'); }
}

async function doUnbind(d) {
  if (!confirm(`确认解绑设备 ${d.device_id}？`)) return;
  try {
    const r = await api('/api/v1/devices/unbind', {
      method: 'POST', body: JSON.stringify({ device_id: d.device_id }),
    });
    const j = await r.json().catch(() => ({}));
    logEvent(`[unbind] ${d.device_id} → ${r.status}`);
    toast(r.ok ? '已解绑' : ('失败 ' + (j.error || r.status)), r.ok ? 'ok' : 'err');
    if (r.ok) loadDevices();
  } catch (e) { toast('请求失败：' + e.message, 'err'); }
}

/* ---------- events WS（尽力而为） ---------- */
function connectWS() {
  if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
  if (!state.token) return;
  let wsBase = state.base.replace(/^https:\/\//, 'wss://').replace(/^http:\/\//, 'ws://').replace(/\/$/, '');
  const url = wsBase + '/api/v1/events?token=' + encodeURIComponent(state.token);
  try {
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => { logEvent('[ws] 已连接事件流'); };
    ws.onmessage = (ev) => {
      try { logEvent('[ws] ' + esc(ev.data)); } catch (e) {}
    };
    ws.onerror = () => { logEvent('[ws] 错误（浏览器端 WS 鉴权需 Authorization 头，原生 WS 不支持；已退回轮询）'); };
    ws.onclose = () => { state.ws = null; };
  } catch (e) {
    logEvent('[ws] 连接失败：' + e.message);
  }
}

function logEvent(line) {
  const box = $('eventLog');
  if (!box) return;
  const div = document.createElement('div');
  div.className = 'line';
  div.textContent = new Date().toLocaleTimeString() + ' ' + line;
  box.prepend(div);
}

/* ---------- 绑定 UI ---------- */
$('connectBtn').onclick = connect;
$('refreshBtn').onclick = () => loadDevices().then(() => toast('已刷新'));
$('drawerClose').onclick = () => { $('drawer').classList.add('hidden'); $('overlay').classList.add('hidden'); state.current = null; };
$('overlay').onclick = $('drawerClose').onclick;
document.querySelectorAll('.tab').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
$('search').oninput = renderTable;
$('actCmd').onclick = () => state.current && doCommand(state.current);
$('actIntercom').onclick = () => state.current && doIntercom(state.current);
$('actUnbind').onclick = () => state.current && doUnbind(state.current);
$('autoRefresh').onchange = (e) => {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  if (e.target.checked && state.mode) state.timer = setInterval(loadDevices, 15000);
};

// 回填上次输入
$('apiBase').value = state.base;
$('token').value = state.token;
