(() => {
  'use strict';

  const bridge = window.echo;
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const TITLES = {
    chat: ['Chat', 'Your documents, memory and tools in one local conversation.'],
    docs: ['Documents', 'Plain Markdown files in the workspace you own.'],
    ask: ['Ask docs', 'Grounded answers with real sources from your library.'],
    browser: ['Browser', 'Optional Playwright control for agent actions.'],
    memory: ['Memory', 'Durable facts the agent can recall in future chats.'],
    outbox: ['Outbox', 'Review drafts before posting anything yourself.'],
    settings: ['Settings', 'Model connection, agent behavior and diagnostics.'],
  };

  const state = {
    config: {}, docs: [], memories: [], drafts: [], sessions: [],
    messages: [], session: '', counts: {}, trace: [], attachments: [],
  };
  let tab = 'chat';
  let currentDoc = null;
  let askHistory = [];
  let busyChat = false;
  let busyAsk = false;
  let appInfo = { home: '~/.echo', platform: 'darwin' };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const icon = (id) => {
    const svg = document.createElementNS(SVG_NS, 'svg');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', `#${id}`);
    svg.append(use);
    return svg;
  };
  const clear = (node) => node.replaceChildren();
  const when = (value) => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  };

  function showScreen(name) {
    for (const id of ['boot', 'setup', 'app']) $(id).classList.toggle('hidden', id !== name);
  }

  function banner(text, kind = '') {
    const node = $('banner');
    if (!text) {
      node.classList.add('hidden');
      return;
    }
    node.textContent = text;
    node.classList.toggle('bad', kind === 'bad');
    node.classList.remove('hidden');
  }

  async function api(apiPath, body) {
    const response = await bridge.api(apiPath, body);
    if (!response.ok) {
      throw new Error(response.data?.error || `Request failed (${response.status})`);
    }
    return response.data;
  }

  async function attempt(fn, fallback = 'Request failed') {
    try {
      return await fn();
    } catch (error) {
      banner(`${fallback}: ${error.message}`, 'bad');
      return null;
    }
  }

  function messageNode(role, content, extras = {}) {
    const wrap = el('div', `msg ${role === 'user' ? 'user' : ''} ${extras.error ? 'error' : ''} ${extras.thinking ? 'thinking' : ''}`);
    wrap.append(el('div', 'role', extras.label || (role === 'user' ? 'You' : 'Echo')));
    wrap.append(el('div', 'bubble', content || ''));
    if (extras.trace?.length) {
      const trace = el('div', 'trace');
      for (const item of extras.trace) {
        trace.append(el('span', '', `${item.name}${item.arguments ? ` · ${item.arguments}` : ''}`));
      }
      wrap.append(trace);
    }
    if (extras.sources?.length) {
      const sources = el('div', 'sources');
      for (const source of extras.sources) sources.append(el('span', 'source', source.title || source.id));
      wrap.append(sources);
    }
    return wrap;
  }

  function scrollToEnd(node) {
    requestAnimationFrame(() => { node.scrollTop = node.scrollHeight; });
  }

  function renderChat() {
    const box = $('chat-messages');
    clear(box);
    if (!state.messages.length) {
      box.append(el('div', 'empty', 'No messages yet. Ask Echo to write a document, search your notes, remember a decision, or research a topic.'));
      return;
    }
    state.messages.forEach((message, index) => {
      const lastAssistant = index === state.messages.length - 1 && message.role === 'assistant';
      box.append(messageNode(message.role, message.content, {
        trace: lastAssistant ? state.trace : [],
      }));
    });
    scrollToEnd(box);
  }

  function renderAsk() {
    const box = $('ask-messages');
    clear(box);
    if (!askHistory.length) {
      box.append(el('div', 'empty', 'Ask a question about your documents. Echo cites the excerpts it used.'));
      return;
    }
    for (const item of askHistory) {
      box.append(messageNode(item.role, item.content, { sources: item.sources }));
    }
    scrollToEnd(box);
  }

  function renderSessions() {
    const box = $('sessions');
    clear(box);
    if (!state.sessions.length) {
      box.append(el('div', 'empty', 'No sessions yet.'));
      return;
    }
    for (const session of state.sessions) {
      const button = el('button', `session ${session.id === state.session ? 'active' : ''}`);
      button.append(el('span', 't', session.title || 'Untitled chat'));
      button.append(el('span', 'm', when(session.updated_at || session.created_at)));
      button.addEventListener('click', () => attempt(() => selectSession(session.id), 'Cannot open session'));
      box.append(button);
    }
  }

  function renderCounters() {
    $('count-chat').textContent = state.counts.sessions ?? state.sessions.length;
    $('count-docs').textContent = state.counts.docs ?? state.docs.length;
    $('count-memory').textContent = state.counts.memories ?? state.memories.length;
    $('count-outbox').textContent = state.counts.drafts ?? state.drafts.length;
  }

  function renderDocs() {
    const list = $('doc-list');
    clear(list);
    if (!state.docs.length) {
      list.append(el('div', 'empty', 'No documents yet. Create your first one.'));
      return;
    }
    for (const doc of state.docs) {
      const button = el('button', `list-item ${currentDoc?.id === doc.id ? 'active' : ''}`);
      button.append(el('span', 't', doc.title || doc.id));
      button.append(el('span', 's', `${doc.tags?.length ? doc.tags.join(', ') + ' · ' : ''}${doc.words ?? 0} words`));
      button.addEventListener('click', () => attempt(() => selectDoc(doc.id), 'Cannot open document'));
      list.append(button);
    }
  }

  function fillDoc(doc) {
    currentDoc = doc || null;
    $('doc-title').value = doc?.title || '';
    $('doc-tags').value = doc?.tags?.join(', ') || '';
    $('doc-body').value = doc?.body || '';
    $('doc-status').textContent = doc ? `${doc.words} words · ${when(doc.updated_at)}` : 'New document';
    renderDocs();
  }

  function renderMemory() {
    const box = $('mem-list');
    clear(box);
    if (!state.memories.length) {
      box.append(el('div', 'empty', 'No memories stored yet.'));
      return;
    }
    for (const item of state.memories) {
      const card = el('div', 'memory-card');
      const left = el('div');
      const text = el('div', 'text');
      text.append(el('span', 'kind', item.kind));
      text.append(document.createTextNode(item.text));
      left.append(text);
      left.append(el('div', 'meta', `${item.tags?.length ? item.tags.join(', ') + ' · ' : ''}${when(item.created_at)}`));
      card.append(left);
      const forget = el('button', 'btn ghost sm danger', 'Forget');
      forget.addEventListener('click', () => attempt(async () => {
        await api('/api/memory/forget', { id: item.id });
        await refreshState();
        banner('Memory removed.');
      }, 'Cannot forget memory'));
      card.append(forget);
      box.append(card);
    }
  }

  function renderOutbox() {
    const box = $('outbox-list');
    clear(box);
    if (!state.drafts.length) {
      box.append(el('div', 'empty', 'The outbox is empty. Ask Echo to draft a social post.'));
      return;
    }
    for (const draft of state.drafts) {
      const card = el('div', 'card');
      const head = el('div', 'detail-head');
      head.append(el('h2', '', draft.platform || draft.name || 'Draft'));
      head.append(el('span', 'muted', `${draft.status || 'draft'} · ${draft.name || ''}`));
      card.append(head);
      if (draft.title) card.append(el('p', 'muted', draft.title));
      const output = el('pre', 'output', '');
      output.classList.add('hidden');
      const show = el('button', 'btn ghost sm', 'Show draft');
      show.addEventListener('click', () => attempt(async () => {
        const data = await api('/api/outbox/show', { name: draft.name });
        output.textContent = data.draft?.body || JSON.stringify(data.draft, null, 2);
        output.classList.remove('hidden');
      }, 'Cannot read draft'));
      const approve = el('button', 'btn sm', 'Approve');
      approve.addEventListener('click', () => attempt(async () => {
        await api('/api/outbox/approve', { name: draft.name });
        await refreshState();
        banner('Draft approved. Nothing was published.');
      }, 'Cannot approve draft'));
      const row = el('div', 'row');
      row.append(show, approve);
      card.append(row, output);
      box.append(card);
    }
  }

  function renderSettings() {
    const cfg = state.config || {};
    $('cfg-provider').value = cfg.provider || 'openai';
    $('cfg-model').value = cfg.model || '';
    $('cfg-base-url').value = cfg.base_url || '';
    $('cfg-embed').value = cfg.embed_model || '';
    $('cfg-temperature').value = cfg.temperature ?? 0.3;
    $('cfg-max-steps').value = cfg.max_tool_steps ?? 12;
    $('cfg-retries').value = cfg.llm_retries ?? 2;
    $('cfg-retry-delay').value = cfg.llm_retry_delay ?? 1;
    $('cfg-context').value = cfg.context_chars ?? 14000;
    $('cfg-context-summary').value = cfg.context_summary_chars ?? 1800;
    $('cfg-auto-memory').value = String(cfg.auto_memory ?? true);
    $('cfg-browser-auto').value = String(cfg.browser_auto_install ?? true);
    $('cfg-system-prompt').value = cfg.system_prompt || '';
    $('cfg-home').textContent = cfg.home || appInfo.home;
    $('settings-home').textContent = `${cfg.home || appInfo.home}/.env`;
    $('model-pill').textContent = cfg.model ? `model: ${cfg.model}` : 'no model selected';
    $('workspace-path').textContent = cfg.home || appInfo.home;
  }

  function renderAll() {
    renderCounters();
    renderSessions();
    renderChat();
    renderDocs();
    renderMemory();
    renderOutbox();
    if (tab === 'settings') renderSettings();
    if (tab === 'browser') attempt(renderBrowserStatus, 'Browser status unavailable');
  }

  async function refreshState(session = '') {
    const query = session ? `?session=${encodeURIComponent(session)}` : '';
    const data = await api(`/api/state${query}`);
    Object.assign(state, data);
    renderAll();
  }

  function setTab(name) {
    if (!TITLES[name]) return;
    tab = name;
    for (const button of document.querySelectorAll('#nav button')) {
      button.classList.toggle('active', button.dataset.tab === name);
    }
    for (const panel of document.querySelectorAll('.panel')) {
      panel.classList.toggle('active', panel.id === `panel-${name}`);
    }
    $('view-title').textContent = TITLES[name][0];
    $('view-subtitle').textContent = TITLES[name][1];
    if (name === 'settings') {
      attempt(async () => { await loadConfig(); renderSettings(); await renderHealth(); await renderLog(); }, 'Cannot load settings');
    }
    if (name === 'browser') attempt(renderBrowserStatus, 'Browser status unavailable');
    if (name === 'docs') renderDocs();
    if (name === 'memory') renderMemory();
    if (name === 'outbox') renderOutbox();
  }

  async function selectSession(id) {
    const data = await api('/api/chat/select', { session: id });
    state.session = data.session;
    state.messages = data.messages || [];
    state.trace = [];
    state.attachments = [];
    renderAttachments();
    renderSessions();
    renderChat();
    setTab('chat');
  }

  async function newChat() {
    const data = await api('/api/chat/new');
    state.session = data.session;
    state.messages = [];
    state.trace = [];
    state.attachments = [];
    renderAttachments();
    setTab('chat');
    renderAll();
    $('chat-input').focus();
  }

  async function deleteChat() {
    if (!state.session) return;
    await api('/api/chat/delete', { session: state.session });
    state.trace = [];
    await refreshState();
    banner('Chat deleted. Your documents are unchanged.');
  }

  function renderAttachments() {
    const box = $('attachment-chips');
    clear(box);
    box.classList.toggle('hidden', state.attachments.length === 0);
    state.attachments.forEach((file, index) => {
      const chip = el('span', 'chip', file.name);
      chip.title = file.name;
      const remove = el('button', 'chip-x', '×');
      remove.type = 'button';
      remove.title = 'Remove ' + file.name;
      remove.addEventListener('click', () => {
        state.attachments.splice(index, 1);
        renderAttachments();
      });
      chip.append(remove);
      box.append(chip);
    });
  }

  async function pickAttachments() {
    const files = await bridge.pickFiles();
    const errors = [];
    for (const file of files) {
      if (file.error) {
        errors.push(`${file.name}: ${file.error}`);
      } else if (state.attachments.length >= 12) {
        errors.push('Maximum 12 attachments per question.');
        break;
      } else {
        state.attachments.push({ name: file.name, content: file.content });
      }
    }
    renderAttachments();
    if (errors.length) banner(errors.join(' · '), 'bad');
    else if (files.length) banner(`Attached ${files.length} file${files.length === 1 ? '' : 's'}.`);
    $('chat-input').focus();
  }

  async function sendChat() {
    if (busyChat) return;
    const text = $('chat-input').value.trim();
    const attachments = state.attachments.slice();
    if (!text && !attachments.length) return;
    const question = text || 'Please review the attached files and tell me what matters.';
    busyChat = true;
    $('chat-send').disabled = true;
    $('chat-input').value = '';
    const box = $('chat-messages');
    const label = attachments.length
      ? `${question}\n[${attachments.length} attached file${attachments.length === 1 ? '' : 's'}]`
      : question;
    box.append(messageNode('user', label));
    const thinking = messageNode('assistant', 'Thinking…', { thinking: true });
    box.append(thinking);
    scrollToEnd(box);
    try {
      const data = await api('/api/chat', {
        message: question, attachments, session: state.session,
      });
      state.session = data.session;
      state.trace = data.trace || [];
      state.attachments = [];
      renderAttachments();
      await refreshState(data.session);
    } catch (error) {
      thinking.remove();
      box.append(messageNode('assistant', error.message, { error: true, label: 'Error' }));
      scrollToEnd(box);
    } finally {
      busyChat = false;
      $('chat-send').disabled = false;
      $('chat-input').focus();
    }
  }

  async function sendAsk() {
    if (busyAsk) return;
    const question = $('ask-input').value.trim();
    if (!question) return;
    busyAsk = true;
    $('ask-send').disabled = true;
    $('ask-input').value = '';
    askHistory.push({ role: 'user', content: question });
    renderAsk();
    try {
      const data = await api('/api/ask', { question });
      askHistory.push({ role: 'assistant', content: data.answer, sources: data.sources || [] });
    } catch (error) {
      askHistory.push({ role: 'assistant', content: error.message, error: true });
    } finally {
      busyAsk = false;
      $('ask-send').disabled = false;
      renderAsk();
    }
  }

  async function selectDoc(id) {
    const data = await api(`/api/doc?id=${encodeURIComponent(id)}`);
    fillDoc(data.doc);
  }

  function newDoc() {
    fillDoc(null);
    $('doc-title').focus();
  }

  async function saveDoc() {
    const payload = {
      id: currentDoc?.id || '',
      title: $('doc-title').value,
      tags: $('doc-tags').value,
      body: $('doc-body').value,
    };
    const data = await api('/api/doc/save', payload);
    fillDoc(data.doc);
    await refreshState();
    banner('Document saved.');
  }

  async function archiveDoc() {
    if (!currentDoc) return;
    await api('/api/doc/delete', { id: currentDoc.id });
    fillDoc(null);
    await refreshState();
    banner('Document archived (nothing was deleted).');
  }

  async function addMemory() {
    const text = $('mem-text').value.trim();
    if (!text) return;
    await api('/api/memory', { text, kind: $('mem-kind').value });
    $('mem-text').value = '';
    await refreshState();
    banner('Memory saved.');
  }

  async function renderBrowserStatus() {
    const status = await api('/api/browser/status');
    const running = Boolean(status.open || status.running);
    const install = status.install || {};
    const installLabel = {
      installing: 'Installing Chromium…',
      installed: 'Chromium ready',
      failed: 'Chromium install failed',
      skipped: 'Browser install disabled',
      'not-installed': 'Chromium missing',
    }[install.status] || (status.installed ? 'Chromium ready' : 'Chromium missing');
    $('browser-status').textContent = running ? `Running · ${installLabel}` : installLabel;
    $('browser-detail').textContent = install.message || (status.chromium_path || 'No browser path reported.');
    $('browser-install').disabled = install.status === 'installing';
    $('count-browser').textContent = running ? 'on' : 'off';
  }

  async function browserOp(apiPath, body, label) {
    try {
      const data = await api(apiPath, body);
      $('browser-out').textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
      await renderBrowserStatus();
    } catch (error) {
      banner(`${label}: ${error.message}`, 'bad');
      $('browser-out').textContent = error.message;
    }
  }

  async function loadConfig() {
    state.config = await api('/api/config');
  }

  async function loadModels() {
    try {
      const data = await api('/api/models');
      const list = $('cfg-model-list');
      clear(list);
      for (const name of data.models || []) list.append(el('option', '', name));
      if (!data.model_present && data.model) {
        banner(`Model ${data.model} is not loaded in ${data.provider}. Pick one from the model list in Settings.`, 'bad');
      }
      $('model-pill').textContent = `model: ${data.model}`;
    } catch (error) {
      $('model-pill').textContent = 'model unavailable';
    }
  }

  async function saveConfig() {
    const payload = {
      provider: $('cfg-provider').value,
      model: $('cfg-model').value.trim(),
      base_url: $('cfg-base-url').value.trim(),
      embed_model: $('cfg-embed').value.trim(),
      temperature: Number($('cfg-temperature').value),
      max_tool_steps: Number($('cfg-max-steps').value),
      llm_retries: Number($('cfg-retries').value),
      llm_retry_delay: Number($('cfg-retry-delay').value),
      context_chars: Number($('cfg-context').value),
      context_summary_chars: Number($('cfg-context-summary').value),
      auto_memory: $('cfg-auto-memory').value === '1',
      browser_auto_install: $('cfg-browser-auto').value === '1',
      system_prompt: $('cfg-system-prompt').value,
    };
    const apiKey = $('cfg-api-key').value;
    if (apiKey) payload.api_key = apiKey;
    await api('/api/config', payload);
    $('cfg-api-key').value = '';
    await loadConfig();
    renderSettings();
    await loadModels();
    banner('Settings saved to your workspace.');
  }

  async function testModel() {
    const data = await api('/api/models');
    const models = (data.models || []).slice(0, 20);
    $('diag').textContent = [
      `Provider: ${data.provider}`,
      `Model: ${data.model}`,
      `Model present: ${data.model_present}`,
      models.length ? `Available models:\n${models.join('\n')}` : 'No models reported by the endpoint.',
    ].join('\n');
    $('cfg-status').textContent = data.model_present ? 'Connected.' : 'Endpoint reachable, model missing.';
  }

  async function renderHealth() {
    const data = await api('/api/health');
    const problems = data.problems || [];
    $('health-status').textContent = problems.length ? `${problems.length} issue(s)` : 'Workspace OK';
    if (problems.length) banner(`Workspace check: ${problems.join(' · ')}`, 'bad');
  }

  async function renderLog() {
    const text = await bridge.log();
    $('log-tail').textContent = text || 'No backend log yet.';
    $('log-tail').scrollTop = $('log-tail').scrollHeight;
  }

  async function refreshAll() {
    await refreshState(state.session);
    if (currentDoc) await attempt(() => selectDoc(currentDoc.id), 'Cannot reload document');
    await attempt(loadModels, 'Cannot load models');
    await attempt(renderHealth, 'Cannot check workspace');
    await attempt(renderLog, 'Cannot read log');
    banner('Refreshed.');
  }

  function fillModelDatalist(id, models) {
    const list = $(id);
    clear(list);
    for (const name of models) list.append(el('option', '', name));
  }

  async function boot() {
    if (!bridge) {
      $('boot-status').textContent = 'This page must be opened inside the Echo desktop app.';
      return;
    }
    document.body.dataset.platform = bridge.platform;
    appInfo = await bridge.info();
    $('setup-home').textContent = `${appInfo.home}/.env`;

    const setup = await bridge.setupInfo();
    const models = setup.lmStudio.models || [];
    $('setup-base-url').value = setup.lmStudio.baseUrl;
    fillModelDatalist('setup-model-list', models);
    if (models.length) {
      $('setup-model').value = models[0];
      $('setup-detected').textContent = `LM Studio detected · ${models.length} model${models.length === 1 ? '' : 's'} available.`;
    } else {
      $('setup-detected').textContent = 'LM Studio was not detected on port 1234. Enter an Ollama or OpenAI-compatible endpoint below.';
      $('setup-detected').classList.add('off');
    }

    if (setup.firstRun) {
      showScreen('setup');
    } else {
      await startApp();
    }
  }

  async function startApp() {
    showScreen('boot');
    $('boot-status').textContent = 'Starting Echo backend…';
    $('boot-error').classList.add('hidden');
    try {
      await bridge.startBackend();
      $('status-dot').classList.add('ok');
      $('status-text').textContent = 'Local backend running';
      $('boot-status').textContent = 'Loading your workspace…';
      await refreshState();
      showScreen('app');
      setTab('chat');
      attempt(loadModels, 'Cannot load models');
      attempt(renderHealth, 'Cannot check workspace');
      attempt(renderLog, 'Cannot read log');
    } catch (error) {
      $('boot-status').textContent = 'Echo could not start.';
      $('status-dot').classList.add('bad');
      $('status-text').textContent = 'Backend unavailable';
      $('boot-error').textContent = error.message;
      $('boot-error').classList.remove('hidden');
    }
  }

  function wire() {
    $('setup-connect').addEventListener('click', () => attempt(async () => {
      const provider = $('setup-provider').value;
      if (provider === 'openai' && !$('setup-base-url').value.trim()) {
        throw new Error('Enter the LM Studio endpoint.');
      }
      const model = $('setup-model').value.trim();
      if (!model) throw new Error('Choose the model to use.');
      await bridge.saveSetup({
        provider,
        baseUrl: $('setup-base-url').value,
        model,
        apiKey: $('setup-api-key').value,
      });
      await startApp();
    }, 'Cannot save connection'));

    $('new-chat').addEventListener('click', () => attempt(newChat, 'Cannot create chat'));
    $('delete-chat').addEventListener('click', () => attempt(deleteChat, 'Cannot delete chat'));
    $('refresh').addEventListener('click', () => attempt(refreshAll, 'Cannot refresh'));
    $('open-workspace').addEventListener('click', () => bridge.openFolder(state.config.home || appInfo.home));
    for (const button of document.querySelectorAll('#nav button')) {
      button.addEventListener('click', () => setTab(button.dataset.tab));
    }

    $('chat-attach').addEventListener('click', () => attempt(pickAttachments, 'Cannot attach files'));
    $('chat-send').addEventListener('click', () => attempt(sendChat, 'Cannot send message'));
    $('ask-send').addEventListener('click', () => attempt(sendAsk, 'Cannot ask'));
    for (const [input, button] of [['chat-input', 'chat-send'], ['ask-input', 'ask-send']]) {
      $(input).addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          $(button).click();
        }
      });
    }

    $('new-doc').addEventListener('click', newDoc);
    $('save-doc').addEventListener('click', () => attempt(saveDoc, 'Cannot save document'));
    $('archive-doc').addEventListener('click', () => attempt(archiveDoc, 'Cannot archive document'));
    $('mem-add').addEventListener('click', () => attempt(addMemory, 'Cannot save memory'));

    $('browser-install').addEventListener('click', () => browserOp('/api/browser/install', {}, 'Cannot install browser'));
    $('browser-start').addEventListener('click', () => browserOp('/api/browser/start', {
      profile_dir: $('browser-profile').value,
      headless: $('browser-headless').checked,
    }, 'Cannot start browser'));
    $('browser-stop').addEventListener('click', () => browserOp('/api/browser/stop', {}, 'Cannot stop browser'));
    $('browser-goto').addEventListener('click', () => browserOp('/api/browser/goto', { url: $('browser-url').value }, 'Cannot navigate'));
    $('browser-snap').addEventListener('click', () => browserOp('/api/browser/snapshot', { limit: 120 }, 'Cannot snapshot'));
    $('browser-click').addEventListener('click', () => browserOp('/api/browser/click', { ref: $('browser-ref').value }, 'Cannot click'));
    $('browser-fill').addEventListener('click', () => browserOp('/api/browser/fill', {
      ref: $('browser-ref').value, text: $('browser-text').value,
    }, 'Cannot fill'));
    $('browser-press').addEventListener('click', () => browserOp('/api/browser/press', { key: $('browser-text').value }, 'Cannot press key'));

    $('save-cfg').addEventListener('click', () => attempt(saveConfig, 'Cannot save settings'));
    $('test-model').addEventListener('click', () => attempt(testModel, 'Cannot reach model'));

    bridge.onAction((action) => {
      if (action.startsWith('tab:')) {
        setTab(action.slice(4));
      } else if (action === 'new-chat') {
        attempt(newChat, 'Cannot create chat');
      } else if (action === 'new-doc') {
        setTab('docs');
        newDoc();
      } else if (action === 'open-workspace') {
        bridge.openFolder(state.config.home || appInfo.home);
      } else if (action === 'refresh') {
        attempt(refreshAll, 'Cannot refresh');
      }
    });
  }

  wire();
  boot().catch((error) => {
    $('boot-status').textContent = 'Echo could not start.';
    $('boot-error').textContent = error.message;
    $('boot-error').classList.remove('hidden');
    showScreen('boot');
  });
})();
