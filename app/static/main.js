const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const spinner = document.getElementById('spinner');
const redisBanner = document.getElementById('redisBanner');
const toastBox = document.getElementById('toast');
let chart;
let logTimer = null;

function loadQueue() {
  return JSON.parse(localStorage.getItem('syncQueue') || '[]');
}

function saveQueue(q) {
  localStorage.setItem('syncQueue', JSON.stringify(q));
}

function showSpinner() { spinner.classList.remove('hidden'); }
function hideSpinner() { spinner.classList.add('hidden'); }
function showToast(msg, ok = true) {
  toastBox.textContent = msg;
  toastBox.classList.remove('hidden');
  toastBox.classList.toggle('bg-red-500', !ok);
  toastBox.classList.toggle('bg-green-500', ok);
  setTimeout(() => toastBox.classList.add('hidden'), 3000);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function modeBadge(kind, mode) {
  const label = mode === 'local_cookie_test' ? 'cookie test'
    : mode === 'local_test' ? 'local test'
    : mode === 'live_kick' || mode === 'live' ? 'live kick'
    : kind || 'unknown';
  const color = mode === 'live_kick' || mode === 'live' ? 'bg-blue-600'
    : mode === 'local_cookie_test' ? 'bg-yellow-600'
    : kind === 'empty' ? 'bg-red-600'
    : 'bg-gray-600';
  return `<span class="${color} text-white px-2 py-0.5 rounded text-xs">${label}</span>`;
}

async function checkRedis() {
  const res = await fetch('/dashboard/api/status').catch(() => null);
  if (!res || !res.ok) return;
  const data = await res.json();
  if (data.redis_online) redisBanner.classList.add('hidden');
  else redisBanner.classList.remove('hidden');
}

function getAccess() {
  return localStorage.getItem('accessToken');
}

async function refreshToken() {
  const refresh = localStorage.getItem('refreshToken');
  if (!refresh) return null;
  const res = await fetch('/auth/refresh', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + refresh }
  }).catch(() => null);
  if (!res || !res.ok) return null;
  const data = await res.json();
  localStorage.setItem('accessToken', data.access_token);
  return data.access_token;
}

async function api(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers, {
    'X-CSRFToken': csrfToken
  });
  const access = getAccess();
  if (access) opts.headers['Authorization'] = 'Bearer ' + access;
  showSpinner();
  let res = await fetch(url, opts).catch(() => null);
  if (res && res.status === 401) {
    const newTok = await refreshToken();
    if (newTok) {
      opts.headers['Authorization'] = 'Bearer ' + newTok;
      res = await fetch(url, opts).catch(() => null);
    }
  }
  hideSpinner();
  if (!res) return null;
  return res.json();
}


function openModal(id) { document.getElementById(id).showModal(); }
function closeModal(id) { document.getElementById(id).close(); }
function closeCmd() { document.getElementById('cmdDialog').close(); }
function openCmd(id) {
  document.getElementById('cmd-id').value = id;
  document.getElementById('cmdDialog').showModal();
}

async function syncPull() {
  const res = await api('/sync/pull');
  if (!res) return;
  res.events.forEach(evt => {
    if (evt.entity === 'group') loadGroups();
    if (evt.entity === 'account') loadAccounts();
    if (evt.entity === 'bot') loadBots();
  });
}

async function syncPush() {
  if (!navigator.onLine) return;
  const queue = loadQueue();
  if (queue.length === 0) return;
  const res = await api('/sync/push', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(queue)
  });
  if (res) saveQueue([]);
}

async function loadGroups() {
  const q = document.getElementById('groupSearch').value.trim();
  const url = q.length > 1 ? '/dashboard/api/groups?search=' + encodeURIComponent(q) : '/dashboard/api/groups';
  const data = await api(url);
  if (!data) return;
  const groups = data.items || data;
  const list = document.getElementById('groupList');
  list.innerHTML = '';
  if (!groups.length) {
    list.innerHTML = '<li class="text-gray-500 text-sm">No groups created yet</li>';
    return;
  }
  groups.forEach(g => {
    const li = document.createElement('li');
    li.className = 'mb-1';
    li.innerHTML = `<div class="font-semibold">${g.name} (${g.target})</div>`;
    if (g.bots && g.bots.length) {
      const ul = document.createElement('ul');
      ul.className = 'pl-4 list-disc';
      g.bots.forEach(b => {
        const bi = document.createElement('li');
        bi.textContent = `${b.username} (#${b.id})`;
        ul.appendChild(bi);
      });
      li.appendChild(ul);
    }
    list.appendChild(li);
  });
}

