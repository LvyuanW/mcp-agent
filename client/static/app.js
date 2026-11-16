'use strict';

const $ = (selector) => document.querySelector(selector);
const state = {
  bootstrap: null,
  sessions: [],
  current: null,
  selectedId: null,
  view: 'chat',
  busy: false,
  pollTimer: null,
  pollCount: 0,
  selectionVersion: 0,
  messageSignature: '',
  expanded: new Set(),
  filePath: '',
  files: [],
  selectedFile: null,
  fileVersion: 0,
  toastTimer: null,
  refreshPromise: null,
  submission: null,
  lastResumeAt: 0,
  uploadFile: null,
};

const STATUS_LABELS = {
  idle: '等待任务',
  running: '正在执行',
  completed: '已完成',
  error: '执行中断',
  cancelled: '已停止',
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function errorText(value) {
  if (typeof value === 'string') return value;
  if (value && typeof value.message === 'string') return value.message;
  if (value && value.error) return errorText(value.error);
  return '请求未能完成，请稍后重试。';
}

async function refreshAuthorization() {
  if (!state.refreshPromise) {
    state.refreshPromise = fetch('/auth/refresh', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'mcp-agent'}, body: '{}',
    }).then((response) => response.ok).catch(() => false).finally(() => { state.refreshPromise = null; });
  }
  return state.refreshPromise;
}

async function api(path, options = {}, retryAuthorization = true) {
  const headers = {Accept: 'application/json', ...options.headers};
  const method = options.method || 'GET';
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    headers['X-Requested-With'] = 'mcp-agent';
  }
  let response;
  try {
    response = await fetch(path, {credentials: 'same-origin', ...options, method, headers});
  } catch (error) {
    throw new Error('无法连接客户端服务。请检查连接后重试，远端会话会保留。');
  }
  if (response.status === 401 && retryAuthorization && await refreshAuthorization()) {
    return api(path, options, false);
  }
  const raw = await response.text();
  let data;
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (error) {
    throw new Error(`服务返回了无法读取的响应（HTTP ${response.status}）。`);
  }
  if (!response.ok) {
    const error = new Error(errorText(data.error || data));
    error.status = response.status;
    if (response.status === 401) showLoggedOut('登录已过期，请重新连接。');
    throw error;
  }
  return data;
}

function notify(message) {
  clearTimeout(state.toastTimer);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  state.toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 3500);
}

function showError(error) {
  if (error.status === 401) return;
  $('#page-error p').textContent = errorText(error);
  $('#page-error').hidden = false;
}

function clearError() { $('#page-error').hidden = true; }

function workspaceInfo() {
  const bootstrap = state.bootstrap || {};
  const workspace = bootstrap.workspace;
  if (workspace && typeof workspace === 'object') return workspace;
  return {workspace: workspace || '', skills: bootstrap.skills || []};
}

function workspacePath() {
  const info = workspaceInfo();
  return info.workspace || info.path || '';
}

function userName() {
  const user = state.bootstrap?.user;
  if (typeof user === 'string') return user;
  return user?.name || user?.username || user?.email || user?.tenant_id || workspaceInfo().tenant_id || '开发者';
}

function basename(path) {
  return String(path || '').replace(/\/$/, '').split('/').filter(Boolean).pop() || '工作区';
}

function currentConfig() {
  return {...(state.bootstrap?.model_defaults || {}), ...(state.current?.config || {})};
}

function configWithoutSecrets(config) {
  const copy = {...config};
  delete copy.api_key;
  delete copy.api_key_set;
  return copy;
}

function showLoggedOut(message) {
  clearTimeout(state.pollTimer);
  state.current = null;
  $('#app').hidden = true;
  $('#connection-screen').hidden = false;
  $('#connection-loading').hidden = true;
  $('#login-button').hidden = false;
  $('#connection-retry').hidden = true;
  $('#connection-description').textContent = message || '登录 MCP 服务，载入你的会话、技能与工作成果。';
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
}

async function bootstrap() {
  $('#connection-loading').hidden = false;
  $('#connection-retry').hidden = true;
  $('#connection-error').hidden = true;
  $('#login-button').hidden = true;
  try {
    let data = await api('/api/bootstrap');
    if (!data.authenticated && await refreshAuthorization()) data = await api('/api/bootstrap');
    state.bootstrap = data;
    $('#login-endpoint').textContent = data.mcp_url || '尚未配置 MCP 地址';
    $('#connection-loading').hidden = true;
    if (!data.authenticated) {
      showLoggedOut();
      return;
    }
    $('#connection-screen').hidden = true;
    $('#app').hidden = false;
    const path = workspacePath();
    const name = basename(path);
    $('#workspace-name').textContent = name;
    $('#workspace-breadcrumb').textContent = name;
    $('#workspace-breadcrumb').title = path;
    $('#workspace-tenant').textContent = workspaceInfo().tenant_id || data.user?.tenant_id || path;
    $('#composer-workspace').textContent = name;
    $('#composer-workspace').title = path;
    $('#empty-workspace').textContent = `${name} · 远程工作区`;
    $('#user-name').textContent = userName();
    $('#user-avatar').textContent = Array.from(userName())[0].toUpperCase();
    renderCapabilities();
    await loadSessions();
    const selected = new URL(window.location.href).searchParams.get('session');
    if (selected) await selectSession(selected);
    else renderSession();
    schedulePoll();
  } catch (error) {
    $('#connection-loading').hidden = true;
    $('#connection-error').textContent = errorText(error);
    $('#connection-error').hidden = false;
    $('#connection-retry').hidden = false;
    $('#connection-description').textContent = '客户端服务暂时未能连接 MCP 服务端。';
  }
}

