(function () {
  'use strict';

  const $ = function (selector, scope) { return (scope || document).querySelector(selector); };
  const $$ = function (selector, scope) { return Array.from((scope || document).querySelectorAll(selector)); };
  const labels = { overview: '算力总览', models: '模型与会话', chat: '对话测试', keys: 'API 访问', logs: '运行日志', settings: '连接设置' };
  const state = { authenticated: false, setup: false, currentView: 'overview', status: null, settings: {}, history: [], samples: [], logs: [], pollBusy: false, chatBusy: false, chatJob: null, commandBusy: false, pendingAuth: false, connectionFresh: false };
  const dirtyForms = new Set();

  function safeNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function text(selector, value) { const el = $(selector); if (el) el.textContent = value === null || value === undefined ? '—' : String(value); }
  function formatNumber(value) { const n = safeNumber(value); return n === null ? '—' : n.toLocaleString('zh-CN', { maximumFractionDigits: 1 }); }
  function formatMemory(mb) { const n = safeNumber(mb); return n === null ? '—' : (n / 1024).toFixed(1) + ' GiB'; }
  function formatDate(value, includeTime) {
    if (!value) return '—';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: includeTime ? '2-digit' : undefined, hour12: false });
  }
  function formatDuration(seconds) {
    const n = safeNumber(seconds);
    if (n === null) return '—';
    const s = Math.max(0, Math.floor(n));
    return String(Math.floor(s / 3600)).padStart(2, '0') + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name);
    svg.append(use);
    return svg;
  }
  function node(tag, className, content) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (content !== undefined && content !== null) el.textContent = String(content);
    return el;
  }
  function toast(message, kind) {
    const el = node('div', 'toast ' + (kind || ''), message);
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    $('#toast-container').append(el);
    window.setTimeout(function () { el.remove(); }, kind === 'error' ? 8000 : 4500);
  }
  function inlineStatus(selector, message, error) {
    const el = $(selector);
    el.textContent = message;
    el.classList.toggle('error', Boolean(error));
  }
  function errorText(payload, fallback) {
    if (!payload) return fallback;
    if (typeof payload === 'string') return payload;
    if (typeof payload.detail === 'string') return payload.detail;
    if (Array.isArray(payload.detail)) return payload.detail.map(function (part) { return part.msg || String(part); }).join('；');
    if (typeof payload.error === 'string') return payload.error;
    if (payload.error && payload.error.message) return payload.error.message;
    return payload.message || fallback;
  }
  async function api(path, options) {
    options = options || {};
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    const response = await fetch(path, Object.assign({}, options, {
      credentials: 'same-origin', headers: headers, body: options.body === undefined ? undefined : JSON.stringify(options.body)
    }));
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.indexOf('application/json') !== -1 ? await response.json() : await response.text();
    if (!response.ok) {
      if (response.status === 401 && state.authenticated) showAuth(false);
      const error = new Error(errorText(payload, '请求失败（HTTP ' + response.status + '）'));
      error.status = response.status;
      throw error;
    }
    return payload;
  }
  function setBadge(selector, label, style) {
    const el = $(selector);
    el.className = 'badge ' + (style || 'neutral');
    const dot = node('span', 'status-dot');
    el.replaceChildren(dot, document.createTextNode(label));
  }
  function isAgentOnline() {
    const agent = state.status && state.status.agent;
    return Boolean(state.connectionFresh && agent && ['online', 'idle', 'ready', 'loading', 'starting', 'busy', 'error', 'stopping'].includes(String(agent.status || '').toLowerCase()));
  }
  function modelName(agent) {
    if (!agent || !agent.model) return '';
    if (typeof agent.model === 'string') return agent.model;
    return agent.model.name || agent.model.id || agent.model.model || agent.model.path || '';
  }
  function modelReady() {
    const agent = state.status && state.status.agent;
    if (!isAgentOnline() || !agent) return false;
    const status = String(agent.status || '').toLowerCase();
    const modelStatus = agent.model && typeof agent.model === 'object' ? String(agent.model.status || '').toLowerCase() : '';
    return Boolean(modelName(agent) && ['ready', 'online', 'idle'].includes(status) && modelStatus === 'ready');
  }
  function updateActionAvailability() {
    const online = isAgentOnline();
    $$('[data-agent-action]').forEach(function (button) { button.disabled = !online || state.commandBusy; });
    $('#chat-send-button').disabled = !modelReady() || state.chatBusy;
    $('#clear-chat-button').disabled = state.chatBusy;
  }
  function showAuth(setup) {
    state.authenticated = false;
    state.connectionFresh = false;
    dirtyForms.clear();
    hideNewKey();
    state.setup = setup;
    $('#app-shell').hidden = true;
    $('#auth-screen').hidden = false;
    $('#auth-fields').hidden = false;
    $('#auth-retry').hidden = true;
    $('#setup-token-field').hidden = !setup;
    $('#setup-token').required = setup;
    $('#auth-password').minLength = setup ? 12 : 1;
    $('#auth-password').autocomplete = setup ? 'new-password' : 'current-password';
    text('#auth-eyebrow', setup ? 'INITIAL WORKSPACE SETUP' : 'PRIVATE WORKSPACE');
    text('#auth-title', setup ? '创建你的管理账号' : '欢迎回到工作空间');
    text('#auth-description', setup ? '使用部署时生成的一次性令牌，设置用户名和至少 12 位密码。' : '登录后管理你的 Kaggle 算力与模型服务。');
    text('#auth-submit', setup ? '完成首次设置' : '进入工作空间');
    $('#auth-submit').append(icon('arrow'));
    $('#auth-error').textContent = '';
  }
  async function checkAuth() {
    $('#auth-retry').hidden = true;
    $('#auth-fields').hidden = true;
    text('#auth-title', '正在连接控制台');
    text('#auth-description', '检查登录状态，请稍候。');
    $('#auth-error').textContent = '';
    try {
      const me = await api('/api/me');
      if (me.authenticated) await showApp(me.username);
      else showAuth(Boolean(me.setup_required));
    } catch (error) {
      text('#auth-title', '暂时无法连接');
      text('#auth-description', '请检查服务是否已启动，或稍后重新连接。');
      $('#auth-error').textContent = error.message;
      $('#auth-retry').hidden = false;
    }
  }
  async function showApp(username) {
    state.authenticated = true;
    $('#auth-screen').hidden = true;
    $('#app-shell').hidden = false;
    text('#username-label', username || '管理员');
    changeView(location.hash.slice(1) || 'overview');
    await refreshStatus();
  }
  function closeSidebar() {
    $('#sidebar').classList.remove('open');
    $('#sidebar-backdrop').hidden = true;
    $('#menu-button').setAttribute('aria-expanded', 'false');
  }
  function changeView(view) {
    if (!Object.hasOwn(labels, view)) view = 'overview';
    state.currentView = view;
    $$('.view').forEach(function (el) { const active = el.id === 'view-' + view; el.classList.toggle('active', active); el.hidden = !active; });
    $$('.nav-item').forEach(function (el) { const active = el.dataset.view === view; el.classList.toggle('active', active); if (active) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current'); });
    text('#breadcrumb-current', labels[view]);
    closeSidebar();
    if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
    if (!state.authenticated) return;
    if (view === 'keys') refreshKeys();
    if (view === 'logs') refreshLogs();
    if (view === 'overview') requestAnimationFrame(drawChart);
  }
  function populateForm(form, settings) {
    if (dirtyForms.has(form.id)) return;
    Object.keys(settings).forEach(function (key) {
      const field = form.elements.namedItem(key);
      if (!field || field === document.activeElement) return;
      if (field.type === 'checkbox') field.checked = Boolean(settings[key]);
      else field.value = settings[key] === null || settings[key] === undefined ? '' : String(settings[key]);
    });
  }
  function updateSettings(settings) {
    if (!settings) return;
    state.settings = Object.assign({}, state.settings, settings);
    populateForm($('#model-form'), state.settings);
    populateForm($('#settings-form'), state.settings);
    const context = safeNumber(state.settings.context_length);
    text('#metric-context', context === null ? '—' : formatNumber(context));
    const publicUrl = String(state.settings.public_url || '').replace(/\/+$/, '');
    const suppliedEndpoint = state.status && state.status.api_endpoint;
    const endpoint = suppliedEndpoint || (publicUrl ? publicUrl + '/v1' : '');
    const displayEndpoint = endpoint || '配置访问地址后显示';
    text('#overview-endpoint', displayEndpoint);
    text('#api-base-url', displayEndpoint);
    const model = modelName(state.status && state.status.agent) || 'strata';
    const example = `from openai import OpenAI

client = OpenAI(
    base_url=${JSON.stringify(endpoint || 'https://你的访问域名/v1')},
    api_key="替换为你创建的 API Key",
    timeout=1800,
)

response = client.chat.completions.create(
    model=${JSON.stringify(model)},
    messages=[{"role": "user", "content": "你好，介绍一下你自己"}],
    max_tokens=512,
    stream=True,
)
for chunk in response:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="")
# 当前服务保持连接，等模型完成后一次返回内容。`;
    text('#api-example-code', example);
  }
  async function refreshStatus() {
    if (!state.authenticated || state.pollBusy) return;
    state.pollBusy = true;
    $('#refresh-button').classList.add('refreshing');
    try {
      const payload = await api('/api/status');
      if (!state.authenticated) return;
      state.status = payload;
      state.connectionFresh = true;
      updateSettings(payload.settings || {});
      renderStatus(payload);
      if (state.currentView === 'logs') {
        if (Array.isArray(payload.logs)) { state.logs = payload.logs; renderLogs(); }
        else await refreshLogs();
      }
      text('#updated-at', '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      $('#server-dot').className = 'status-dot good';
      text('#server-state', '管理服务在线');
      text('#server-detail', payload.server && (payload.server.hostname || payload.server.name) || '请求转发服务就绪');
    } catch (error) {
      state.connectionFresh = false;
      updateActionAvailability();
      $('#server-dot').className = 'status-dot bad';
      text('#server-state', '连接中断');
      text('#server-detail', error.message);
      ['#connection-badge', '#model-state', '#chat-state'].forEach(function (selector) { setBadge(selector, '状态同步失败', 'bad'); });
      if (state.status) text('#updated-at', '数据已过期 · 等待重连');
      else text('#updated-at', '同步失败 · 等待重连');
    } finally {
      state.pollBusy = false;
      $('#refresh-button').classList.remove('refreshing');
    }
  }
  function renderStatus(payload) {
    const agent = payload.agent;
    const online = isAgentOnline();
    const name = modelName(agent);
    const status = agent ? String(agent.status || 'online').toLowerCase() : 'offline';
    const label = !online ? 'Kaggle 离线' : status === 'loading' || status === 'starting' ? '模型加载中' : status === 'error' ? '模型异常' : modelReady() ? '模型已就绪' : '代理在线';
    const style = !online ? 'neutral' : status === 'error' ? 'bad' : status === 'loading' || status === 'starting' ? 'warn' : 'good';
    setBadge('#connection-badge', label, style);
    setBadge('#model-state', label, style);
    setBadge('#chat-state', modelReady() ? '可以发送' : label, modelReady() ? 'good' : style);
    $('#onboarding-panel').hidden = online;
    text('#metric-model', name ? name.split('/').pop() : '尚未加载');
    $('#metric-model').title = name;
    text('#metric-model-detail', name ? (online ? label : '上次模型 · 代理已离线') : '连接后显示真实模型');
    text('#session-model', name || '尚未加载');
    text('#chat-model-label', name || '模型尚未就绪');
    text('#session-last-seen', agent && agent.last_seen ? formatDate(agent.last_seen, true) : '—');
    text('#metric-runtime', agent ? formatDuration(agent.runtime_seconds) : '—');
    text('#metric-runtime-detail', online ? '当前 Kaggle 代理会话' : agent ? '上次会话 · 已离线' : 'Kaggle 会话尚未连接');
    if (!state.chatBusy) text('#chat-request-status', modelReady() ? '当前页对话 · 可发送请求' : '连接并加载模型后即可发送');
    renderGpus(agent && agent.metrics, online);
    renderJobs(payload.recent_jobs || []);
    renderServer(payload.server || {});
    updateActionAvailability();
  }
  function renderGpus(metrics, online) {
    const gpus = metrics && Array.isArray(metrics.gpus) ? metrics.gpus : [];
    const holder = $('#gpu-cards');
    holder.replaceChildren();
    text('#gpu-count', gpus.length ? gpus.length + ' 张设备' + (online ? '' : ' · 上次数据') : '无设备');
    let used = 0, total = 0, memoryValid = false;
    const utilities = [];
    gpus.forEach(function (gpu, index) {
      const memoryUsed = safeNumber(gpu.memory_used_mb);
      const memoryTotal = safeNumber(gpu.memory_total_mb);
      const utilization = safeNumber(gpu.utilization);
      const temperature = safeNumber(gpu.temperature);
      if (memoryUsed !== null && memoryTotal !== null) { used += memoryUsed; total += memoryTotal; memoryValid = true; }
      if (utilization !== null) utilities.push(Math.min(100, Math.max(0, utilization)));
      const card = node('div', 'gpu-card');
      const head = node('div', 'gpu-card-header');
      head.append(node('span', '', 'GPU ' + (gpu.index === undefined ? index : gpu.index)), icon('chip'));
      const name = node('h3', '', gpu.name || '未报告设备名称');
      const vram = node('p', 'gpu-vram', formatMemory(memoryUsed) + ' / ' + formatMemory(memoryTotal));
      const track = node('div', 'progress-track');
      const fill = node('div', 'progress-fill');
      fill.style.width = memoryTotal > 0 && memoryUsed !== null ? Math.min(100, memoryUsed / memoryTotal * 100) + '%' : '0%';
      track.append(fill);
      const details = node('div', 'gpu-details');
      const utility = node('div');
      utility.append(node('span', '', '利用率'));
      const utilityValue = node('strong', '', utilization === null ? '—' : Math.round(utilization));
      if (utilization !== null) utilityValue.append(node('small', '', '%'));
      utility.append(utilityValue);
      const thermal = node('div');
      thermal.append(node('span', '', '温度'));
      const thermalValue = node('strong', '', temperature === null ? '—' : Math.round(temperature));
      if (temperature !== null) thermalValue.append(node('small', '', '°C'));
      thermal.append(thermalValue);
      details.append(utility, thermal);
      card.append(head, name, vram, track, details);
      holder.append(card);
    });
    if (!gpus.length) {
      const empty = node('div', 'empty-state compact');
      empty.append(icon('chip'), node('h3', '', '等待 GPU 数据'), node('p', '', '代理连接后，这里会显示实际显存、利用率和温度。'));
      holder.append(empty);
    }
    text('#metric-vram', memoryValid ? (used / 1024).toFixed(1) : '—');
    text('#metric-vram-unit', memoryValid ? 'GiB' : '');
    text('#metric-vram-detail', memoryValid ? '共 ' + (total / 1024).toFixed(1) + ' GiB · 各卡显存独立' + (online ? '' : ' · 上次数据') : '等待 GPU 遥测');
    const ramUsed = safeNumber(metrics && metrics.ram_used_mb), ramTotal = safeNumber(metrics && metrics.ram_total_mb);
    text('#ram-summary', ramUsed === null || ramTotal === null ? '—' : formatMemory(ramUsed) + ' / ' + formatMemory(ramTotal));
    const ramPercent = ramTotal > 0 && ramUsed !== null ? Math.min(100, ramUsed / ramTotal * 100) : null;
    $('#ram-progress').style.width = (ramPercent || 0) + '%';
    if (online && (utilities.length || ramPercent !== null)) {
      state.samples.push({ time: Date.now(), gpu: utilities.length ? utilities.reduce(function (a, b) { return a + b; }, 0) / utilities.length : null, ram: ramPercent });
      if (state.samples.length > 72) state.samples.shift();
    }
    $('#chart-empty').hidden = state.samples.length > 1;
    text('#chart-range', state.samples.length ? '本页最近 ' + Math.max(1, Math.round((Date.now() - state.samples[0].time) / 1000)) + ' 秒' : '尚无历史');
    drawChart();
  }
  function drawChart() {
    const canvas = $('#resource-chart');
    if (!canvas || !canvas.clientWidth || !canvas.clientHeight) return;
    const scale = Math.max(1, window.devicePixelRatio || 1);
    const width = canvas.clientWidth, height = canvas.clientHeight;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(scale, scale);
    const top = 8, bottom = height - 9;
    ctx.strokeStyle = '#243246'; ctx.lineWidth = 1;
    for (let i = 0; i < 4; i++) { const y = top + (bottom - top) / 3 * i; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
    const samples = state.samples;
    if (samples.length < 2) return;
    ['gpu', 'ram'].forEach(function (key) {
      ctx.strokeStyle = key === 'gpu' ? '#5ee1bc' : '#72a9f7'; ctx.lineWidth = 1.8;
      ctx.beginPath(); let started = false;
      samples.forEach(function (sample, i) {
        const value = sample[key];
        if (value === null) { started = false; return; }
        const x = 2 + i / (samples.length - 1) * (width - 4), y = bottom - value / 100 * (bottom - top);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  }
  function jobStatus(status) {
    const map = { pending: ['排队中', 'neutral'], queued: ['排队中', 'neutral'], running: ['执行中', 'warn'], processing: ['执行中', 'warn'], completed: ['已完成', 'good'], succeeded: ['已完成', 'good'], error: ['失败', 'bad'], failed: ['失败', 'bad'], cancelled: ['已取消', 'neutral'], expired: ['已过期', 'bad'] };
    return map[String(status || '').toLowerCase()] || [status || '未知', 'neutral'];
  }
  function renderJobs(jobs) {
    const holder = $('#recent-jobs'); holder.replaceChildren();
    if (!Array.isArray(jobs) || !jobs.length) { const empty = node('div', 'empty-state compact'); empty.append(node('p', '', '还没有任务记录')); holder.append(empty); return; }
    jobs.slice(0, 5).forEach(function (job) {
      const item = node('div', 'activity-item'), mark = node('span', 'activity-icon'), content = node('div');
      const action = job.action || job.type || '推理请求';
      const actionLabels = { chat: '对话推理', completion: '对话推理', load_model: '加载模型', stop_model: '停止模型', shutdown: '结束代理' };
      mark.append(icon(action === 'chat' || action === 'completion' ? 'chat' : 'chip'));
      content.append(node('strong', '', job.name || actionLabels[action] || action), node('small', '', formatDate(job.created_at || job.timestamp, true) + (job.id ? ' · ' + String(job.id).slice(0, 8) : '')));
      const status = jobStatus(job.status);
      item.append(mark, content, node('span', 'badge ' + status[1], status[0]));
      holder.append(item);
    });
  }
  function renderServer(server) {
    const pairs = [['状态', server.status || '在线'], ['主机', server.hostname || server.name], ['运行时长', server.uptime_seconds !== undefined ? formatDuration(server.uptime_seconds) : server.uptime], ['版本', server.version], ['架构', server.architecture || server.arch], ['时区', server.timezone]];
    const holder = $('#server-info'); holder.replaceChildren();
    pairs.forEach(function (pair) { if (pair[1] === undefined || pair[1] === null || pair[1] === '') return; const el = node('div'); el.append(node('dt', '', pair[0]), node('dd', '', pair[1])); holder.append(el); });
  }
  async function saveSettings(form, feedback) {
    const fields = {};
    Array.from(form.elements).forEach(function (field) {
      if (!field.name) return;
      fields[field.name] = field.type === 'checkbox' ? field.checked : field.type === 'number' ? Number(field.value) : field.value.trim();
    });
    const submit = $('button[type=submit]', form); submit.disabled = true;
    inlineStatus(feedback, '正在保存…');
    try {
      const result = await api('/api/settings', { method: 'PUT', body: fields });
      dirtyForms.delete(form.id);
      updateSettings(result.settings || result);
      inlineStatus(feedback, '已保存');
      toast('配置已保存');
      await refreshStatus();
    } catch (error) { inlineStatus(feedback, error.message, true); }
    finally { submit.disabled = false; }
  }
  async function refreshKeys() {
    if (!state.authenticated) return;
    try {
      const payload = await api('/api/keys');
      const keys = (payload.keys || []).filter(function (key) { return !key.revoked; });
      text('#key-count', keys.length + ' 个密钥');
      $('#keys-error').textContent = '';
      const body = $('#keys-table-body'); body.replaceChildren();
      if (!keys.length) { const row = node('tr'), cell = node('td', 'table-empty', '暂无 API Key'); cell.colSpan = 5; row.append(cell); body.append(row); return; }
      keys.forEach(function (key) {
        const row = node('tr');
        row.append(node('td', '', key.name || '未命名'), node('td', '', (key.prefix || '—') + '…'), node('td', '', formatDate(key.created_at)), node('td', '', key.last_used_at ? formatDate(key.last_used_at) : '尚未使用'));
        const action = node('td'), revoke = node('button', 'revoke-button', '撤销');
        revoke.type = 'button';
        revoke.addEventListener('click', async function () {
          const confirmed = await confirmAction('撤销 API Key', '撤销“' + (key.name || '未命名') + '”后，使用这枚密钥的客户端将无法继续调用。', '撤销密钥');
          if (!confirmed) return;
          revoke.disabled = true;
          try { await api('/api/keys/' + encodeURIComponent(key.id), { method: 'DELETE' }); toast('密钥已撤销'); await refreshKeys(); }
          catch (error) { toast(error.message, 'error'); revoke.disabled = false; }
        });
        action.append(revoke); row.append(action); body.append(row);
      });
    } catch (error) { $('#keys-error').textContent = error.message; }
  }
  async function createKey(event) {
    event.preventDefault();
    const submit = $('button[type=submit]', $('#key-form')); submit.disabled = true;
    try {
      const payload = await api('/api/keys', { method: 'POST', body: { name: $('#key-name').value.trim() } });
      if (!payload.key) throw new Error('服务没有返回完整密钥，请检查响应。');
      text('#new-key-value', payload.key);
      $('#new-key-panel').hidden = false;
      $('#key-name').value = '';
      toast('密钥已创建，请立即保存');
      await refreshKeys();
    } catch (error) { toast(error.message, 'error'); }
    finally { submit.disabled = false; }
  }
  function hideNewKey() { text('#new-key-value', ''); $('#new-key-panel').hidden = true; }
  async function copyText(target) {
    const value = $(target) && $(target).textContent;
    if (!value || value === '配置访问地址后显示') { toast('请先配置访问地址', 'info'); return; }
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
      else {
        const field = document.createElement('textarea'); field.value = value; field.style.position = 'fixed'; field.style.opacity = '0'; document.body.append(field); field.select();
        const copied = document.execCommand('copy'); field.remove(); if (!copied) throw new Error('复制失败');
      }
      toast('已复制到剪贴板');
    } catch (error) { toast('无法自动复制，请手动选择内容复制', 'info'); }
  }
  async function refreshLogs() {
    if (!state.authenticated || document.hidden) return;
    try { const result = await api('/api/logs'); state.logs = result.logs || []; renderLogs(); }
    catch (error) { toast(error.message, 'error'); }
  }
  function renderLogs() {
    const query = $('#log-filter').value.toLowerCase().trim();
    const logs = state.logs.filter(function (log) { return !query || [log.timestamp, log.level, log.message].join(' ').toLowerCase().includes(query); });
    text('#log-count', logs.length + ' 条记录');
    const holder = $('#log-lines'); const nearBottom = holder.scrollHeight - holder.clientHeight - holder.scrollTop < 70; holder.replaceChildren();
    if (!logs.length) {
      const empty = node('div', 'empty-state');
      empty.append(icon('lines'), node('h3', '', query ? '没有匹配记录' : '暂无运行日志'), node('p', '', query ? '尝试修改筛选词。' : '连接代理或发送任务后，记录会出现在这里。'));
      holder.append(empty); return;
    }
    logs.forEach(function (log) {
      const row = node('div', 'log-line'), level = String(log.level || 'info').toLowerCase();
      row.append(node('time', '', formatDate(log.timestamp, true)), node('span', 'log-level ' + (['error', 'critical', 'warn', 'warning', 'info', 'debug'].includes(level) ? level : 'info'), level), node('span', 'log-message', log.message || ''));
      holder.append(row);
    });
    if (nearBottom) holder.scrollTop = holder.scrollHeight;
  }
  function confirmAction(title, description, label) {
    return new Promise(function (resolve) {
      const dialog = $('#confirm-dialog');
      text('#confirm-title', title); text('#confirm-description', description); text('#confirm-proceed', label || '确认');
      dialog.returnValue = 'cancel';
      const onClose = function () { dialog.removeEventListener('close', onClose); resolve(dialog.returnValue === 'confirm'); };
      dialog.addEventListener('close', onClose); dialog.showModal();
    });
  }
  async function waitForJob(jobId, onProgress) {
    const start = Date.now();
    while (state.authenticated && Date.now() - start < 30 * 60 * 1000) {
      if (document.hidden) {
        await new Promise(function (resolve) {
          function visible() { if (!document.hidden) { document.removeEventListener('visibilitychange', visible); resolve(); } }
          document.addEventListener('visibilitychange', visible);
        });
      }
      const job = await api('/api/jobs/' + encodeURIComponent(jobId));
      const status = String(job.status || '').toLowerCase();
      if (['completed', 'succeeded', 'done'].includes(status)) return job.result || {};
      if (['failed', 'error', 'cancelled', 'expired'].includes(status)) throw new Error(errorText(job, '任务未完成：' + (jobStatus(status)[0])));
      if (onProgress) onProgress(job, Math.round((Date.now() - start) / 1000));
      await new Promise(function (resolve) { setTimeout(resolve, 1200); });
    }
    throw new Error('等待超过 30 分钟。任务可能仍在服务器执行，请查看日志；此页面不会自动重复提交。');
  }
  async function agentCommand(action, button) {
    const descriptions = {
      load_model: ['加载推理模型', '按照已保存的配置，在当前 Kaggle 会话中加载模型。加载过程会使用内存、显存和 GPU 配额。', '加载模型'],
      stop_model: ['停止推理进程', '停止当前 Strata 模型进程。已连接的代理会保留，之后可以重新加载模型。', '停止模型'],
      shutdown: ['结束代理会话', '结束 Kaggle 代理连接。再次使用时需要重新运行接入 Notebook。', '结束会话']
    };
    if (state.commandBusy || !isAgentOnline()) return;
    const info = descriptions[action];
    if (!info || !(await confirmAction(info[0], info[1], info[2]))) return;
    state.commandBusy = true; updateActionAvailability();
    inlineStatus('#command-result', '正在提交命令…');
    try {
      const result = await api('/api/agent/command', { method: 'POST', body: { action: action } });
      if (result.job_id) {
        inlineStatus('#command-result', '命令已提交，等待 Kaggle 执行…');
        const completed = await waitForJob(result.job_id, function (job, elapsed) { inlineStatus('#command-result', jobStatus(job.status)[0] + ' · ' + formatDuration(elapsed)); });
        inlineStatus('#command-result', completed.message || '命令执行完成');
      } else inlineStatus('#command-result', result.message || '命令已提交');
      toast('操作已完成');
      await refreshStatus();
    } catch (error) { inlineStatus('#command-result', error.message, true); }
    finally { state.commandBusy = false; updateActionAvailability(); }
  }
  function appendMessage(role, message, pending) {
    $('#chat-welcome').hidden = true;
    const row = node('div', 'chat-message ' + role), avatar = node('span', 'message-avatar', role === 'user' ? 'YOU' : 'AI'), content = node('div', 'message-content'), head = node('div', 'message-heading');
    head.append(node('strong', '', role === 'user' ? '你' : '模型'), node('span', '', new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })));
    const body = node('div', pending ? 'message-pending' : 'message-body', pending ? undefined : message);
    if (pending) body.append(node('span', 'spinner'), node('span', '', '请求已提交，等待模型回答…'));
    content.append(head, body); row.append(avatar, content); $('#chat-messages').append(row); scrollChat();
    return { row: row, body: body, content: content };
  }
  function scrollChat() { const holder = $('#chat-messages'); holder.scrollTop = holder.scrollHeight; }
  function responseContent(result) {
    if (typeof result === 'string') return { content: result };
    if (result.choices && result.choices[0]) return result.choices[0].message || { content: result.choices[0].text || '' };
    if (result.message && typeof result.message === 'object') return result.message;
    return { content: result.content || result.response || result.text || '', reasoning_content: result.reasoning_content, tool_calls: result.tool_calls };
  }
  async function sendChat(event) {
    event.preventDefault();
    if (state.chatBusy || !modelReady()) return;
    const prompt = $('#chat-input').value.trim(); if (!prompt) return;
    const maxTokens = safeNumber($('#chat-max-tokens').value), temperature = safeNumber($('#chat-temperature').value);
    if (maxTokens === null || maxTokens < 1 || maxTokens > 32768 || temperature === null || temperature < 0 || temperature > 2) { toast('请检查最大输出 tokens 和 Temperature', 'error'); return; }
    state.chatBusy = true; updateActionAvailability();
    state.history.push({ role: 'user', content: prompt });
    appendMessage('user', prompt);
    $('#chat-input').value = '';
    const responseEl = appendMessage('assistant', '', true);
    text('#chat-request-status', '正在提交…');
    try {
      const task = await api('/api/chat', { method: 'POST', body: { messages: state.history.slice(), max_tokens: maxTokens, temperature: temperature } });
      if (!task.job_id) throw new Error('服务没有返回任务 ID。请求不会自动重试，请检查日志。');
      state.chatJob = task.job_id;
      const result = await waitForJob(task.job_id, function (job, elapsed) { text('#chat-request-status', jobStatus(job.status)[0] + ' · 已等待 ' + formatDuration(elapsed)); });
      const message = responseContent(result);
      let content = typeof message.content === 'string' ? message.content : message.content ? JSON.stringify(message.content, null, 2) : '';
      if (message.tool_calls && message.tool_calls.length) content += (content ? '\n\n' : '') + '模型请求调用工具：\n' + JSON.stringify(message.tool_calls, null, 2) + '\n\n此测试页仅展示请求，不自动执行工具。';
      if (!content) content = '模型返回了空内容，请查看运行日志。';
      responseEl.body.className = 'message-body'; responseEl.body.textContent = content;
      const reasoning = message.reasoning_content || message.reasoning;
      if (reasoning) {
        const details = node('details', 'message-reasoning');
        details.append(node('summary', '', '查看模型思考过程'), node('div', '', typeof reasoning === 'string' ? reasoning : JSON.stringify(reasoning)));
        responseEl.content.insertBefore(details, responseEl.body);
      }
      const saved = { role: 'assistant', content: typeof message.content === 'string' ? message.content : '' };
      if (message.tool_calls && message.tool_calls.length) {
        state.history = state.history.slice(0, -1);
        toast('模型返回了工具调用；本页不执行工具，下一轮不会携带未完成的调用。', 'info');
      } else state.history.push(saved);
      const usage = result.usage || {};
      text('#usage-prompt', formatNumber(usage.prompt_tokens ?? usage.input_tokens));
      text('#usage-completion', formatNumber(usage.completion_tokens ?? usage.output_tokens));
      text('#usage-total', formatNumber(usage.total_tokens));
      text('#chat-request-status', '回答完成' + (result.usage ? ' · 实际用量见右侧' : ' · 模型未报告用量'));
      scrollChat(); await refreshStatus();
    } catch (error) {
      responseEl.body.className = 'message-body error'; responseEl.body.textContent = error.message;
      if (state.history.length && state.history[state.history.length - 1].role === 'user') state.history.pop();
      text('#chat-request-status', '请求未完成 · 不会自动重复提交');
      scrollChat();
    } finally { state.chatBusy = false; state.chatJob = null; updateActionAvailability(); }
  }
  async function showBootstrap() {
    const button = $('#bootstrap-button'); button.disabled = true;
    try {
      const payload = await api('/api/bootstrap');
      const cfg = payload.config || {};
      const safeCfg = {};
      ['public_url', 'server_url', 'context_length', 'model_path', 'strata_path', 'kv_type', 'low_ram', 'experimental_projection'].forEach(function (name) { if (cfg[name] !== undefined) safeCfg[name] = cfg[name]; });
      const lines = [
        '接入 Notebook：' + (payload.notebook_url || '/api/notebook'),
        '代理脚本：' + (payload.agent_download_url || '/api/agent-download'),
        '',
        JSON.stringify(safeCfg, null, 2),
        '',
        '在连接设置下载 Worker 凭据，保存到 Kaggle Secrets 的 STRATA_WORKER_KEY；Notebook 不内嵌密钥。'
      ];
      text('#bootstrap-result', lines.join('\n')); $('#bootstrap-result').hidden = false;
    } catch (error) { toast(error.message, 'error'); }
    finally { button.disabled = false; }
  }

  $$('.nav-item').forEach(function (el) { el.addEventListener('click', function () { changeView(el.dataset.view); }); });
  $$('[data-open-view]').forEach(function (el) { el.addEventListener('click', function () { changeView(el.dataset.openView); }); });
  $$('[data-copy-target]').forEach(function (el) { el.addEventListener('click', function () { copyText('#' + el.dataset.copyTarget); }); });
  $$('[data-agent-action]').forEach(function (el) { el.addEventListener('click', function () { agentCommand(el.dataset.agentAction, el); }); });
  $('#menu-button').addEventListener('click', function () { const open = $('#sidebar').classList.toggle('open'); $('#sidebar-backdrop').hidden = !open; $('#menu-button').setAttribute('aria-expanded', String(open)); });
  $('#sidebar-backdrop').addEventListener('click', closeSidebar);
  $('#refresh-button').addEventListener('click', refreshStatus);
  $('#auth-retry').addEventListener('click', checkAuth);
  $('#auth-form').addEventListener('submit', async function (event) {
    event.preventDefault(); if (state.pendingAuth) return;
    state.pendingAuth = true; $('#auth-submit').disabled = true; $('#auth-error').textContent = '';
    const credentials = { username: $('#auth-username').value.trim(), password: $('#auth-password').value };
    if (state.setup) credentials.setup_token = $('#setup-token').value.trim();
    try {
      await api(state.setup ? '/api/setup' : '/api/login', { method: 'POST', body: credentials });
      $('#auth-password').value = ''; $('#setup-token').value = '';
      const me = await api('/api/me');
      if (!me.authenticated) { if (state.setup && !me.setup_required) { showAuth(false); toast('账号已创建，请登录', 'info'); } else throw new Error('登录未建立，请检查用户名与密码。'); }
      else await showApp(me.username);
    } catch (error) { $('#auth-error').textContent = error.message; }
    finally { state.pendingAuth = false; $('#auth-submit').disabled = false; }
  });
  $('#logout-button').addEventListener('click', async function () {
    try { await api('/api/logout', { method: 'POST' }); hideNewKey(); state.history = []; state.samples = []; state.status = null; showAuth(false); }
    catch (error) { toast(error.message, 'error'); }
  });
  $('#model-form').addEventListener('submit', function (event) { event.preventDefault(); saveSettings(event.currentTarget, '#model-save-status'); });
  $('#settings-form').addEventListener('submit', function (event) { event.preventDefault(); saveSettings(event.currentTarget, '#settings-save-status'); });
  ['#model-form', '#settings-form'].forEach(function (selector) {
    $(selector).addEventListener('input', function () { dirtyForms.add(this.id); });
    $(selector).addEventListener('change', function () { dirtyForms.add(this.id); });
  });
  $('#key-form').addEventListener('submit', createKey);
  $('#dismiss-key-button').addEventListener('click', hideNewKey);
  $('#logs-refresh-button').addEventListener('click', refreshLogs);
  $('#log-filter').addEventListener('input', renderLogs);
  $('#chat-form').addEventListener('submit', sendChat);
  $('#chat-input').addEventListener('keydown', function (event) { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); if (!$('#chat-send-button').disabled) $('#chat-form').requestSubmit(); } });
  $('#clear-chat-button').addEventListener('click', function () {
    if (state.chatBusy) return;
    state.history = []; $$('.chat-message', $('#chat-messages')).forEach(function (el) { el.remove(); }); $('#chat-welcome').hidden = false;
    text('#usage-prompt', '—'); text('#usage-completion', '—'); text('#usage-total', '—');
    toast('本页对话已清空');
  });
  $('#bootstrap-button').addEventListener('click', showBootstrap);
  window.addEventListener('hashchange', function () { if (state.authenticated) changeView(location.hash.slice(1)); });
  window.addEventListener('resize', function () { requestAnimationFrame(drawChart); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && state.authenticated) refreshStatus(); });
  window.setInterval(function () { if (state.authenticated && !document.hidden) refreshStatus(); }, 5000);
  checkAuth();
})();