async function loadAccounts() {
  const q = document.getElementById('accountSearch').value.trim();
  const url = q.length > 1 ? '/dashboard/api/accounts?search=' + encodeURIComponent(q) : '/dashboard/api/accounts';
  const data = await api(url);
  if (!data) return;
  const accs = data.items || data;
  const table = document.getElementById('accountTable');
  table.innerHTML = '<tr><th>ID</th><th>User</th><th>Group</th><th>Token</th></tr>';
  if (!accs.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td class="border px-2 text-center" colspan="4">No accounts</td>';
    table.appendChild(row);
    return;
  }
  accs.forEach(a => {
    const row = document.createElement('tr');
    row.innerHTML =
      `<td class="border px-2">${a.id}</td>` +
      `<td class="border px-2">${a.username}</td>` +
      `<td class="border px-2">${a.group_id}</td>` +
      `<td class="border px-2 text-center">${modeBadge(a.token_kind, a.token_mode)}</td>`;
    table.appendChild(row);
  });
}

async function loadBots() {
  const q = document.getElementById('botSearch').value.trim();
  const url = q.length > 1 ? '/dashboard/api/bots?search=' + encodeURIComponent(q) : '/dashboard/api/bots';
  const data = await api(url);
  if (!data) return;
  const bots = data.items || data;
  const table = document.getElementById('botTable');
  table.innerHTML = '<tr><th>ID</th><th>User</th><th>Status</th><th>Mode</th><th>Actions</th></tr>';
  if (!bots.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td class="border px-2 text-center" colspan="5">No bots created yet</td>';
    table.appendChild(row);
    return;
  }
  bots.forEach(b => {
    const row = document.createElement('tr');
    const badge = b.status === 'online'
      ? '<span class="bg-green-500 text-white px-2 py-0.5 rounded">running</span>'
      : '<span class="bg-red-500 text-white px-2 py-0.5 rounded">stopped</span>';
    row.innerHTML =
      `<td class="border px-2">${b.id}</td>` +
      `<td class="border px-2">${b.username}</td>` +
      `<td class="border px-2 text-center">${badge}</td>` +
      `<td class="border px-2 text-center">${modeBadge(b.token_kind, b.mode)}</td>` +
      `<td class="border px-2 space-x-1">` +
        `<button onclick="startBot(${b.id})" class="bg-green-600 text-white px-2 py-1 text-xs rounded">Start</button>` +
        `<button onclick="stopBot(${b.id})" class="bg-red-600 text-white px-2 py-1 text-xs rounded">Stop</button>` +
        `<button onclick="openCmd(${b.id})" class="bg-blue-500 text-white px-2 py-1 text-xs rounded">Cmd</button>` +
        `<button onclick="fetchLogs(${b.id})" class="underline text-xs">Logs</button>` +
      `</td>`;
    table.appendChild(row);
  });
}

async function refreshStats() {
  const stats = await api('/dashboard/api/stats');
  if (!stats) return;
  if (!chart) {
    const ctx = document.getElementById('chart');
    chart = new Chart(ctx, {
      type: 'bar',
      data: { labels: ['Runs', 'Errors'], datasets: [{ data: [stats.runs, stats.errors], backgroundColor: ['#4ade80','#f87171'] }] },
      options: { plugins: { legend: { display: false } } }
    });
  } else {
    chart.data.datasets[0].data = [stats.runs, stats.errors];
    chart.update();
  }
}

async function startScheduler() {
  await api('/dashboard/api/scheduler/start', {method: 'POST'});
}

async function startBot(id) {
  const res = await api(`/dashboard/api/bots/${id}/start`, {method: 'POST'});
  if (res && res.error) showToast(res.error, false);
  else if (res && res.pid && res.mode === 'local_cookie_test') showToast('Local cookie test started');
  else if (res && res.pid) showToast('Bot started');
  fetchLogs(id);
  loadLocalEvents();
  loadBots();
}

async function stopBot(id) {
  const res = await api(`/dashboard/api/bots/${id}/stop`, {method: 'POST'});
  if (res && res.stopped) showToast('Bot stopped');
  loadBots();
}

async function fetchLogs(id) {
  if (logTimer) clearInterval(logTimer);
  async function load() {
    const lines = await api(`/dashboard/api/bots/${id}/logs`);
    if (lines) {
      const box = document.getElementById('logBox');
      box.textContent = lines.join('\n');
      box.scrollTop = box.scrollHeight;
    }
  }
  await load();
  logTimer = setInterval(load, 3000);
}