async function loadSessions() {
  try {
    const data = await api('/api/sessions');
    state.sessions = Array.isArray(data) ? data : (data.sessions || []);
    $('#session-list-error').hidden = true;
    renderSessions();
  } catch (error) {
    if (error.status !== 401) {
      $('#session-list-error p').textContent = errorText(error);
      $('#session-list-error').hidden = false;
    }
  }
}

function sessionDate(timestamp) {
  const date = new Date(Number(timestamp || 0) * 1000);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return '今天';
  today.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return '昨天';
  return '更早';
}

function renderSessions() {
  const query = $('#session-search').value.trim().toLowerCase();
  const list = $('#session-list');
  list.replaceChildren();
  $('#session-count').textContent = state.sessions.length || '';
  const sessions = [...state.sessions]
    .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))
    .filter((session) => !query || (session.title || '').toLowerCase().includes(query));
  if (!sessions.length) {
    list.append(element('p', 'session-empty', query ? '没有匹配的会话' : '新建一个任务，开始工作。'));
    return;
  }
  let lastGroup = '';
  for (const session of sessions) {
    const group = sessionDate(session.updated_at);
    if (group !== lastGroup) {
      list.append(element('div', 'session-date-label', group));
      lastGroup = group;
    }
    const button = element('button', 'session-item');
    button.type = 'button';
    button.dataset.status = session.status;
    button.classList.toggle('selected', session.id === state.selectedId);
    if (session.id === state.selectedId) button.setAttribute('aria-current', 'page');
    const title = session.title || '新的任务';
    button.title = `${title} · ${STATUS_LABELS[session.status] || session.status || ''}`;
    const dot = element('span', 'session-item-dot');
    dot.setAttribute('aria-hidden', 'true');
    button.append(dot, element('span', 'session-item-title', title));
    button.addEventListener('click', () => selectSession(session.id).catch(showError));
    list.append(button);
  }
}

function syncSessionSummary(session) {
  const entry = {id: session.id, title: session.title, status: session.status, updated_at: session.updated_at, revision: session.revision};
  const index = state.sessions.findIndex((item) => item.id === session.id);
  if (index === -1) state.sessions.unshift(entry);
  else state.sessions[index] = entry;
  renderSessions();
}

function updateUrl(id, replace = false) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set('session', id);
  else url.searchParams.delete('session');
  window.history[replace ? 'replaceState' : 'pushState']({}, '', url);
}

async function selectSession(id, {updateHistory = true} = {}) {
  clearError();
  const version = ++state.selectionVersion;
  state.selectedId = id;
  state.current = null;
  state.expanded.clear();
  state.messageSignature = '';
  if (updateHistory) updateUrl(id);
  $('#composer-input').value = '';
  resizeComposer();
  renderSessions();
  $('#session-title').textContent = '载入会话…';
  $('#messages').replaceChildren();
  $('#empty-state').hidden = true;
  $('#composer-input').disabled = true;
  $('#send-button').disabled = true;
  closeSidebar();
  switchView('chat');
  try {
    const session = await api(`/api/sessions/${encodeURIComponent(id)}`);
    if (version !== state.selectionVersion) return;
    state.current = session;
    syncSessionSummary(session);
    renderSession({forceScroll: true});
    // Every deployment can reattach to the durable remote session. The server
    // lease determines whether this process may advance the current turn.
    if (session.status === 'running') {
      state.lastResumeAt = Date.now();
      api(`/api/sessions/${encodeURIComponent(id)}/resume`, {method: 'POST', body: '{}'}).catch((error) => {
        if (error.status !== 409) showError(error);
      });
    }
  } catch (error) {
    if (version !== state.selectionVersion) return;
    state.selectedId = null;
    state.current = null;
    updateUrl(null, true);
    renderSession();
    showError(error);
  }
  schedulePoll();
}

async function createSession(config = null) {
  const selectedConfig = config || configWithoutSecrets(state.bootstrap?.model_defaults || {});
  const session = await api('/api/sessions', {
    method: 'POST',
    body: JSON.stringify({title: '新的任务', config: selectedConfig}),
  });
  ++state.selectionVersion;
  state.current = session;
  state.selectedId = session.id;
  state.messageSignature = '';
  state.expanded.clear();
  updateUrl(session.id);
  syncSessionSummary(session);
  renderSession({forceScroll: true});
  switchView('chat');
  closeSidebar();
  return session;
}

async function newSession() {
  if (state.busy) return;
  let created = false;
  state.busy = true;
  $('#new-session').disabled = true;
  try {
    await createSession();
    created = true;
    $('#composer-input').value = '';
    resizeComposer();
    clearError();
  } catch (error) { showError(error); }
  finally {
    state.busy = false;
    $('#new-session').disabled = false;
    renderControls();
    if (created) $('#composer-input').focus();
  }
}

function textContent(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return content == null ? '' : JSON.stringify(content, null, 2);
  return content.map((block) => {
    if (block.type === 'text') return block.text || '';
    if (block.type === 'thinking') return '';
    if (block.type === 'resource' && block.resource) return block.resource.text || block.resource.uri || '';
    if (block.type === 'resource_link') return block.uri || '';
    return block.text || '';
  }).filter(Boolean).join('\n\n');
}

