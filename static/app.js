(function () {
  'use strict';

  const $ = function (selector, scope) { return (scope || document).querySelector(selector); };
  const $$ = function (selector, scope) { return Array.from((scope || document).querySelectorAll(selector)); };
  const labels = { overview: '算力总览', quotas: 'Kaggle 配额', models: '模型与会话', chat: '对话测试', keys: 'API 访问', requests: '请求监控', launches: '启动统计', logs: '运行日志', settings: '连接设置' };
  const CHAT_IMAGE_LIMIT = 4;
  const CHAT_IMAGE_TOTAL_BYTES = 2 * 1024 * 1024;
  const presetSelect = document.querySelector('#model-preset');
  const mmprojInput = document.querySelector('input[name="llama_mmproj_path"]');
  if (mmprojInput) {
    mmprojInput.placeholder = '留空按预设决定';
    const hint = mmprojInput.parentElement && mmprojInput.parentElement.querySelector('small');
    if (hint) hint.textContent = 'Qwen 与 Ornith 视觉预设自动加载配套投影；高速 Ornith 不加载。';
  }
  const state = { authenticated: false, setup: false, currentView: 'overview', status: null, runtime: null, settings: {}, presets: [], history: [], chatImages: [], chatImageBusy: false, samples: [], logs: [], requests: { summary: {}, rows: [] }, quotas: null, requestPeriod: '7d', requestsBusy: false, launches: { summary: {}, rows: [] }, launchPeriod: '30d', launchesBusy: false, launchesAt: 0, quotasBusy: false, pollBusy: false, chatBusy: false, chatJob: null, chatAbort: null, chatStopping: false, commandBusy: false, runtimeBusy: false, pendingAuth: false, connectionFresh: false };
  const dirtyForms = new Set();

  function safeNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function text(selector, value) { const el = $(selector); if (el) el.textContent = value === null || value === undefined ? '—' : String(value); }
  function formatNumber(value) { const n = safeNumber(value); return n === null ? '—' : n.toLocaleString('zh-CN', { maximumFractionDigits: 1 }); }
  function formatCompactNumber(value) {
    const n = safeNumber(value);
    if (n === null) return '—';
    if (Math.abs(n) < 1000) return Math.round(n).toLocaleString('zh-CN');
    return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
  }
  function formatRequestDuration(value) {
    const n = safeNumber(value);
    if (n === null) return '—';
    if (n < 1000) return Math.round(n) + ' ms';
    if (n < 60000) return (n / 1000).toFixed(n < 10000 ? 2 : 1) + ' 秒';
    return formatDuration(n / 1000);
  }
  function formatTokensPerSecond(value) {
    const n = safeNumber(value);
    if (n === null) return '—';
    return n.toLocaleString('zh-CN', { minimumFractionDigits: n < 10 ? 2 : 1, maximumFractionDigits: 2 }) + ' tok/s';
  }
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
  function modelVisionReady() {
    const agent = state.status && state.status.agent;
    return Boolean(modelCallable() && agent && agent.model && typeof agent.model === 'object' && agent.model.vision === true);
  }
  // What the preset dropdown offers comes from the server (`presets` in
  // /api/status). An option is a preset plus, for the two fast presets, the
  // KV type: the 136K / 176K entries are F16, the 200K entries are Q8.
  function presetOption(id) { return state.presets.find(function (item) { return item.id === id; }) || null; }
  function listedOption(id) {
    return id && presetSelect ? Array.from(presetSelect.options).find(function (el) { return el.value === id; }) || null : null;
  }
  function optionLabel(id) {
    const option = presetOption(id);
    if (option) return option.label + '（' + option.detail + '）';
    const listed = listedOption(id);
    return listed ? listed.textContent : id || '未选择';
  }
  function optionFor(preset, kvType) {
    // The server's rule: a fast preset running Q8 KV is its 200K option.
    return kvType === 'q8_0' && listedOption(preset + '_200k') ? preset + '_200k' : preset;
  }
  function kvLabel(value) { return { f16: 'F16 KV', q8_0: 'Q8 KV' }[value] || ''; }
  function selectedOption() {
    return presetSelect && presetSelect.value || state.settings.llama_preset_option || 'qwen38_27b';
  }
  function loadedOption() {
    const agent = state.status && state.status.agent;
    const model = agent && agent.model && typeof agent.model === 'object' ? agent.model : {};
    let preset = model.preset || model.profile || model.llama_preset;
    if (!listedOption(preset)) {
      const name = modelName(agent).toLowerCase();
      preset = name.includes('ornith') ? 'ornith_35b_a3b' : name.includes('qwen3.8') || name.includes('qwen3-8') ? 'qwen38_27b' : '';
    }
    // Only the fast presets report their KV type. A model that does not is
    // taken to hold the saved type, which is also how the server reads it.
    return preset ? optionFor(preset, model.kv_type || state.settings.llama_kv_type) : '';
  }
  function modelCallable() {
    if (!modelReady() || modelSwitchRequired()) return false;
    const selectedBackend = state.settings.backend || 'strata';
    const agent = state.status && state.status.agent;
    const model = agent && agent.model && typeof agent.model === 'object' ? agent.model : {};
    const loadedBackend = String(model.backend || '').toLowerCase();
    if (selectedBackend !== 'llama_cpp') {
      // Older Strata workers did not report a backend, so an empty value is
      // compatible only with Strata. A reported llama.cpp model must never be
      // presented as callable after the saved target changes to Strata.
      return !loadedBackend || loadedBackend === selectedBackend;
    }
    return loadedBackend === 'llama_cpp'
      && loadedOption() === (state.settings.llama_preset_option || 'qwen38_27b')
      && Number(model.parallel || 1) === Number(state.settings.llama_parallel || 1);
  }
  function modelTransitioning() {
    const agent = state.status && state.status.agent;
    const agentStatus = String(agent && agent.status || '').toLowerCase();
    const modelStatus = String(agent && agent.model && typeof agent.model === 'object' && agent.model.status || '').toLowerCase();
    const runtimeStatus = runtimeState(state.runtime || state.status && state.status.runtime);
    return state.commandBusy || ['loading', 'starting', 'busy', 'stopping'].includes(agentStatus)
      || ['loading', 'starting', 'stopping'].includes(modelStatus)
      || ['loading_model', 'verifying_model'].includes(runtimeStatus);
  }
  function renderModelTarget() {
    const backendField = $('#model-form') && $('#model-form').elements.namedItem('backend');
    const backend = backendField && backendField.value || state.settings.backend || 'strata';
    const unsaved = dirtyForms.has('model-form');
    const parallelField = $('#model-form').elements.namedItem('llama_parallel');
    text('#session-target', backend === 'llama_cpp' ? optionLabel(selectedOption()) + ' · ' + (parallelField.value || 1) + ' 路请求' + (unsaved ? ' · 未保存' : '') : 'Strata');
  }
  function runtimeState(runtime) { return String(runtime && runtime.state || (runtime && runtime.configured ? 'stopped' : 'unconfigured')).toLowerCase(); }
  function modelSwitchRequired() { return runtimeState(state.runtime || state.status && state.status.runtime) === 'model_switch_required'; }
  function runtimeActive(runtime) { return ['queued_local', 'submitting', 'queued', 'pending', 'running', 'waiting_worker', 'unknown', 'loading_model', 'verifying_model', 'stop_requested', 'stopping'].includes(runtimeState(runtime)); }
  function runtimePhase(runtime) {
    const phases = {
      unconfigured: ['未配置自动启动', 'neutral', '需要先在服务端配置专用 Kaggle Notebook。'],
      stopped: ['算力已停止', 'neutral', '点击启动算力后，管理站会提交专用私有 Notebook。'],
      queued_local: ['启动请求已受理', 'warn', '正在准备专用 Notebook，尚未重复提交 Kaggle。'],
      submitting: ['正在提交 Notebook', 'warn', '正在通过 Kaggle 官方接口创建本次运行。'],
      queued: ['Kaggle 排队中', 'warn', '已提交运行，正在等待 Kaggle 分配资源。'],
      pending: ['Kaggle 排队中', 'warn', '已提交运行，正在等待 Kaggle 分配资源。'],
      running: ['Kaggle 运行中', 'warn', 'GPU 已启动，正在等待 Worker 连接。'],
      waiting_worker: ['等待 Worker', 'warn', 'GPU 已启动，正在等待 Worker 首次心跳。'],
      unknown: ['提交结果待确认', 'warn', '本次提交结果尚不确定，正在查询 Kaggle 官方状态；不会自动重复提交。'],
      loading_model: ['正在加载模型', 'warn', 'Worker 已连接，正在加载当前模型配置。'],
      verifying_model: ['等待模型确认', 'warn', '加载命令已完成，正在等待 Worker 心跳确认模型可用。'],
      model_stopped: ['模型已停止', 'neutral', 'Worker 仍在线；再次点击加载模型即可恢复推理。'],
      model_switch_required: ['等待切换模型', 'warn', '当前已加载模型与管理面板所选预设不同。'],
      ready: ['模型已就绪', 'good', '算力与模型均已就绪。'],
      stop_requested: ['正在结束运行', 'warn', '已请求 Worker 退出，等待 Kaggle 运行自然结束。'],
      stopping: ['正在结束运行', 'warn', '已请求 Worker 退出，等待 Kaggle 运行自然结束。'],
      failed: ['启动失败', 'bad', '请查看错误后重试；不会自动重复提交。']
    };
    return phases[runtimeState(runtime)] || ['状态未知', 'neutral', '等待 Kaggle 返回最新运行状态。'];
  }
  function setRuntimeButton(button, label, busy) {
    button.replaceChildren();
    if (busy) button.append(node('span', 'spinner'));
    button.append(document.createTextNode(label));
  }
  function runtimeElapsed(runtime) {
    const supplied = safeNumber(runtime && runtime.elapsed_seconds);
    if (supplied !== null) return supplied;
    if (!runtime || !runtime.requested_at) return null;
    const started = new Date(typeof runtime.requested_at === 'number' && runtime.requested_at < 1e12 ? runtime.requested_at * 1000 : runtime.requested_at).getTime();
    return Number.isFinite(started) ? Math.max(0, (Date.now() - started) / 1000) : null;
  }
  function renderRuntime(runtime) {
    state.runtime = runtime || null;
    const item = state.runtime || { configured: false, state: 'unconfigured' };
    const online = isAgentOnline();
    const configured = Boolean(item.configured);
    let phase = runtimePhase(item);
    const agentStatus = String(state.status && state.status.agent && state.status.agent.status || '').toLowerCase();
    const currentModelStatus = String(state.status && state.status.agent && state.status.agent.model && typeof state.status.agent.model === 'object' && state.status.agent.model.status || '').toLowerCase();
    const stopping = ['stop_requested', 'stopping'].includes(runtimeState(item)) || Boolean(item.stop_requested);
    const authoritativeRuntimeState = ['failed', 'verifying_model', 'model_stopped', 'model_switch_required'].includes(runtimeState(item));
    if (stopping || authoritativeRuntimeState) phase = runtimePhase(item);
    else if (modelCallable()) phase = ['模型已就绪', 'good', '算力与模型均已就绪，可以调用 API。'];
    else if (online && (['loading', 'starting'].includes(agentStatus) || ['loading', 'starting'].includes(currentModelStatus))) phase = ['正在加载模型', 'warn', 'Worker 已连接，正在加载当前模型配置。'];
    else if (online) phase = ['Worker 已连接', 'good', 'Worker 已上线，等待模型就绪。'];
    text('#runtime-state', phase[0] + (item.kernel_status ? ' · ' + item.kernel_status : ''));
    const elapsed = runtimeElapsed(item);
    text('#runtime-elapsed', elapsed === null ? '—' : formatDuration(elapsed));
    const banner = $('#onboarding-panel');
    banner.hidden = online;
    banner.classList.toggle('warning', phase[1] === 'warn');
    banner.classList.toggle('error', phase[1] === 'bad');
    text('#runtime-banner-title', configured ? phase[0] : '尚未配置一键启动');
    text('#runtime-banner-detail', item.last_error || item.message || phase[2]);
    $('#runtime-setup-button').hidden = configured;
    const resultMessage = item.last_error || item.message || '';
    inlineStatus('#runtime-result', resultMessage, runtimeState(item) === 'failed' || Boolean(item.last_error));
    text('#runtime-help', configured ? '启动操作会提交专用私有 Notebook 并使用 GPU 配额。模型就绪后连续空闲 8 分钟（没有推理或模型操作）会自动结束运行并释放 GPU，单次最长 3 小时。Kaggle 官方没有运行中的硬取消接口，异常时仍需在 Kaggle 页面处理。' : '服务端尚未配置专用 Notebook 自动启动；可继续使用手动 Kaggle 接入流程。');
    updateActionAvailability();
  }
  function updateActionAvailability() {
    const online = isAgentOnline();
    const agent = state.status && state.status.agent;
    const agentStatus = String(agent && agent.status || '').toLowerCase();
    const modelStatus = String(agent && agent.model && typeof agent.model === 'object' && agent.model.status || '').toLowerCase();
    const transitioning = modelTransitioning();
    const selectedId = selectedOption();
    const loadedId = loadedOption();
    const backendField = $('#model-form') && $('#model-form').elements.namedItem('backend');
    const selectedBackend = backendField && backendField.value || state.settings.backend || 'strata';
    const modelFormDirty = dirtyForms.has('model-form');
    const switchRequired = modelSwitchRequired();
    $$('[data-agent-action]').forEach(function (button) {
      const action = button.dataset.agentAction;
      let available = online;
      if (button.id === 'load-model-button') {
        const switching = modelReady() && selectedBackend === 'llama_cpp';
        button.dataset.agentAction = switching ? 'switch_model' : 'load_model';
        if (modelFormDirty) button.textContent = '请先保存模型选择';
        else if (modelReady() && selectedBackend !== 'llama_cpp') button.textContent = '切换 Strata 前请先停止当前模型';
        else if (transitioning) button.textContent = switching ? '正在切换模型…' : '正在加载模型…';
        else if (switching && loadedId === selectedId && !switchRequired) button.textContent = '已加载所选模型';
        else button.textContent = switching ? '切换到所选模型' : '加载所选模型';
        available = online && !modelFormDirty && !transitioning &&
          (!modelReady() || (switching && (loadedId !== selectedId || switchRequired)));
      } else if (action === 'load_model' || action === 'switch_model') {
        available = online && !modelFormDirty && !transitioning;
      }
      if (action === 'stop_model') available = online && !transitioning && Boolean(modelStatus) && modelStatus !== 'stopped';
      button.disabled = !available || state.commandBusy;
    });
    const runtime = state.runtime || {};
    const configured = Boolean(runtime.configured);
    const active = runtimeActive(runtime);
    const stoppedRequested = ['stop_requested', 'stopping'].includes(runtimeState(runtime)) || Boolean(runtime.stop_requested);
    $$('[data-runtime-action="start"]').forEach(function (button) {
      button.hidden = !configured || online;
      const label = state.runtimeBusy ? '正在提交…' : active ? runtimePhase(runtime)[0] : runtimeState(runtime) === 'failed' ? '重新启动' : '启动算力';
      setRuntimeButton(button, label, state.runtimeBusy || active);
      button.disabled = state.runtimeBusy || online || active || runtime.can_start === false;
    });
    $$('[data-runtime-action="stop"]').forEach(function (button) {
      const canStop = configured && (active || online);
      button.hidden = !canStop;
      setRuntimeButton(button, stoppedRequested ? '正在停止…' : '停止算力', state.runtimeBusy || stoppedRequested);
      button.disabled = state.runtimeBusy || stoppedRequested;
    });
    $('#chat-send-button').disabled = !modelCallable() || transitioning || state.chatBusy;
    $('#chat-send-button').hidden = state.chatBusy;
    $('#chat-stop-button').hidden = !state.chatBusy;
    $('#chat-stop-button').disabled = !state.chatJob || state.chatStopping;
    $('#chat-image-button').disabled = !modelVisionReady() || transitioning || state.chatBusy || state.chatImageBusy;
    $('#clear-chat-button').disabled = state.chatBusy;
    renderModelTarget();
  }
  function showAuth(setup) {
    if (state.chatAbort) state.chatAbort.abort();
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
    if (view === 'quotas') refreshQuotas();
    if (view === 'requests') refreshRequests();
    if (view === 'launches') refreshLaunches();
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
  function syncBackendForm() {
    const form = $('#model-form');
    const field = form.elements.namedItem('backend');
    const backend = field && field.value || state.settings.backend || 'strata';
    $$('[data-backend-panel]', form).forEach(function (el) { el.hidden = el.dataset.backendPanel !== backend; });
    $$('[data-backend-required]', form).forEach(function (el) { el.required = el.dataset.backendRequired === backend; });
    syncLlamaPresetForm();
  }
  function syncLlamaPresetForm() {
    if (!presetSelect || !mmprojInput) return;
    const form = $('#model-form');
    const option = presetOption(presetSelect.value);
    const preset = option ? option.preset : presetSelect.value.replace(/_200k$/, '');
    const textOnlyMtp = preset === 'qwen38_27b_mtp';
    const visionMtp = preset === 'qwen38_27b_mtp_vision';
    const fastText = preset === 'qwen38_27b_fast';
    const fastVision = preset === 'qwen38_27b_fast_vision';
    mmprojInput.disabled = textOnlyMtp || fastText;
    if (textOnlyMtp || fastText) mmprojInput.value = '';
    const hint = mmprojInput.parentElement && mmprojInput.parentElement.querySelector('small');
    if (hint) hint.textContent = fastText
      ? '高速纯文本预设不加载视觉投影。'
      : fastVision
        ? '高速视觉预设自动加载固定投影。'
      : textOnlyMtp
      ? 'MTP n=2 纯文本预设不加载视觉投影。'
      : visionMtp
        ? '实验预设会同时加载内嵌 MTP（n=2）与固定视觉投影；n=4 已实测不增速。'
        : 'Qwen 与 Ornith 视觉预设自动加载配套投影；高速文本预设不加载。';
    text('#model-preset-note', option ? option.note : '');
    // The 136K / 176K and 200K entries differ in the KV type, so for the fast
    // presets it follows the entry instead of being a second choice.
    const pinned = Boolean(option && option.kv_pinned);
    const kvField = form.elements.namedItem('llama_kv_type');
    if (pinned) kvField.value = option.fill.llama_kv_type;
    kvField.disabled = pinned;
    $('#model-kv-note').hidden = !pinned;
    const limit = option && safeNumber(option.max_context);
    form.elements.namedItem('llama_context_length').max = limit || 1048576;
    text('#model-context-note', limit ? '该选项在双 T4 上实测最多 ' + formatNumber(limit) + ' tokens。' : '128K 为默认目标；更长窗口会增加显存占用。');
  }
  function applyPresetOption() {
    // Choosing an entry fills in the parameters it was measured with.
    const form = $('#model-form');
    const option = presetOption(presetSelect.value);
    if (option) {
      Object.keys(option.fill).forEach(function (key) {
        const field = form.elements.namedItem(key);
        if (field) field.value = String(option.fill[key]);
      });
      // A pinned model or projector path belongs to the saved preset: the
      // server clears both when the preset changes, so the form shows that.
      ['llama_model_path', 'llama_mmproj_path'].forEach(function (key) {
        const field = form.elements.namedItem(key);
        if (option.preset !== state.settings.llama_preset) field.value = '';
        else if (!field.value) field.value = state.settings[key] || '';
      });
    }
    syncLlamaPresetForm();
    renderModelTarget();
    updateActionAvailability();
  }
  function updateSettings(settings) {
    if (!settings) return;
    state.settings = Object.assign({ llama_parallel: 1 }, state.settings, settings);
    populateForm($('#model-form'), state.settings);
    populateForm($('#settings-form'), state.settings);
    syncBackendForm();
    const backend = state.settings.backend || 'strata';
    const context = safeNumber(backend === 'llama_cpp' ? state.settings.llama_context_length : state.settings.context_length);
    text('#metric-context', context === null ? '—' : formatNumber(context));
    text('#metric-context-detail', (backend === 'llama_cpp' ? 'llama.cpp · ' + optionLabel(state.settings.llama_preset_option || 'qwen38_27b') : 'Strata') + ' 已保存配置');
    const publicUrl = String(state.settings.public_url || '').replace(/\/+$/, '');
    const suppliedEndpoint = state.status && state.status.api_endpoint;
    const endpoint = suppliedEndpoint || (publicUrl ? publicUrl + '/v1' : '');
    const displayEndpoint = endpoint || '配置访问地址后显示';
    text('#overview-endpoint', displayEndpoint);
    text('#api-base-url', displayEndpoint);
    const model = modelName(state.status && state.status.agent) || (backend === 'llama_cpp' ? state.settings.llama_alias || 'qwen3.8-27b' : 'strata');
    const example = `from openai import OpenAI

client = OpenAI(
    base_url=${JSON.stringify(endpoint || 'https://你的访问域名/v1')},
    api_key="替换为你创建的 API Key",
    timeout=1800,
)

response = client.chat.completions.create(
    model=${JSON.stringify(model)},
    messages=[{"role": "user", "content": "你好，介绍一下你自己"}],
    max_tokens=32768,
    stream=True,
)
for chunk in response:
    if chunk.choices:
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            print(reasoning, end="", flush=True)
        if delta.content:
            print(delta.content, end="", flush=True)
# llama.cpp 实时输出思考与回答；Strata 在推理完成后返回。`;
    text('#api-example-code', example);
    renderModelTarget();
  }
  async function refreshStatus() {
    if (!state.authenticated || state.pollBusy) return;
    state.pollBusy = true;
    $('#refresh-button').classList.add('refreshing');
    try {
      const payload = await api('/api/status');
      if (!state.authenticated) return;
      state.status = payload;
      if (Array.isArray(payload.presets)) state.presets = payload.presets;
      state.runtime = payload.runtime || null;
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
    const reportedModelStatus = String(agent && agent.model && typeof agent.model === 'object' && agent.model.status || '').toLowerCase();
    const runtime = payload.runtime || state.runtime || {};
    const runtimeInfo = runtimePhase(runtime);
    const launching = !online && runtimeActive(runtime);
    const runtimeFailed = !online && runtimeState(runtime) === 'failed';
    const switchRequired = online && runtimeState(runtime) === 'model_switch_required';
    const loading = ['loading', 'starting'].includes(status) || ['loading', 'starting'].includes(reportedModelStatus);
    const label = launching ? runtimeInfo[0] : runtimeFailed ? '启动失败' : !online ? runtimeState(runtime) === 'stopped' ? '算力已停止' : 'Kaggle 离线' : switchRequired ? '等待切换模型' : loading ? '模型加载中' : status === 'error' ? '模型异常' : modelReady() ? '模型已就绪' : '代理在线';
    const style = launching ? 'warn' : runtimeFailed ? 'bad' : !online ? 'neutral' : status === 'error' ? 'bad' : switchRequired || loading ? 'warn' : 'good';
    setBadge('#connection-badge', label, style);
    setBadge('#model-state', label, style);
    setBadge('#chat-state', modelCallable() ? modelVisionReady() ? '文字 / 图片' : '可以发送文字' : label, modelCallable() ? 'good' : style);
    text('#metric-model', name ? name.split('/').pop() : '尚未加载');
    $('#metric-model').title = name;
    const modelInfo = agent && agent.model && typeof agent.model === 'object' ? agent.model : {};
    const backend = modelInfo.backend || payload.settings && payload.settings.backend;
    const backendLabel = backend === 'llama_cpp' ? 'llama.cpp' : backend === 'strata' ? 'Strata' : '';
    const loadedContext = safeNumber(modelInfo.context);
    const loadedTotalContext = safeNumber(modelInfo.context_total) ?? loadedContext;
    const loadedKv = kvLabel(modelInfo.kv_type);
    const speculativeLabel = modelInfo.speculative
      ? (modelInfo.split_mode === 'tensor' ? 'tensor 切分 · ' : '') + 'MTP n=' + (modelInfo.spec_draft_n_max || '—')
        + (String(modelInfo.speculative).includes('ngram-mod') ? ' + ngram' : '')
      : '';
    const parallelLabel = modelInfo.backend === 'llama_cpp' ? (modelInfo.parallel || 1) + ' 路请求' : '';
    const modelDetail = [online ? label : name ? '上次模型 · 代理已离线' : '', backendLabel, parallelLabel, speculativeLabel, modelInfo.vision === true ? '视觉已启用' : '', loadedKv, loadedContext === null ? '' : formatNumber(loadedContext) + ' tokens' + (Number(modelInfo.parallel || 1) > 1 ? ' / 路' : '')].filter(Boolean).join(' · ');
    text('#metric-model-detail', name ? modelDetail : backendLabel ? '等待 ' + backendLabel + ' 加载' : '连接后显示真实模型');
    const loadedPreset = presetOption(modelInfo.preset);
    const loadedPresetLabel = loadedPreset ? loadedPreset.label : '';
    text('#session-model', name ? name + (loadedPresetLabel && !name.toLowerCase().includes(loadedPresetLabel.toLowerCase()) ? ' · ' + loadedPresetLabel : '') + (backendLabel ? ' · ' + backendLabel : '') + (speculativeLabel ? ' · ' + speculativeLabel : '') + (modelInfo.vision === true ? ' · 视觉' : '') + (loadedKv ? ' · ' + loadedKv : '') : '尚未加载');
    text('#chat-model-label', name ? name + (speculativeLabel ? ' · ' + speculativeLabel : '') + (parallelLabel ? ' · ' + parallelLabel : '') + (modelInfo.vision === true ? ' · 可识图' : ' · 仅文字') : '模型尚未就绪');
    text('#session-last-seen', agent && agent.last_seen ? formatDate(agent.last_seen, true) : '—');
    // The window is fixed when llama-server starts. Saving another length for
    // the same entry does not reload the model, so say what is really loaded.
    const savedContext = safeNumber(state.settings.llama_context_length);
    const contextPending = modelCallable() && modelInfo.backend === 'llama_cpp' && loadedTotalContext !== null && savedContext !== null && loadedTotalContext !== savedContext;
    $('#session-hint').hidden = !contextPending;
    text('#session-hint', contextPending ? '已保存的总上下文是 ' + formatNumber(savedContext) + '，当前加载的仍是 ' + formatNumber(loadedTotalContext) + '。要让新窗口生效，先“停止推理进程”，再“加载所选模型”。' : '');
    const launchElapsed = runtimeElapsed(runtime);
    text('#metric-runtime', agent ? formatDuration(agent.runtime_seconds) : launchElapsed === null ? '—' : formatDuration(launchElapsed));
    text('#metric-runtime-detail', online ? '当前 Kaggle 代理会话' : launching ? runtimeInfo[0] : agent ? '上次会话 · 已离线' : 'Kaggle 会话尚未连接');
    if (loadedContext !== null) {
      text('#metric-context', formatNumber(loadedContext));
      text('#metric-context-detail', (backendLabel || '模型') + (Number(modelInfo.parallel || 1) > 1 ? ' 每路窗口 · 总计 ' + formatNumber(loadedTotalContext) : ' 实际加载窗口'));
    }
    if (!state.chatBusy) text('#chat-request-status', modelTransitioning() ? '模型正在加载或切换，请稍候' : modelCallable() ? modelVisionReady() ? '当前页对话 · 可发送文字或图片' : '当前模型未加载视觉投影，仅支持文字' : '连接并加载所选模型后即可发送');
    renderGpus(agent && agent.metrics, online);
    renderJobs(payload.recent_jobs || []);
    renderServer(payload.server || {});
    renderRuntime(runtime);
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
    // The chart takes its colours from the stylesheet, like everything else.
    const palette = getComputedStyle(document.documentElement);
    const colour = function (name, fallback) { return palette.getPropertyValue(name).trim() || fallback; };
    ctx.strokeStyle = colour('--chart-grid', '#e4e9ef'); ctx.lineWidth = 1;
    for (let i = 0; i < 4; i++) { const y = top + (bottom - top) / 3 * i; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
    const samples = state.samples;
    if (samples.length < 2) return;
    ['gpu', 'ram'].forEach(function (key) {
      ctx.strokeStyle = key === 'gpu' ? colour('--chart-gpu', '#12a583') : colour('--chart-ram', '#3d7fe0'); ctx.lineWidth = 1.8;
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
      const actionLabels = { chat: '对话推理', completion: '对话推理', load_model: '加载模型', switch_model: '切换模型', stop_model: '停止模型', shutdown: '结束代理' };
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
      fields[field.name] = field.type === 'checkbox' ? field.checked : (field.type === 'number' || field.name === 'llama_parallel') ? Number(field.value) : field.value.trim();
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
      if (!keys.length) { const row = node('tr'), cell = node('td', 'table-empty', '暂无 API Key'); cell.colSpan = 6; row.append(cell); body.append(row); return; }
      keys.forEach(function (key) {
        const row = node('tr');
        row.append(node('td', '', key.name || '未命名'), node('td', '', (key.prefix || '—') + '…'), node('td', '', formatDate(key.created_at)), node('td', '', key.last_used_at ? formatDate(key.last_used_at) : '尚未使用'));
        const wake = node('td'), wakeBadge = node('span', 'badge ' + (key.can_wake ? 'good' : 'neutral'), key.can_wake ? '允许' : '关闭');
        wake.append(wakeBadge); row.append(wake);
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
      const payload = await api('/api/keys', { method: 'POST', body: { name: $('#key-name').value.trim(), can_wake: $('#key-can-wake').checked } });
      if (!payload.key) throw new Error('服务没有返回完整密钥，请检查响应。');
      text('#new-key-value', payload.key);
      $('#new-key-panel').hidden = false;
      $('#key-name').value = '';
      $('#key-can-wake').checked = false;
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
  function requestStatus(status) {
    const value = String(status || '').toLowerCase();
    if (['completed', 'succeeded', 'success', 'done'].includes(value)) return ['成功', 'good'];
    if (['failed', 'error'].includes(value)) return ['失败', 'bad'];
    if (value === 'expired') return ['已过期', 'bad'];
    if (value === 'cancelled') return ['已取消', 'warn'];
    if (value === 'running') return ['执行中', 'warn'];
    if (['pending', 'queued'].includes(value)) return ['排队中', 'neutral'];
    return [status || '未知', 'neutral'];
  }
  function requestSource(row) {
    if (row.source === 'api') {
      const name = row.key_name || row.api_key_name || '公开 API';
      const prefix = row.key_prefix || row.api_key_prefix;
      return [name, prefix ? prefix + '…' : 'API Key'];
    }
    return ['管理站', '登录管理员'];
  }
  function renderRequests() {
    const payload = state.requests || {};
    const summary = payload.summary || {};
    const rows = Array.isArray(payload.rows) ? payload.rows : Array.isArray(payload.requests) ? payload.requests : [];
    text('#requests-calls', formatCompactNumber(summary.calls || 0));
    text('#requests-succeeded', formatNumber(summary.succeeded || 0));
    text('#requests-failed', formatCompactNumber(summary.failed || 0));
    const rate = safeNumber(summary.success_rate);
    text('#requests-success-rate', rate === null ? '—' : rate.toFixed(1) + '%');
    text('#requests-duration', formatRequestDuration(summary.avg_duration_ms));
    text('#requests-ttft', formatRequestDuration(summary.avg_ttft_ms));
    text('#requests-output-tps', formatTokensPerSecond(summary.avg_decode_tps ?? summary.avg_output_tps ?? summary.output_tps));
    text('#requests-total-tokens', formatCompactNumber(summary.total_tokens || 0));
    text('#requests-prompt-tokens', formatCompactNumber(summary.prompt_tokens || 0));
    text('#requests-completion-tokens', formatCompactNumber(summary.completion_tokens || 0));
    text('#requests-cached-tokens', formatCompactNumber(summary.cached_tokens || 0));
    text('#requests-count', rows.length + ' 条记录');
    const body = $('#requests-table-body'); body.replaceChildren();
    if (!rows.length) {
      const row = node('tr'), cell = node('td', 'table-empty', '当前筛选范围内没有请求记录');
      cell.colSpan = 9; row.append(cell); body.append(row); return;
    }
    rows.forEach(function (item) {
      const row = node('tr'), source = requestSource(item), status = requestStatus(item.status);
      const when = node('td', 'request-time');
      when.append(node('strong', '', formatDate(item.created_at, true)));
      if (item.completed_at) when.append(node('small', '', '完成 ' + formatDate(item.completed_at, true)));
      const origin = node('td', 'request-origin');
      origin.append(node('strong', '', source[0]), node('small', '', source[1]));
      const model = node('td', 'request-model', item.model || '—');
      const statusCell = node('td');
      const badge = node('span', 'badge ' + status[1]); badge.append(node('span', 'status-dot'), document.createTextNode(status[0])); statusCell.append(badge);
      const duration = node('td', 'request-duration', formatRequestDuration(item.duration_ms));
      const performance = node('td', 'request-performance');
      performance.append(node('strong', '', formatTokensPerSecond(item.decode_tps ?? item.output_tps)));
      const phases = [
        ['排队', item.queue_ms],
        ['TTFT', item.ttft_ms],
        ['模型', item.decode_ms],
        ['交付', item.delivery_ms ?? item.generation_ms]
      ].filter(function (phase) { return safeNumber(phase[1]) !== null; });
      if (phases.length) {
        phases.forEach(function (phase) { performance.append(node('small', '', phase[0] + ' ' + formatRequestDuration(phase[1]))); });
      } else {
        performance.append(node('small', '', '旧记录无分段时序'));
      }
      if (safeNumber(item.delivery_tps) !== null) performance.append(node('small', '', '交付 ' + formatTokensPerSecond(item.delivery_tps)));
      const tokens = node('td', 'request-tokens');
      tokens.append(node('strong', '', formatCompactNumber(item.total_tokens || 0)), node('small', '', '↑ ' + formatCompactNumber(item.prompt_tokens || 0) + '  ↓ ' + formatCompactNumber(item.completion_tokens || 0)));
      const cached = node('td', 'request-cached', formatCompactNumber(item.cached_tokens || 0));
      const detail = node('td', 'request-detail');
      detail.append(node('code', '', String(item.job_id || '—').slice(0, 12)));
      if (item.error) detail.append(node('small', 'request-error', item.error));
      row.append(when, origin, model, statusCell, duration, performance, tokens, cached, detail); body.append(row);
    });
  }
  async function refreshRequests() {
    if (!state.authenticated || state.requestsBusy) return;
    state.requestsBusy = true;
    const button = $('#requests-refresh-button'); button.disabled = true; button.classList.add('refreshing');
    $('#requests-error').textContent = '';
    const params = new URLSearchParams({ period: state.requestPeriod, limit: '100' });
    const source = $('#request-source').value, status = $('#request-status').value, query = $('#request-query').value.trim();
    if (source) params.set('source', source);
    if (status) params.set('status', status);
    if (query) params.set('q', query);
    try {
      state.requests = await api('/api/requests?' + params.toString());
      renderRequests();
      text('#requests-updated-at', '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }));
    } catch (error) { $('#requests-error').textContent = error.message; }
    finally { state.requestsBusy = false; button.disabled = false; button.classList.remove('refreshing'); }
  }
  // Launch history: one row per click on "启动算力" (or per API wake-up).
  const LAUNCH_PHASES = { submitting: ['正在提交', 'warn'], queued: ['Kaggle 排队中', 'warn'], loading: ['正在加载模型', 'warn'], ready: ['运行中', 'good'] };
  const LAUNCH_END_REASONS = {
    idle: ['空闲自动停止', ''], max_runtime: ['达到最长运行时间', ''], shutdown: ['按停止请求结束', ''], stopped: ['按停止请求结束', ''],
    cancelled_before_submit: ['提交前已取消', ''], superseded: ['被新的启动取代', ''], worker_stopped: ['Worker 已退出（未报告原因）', ''],
    kaggle_complete: ['Kaggle 运行已结束', ''], kaggle_cancelled: ['Kaggle 运行被取消', 'bad'], kaggle_error: ['Kaggle 运行出错', 'bad'],
    submit_failed: ['提交 Kaggle 失败', 'bad'], lost_contact: ['与控制台失联', 'bad'], rejected: ['控制台拒绝了 Worker', 'bad'],
    interrupted: ['运行被中断', 'bad'], job_expired: ['任务超时后退出', 'bad'], register_failed: ['Worker 注册失败', 'bad'],
    crashed: ['Worker 异常退出', 'bad'], unknown: ['原因未知', '']
  };
  function formatSpan(seconds) {
    const n = safeNumber(seconds);
    if (n === null) return '—';
    const s = Math.max(0, Math.round(n));
    if (s < 60) return s + ' 秒';
    if (s < 3600) return Math.floor(s / 60) + ' 分 ' + String(s % 60).padStart(2, '0') + ' 秒';
    return Math.floor(s / 3600) + ' 小时 ' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + ' 分';
  }
  function launchTarget(item) {
    const target = item.target || {};
    if (target.backend && target.backend !== 'llama_cpp') return ['Strata', ''];
    const option = presetOption(target.option);
    const context = safeNumber(target.context_length);
    return [option ? option.label + ' · ' + option.detail : target.option || target.preset || '—',
      [kvLabel(target.kv_type), context === null ? '' : formatNumber(context) + ' tokens'].filter(Boolean).join(' · ')];
  }
  function spanCell(seconds, notes, live) {
    // `live` marks a phase that is still running: the figure is so far, not final.
    const cell = node('td', 'launch-span' + (live ? ' live' : ''));
    cell.append(node('strong', '', formatSpan(seconds) + (live && safeNumber(seconds) !== null ? ' …' : '')));
    notes.filter(Boolean).forEach(function (note) { cell.append(node('small', '', note)); });
    return cell;
  }
  function renderLaunches() {
    const payload = state.launches || {};
    const summary = payload.summary || {};
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    const total = summary.launches || 0;
    text('#launches-total', formatNumber(total));
    text('#launches-total-note', total ? [(summary.ready || 0) + ' 次就绪', summary.not_ready ? summary.not_ready + ' 次未就绪' : '', summary.in_progress ? summary.in_progress + ' 次进行中' : ''].filter(Boolean).join(' · ') : '当前时间范围内没有启动');
    [['queue', '提交 Notebook 到 Worker 上线'], ['load', 'Worker 上线到模型就绪'], ['ready', '点击启动到可以调用']].forEach(function (pair) {
      const longest = safeNumber(summary[pair[0] + '_max_seconds']);
      text('#launches-' + pair[0], formatSpan(summary[pair[0] + '_median_seconds']));
      text('#launches-' + pair[0] + '-note', (longest === null ? '' : '最长 ' + formatSpan(longest) + ' · ') + pair[1]);
    });
    text('#launches-session', total ? formatSpan(summary.session_total_seconds) : '—');
    text('#launches-idle', formatNumber(summary.idle_stops || 0));
    text('#launches-idle-note', summary.idle_stops ? '其后 30 分钟内又启动 ' + (summary.idle_relaunches || 0) + ' 次' : '连续空闲 8 分钟后结束');
    text('#launches-count', rows.length + ' 次启动');
    const body = $('#launches-table-body'); body.replaceChildren();
    if (!rows.length) {
      const row = node('tr'), cell = node('td', 'table-empty', '当前时间范围内没有启动记录');
      cell.colSpan = 8; row.append(cell); body.append(row); return;
    }
    rows.forEach(function (item) {
      const live = item.phase !== 'ended';
      const row = node('tr');
      const when = node('td', 'request-time');
      when.append(node('strong', '', formatDate(item.requested_at, true)), node('small', '', item.requester && item.requester.kind === 'api' ? 'API Key · ' + item.requester.name : '管理站'));
      if (safeNumber(item.after_idle_seconds) !== null) when.append(node('small', '', '空闲停止后 ' + formatSpan(item.after_idle_seconds) + '又启动'));
      const target = launchTarget(item), config = node('td', 'request-origin');
      config.append(node('strong', '', target[0]));
      if (target[1]) config.append(node('small', '', target[1]));
      const loaded = item.model;
      if (loaded && loaded.name) config.append(node('small', '', '已加载 ' + [loaded.name, kvLabel(loaded.kv_type), safeNumber(loaded.context) === null ? '' : formatNumber(loaded.context) + ' tokens', loaded.vision === true ? '视觉' : ''].filter(Boolean).join(' · ')));
      let phase = live ? LAUNCH_PHASES[item.phase] || [item.phase, 'neutral'] : item.ready_at === null ? ['未就绪', 'bad'] : ['已结束', 'neutral'];
      if (live && item.stop_requested_at !== null) phase = ['正在停止', 'warn'];
      const status = node('td', 'request-time'), badge = node('span', 'badge ' + phase[1]);
      badge.append(node('span', 'status-dot'), document.createTextNode(phase[0])); status.append(badge);
      if (live) status.append(node('small', '', '本阶段已 ' + formatSpan(item.phase_seconds)));
      const queued = live && item.phase === 'queued';
      const split = item.running_at !== null && item.submitted_at !== null && item.worker_at !== null && item.running_at >= item.submitted_at && item.running_at <= item.worker_at;
      const queue = spanCell(queued ? item.phase_seconds : item.queue_seconds, [
        split ? 'Kaggle 分配约 ' + formatSpan(item.running_at - item.submitted_at) : '',
        split ? '启动 Worker 约 ' + formatSpan(item.worker_at - item.running_at) : '',
        safeNumber(item.submit_seconds) === null ? '' : '提交 Notebook ' + formatSpan(item.submit_seconds)
      ], queued);
      const loading = live && item.phase === 'loading';
      const load = spanCell(loading ? item.phase_seconds : item.load_seconds, [], loading);
      const waiting = live && item.ready_at === null;
      const ready = spanCell(waiting ? item.total_seconds : item.ready_seconds, [waiting ? '尚未就绪' : item.ready_at === null ? '结束前没有就绪' : formatDate(item.ready_at, true) + ' 就绪'], waiting);
      const session = spanCell(item.session_seconds, [safeNumber(item.total_seconds) === null ? '' : (live ? '点击至今 ' : '点击到结束 ') + formatSpan(item.total_seconds)], live && item.worker_at !== null);
      const end = node('td', 'request-detail');
      if (live) end.append(node('strong', 'launch-reason', '—'));
      else {
        const reason = LAUNCH_END_REASONS[item.end_reason] || [item.end_reason || '未记录', ''];
        end.append(node('strong', 'launch-reason ' + reason[1], reason[0]), node('small', '', formatDate(item.ended_at, true) + ' 结束'));
      }
      if (item.error) end.append(node('small', 'request-error', item.error));
      row.append(when, config, status, queue, load, ready, session, end); body.append(row);
    });
  }
  async function refreshLaunches() {
    if (!state.authenticated || state.launchesBusy) return;
    state.launchesBusy = true;
    const button = $('#launches-refresh-button'); button.disabled = true;
    $('#launches-error').textContent = '';
    try {
      state.launches = await api('/api/runtime/runs?' + new URLSearchParams({ period: state.launchPeriod, limit: '100' }).toString());
      state.launchesAt = Date.now();
      renderLaunches();
      text('#launches-updated-at', '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }));
    } catch (error) { $('#launches-error').textContent = error.message; }
    finally { state.launchesBusy = false; button.disabled = false; }
  }
  function quotaAmount(value, unit) {
    const number = safeNumber(value);
    if (number === null) return value === null || value === undefined || value === '' ? '—' : String(value);
    if (String(unit || '').toUpperCase() === 'USD') return '$' + number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return number.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + ' 小时';
  }
  function quotaItem(container, names) {
    if (!container) return null;
    if (Array.isArray(container)) {
      return container.find(function (item) {
        const identity = String(item && (item.name || item.id || item.type || item.period) || '').toLowerCase();
        return names.includes(identity);
      }) || null;
    }
    for (const name of names) if (container[name] !== undefined) return container[name];
    return null;
  }
  function normalizedQuotas(payload) {
    const accelerator = payload.accelerator || payload.accelerators || (payload.quotas && payload.quotas.accelerator) || {};
    const ai = payload.ai || payload.ai_models || payload.aiModels || (payload.quotas && payload.quotas.ai) || {};
    return {
      gpu: quotaItem(accelerator, ['gpu', 'kaggle gpu']),
      tpu: quotaItem(accelerator, ['tpu', 'kaggle tpu']),
      daily: quotaItem(ai, ['daily', 'daily_ai', 'daily ai models']),
      monthly: quotaItem(ai, ['monthly', 'monthly_ai', 'monthly ai models'])
    };
  }
  function renderQuotaCard(key, quota, fallbackDate, unavailable) {
    const card = $('[data-quota-card="' + key + '"]');
    if (!card) return;
    const stateBadge = $('[data-quota-state]', card), progress = $('[data-quota-progress]', card);
    const used = quota && safeNumber(quota.used), remaining = quota && safeNumber(quota.remaining), total = quota && safeNumber(quota.total);
    const unit = quota && quota.unit || (key === 'daily' || key === 'monthly' ? 'USD' : 'hours');
    if (!quota) {
      $('[data-quota-value]', card).textContent = unavailable ? '暂不可用' : '—';
      $('[data-quota-used]', card).textContent = '已用 —';
      $('[data-quota-remaining]', card).textContent = '剩余 —';
      $('[data-quota-refresh]', card).textContent = unavailable ? '官方接口本次未返回数据' : '等待官方接口返回';
      stateBadge.className = 'badge ' + (unavailable ? 'warn' : 'neutral');
      stateBadge.textContent = unavailable ? '获取失败' : '未配置';
      progress.style.width = '0%';
      progress.setAttribute('aria-label', unavailable ? '本次未获取到配额' : '尚未配置配额查询');
      return;
    }
    const derivedTotal = total === null && used !== null && remaining !== null ? used + remaining : total;
    const ratio = used !== null && derivedTotal !== null && derivedTotal > 0 ? Math.max(0, Math.min(100, used / derivedTotal * 100)) : null;
    $('[data-quota-value]', card).textContent = quotaAmount(used, unit) + ' / ' + quotaAmount(derivedTotal, unit);
    $('[data-quota-used]', card).textContent = '已用 ' + quotaAmount(used, unit);
    $('[data-quota-remaining]', card).textContent = '剩余 ' + quotaAmount(remaining, unit);
    const refreshAt = quota.refresh_at || quota.refreshAt || quota.refill_at || quota.refillAt;
    $('[data-quota-refresh]', card).textContent = refreshAt ? '刷新时间 ' + formatDate(refreshAt, true) : fallbackDate ? '获取于 ' + formatDate(fallbackDate, true) : '已从官方接口同步';
    stateBadge.className = 'badge good'; stateBadge.textContent = '已同步';
    progress.style.width = (ratio === null ? 0 : ratio) + '%';
    progress.setAttribute('aria-label', ratio === null ? '配额使用比例未知' : '已使用 ' + ratio.toFixed(1) + '%');
  }
  function renderQuotas() {
    const payload = state.quotas || {};
    const configured = payload.configured !== false;
    const items = normalizedQuotas(payload);
    const errors = Array.isArray(payload.errors) ? payload.errors : [];
    $('#quota-setup-panel').hidden = configured;
    $('#quota-partial-panel').hidden = !configured || !errors.length;
    if (errors.length) {
      $('#quota-partial-message').textContent = errors.map(function (item) { return item && (item.message || item.scope) || String(item); }).join('；');
    }
    const hasAcceleratorError = errors.some(function (item) { return String(item && item.scope || '').toLowerCase().includes('accelerator'); });
    const hasAiError = errors.some(function (item) { return String(item && item.scope || '').toLowerCase().includes('ai'); });
    renderQuotaCard('gpu', configured ? items.gpu : null, payload.fetched_at, configured && (hasAcceleratorError || !items.gpu));
    renderQuotaCard('tpu', configured ? items.tpu : null, payload.fetched_at, configured && (hasAcceleratorError || !items.tpu));
    renderQuotaCard('daily', configured ? items.daily : null, payload.fetched_at, configured && (hasAiError || !items.daily));
    renderQuotaCard('monthly', configured ? items.monthly : null, payload.fetched_at, configured && (hasAiError || !items.monthly));
    text('#quotas-updated-at', payload.fetched_at ? '同步于 ' + formatDate(payload.fetched_at, true) : configured ? '本次未完成同步' : '等待服务器配置');
    if (payload.storage_message) text('#quota-storage-message', payload.storage_message);
  }
  async function refreshQuotas() {
    if (!state.authenticated || state.quotasBusy) return;
    state.quotasBusy = true;
    const button = $('#quotas-refresh-button'); button.disabled = true; button.classList.add('refreshing');
    $('#quotas-error').textContent = '';
    try { state.quotas = await api('/api/kaggle-quotas'); renderQuotas(); }
    catch (error) { $('#quotas-error').textContent = error.message; }
    finally { state.quotasBusy = false; button.disabled = false; button.classList.remove('refreshing'); }
  }
  function confirmAction(title, description, label, constructive) {
    // `constructive` marks actions that start something; everything else stops or revokes.
    return new Promise(function (resolve) {
      const dialog = $('#confirm-dialog');
      text('#confirm-title', title); text('#confirm-description', description); text('#confirm-proceed', label || '确认');
      $('#confirm-proceed').className = 'button ' + (constructive ? 'primary' : 'danger');
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
  async function runtimeAction(action) {
    if (state.runtimeBusy || !['start', 'stop'].includes(action)) return;
    const starting = action === 'start';
    const confirmed = await confirmAction(
      starting ? '启动 Kaggle 算力' : '停止 Kaggle 算力',
      starting ? '这会通过 Kaggle 官方接口提交专用私有 Notebook，并开始消耗 GPU 配额。Worker 上线后将自动加载模型。' : '这会请求 Worker 停止模型并退出，专用 Notebook 随后自然结束。Kaggle 官方没有运行中的硬取消接口，异常时仍需到 Kaggle 页面处理。',
      starting ? '启动算力' : '停止算力',
      starting
    );
    if (!confirmed) return;
    state.runtimeBusy = true; updateActionAvailability();
    inlineStatus('#runtime-result', starting ? '正在提交 Kaggle 运行…' : '正在请求结束运行…');
    try {
      const result = await api('/api/runtime/' + action, { method: 'POST' });
      const runtime = result && (result.runtime || (result.state ? result : null));
      if (runtime) {
        state.runtime = runtime;
        if (state.status) state.status.runtime = runtime;
        renderRuntime(runtime);
      }
      inlineStatus('#runtime-result', result && result.message || (starting ? '启动请求已提交，等待 Kaggle 分配资源…' : '停止请求已提交。'));
      toast(starting ? '算力启动请求已提交' : '算力停止请求已提交');
      await refreshStatus();
    } catch (error) { inlineStatus('#runtime-result', error.message, true); }
    finally { state.runtimeBusy = false; updateActionAvailability(); }
  }
  async function agentCommand(action, button) {
    const descriptions = {
      load_model: ['加载推理模型', '按照已保存的配置，在当前 Kaggle 会话中加载模型。加载过程会使用内存、显存和 GPU 配额。', '加载模型'],
      switch_model: ['切换推理模型', '当前请求完成后，Worker 会先停止已加载模型，再加载管理面板中已保存的目标模型。切换期间不能发送新请求。', '切换模型'],
      stop_model: ['停止推理进程', '停止当前模型进程。已连接的代理会保留，之后可以重新加载模型。', '停止模型'],
      shutdown: ['结束代理会话', '结束 Kaggle 代理连接。再次使用时需要重新运行接入 Notebook。', '结束会话']
    };
    if (state.commandBusy || !isAgentOnline()) return;
    const info = descriptions[action];
    if (!info || !(await confirmAction(info[0], info[1], info[2], action === 'load_model' || action === 'switch_model'))) return;
    state.commandBusy = true; updateActionAvailability();
    inlineStatus('#command-result', '正在提交命令…');
    try {
      const result = await api('/api/agent/command', { method: 'POST', body: { action: action } });
      if (result.job_id) {
        inlineStatus('#command-result', '命令已提交，等待 Kaggle 执行…');
        const completed = await waitForJob(result.job_id, function (job, elapsed) { inlineStatus('#command-result', jobStatus(job.status)[0] + ' · ' + formatDuration(elapsed)); });
        inlineStatus('#command-result', completed.message || '命令执行完成');
      } else inlineStatus('#command-result', result.message || '命令已提交');
      toast(action === 'shutdown' ? '结束请求已提交' : '操作已完成');
      await refreshStatus();
    } catch (error) { inlineStatus('#command-result', error.message, true); }
    finally { state.commandBusy = false; updateActionAvailability(); }
  }
  function readBlobAsDataUrl(blob) {
    return new Promise(function (resolve, reject) {
      const reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result || '')); };
      reader.onerror = function () { reject(new Error('读取图片失败')); };
      reader.readAsDataURL(blob);
    });
  }
  function dataUrlBytes(value) {
    const encoded = String(value || '').split(',', 2)[1] || '';
    return Math.max(0, Math.floor(encoded.length * 3 / 4) - (encoded.endsWith('==') ? 2 : encoded.endsWith('=') ? 1 : 0));
  }
  function historyImageUsage() {
    let count = 0, bytes = 0;
    state.history.forEach(function (message) {
      if (!Array.isArray(message.content)) return;
      message.content.forEach(function (part) {
        const url = part && part.type === 'image_url' && part.image_url && part.image_url.url;
        if (typeof url === 'string') { count += 1; bytes += dataUrlBytes(url); }
      });
    });
    return { count: count, bytes: bytes };
  }
  function loadImageSource(file) {
    if (window.createImageBitmap) return createImageBitmap(file, { imageOrientation: 'from-image' });
    return new Promise(function (resolve, reject) {
      const image = new Image();
      const url = URL.createObjectURL(file);
      image.onload = function () { URL.revokeObjectURL(url); resolve(image); };
      image.onerror = function () { URL.revokeObjectURL(url); reject(new Error('无法解析这张图片')); };
      image.src = url;
    });
  }
  function canvasBlob(canvas, quality) {
    return new Promise(function (resolve, reject) {
      canvas.toBlob(function (blob) { if (blob) resolve(blob); else reject(new Error('图片压缩失败')); }, 'image/jpeg', quality);
    });
  }
  async function normalizeChatImage(file, remainingBytes) {
    if (!file || !['image/jpeg', 'image/png'].includes(file.type)) throw new Error('只支持 JPEG 或 PNG 图片');
    if (remainingBytes <= 0) throw new Error('图片总大小已达到 2 MiB 上限');
    const source = await loadImageSource(file);
    const width = source.width || source.naturalWidth;
    const height = source.height || source.naturalHeight;
    if (!width || !height) { if (source.close) source.close(); throw new Error('图片尺寸无效'); }
    const attempts = [[1600, 0.84], [1280, 0.78], [960, 0.72], [720, 0.66]];
    try {
      for (const attempt of attempts) {
        const scale = Math.min(1, attempt[0] / Math.max(width, height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(width * scale));
        canvas.height = Math.max(1, Math.round(height * scale));
        const context = canvas.getContext('2d', { alpha: false });
        if (!context) throw new Error('浏览器无法创建图片画布');
        context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(source, 0, 0, canvas.width, canvas.height);
        const blob = await canvasBlob(canvas, attempt[1]);
        if (blob.size <= remainingBytes) return { name: file.name || '图片', bytes: blob.size, dataUrl: await readBlobAsDataUrl(blob) };
      }
    } finally { if (source.close) source.close(); }
    throw new Error('图片压缩后仍超过剩余的 2 MiB 限额');
  }
  function renderChatImages() {
    const holder = $('#chat-image-preview');
    holder.hidden = state.chatImages.length === 0;
    holder.replaceChildren();
    state.chatImages.forEach(function (item, index) {
      const card = node('div', 'chat-image-card');
      const image = node('img'); image.src = item.dataUrl; image.alt = item.name;
      const meta = node('span', 'chat-image-name', item.name + ' · ' + Math.max(1, Math.round(item.bytes / 1024)) + ' KiB');
      const remove = node('button', 'chat-image-remove', '×');
      remove.type = 'button'; remove.setAttribute('aria-label', '移除 ' + item.name);
      remove.addEventListener('click', function () { if (!state.chatBusy) { state.chatImages.splice(index, 1); renderChatImages(); updateActionAvailability(); } });
      card.append(image, meta, remove); holder.append(card);
    });
  }
  async function addChatImages(files) {
    if (!modelVisionReady()) { toast('当前模型未加载视觉投影，不能添加图片', 'error'); return; }
    const selected = Array.from(files || []);
    if (!selected.length) return;
    const historyUsage = historyImageUsage();
    if (historyUsage.count + state.chatImages.length + selected.length > CHAT_IMAGE_LIMIT) { toast('整个对话最多携带 4 张图片', 'error'); return; }
    state.chatImageBusy = true; updateActionAvailability();
    try {
      for (const file of selected) {
        const used = state.chatImages.reduce(function (sum, item) { return sum + item.bytes; }, 0);
        state.chatImages.push(await normalizeChatImage(file, CHAT_IMAGE_TOTAL_BYTES - historyUsage.bytes - used));
        renderChatImages();
      }
      toast('图片已压缩并加入本次消息');
    } catch (error) { toast(error.message, 'error'); }
    finally { state.chatImageBusy = false; $('#chat-image-input').value = ''; updateActionAvailability(); }
  }
  function appendMessage(role, message, pending, images) {
    $('#chat-welcome').hidden = true;
    const row = node('div', 'chat-message ' + role), avatar = node('span', 'message-avatar', role === 'user' ? 'YOU' : 'AI'), content = node('div', 'message-content'), head = node('div', 'message-heading');
    head.append(node('strong', '', role === 'user' ? '你' : '模型'), node('span', '', new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })));
    const body = node('div', pending ? 'message-pending' : 'message-body', pending ? undefined : message || (images && images.length ? '（仅图片）' : ''));
    if (pending) body.append(node('span', 'spinner'), node('span', '', '请求已提交，等待模型回答…'));
    content.append(head);
    if (images && images.length) {
      const gallery = node('div', 'message-image-gallery');
      images.forEach(function (item) { const image = node('img'); image.src = item.dataUrl; image.alt = item.name; gallery.append(image); });
      content.append(gallery);
    }
    content.append(body); row.append(avatar, content); $('#chat-messages').append(row); scrollChat();
    return { row: row, body: body, content: content };
  }
  function scrollChat() { const holder = $('#chat-messages'); holder.scrollTop = holder.scrollHeight; }
  function responseContent(result) {
    if (typeof result === 'string') return { content: result };
    if (result.choices && result.choices[0]) return result.choices[0].message || { content: result.choices[0].text || '' };
    if (result.message && typeof result.message === 'object') return result.message;
    return { content: result.content || result.response || result.text || '', reasoning_content: result.reasoning_content, tool_calls: result.tool_calls };
  }
  function parseChatEvent(frame) {
    const data = [];
    let event = 'message';
    frame.split(/\r?\n/).forEach(function (line) {
      if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
      else if (line.startsWith('event:')) event = line.slice(6).trim();
    });
    return data.length ? { event: event, data: data.join('\n') } : null;
  }
  async function readChatStream(jobId, signal, onEvent) {
    const response = await fetch('/api/jobs/' + encodeURIComponent(jobId) + '/stream', {
      credentials: 'same-origin', headers: { Accept: 'text/event-stream' }, signal: signal
    });
    if (!response.ok) {
      const payload = await response.json().catch(function () { return null; });
      if (response.status === 401 && state.authenticated) showAuth(false);
      throw new Error(errorText(payload, '读取回答失败（HTTP ' + response.status + '）'));
    }
    if (!response.body || !(response.headers.get('content-type') || '').includes('text/event-stream')) throw new Error('服务没有返回实时回答流');
    const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true });
    let buffer = '', complete = false;
    try {
      while (true) {
        const part = await reader.read();
        buffer += part.done ? decoder.decode() : decoder.decode(part.value, { stream: true });
        let boundary;
        while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
          const frame = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          const event = parseChatEvent(frame);
          if (!event) continue;
          if (event.data === '[DONE]') { complete = true; return; }
          const payload = JSON.parse(event.data);
          if (payload.error) throw new Error(errorText(payload, '模型推理失败'));
          onEvent(event.event, payload);
        }
        if (part.done) throw new Error('回答流提前结束，请检查运行日志');
      }
    } finally {
      // Closing a partial stream also tells the server to cancel this job.
      if (!complete) await reader.cancel().catch(function () {});
      reader.releaseLock();
    }
  }
  function accumulateChatChunk(result, chunk) {
    if (chunk.usage) result.usage = chunk.usage;
    const choice = (chunk.choices || []).find(function (item) { return (item.index || 0) === 0; });
    if (!choice) return false;
    const delta = choice.delta || {}, message = result.choices[0].message;
    if (typeof delta.content === 'string') message.content += delta.content;
    const reasoning = delta.reasoning_content ?? delta.reasoning;
    if (typeof reasoning === 'string') message.reasoning_content += reasoning;
    if (Array.isArray(delta.tool_calls)) {
      delta.tool_calls.forEach(function (part) {
        const index = Number.isInteger(part.index) && part.index >= 0 ? part.index : 0;
        const tool = message.tool_calls[index] || { id: '', type: 'function', function: { name: '', arguments: '' } };
        if (typeof part.id === 'string') tool.id += part.id;
        if (part.type) tool.type = part.type;
        if (part.function) {
          if (typeof part.function.name === 'string') tool.function.name += part.function.name;
          if (typeof part.function.arguments === 'string') tool.function.arguments += part.function.arguments;
        }
        message.tool_calls[index] = tool;
      });
    }
    if (choice.finish_reason) result.choices[0].finish_reason = choice.finish_reason;
    return Boolean(delta.content || reasoning || (delta.tool_calls && delta.tool_calls.length));
  }
  function renderChatResponse(responseEl, message, complete) {
    const reasoning = message.reasoning_content || message.reasoning;
    if (reasoning) {
      if (!responseEl.reasoning) {
        const details = node('details', 'message-reasoning');
        details.open = true;
        responseEl.reasoningTitle = node('summary');
        responseEl.reasoningBody = node('div');
        details.append(responseEl.reasoningTitle, responseEl.reasoningBody);
        responseEl.content.insertBefore(details, responseEl.body);
        responseEl.reasoning = details;
      }
      responseEl.reasoningTitle.textContent = complete || message.content ? '模型思考过程' : '模型正在思考…';
      responseEl.reasoningBody.textContent = typeof reasoning === 'string' ? reasoning : JSON.stringify(reasoning);
    }
    let content = typeof message.content === 'string' ? message.content : message.content ? JSON.stringify(message.content, null, 2) : '';
    if (message.tool_calls && message.tool_calls.length) content += (content ? '\n\n' : '') + '模型请求调用工具：\n' + JSON.stringify(message.tool_calls.filter(Boolean), null, 2) + '\n\n此测试页仅展示请求，不自动执行工具。';
    if (content || complete) {
      responseEl.body.className = 'message-body';
      responseEl.body.textContent = content || (reasoning ? '模型本次仅返回思考过程，没有回答正文。' : '模型返回了空内容，请查看运行日志。');
    } else if (reasoning) {
      responseEl.body.replaceChildren(node('span', 'spinner'), node('span', '', '正在思考，回答将实时显示…'));
    }
    scrollChat();
  }
  async function stopChat() {
    if (!state.chatJob || state.chatStopping) return;
    state.chatStopping = true;
    updateActionAvailability();
    text('#chat-request-status', '正在停止当前回答…');
    try { await api('/api/jobs/' + encodeURIComponent(state.chatJob) + '/cancel', { method: 'POST' }); }
    catch (error) { toast(error.message, 'error'); }
    finally { if (state.chatAbort) state.chatAbort.abort(); }
  }
  async function sendChat(event) {
    event.preventDefault();
    if (state.chatBusy || !modelCallable()) return;
    const prompt = $('#chat-input').value.trim();
    const submittedImages = state.chatImages.slice();
    if (!prompt && !submittedImages.length) return;
    if (submittedImages.length && !modelVisionReady()) { toast('当前模型未加载视觉投影，不能发送图片', 'error'); return; }
    const maxTokens = safeNumber($('#chat-max-tokens').value), temperature = safeNumber($('#chat-temperature').value);
    if (maxTokens === null || maxTokens < 1 || maxTokens > 32768 || temperature === null || temperature < 0 || temperature > 2) { toast('请检查最大输出 tokens 和 Temperature', 'error'); return; }
    state.chatBusy = true; state.chatStopping = false; updateActionAvailability();
    const content = submittedImages.length ? (prompt ? [{ type: 'text', text: prompt }] : []).concat(submittedImages.map(function (item) { return { type: 'image_url', image_url: { url: item.dataUrl } }; })) : prompt;
    state.history.push({ role: 'user', content: content });
    const userEl = appendMessage('user', prompt, false, submittedImages);
    $('#chat-input').value = '';
    state.chatImages = []; renderChatImages();
    const responseEl = appendMessage('assistant', '', true);
    text('#chat-request-status', '正在提交…');
    let inferenceCompleted = false;
    const result = { choices: [{ message: { role: 'assistant', content: '', reasoning_content: '', tool_calls: [] } }] };
    let phase = 'pending', receivedOutput = false;
    const startedAt = Date.now();
    function renderProgress() {
      if (state.chatStopping) return;
      const message = responseContent(result);
      const label = message.content ? '正在生成回答' : message.tool_calls.length ? '正在生成工具调用' : message.reasoning_content ? '模型思考中' : phase === 'running' ? '模型处理中，等待首个输出' : '排队中';
      text('#chat-request-status', label + ' · ' + formatDuration((Date.now() - startedAt) / 1000));
    }
    const progressTimer = window.setInterval(renderProgress, 1000);
    try {
      const task = await api('/api/chat', { method: 'POST', body: { messages: state.history.slice(), max_tokens: maxTokens, temperature: temperature } });
      if (!task.job_id) throw new Error('服务没有返回任务 ID。请求不会自动重试，请检查日志。');
      state.chatJob = task.job_id;
      updateActionAvailability();
      if (!state.authenticated) throw new Error('登录会话已结束');
      state.chatAbort = new AbortController();
      await readChatStream(task.job_id, state.chatAbort.signal, function (event, chunk) {
        if (event === 'status') phase = chunk.status;
        else if (accumulateChatChunk(result, chunk)) {
          receivedOutput = true;
          renderChatResponse(responseEl, responseContent(result), false);
        }
        if (chunk.usage) {
          text('#usage-prompt', formatNumber(chunk.usage.prompt_tokens ?? chunk.usage.input_tokens));
          text('#usage-completion', formatNumber(chunk.usage.completion_tokens ?? chunk.usage.output_tokens));
          text('#usage-total', formatNumber(chunk.usage.total_tokens));
        }
        renderProgress();
      });
      const message = responseContent(result);
      renderChatResponse(responseEl, message, true);
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
      inferenceCompleted = true; scrollChat();
    } catch (error) {
      if (state.chatJob && state.authenticated) {
        await api('/api/jobs/' + encodeURIComponent(state.chatJob) + '/cancel', { method: 'POST' }).catch(function () {});
      }
      if (state.history.length && state.history[state.history.length - 1].role === 'user') state.history.pop();
      if (receivedOutput) {
        renderChatResponse(responseEl, responseContent(result), true);
        responseEl.content.append(node('p', 'message-interrupted', state.chatStopping ? '已停止。上方是已收到的部分内容。' : '回答未完成：' + error.message));
      } else { userEl.row.remove(); responseEl.row.remove(); }
      $('#chat-input').value = prompt;
      state.chatImages = submittedImages; renderChatImages();
      $('#chat-welcome').hidden = Boolean($('.chat-message', $('#chat-messages')));
      text('#chat-request-status', state.chatStopping ? '已停止当前回答 · 文字和图片已恢复' : '请求未完成 · ' + error.message);
      toast(state.chatStopping ? '已停止，文字和图片已恢复' : '请求未完成，文字和图片已恢复', state.chatStopping ? 'info' : 'error');
      scrollChat();
    } finally { window.clearInterval(progressTimer); state.chatBusy = false; state.chatJob = null; state.chatAbort = null; state.chatStopping = false; updateActionAvailability(); }
    if (inferenceCompleted) await refreshStatus();
  }
  async function showBootstrap() {
    const button = $('#bootstrap-button'); button.disabled = true;
    try {
      const payload = await api('/api/bootstrap');
      const cfg = payload.config || {};
      const safeCfg = {};
      ['public_url', 'server_url', 'backend', 'auto_load', 'llama_preset', 'llama_model_path', 'llama_mmproj_path', 'llama_server_path', 'llama_alias', 'llama_context_length', 'llama_parallel', 'llama_kv_type', 'llama_batch_size', 'llama_ubatch_size', 'llama_tensor_split', 'context_length', 'model_path', 'strata_path', 'kv_type', 'low_ram', 'experimental_projection'].forEach(function (name) { if (cfg[name] !== undefined) safeCfg[name] = cfg[name]; });
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
  $$('[data-runtime-action]').forEach(function (el) { el.addEventListener('click', function () { runtimeAction(el.dataset.runtimeAction); }); });
  $('#menu-button').addEventListener('click', function () { const open = $('#sidebar').classList.toggle('open'); $('#sidebar-backdrop').hidden = !open; $('#menu-button').setAttribute('aria-expanded', String(open)); });
  $('#sidebar-backdrop').addEventListener('click', closeSidebar);
  $('#refresh-button').addEventListener('click', function () { refreshStatus(); if (state.currentView === 'quotas') refreshQuotas(); if (state.currentView === 'requests') refreshRequests(); if (state.currentView === 'launches') refreshLaunches(); });
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
    try { if (state.chatJob) await stopChat(); await api('/api/logout', { method: 'POST' }); hideNewKey(); state.history = []; state.chatImages = []; renderChatImages(); state.samples = []; state.status = null; state.runtime = null; state.runtimeBusy = false; state.requests = { summary: {}, rows: [] }; state.launches = { summary: {}, rows: [] }; state.quotas = null; showAuth(false); }
    catch (error) { toast(error.message, 'error'); }
  });
  $('#model-form').addEventListener('submit', function (event) { event.preventDefault(); saveSettings(event.currentTarget, '#model-save-status'); });
  $('#model-backend').addEventListener('change', syncBackendForm);
  $('#model-preset').addEventListener('change', applyPresetOption);
  $('#settings-form').addEventListener('submit', function (event) { event.preventDefault(); saveSettings(event.currentTarget, '#settings-save-status'); });
  ['#model-form', '#settings-form'].forEach(function (selector) {
    $(selector).addEventListener('input', function () { dirtyForms.add(this.id); updateActionAvailability(); });
    $(selector).addEventListener('change', function () { dirtyForms.add(this.id); updateActionAvailability(); });
  });
  $('#key-form').addEventListener('submit', createKey);
  $('#dismiss-key-button').addEventListener('click', hideNewKey);
  $('#logs-refresh-button').addEventListener('click', refreshLogs);
  $('#log-filter').addEventListener('input', renderLogs);
  $('#requests-refresh-button').addEventListener('click', refreshRequests);
  $('#launches-refresh-button').addEventListener('click', refreshLaunches);
  $$('[data-launch-period]').forEach(function (button) {
    button.addEventListener('click', function () {
      state.launchPeriod = button.dataset.launchPeriod;
      $$('[data-launch-period]').forEach(function (item) { item.classList.toggle('active', item === button); });
      refreshLaunches();
    });
  });
  $('#quotas-refresh-button').addEventListener('click', refreshQuotas);
  $('#request-filter-form').addEventListener('submit', function (event) { event.preventDefault(); refreshRequests(); });
  $$('[data-request-period]').forEach(function (button) {
    button.addEventListener('click', function () {
      state.requestPeriod = button.dataset.requestPeriod;
      $$('[data-request-period]').forEach(function (item) { item.classList.toggle('active', item === button); });
      refreshRequests();
    });
  });
  $('#chat-form').addEventListener('submit', sendChat);
  $('#chat-stop-button').addEventListener('click', stopChat);
  $('#chat-image-button').addEventListener('click', function () { if (!this.disabled) $('#chat-image-input').click(); });
  $('#chat-image-input').addEventListener('change', function () { addChatImages(this.files); });
  $('#chat-input').addEventListener('keydown', function (event) { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); if (!$('#chat-send-button').disabled) $('#chat-form').requestSubmit(); } });
  $('#clear-chat-button').addEventListener('click', function () {
    if (state.chatBusy) return;
    state.history = []; state.chatImages = []; renderChatImages(); $$('.chat-message', $('#chat-messages')).forEach(function (el) { el.remove(); }); $('#chat-welcome').hidden = false;
    text('#usage-prompt', '—'); text('#usage-completion', '—'); text('#usage-total', '—');
    toast('本页对话已清空');
  });
  $('#bootstrap-button').addEventListener('click', showBootstrap);
  window.addEventListener('hashchange', function () { if (state.authenticated) changeView(location.hash.slice(1)); });
  window.addEventListener('resize', function () { requestAnimationFrame(drawChart); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && state.authenticated) { refreshStatus(); if (state.currentView === 'quotas') refreshQuotas(); if (state.currentView === 'requests') refreshRequests(); if (state.currentView === 'launches') refreshLaunches(); } });
  window.setInterval(function () { if (state.authenticated && !document.hidden) refreshStatus(); }, 5000);
  window.setInterval(function () { if (state.authenticated && !document.hidden && state.currentView === 'quotas') refreshQuotas(); }, 60000);
  window.setInterval(function () { if (state.authenticated && !document.hidden && state.currentView === 'requests') refreshRequests(); }, 30000);
  window.setInterval(function () {
    // A launch in progress is followed closely; finished history only now and then.
    if (!state.authenticated || document.hidden || state.currentView !== 'launches') return;
    const live = (state.launches.rows || []).some(function (item) { return item.phase !== 'ended'; });
    if (live || Date.now() - state.launchesAt >= 30000) refreshLaunches();
  }, 5000);
  checkAuth();
})();