async function loadLocalEvents() {
  await loadLocalReport();
  await loadLocalQueue();
  const data = await api('/dashboard/api/local/events?limit=50');
  if (!data) return;
  const events = data.items || [];
  const table = document.getElementById('localEventTable');
  table.innerHTML = '<tr><th>Time</th><th>Action</th><th>Status</th><th>Code</th><th>Channel</th><th>Actor</th><th>Content</th></tr>';
  if (!events.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td class="border px-2 text-center" colspan="7">No local events yet</td>';
    table.appendChild(row);
    return;
  }
  events.slice().reverse().forEach(evt => {
    const row = document.createElement('tr');
    const when = (evt.timestamp || '').replace('T', ' ').replace('+00:00', 'Z');
    const content = evt.content || evt.detail || '';
    const statusColor = evt.status === 'success' ? 'text-green-700' : 'text-red-700';
    row.innerHTML =
      `<td class="border px-2">${escapeHtml(when)}</td>` +
      `<td class="border px-2">${escapeHtml(evt.action || '')}</td>` +
      `<td class="border px-2 ${statusColor}">${escapeHtml(evt.status || '')}</td>` +
      `<td class="border px-2">${escapeHtml(evt.code || '')}</td>` +
      `<td class="border px-2">${escapeHtml(evt.channel || '')}</td>` +
      `<td class="border px-2">${escapeHtml(evt.actor || '')}</td>` +
      `<td class="border px-2">${escapeHtml(content)}</td>`;
    table.appendChild(row);
  });
}

async function loadLocalReport() {
  const report = await api('/dashboard/api/local/report');
  if (!report) return;
  const box = document.getElementById('localReport');
  const action = report.actions || {};
  const queue = report.queue || {};
  const accounts = report.accounts || {};
  const accountStatuses = accounts.by_status || {};
  box.innerHTML = [
    ['Actions', action.total || 0, `ok ${action.success || 0} / failed ${action.failed || 0}`],
    ['Rate limit', action.rate_limited || 0, 'blocked by limits'],
    ['Queue', queue.total || 0, Object.entries(queue.by_status || {}).map(([k, v]) => `${k}: ${v}`).join(', ') || 'empty'],
    ['Accounts', accounts.total || 0, Object.entries(accountStatuses).map(([k, v]) => `${k}: ${v}`).join(', ') || 'none'],
  ].map(([label, value, detail]) =>
    `<div class="border rounded p-2 bg-gray-50"><div class="text-gray-500">${label}</div><div class="font-bold text-lg">${value}</div><div class="text-xs">${escapeHtml(detail)}</div></div>`
  ).join('');
}

async function loadLocalQueue() {
  const data = await api('/dashboard/api/local/queue');
  if (!data) return;
  const jobs = data.items || [];
  const table = document.getElementById('localQueueTable');
  table.innerHTML = '<tr><th>ID</th><th>Action</th><th>Status</th><th>Attempts</th><th>Account</th><th>Channel</th><th>Error</th></tr>';
  if (!jobs.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td class="border px-2 text-center" colspan="7">Queue is empty</td>';
    table.appendChild(row);
    return;
  }
  jobs.slice(-20).reverse().forEach(job => {
    const row = document.createElement('tr');
    row.innerHTML =
      `<td class="border px-2">${escapeHtml(String(job.id || '').slice(0, 8))}</td>` +
      `<td class="border px-2">${escapeHtml(job.action || '')}</td>` +
      `<td class="border px-2">${escapeHtml(job.status || '')}</td>` +
      `<td class="border px-2">${escapeHtml(job.attempts || 0)}/${escapeHtml(job.max_attempts || 0)}</td>` +
      `<td class="border px-2">${escapeHtml(job.account_id || '')}</td>` +
      `<td class="border px-2">${escapeHtml(job.channel || '')}</td>` +
      `<td class="border px-2">${escapeHtml(job.last_error || '')}</td>`;
    table.appendChild(row);
  });
}

async function processLocalQueue() {
  const res = await api('/dashboard/api/local/queue/process', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({limit: 200})
  });
  if (res) showToast(`Processed ${res.count || 0} local jobs`);
  loadLocalEvents();
}

async function runLocalMassTest() {
  const res = await api('/dashboard/api/local/mass-test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action_count: 1000, account_count: 50, channel: 'local-load-test', process: true})
  });
  if (res) showToast(`Queued ${res.queued || 0}, processed ${res.processed || 0}`);
  loadLocalEvents();
}