function thinkingContent(content) {
  if (!Array.isArray(content)) return '';
  return content.filter((block) => block.type === 'thinking').map((block) => block.thinking || block.text || '').join('\n');
}

function safeLink(target) {
  try {
    const url = new URL(target, window.location.origin);
    if (url.protocol === 'https:' || url.protocol === 'http:' || url.protocol === 'mailto:') return url;
  } catch (error) { /* Render unsupported link targets as ordinary text. */ }
  return null;
}

function appendInline(parent, input, depth = 0) {
  const text = String(input);
  if (depth > 4) { parent.append(document.createTextNode(text)); return; }
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\[[^\]\n]+\]\([^\s)]+\)|\*[^*\n]+\*)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith('`')) {
      parent.append(element('code', '', token.slice(1, -1)));
    } else if (token.startsWith('**') || token.startsWith('__')) {
      const strong = element('strong');
      appendInline(strong, token.slice(2, -2), depth + 1);
      parent.append(strong);
    } else if (token.startsWith('*')) {
      parent.append(element('em', '', token.slice(1, -1)));
    } else {
      const parts = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const destination = safeLink(parts[2]);
      if (destination) {
        const link = element('a', '', parts[1]);
        link.href = destination.href;
        link.rel = 'noopener noreferrer';
        const remoteFile = !/^[a-z][a-z\d+.-]*:/i.test(parts[2]) && !parts[2].startsWith('#');
        if (remoteFile) {
          link.addEventListener('click', (event) => {
            event.preventDefault();
            let path = parts[2];
            if (path.startsWith(workspacePath() + '/')) path = path.slice(workspacePath().length + 1);
            switchView('files');
            openFile(path).catch(showError);
          });
        } else link.target = '_blank';
        parent.append(link);
      } else parent.append(document.createTextNode(parts[1]));
    }
    cursor = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(cursor)));
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    if (button) {
      const original = button.textContent;
      button.textContent = '已复制';
      setTimeout(() => { button.textContent = original; }, 1600);
    } else notify('已复制');
  } catch (error) { notify('浏览器未允许访问剪贴板，请选择内容后复制。'); }
}

function codeBlock(code, language) {
  const wrapper = element('div', 'code-block');
  const toolbar = element('div', 'code-toolbar');
  const copy = element('button', '', '复制');
  copy.type = 'button';
  copy.addEventListener('click', () => copyText(code, copy));
  toolbar.append(element('span', '', language || 'code'), copy);
  const pre = element('pre');
  pre.tabIndex = 0;
  pre.append(element('code', '', code));
  wrapper.append(toolbar, pre);
  return wrapper;
}

function tableCells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

function markdown(input) {
  const root = element('div', 'markdown');
  const lines = String(input || '').replace(/\r\n?/g, '\n').split('\n');
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }
    const fence = line.match(/^\s*```([^\s`]*)\s*$/);
    if (fence) {
      const code = [];
      index++;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index++;
      root.append(codeBlock(code.join('\n'), fence[1]));
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const node = element(`h${heading[1].length}`);
      appendInline(node, heading[2]);
      root.append(node);
      index++;
      continue;
    }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { root.append(element('hr')); index++; continue; }
    if (line.includes('|') && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1])) {
      const wrapper = element('div', 'markdown-table-wrapper');
      const table = element('table');
      const headerRow = element('tr');
      for (const cell of tableCells(line)) { const th = element('th'); appendInline(th, cell); headerRow.append(th); }
      const head = element('thead');
      head.append(headerRow);
      table.append(head);
      const body = element('tbody');
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const row = element('tr');
        for (const cell of tableCells(lines[index++])) { const td = element('td'); appendInline(td, cell); row.append(td); }
        body.append(row);
      }
      table.append(body);
      wrapper.append(table);
      root.append(wrapper);
      continue;
    }
    const listMatch = line.match(/^\s*([-*+]\s+|\d+[.)]\s+)(.+)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]);
      const list = element(ordered ? 'ol' : 'ul');
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-*+]\s+|\d+[.)]\s+)(.+)$/);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        const li = element('li');
        appendInline(li, item[2]);
        list.append(li);
        index++;
      }
      root.append(list);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = element('blockquote');
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        const paragraph = element('p');
        appendInline(paragraph, lines[index++].replace(/^>\s?/, ''));
        quote.append(paragraph);
      }
      root.append(quote);
      continue;
    }
    const paragraph = element('p');
    appendInline(paragraph, line);
    index++;
    while (index < lines.length && lines[index].trim() && !/^\s*(```|#{1,4}\s|[-*+]\s|\d+[.)]\s|>\s?)/.test(lines[index])) {
      if (lines[index].includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1] || '')) break;
      paragraph.append(element('br'));
      appendInline(paragraph, lines[index++]);
    }
    root.append(paragraph);
  }
  return root;
}

function parseJSON(value) {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch (error) { return null; }
}

function parsedToolResult(message) {
  if (!message) return null;
  const raw = textContent(message.content);
  return parseJSON(raw) || {text: raw};
}

function appendOutput(parent, label, value, className = '') {
  if (value === undefined || value === null || value === '') return;
  const string = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const toolbar = element('div', 'code-toolbar');
  const copy = element('button', '', '复制');
  copy.type = 'button';
  copy.addEventListener('click', () => copyText(string, copy));
  toolbar.append(element('span', '', label), copy);
  parent.append(toolbar);
  const shown = string.length > 150000 ? string.slice(0, 150000) + '\n\n…页面仅展示前 150,000 个字符。' : string;
  const pre = element('pre', className, shown);
  pre.tabIndex = 0;
  parent.append(pre);
}

function toolCard(call, resultMessage, index) {
  const name = call.name || call.function?.name || 'tool';
  const rawArgs = call.arguments ?? call.function?.arguments ?? {};
  const args = parseJSON(rawArgs) || {};
  const key = call.id || `${name}-${index}`;
  const result = parsedToolResult(resultMessage);
  const running = !result && state.current?.status === 'running';
  const failed = result && (result.isError || result.error || ['failed', 'cancelled', 'timed_out'].includes(result.status) || (typeof result.exit_code === 'number' && result.exit_code !== 0));
  const details = element('details', `tool-card${failed ? ' failed' : ''}`);
  details.open = state.expanded.has(key);
  details.addEventListener('toggle', () => {
    if (details.open) state.expanded.add(key);
    else state.expanded.delete(key);
  });
  const summary = element('summary');
  summary.append(element('span', 'tool-icon', name === 'shell' ? '>_' : '◇'));
  const title = args.command ? args.command.split('\n')[0] : name;
  const titleNode = element('span', 'tool-card-title', title);
  titleNode.title = name === 'shell' ? args.command || name : name;
  summary.append(titleNode, element('span', 'tool-state', failed ? '执行失败' : result ? '已完成' : running ? '执行中' : '待执行'), element('span', 'tool-card-chevron', '⌄'));
  details.append(summary);
  const body = element('div', 'tool-details');
  appendOutput(body, args.command ? '命令' : '参数', args.command || (Object.keys(args).length ? args : rawArgs));
  if (args.cwd) body.append(element('p', 'exit-code', `工作目录 · ${args.cwd}`));
  if (result) {
    const content = result.structuredContent || result;
    if ('stdout' in content || 'stderr' in content) {
      appendOutput(body, '输出', content.stdout);
      appendOutput(body, '错误输出', content.stderr, 'stderr');
      if (content.exit_code !== undefined && content.exit_code !== null) body.append(element('div', 'exit-code', `退出码 ${content.exit_code}${content.job_id ? ' · 作业 ' + content.job_id : ''}`));
      if (!content.stdout && !content.stderr) body.append(element('p', 'tool-result-placeholder', '命令未产生文本输出。'));
    } else appendOutput(body, '结果', content.text || content);
  } else {
    const activity = state.current?.activity;
    if (activity && activity.tool_call_id === call.id) {
      appendOutput(body, '输出', activity.stdout);
      appendOutput(body, '错误输出', activity.stderr, 'stderr');
    }
    body.append(element('p', 'tool-result-placeholder', running ? '等待远端执行结果。' : '执行状态将在恢复会话后更新。'));
  }
  details.append(body);
  return details;
}

function makeMessage(message, index, results, draft = false) {
  const user = message.role === 'user';
  const item = element('li', `message ${user ? 'message-user' : 'message-assistant'}`);
  const author = element('div', 'message-author');
  author.append(element('span', 'message-author-icon', user ? Array.from(userName())[0].toUpperCase() : 'm'), element('span', '', user ? '你' : draft ? 'Agent · 正在生成' : 'Agent'));
  const body = element('div', 'message-body');
  const thinking = thinkingContent(message.content);
  if (thinking) {
    const details = element('details', 'thinking-block');
    const key = `thinking-${index}`;
    details.open = state.expanded.has(key);
    details.addEventListener('toggle', () => details.open ? state.expanded.add(key) : state.expanded.delete(key));
    details.append(element('summary', '', '思考过程'), element('div', '', thinking));
    body.append(details);
  }
  const text = textContent(message.content);
  if (text) body.append(markdown(text));
  if (Array.isArray(message.content)) {
    for (const block of message.content) {
      if (['image', 'image_url', 'file', 'resource_link'].includes(block.type)) body.append(element('div', 'message-attachment', block.name || block.path || block.uri || block.source_url || '附件'));
    }
  }
  for (const [callIndex, call] of (message.tool_calls || []).entries()) body.append(toolCard(call, results.get(call.id), `${index}-${callIndex}`));
  item.append(author, body);
  return item;
}

function renderMessages({forceScroll = false} = {}) {
  const session = state.current;
  const messages = session?.messages || [];
  const signature = JSON.stringify([messages, session?.draft, session?.activity, session?.status]);
  if (signature === state.messageSignature && !forceScroll) return;
  state.messageSignature = signature;
  const scroller = $('#chat-scroll');
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
  const oldTop = scroller.scrollTop;
  const results = new Map(messages.filter((message) => message.role === 'tool').map((message) => [message.tool_call_id, message]));
  const calledIds = new Set(messages.flatMap((message) => (message.tool_calls || []).map((call) => call.id)));
  const fragment = document.createDocumentFragment();
  let displayed = 0;
  for (const [index, message] of messages.entries()) {
    if (message.role === 'system') continue;
    if (message.role === 'tool') {
      if (calledIds.has(message.tool_call_id)) continue;
      const item = element('li', 'message message-assistant');
      item.append(toolCard({id: message.tool_call_id, name: message.name, arguments: '{}'}, message, index));
      fragment.append(item);
    } else fragment.append(makeMessage(message, index, results));
    displayed++;
  }
  const draft = session?.draft;
  if (draft && (draft.text || draft.thinking)) {
    const content = [];
    if (draft.thinking) content.push({type: 'thinking', thinking: draft.thinking});
    if (draft.text) content.push({type: 'text', text: draft.text});
    fragment.append(makeMessage({role: 'assistant', content}, 'draft', results, true));
    displayed++;
  }
  $('#messages').replaceChildren(fragment);
  $('#empty-state').hidden = displayed > 0;
  if (forceScroll || nearBottom) scroller.scrollTop = scroller.scrollHeight;
  else scroller.scrollTop = oldTop;
}

function activityLabel(session) {
  const activity = session.activity || {};
  if (activity.message) return activity.message;
  const labels = {starting: '正在恢复远端会话', model: '模型正在处理任务', tool: `正在调用 ${activity.name || '远端工具'}`, job: '远端作业正在执行', retry: `正在重试${activity.attempt ? ' · 第 ' + activity.attempt + ' 次' : ''}`, compaction: '正在整理会话上下文'};
  return labels[activity.kind] || (session.pending ? '正在恢复工具执行结果' : 'Agent 正在执行任务');
}

function renderControls() {
  const session = state.current;
  const running = session?.status === 'running';
  const loading = Boolean(state.selectedId && !session);
  const input = $('#composer-input');
  input.disabled = state.busy || running || loading;
  $('#send-button').disabled = state.busy || running || loading || !input.value.trim();
  $('#send-button').hidden = running;
  $('#stop-button').hidden = !running;
  $('#stop-button').disabled = state.busy;
  $('#settings-button').disabled = running || state.busy;
  $('#composer-model').disabled = running || state.busy;
  const model = currentConfig().model;
  $('#model-label').textContent = model || '配置模型';
  const status = $('#run-status');
  status.hidden = !session || session.status === 'idle';
  status.classList.toggle('error', session?.status === 'error');
  const icon = $('#run-status-icon');
  icon.replaceChildren();
  if (running) icon.append(element('span', 'spinner'));
  else icon.textContent = session?.status === 'error' ? '!' : session?.status === 'cancelled' ? '■' : '✓';
  $('#run-status-text').textContent = running ? activityLabel(session) : session?.status === 'error' ? errorText(session.error) : session?.status === 'cancelled' ? '执行已停止，会话记录已保存。' : '任务已完成';
  const recoverable = session && (session.status === 'error' || session.status === 'cancelled');
  $('#resume-button').hidden = !recoverable;
  $('#resume-button').disabled = state.busy;
  $('#save-status').textContent = running ? '执行进度保存在远端' : '会话与产物保存至 MCP 服务端';
}

function renderSession(options = {}) {
  const session = state.current;
  $('#session-title').textContent = session?.title || '新的任务';
  document.title = session?.title ? `${session.title} · MCP Agent` : 'MCP Agent';
  renderMessages(options);
  renderControls();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (!state.bootstrap?.authenticated || $('#app').hidden) return;
  state.pollTimer = setTimeout(poll, document.hidden ? 6000 : state.current?.status === 'running' ? 1200 : 5000);
}

async function poll() {
  const id = state.selectedId;
  const version = state.selectionVersion;
  try {
    if (id && !state.busy) {
      const session = await api(`/api/sessions/${encodeURIComponent(id)}`);
      if (version === state.selectionVersion && id === state.selectedId) {
        state.current = session;
        syncSessionSummary(session);
        renderSession();
        if (session.status === 'running' && Date.now() - state.lastResumeAt > 10000) {
          state.lastResumeAt = Date.now();
          await api(`/api/sessions/${encodeURIComponent(id)}/resume`, {method: 'POST', body: '{}'});
        }
      }
    }
    if (++state.pollCount % 5 === 0) await loadSessions();
  } catch (error) {
    if (error.status !== 401 && error.status !== 409) {
      $('#save-status').textContent = '连接暂时中断，正在重连';
      $('#save-status').title = errorText(error);
    }
  } finally { schedulePoll(); }
}

function requestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16))).map((value) => value.toString(16).padStart(2, '0')).join('');
}

async function sendMessage(event) {
  event.preventDefault();
  const content = $('#composer-input').value.trim();
  if (!content || state.busy || state.current?.status === 'running') return;
  if (!currentConfig().model) { openSettings(); return; }
  state.busy = true;
  renderControls();
  clearError();
  try {
    if (!state.current) await createSession();
    const id = state.selectedId;
    if (!state.submission || state.submission.sessionId !== id || state.submission.content !== content) {
      state.submission = {sessionId: id, content, requestId: requestId()};
    }
    const session = await api(`/api/sessions/${encodeURIComponent(id)}/messages`, {
      method: 'POST', body: JSON.stringify({content, request_id: state.submission.requestId}),
    });
    state.submission = null;
    if (id === state.selectedId) {
      state.current = session;
      $('#composer-input').value = '';
      resizeComposer();
      syncSessionSummary(session);
      renderSession({forceScroll: true});
    }
  } catch (error) { showError(error); }
  finally {
    state.busy = false;
    renderControls();
    schedulePoll();
  }
}

async function resumeSession() {
  if (!state.selectedId || state.busy) return;
  state.busy = true;
  renderControls();
  clearError();
  try {
    const id = state.selectedId;
    await api(`/api/sessions/${encodeURIComponent(id)}/resume`, {method: 'POST', body: '{}'});
    const session = await api(`/api/sessions/${encodeURIComponent(id)}`);
    if (id === state.selectedId) { state.current = session; renderSession(); }
  } catch (error) { showError(error); }
  finally { state.busy = false; renderControls(); schedulePoll(); }
}

async function cancelSession() {
  if (!state.selectedId || state.busy) return;
  state.busy = true;
  renderControls();
  try {
    const id = state.selectedId;
    await api(`/api/sessions/${encodeURIComponent(id)}/cancel`, {method: 'POST', body: '{}'});
    notify('已发送停止请求');
    const session = await api(`/api/sessions/${encodeURIComponent(id)}`);
    if (id === state.selectedId) { state.current = session; renderSession(); }
  } catch (error) { showError(error); }
  finally { state.busy = false; renderControls(); schedulePoll(); }
}

function resizeComposer() {
  const textarea = $('#composer-input');
  textarea.style.height = 'auto';
  textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  renderControls();
}

function openSettings() {
  if (state.current?.status === 'running') return;
  const config = currentConfig();
  $('#provider-input').value = config.provider || 'openai';
  $('#format-input').value = config.api_format || 'auto';
  $('#model-input').value = config.model || '';
  $('#base-url-input').value = config.base_url || '';
  $('#api-key-input').value = '';
  $('#api-key-input').placeholder = config.api_key_set ? '已保存密钥，填写以更新' : '输入 API Key，或使用服务端配置';
  $('#api-key-note').textContent = config.api_key_set ? '留空保留远端已保存的密钥。' : '加密传输到服务端。留空使用客户端服务的环境配置。';
  $('#output-tokens-input').value = config.max_output_tokens || 8192;
  $('#context-window-input').value = config.context_window || config.model_context_window || 128000;
  $('#settings-description').textContent = state.current ? '设置保存至当前远端会话，并在下一次执行时生效。' : '保存后创建远端会话，随时可以继续工作。';
  $('#settings-error').hidden = true;
  updateProviderFields();
  $('#settings-dialog').showModal();
}

function updateProviderFields() {
  const anthropic = $('#provider-input').value === 'anthropic';
  $('#format-input').disabled = anthropic;
  if (anthropic) $('#format-input').value = 'auto';
  $('#base-url-input').placeholder = anthropic ? 'https://api.anthropic.com' : 'https://api.openai.com/v1';
}

async function saveSettings(event) {
  event.preventDefault();
  const button = $('#save-settings');
  button.disabled = true;
  $('#settings-error').hidden = true;
  const config = {
    ...configWithoutSecrets(currentConfig()),
    provider: $('#provider-input').value,
    api_format: $('#format-input').value,
    model: $('#model-input').value.trim(),
    base_url: $('#base-url-input').value.trim(),
    max_output_tokens: Number($('#output-tokens-input').value),
    context_window: Number($('#context-window-input').value),
  };
  const apiKey = $('#api-key-input').value.trim();
  if (apiKey) config.api_key = apiKey;
  try {
    if (!config.model) throw new Error('请填写模型名称。');
    if (!Number.isSafeInteger(config.max_output_tokens) || config.max_output_tokens < 1) throw new Error('最大输出 tokens 需要填写正整数。');
    if (!Number.isSafeInteger(config.context_window) || config.context_window < 1024) throw new Error('上下文窗口至少需要 1024 tokens。');
    if (config.max_output_tokens >= config.context_window) throw new Error('最大输出 tokens 应小于上下文窗口。');
    if (!state.current) await createSession(config);
    else {
      // A fresh revision avoids a stale settings form racing normal polling.
      const latest = await api(`/api/sessions/${encodeURIComponent(state.selectedId)}`);
      if (latest.status === 'running') throw new Error('请等待当前执行完成后修改设置。');
      const session = await api(`/api/sessions/${encodeURIComponent(state.selectedId)}`, {
        method: 'PATCH', body: JSON.stringify({expected_revision: latest.revision, patch: {config}}),
      });
      state.current = session;
      syncSessionSummary(session);
      renderSession();
    }
    $('#api-key-input').value = '';
    $('#settings-dialog').close();
    notify('设置已保存至远端');
    $('#composer-input').focus();
  } catch (error) {
    $('#settings-error').textContent = errorText(error);
    $('#settings-error').hidden = false;
  } finally { button.disabled = false; }
}

function renderCapabilities() {
  $('#capabilities-endpoint').textContent = state.bootstrap?.mcp_url || '';
  const tools = (state.bootstrap?.tools || []).filter((tool) => tool._meta?.['io.mcp-agent/visibility'] !== 'runtime');
  const skills = state.bootstrap?.skills || workspaceInfo().skills || [];
  $('#tools-count').textContent = tools.length;
  $('#skills-count').textContent = skills.length;
  for (const [selector, items, empty] of [['#tools-list', tools, '服务端尚未提供模型可用工具。'], ['#skills-list', skills, '工作区中还没有可用技能。']]) {
    const list = $(selector);
    list.replaceChildren();
    if (!items.length) { list.append(element('div', 'capability-empty', empty)); continue; }
    for (const item of items) {
      const block = element('div', 'capability-item');
      block.append(element('div', 'capability-name', item.name || item.title), element('p', 'capability-description', item.description || ''));
      if (item.path) block.append(element('div', 'capability-path', item.path));
      list.append(block);
    }
  }
}

function switchView(view) {
  state.view = view;
  $('#chat-view').hidden = view !== 'chat';
  $('#files-view').hidden = view !== 'files';
  $('#chat-tab').classList.toggle('active', view === 'chat');
  $('#files-tab').classList.toggle('active', view === 'files');
  $('#chat-tab').setAttribute('aria-pressed', String(view === 'chat'));
  $('#files-tab').setAttribute('aria-pressed', String(view === 'files'));
  if (view === 'files' && !state.files.length) loadFiles(state.filePath).catch(showError);
}

function formatSize(bytes) {
  if (bytes === undefined || bytes === null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderFileBreadcrumbs() {
  const root = $('#file-breadcrumbs');
  root.replaceChildren();
  const button = element('button', '', basename(workspacePath()));
  button.type = 'button';
  button.addEventListener('click', () => loadFiles('').catch(showError));
  root.append(button);
  const parts = state.filePath.split('/').filter(Boolean);
  parts.forEach((part, index) => {
    root.append(element('span', '', '/'));
    const crumb = element('button', '', part);
    crumb.type = 'button';
    crumb.addEventListener('click', () => loadFiles(parts.slice(0, index + 1).join('/')).catch(showError));
    root.append(crumb);
  });
}

function renderFiles() {
  const list = $('#file-list');
  list.replaceChildren();
  renderFileBreadcrumbs();
  if (state.filePath) {
    const parent = element('button', 'file-row');
    parent.type = 'button';
    parent.append(element('span', '', '↰'), element('span', '', '上级目录'));
    parent.addEventListener('click', () => loadFiles(state.filePath.split('/').slice(0, -1).join('/')).catch(showError));
    list.append(parent);
  }
  if (!state.files.length) list.append(element('p', 'file-list-message', '此目录为空。'));
  const isDirectory = (file) => ['directory', 'dir', 'folder'].includes(file.type);
  const files = [...state.files].sort((a, b) => Number(isDirectory(b)) - Number(isDirectory(a)) || a.name.localeCompare(b.name));
  for (const file of files) {
    const button = element('button', 'file-row');
    button.type = 'button';
    const path = file.path ?? [state.filePath, file.name].filter(Boolean).join('/');
    button.classList.toggle('selected', state.selectedFile?.path === path);
    button.title = path;
    const icon = element('span', isDirectory(file) ? 'folder-icon' : 'file-page-icon');
    icon.setAttribute('aria-hidden', 'true');
    button.append(icon, element('span', '', file.name), element('span', 'file-size', isDirectory(file) ? '' : formatSize(file.size)));
    button.addEventListener('click', () => (isDirectory(file) ? loadFiles(path) : openFile(path)).catch(showError));
    list.append(button);
  }
}

async function loadFiles(path = '') {
  const version = ++state.fileVersion;
  $('#refresh-files').disabled = true;
  $('#file-list').replaceChildren(element('p', 'file-list-message', '正在读取远程目录…'));
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    if (version !== state.fileVersion) return;
    state.filePath = (data.path || path).split('/').filter((part) => part && part !== '.').join('/');
    state.files = data.entries || [];
    renderFiles();
  } catch (error) {
    if (version === state.fileVersion) $('#file-list').replaceChildren(element('p', 'file-list-message', errorText(error)));
    throw error;
  } finally { if (version === state.fileVersion) $('#refresh-files').disabled = false; }
}

async function openFile(path) {
  const requestedPath = path;
  state.selectedFile = {path: requestedPath};
  renderFiles();
  $('#preview-name').textContent = path;
  $('#file-preview-empty').hidden = false;
  $('#file-preview-empty p').textContent = '正在读取文件…';
  $('#file-content').hidden = true;
  $('#copy-file').hidden = true;
  $('#download-file').hidden = false;
  $('#file-preview-note').hidden = true;
  try {
    const file = await api(`/api/file?path=${encodeURIComponent(path)}`);
    if (state.selectedFile?.path !== requestedPath) return;
    state.selectedFile = {...file, path: file.path || requestedPath};
    $('#preview-name').textContent = file.path || path;
    $('#file-content').textContent = file.content || '';
    $('#file-content').hidden = false;
    $('#file-preview-empty').hidden = true;
    $('#copy-file').hidden = false;
    $('#file-preview-note').textContent = [formatSize(file.size), file.revision ? `版本 ${String(file.revision).slice(0, 12)}` : '', file.truncated ? '内容已截断' : ''].filter(Boolean).join(' · ');
    $('#file-preview-note').hidden = false;
  } catch (error) {
    if (state.selectedFile?.path === requestedPath) $('#file-preview-empty p').textContent = errorText(error);
    throw error;
  }
}

function selectUpload() {
  const file = $('#upload-file-input').files[0];
  $('#upload-file-input').value = '';
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) {
    showError(new Error('单个文件最大为 8 MiB，请选择较小的文件。'));
    return;
  }
  state.uploadFile = file;
  $('#upload-description').textContent = `${file.name} · ${formatSize(file.size)}`;
  $('#upload-path').value = [state.filePath, file.name].filter(Boolean).join('/');
  $('#upload-error').hidden = true;
  $('#upload-dialog').showModal();
}

async function uploadFile(event) {
  event.preventDefault();
  if (!state.uploadFile) return;
  const button = $('#upload-submit');
  button.disabled = true;
  button.textContent = '正在上传…';
  $('#upload-error').hidden = true;
  try {
    const path = $('#upload-path').value.trim();
    if (!path || path.startsWith('/') || path.split('/').includes('..')) throw new Error('请填写工作区内的相对路径。');
    const bytes = new Uint8Array(await state.uploadFile.arrayBuffer());
    const parts = [];
    for (let offset = 0; offset < bytes.length; offset += 32768) {
      parts.push(String.fromCharCode(...bytes.subarray(offset, offset + 32768)));
    }
    await api('/api/upload', {
      method: 'POST',
      body: JSON.stringify({path, content_base64: btoa(parts.join('')), expected_revision: null}),
    });
    state.uploadFile = null;
    $('#upload-dialog').close();
    notify('文件已上传至远程工作区');
    await loadFiles(path.split('/').slice(0, -1).join('/')).catch(showError);
  } catch (error) {
    $('#upload-error').textContent = error.status === 409 ? '目标文件已存在或已发生变化，请修改保存路径后上传。' : errorText(error);
    $('#upload-error').hidden = false;
  } finally { button.disabled = false; button.textContent = '上传'; }
}

async function downloadFile() {
  const path = state.selectedFile?.path;
  if (!path) return;
  const button = $('#download-file');
  button.disabled = true;
  try {
    const url = `/api/download?path=${encodeURIComponent(path)}`;
    let response = await fetch(url, {credentials: 'same-origin'});
    if (response.status === 401 && await refreshAuthorization()) response = await fetch(url, {credentials: 'same-origin'});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(errorText(data.error || data));
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = element('a');
    link.href = objectUrl;
    link.download = basename(path);
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
  } catch (error) { showError(error); }
  finally { button.disabled = false; }
}

function syncSidebarAccessibility() {
  const mobile = window.matchMedia('(max-width: 800px)').matches;
  const open = $('#sidebar').classList.contains('open');
  $('#sidebar').inert = mobile && !open;
  $('#main-content').inert = mobile && open;
}

function openSidebar() {
  $('#sidebar').classList.add('open');
  $('#sidebar-backdrop').hidden = false;
  $('#open-sidebar').setAttribute('aria-expanded', 'true');
  syncSidebarAccessibility();
  $('#close-sidebar').focus();
}

function closeSidebar() {
  const restoreFocus = $('#sidebar').classList.contains('open') && $('#sidebar').contains(document.activeElement);
  $('#sidebar').classList.remove('open');
  $('#sidebar-backdrop').hidden = true;
  $('#open-sidebar').setAttribute('aria-expanded', 'false');
  syncSidebarAccessibility();
  if (restoreFocus && window.innerWidth <= 800) $('#open-sidebar').focus();
}

$('#connection-retry').addEventListener('click', bootstrap);
$('#new-session').addEventListener('click', newSession);
$('#sessions-retry').addEventListener('click', loadSessions);
$('#session-search').addEventListener('input', renderSessions);
$('#page-error-dismiss').addEventListener('click', clearError);
$('#composer-form').addEventListener('submit', sendMessage);
$('#composer-input').addEventListener('input', resizeComposer);
$('#composer-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('#send-button').disabled) $('#composer-form').requestSubmit();
  }
});
$('#stop-button').addEventListener('click', cancelSession);
$('#resume-button').addEventListener('click', resumeSession);
$('#settings-button').addEventListener('click', openSettings);
$('#composer-model').addEventListener('click', openSettings);
$('#settings-form').addEventListener('submit', saveSettings);
$('#provider-input').addEventListener('change', updateProviderFields);
$('#settings-dialog').addEventListener('close', () => { $('#api-key-input').value = ''; });
$('#capabilities-button').addEventListener('click', () => { renderCapabilities(); $('#capabilities-dialog').showModal(); });
$('#chat-tab').addEventListener('click', () => switchView('chat'));
$('#files-tab').addEventListener('click', () => switchView('files'));
$('#workspace-navigation').addEventListener('click', () => { switchView('files'); closeSidebar(); });
$('#refresh-files').addEventListener('click', () => loadFiles(state.filePath).catch(showError));
$('#copy-file').addEventListener('click', () => copyText(state.selectedFile?.content || '', $('#copy-file')));
$('#download-file').addEventListener('click', downloadFile);
$('#upload-file-button').addEventListener('click', () => $('#upload-file-input').click());
$('#upload-file-input').addEventListener('change', selectUpload);
$('#upload-form').addEventListener('submit', uploadFile);
$('#upload-dialog').addEventListener('close', () => { state.uploadFile = null; });
$('#open-sidebar').addEventListener('click', openSidebar);
$('#close-sidebar').addEventListener('click', closeSidebar);
$('#sidebar-backdrop').addEventListener('click', closeSidebar);
$('#logout-button').addEventListener('click', async () => {
  try {
    await api('/auth/logout', {method: 'POST', body: '{}'});
    window.location.assign('/');
  } catch (error) { showError(error); }
});
for (const button of document.querySelectorAll('[data-close-dialog]')) button.addEventListener('click', () => button.closest('dialog').close());
for (const button of document.querySelectorAll('[data-prompt]')) button.addEventListener('click', () => {
  $('#composer-input').value = button.dataset.prompt;
  resizeComposer();
  $('#composer-input').focus();
});
window.addEventListener('popstate', () => {
  const id = new URL(window.location.href).searchParams.get('session');
  if (id) selectSession(id, {updateHistory: false}).catch(showError);
  else {
    ++state.selectionVersion;
    state.selectedId = null;
    state.current = null;
    state.messageSignature = '';
    renderSessions();
    renderSession();
  }
});
document.addEventListener('visibilitychange', schedulePoll);
window.addEventListener('resize', () => {
  if (window.innerWidth > 800) closeSidebar();
  else syncSidebarAccessibility();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeSidebar();
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !$('#app').hidden) {
    event.preventDefault();
    if (window.innerWidth <= 800) openSidebar();
    $('#session-search').focus();
  }
});

syncSidebarAccessibility();
bootstrap();