async function clearLocalEvents() {
  const res = await api('/dashboard/api/local/events', {method: 'DELETE'});
  if (res && res.status === 'ok') showToast('Local events cleared');
  loadLocalEvents();
}

document.getElementById('addGroupBtn').addEventListener('click', () => openModal('groupModal'));
document.getElementById('addAccountBtn').addEventListener('click', () => openModal('accountModal'));
let groupTimer, accountTimer, botTimer;
document.getElementById('groupSearch').addEventListener('input', () => {
  clearTimeout(groupTimer);
  groupTimer = setTimeout(() => {
    const q = document.getElementById('groupSearch').value.trim();
    if (q.length === 0 || q.length > 1) loadGroups();
  }, 300);
});
document.getElementById('accountSearch').addEventListener('input', () => {
  clearTimeout(accountTimer);
  accountTimer = setTimeout(() => {
    const q = document.getElementById('accountSearch').value.trim();
    if (q.length === 0 || q.length > 1) loadAccounts();
  }, 300);
});
document.getElementById('botSearch').addEventListener('input', () => {
  clearTimeout(botTimer);
  botTimer = setTimeout(() => {
    const q = document.getElementById('botSearch').value.trim();
    if (q.length === 0 || q.length > 1) loadBots();
  }, 300);
});

document.getElementById('groupForm').addEventListener('submit', async e => {
  e.preventDefault();
  const data = {
    name: document.getElementById('g-name').value,
    target: document.getElementById('g-target').value,
    interval: parseInt(document.getElementById('g-interval').value, 10)
  };
  const res = await api('/dashboard/api/groups', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  });
  if (!res) {
    const q = loadQueue();
    q.push({entity: 'group', action: 'create', payload: data, timestamp: new Date().toISOString()});
    saveQueue(q);
    showToast('Queued offline', true);
  } else if (res.error) {
    showToast(res.error, false);
  } else {
    loadGroups();
    closeModal('groupModal');
    syncPush();
    showToast('Group created');
  }
});

document.getElementById('accountForm').addEventListener('submit', async e => {
  e.preventDefault();
  const data = {
    username: document.getElementById('a-user').value,
    password: document.getElementById('a-pass').value,
    proxy: document.getElementById('a-proxy').value,
    messages_file: document.getElementById('a-msg').value,
    group_id: parseInt(document.getElementById('a-group').value, 10)
  };
  const res = await api('/dashboard/api/accounts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  });
  if (!res) {
    const q = loadQueue();
    q.push({entity: 'account', action: 'create', payload: data, timestamp: new Date().toISOString()});
    saveQueue(q);
    showToast('Queued offline', true);
  } else if (res.error) {
    showToast(res.error, false);
  } else {
    loadAccounts();
    loadBots();
    closeModal('accountModal');
    syncPush();
    showToast('Account created');
  }
});

document.getElementById('cmdForm').addEventListener('submit', async e => {
  e.preventDefault();
  const id = document.getElementById('cmd-id').value;
  const cmd = document.getElementById('cmd-type').value;
  const args = document.getElementById('cmd-args').value;
  const payload = {cmd: cmd, args: {message: args}};
  const res = await api(`/dashboard/api/bots/${id}/command`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (!res) {
    const q = loadQueue();
    q.push({entity: 'bot', action: cmd, payload: {id: id, args: args}, timestamp: new Date().toISOString()});
    saveQueue(q);
    showToast('Queued offline', true);
  } else if (res && res.status === 'ok') {
    showToast('Command queued');
  } else if (res && res.error) {
    showToast(res.error, false);
  }
  closeCmd();
  fetchLogs(id);
  loadLocalEvents();
  syncPush();
});

if (Notification && Notification.permission !== 'granted') { Notification.requestPermission(); }

const socket = io();
['bot_started','bot_stopped','bot_error','status'].forEach(evt => {
  socket.on(evt, () => { loadBots(); refreshStats(); loadLocalEvents(); });
});
socket.on('redis_status', () => checkRedis());
socket.on('sync_event', syncPull);
socket.on('connect', () => { syncPull(); syncPush(); checkRedis(); });

window.addEventListener('load', () => {
  loadGroups();
  loadAccounts();
  loadBots();
  loadLocalEvents();
  refreshStats();
  syncPull();
  syncPush();
  checkRedis();
  setInterval(checkRedis, 10000);
  if (!navigator.onLine) document.getElementById('offlineBanner').classList.remove('hidden');
});
