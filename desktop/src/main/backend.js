'use strict';

/** Backend lifecycle for the native Echo app.
 *
 * The UI is Electron, but the agent stays the Python implementation in this
 * repo. In development the app runs `python -m echo_agent.sidecar`; packaged
 * builds run the PyInstaller sidecar shipped in `resources/backend`.
 */

const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const LM_STUDIO_URL = 'http://127.0.0.1:1234/v1';
const HOST = '127.0.0.1';

function echoHome() {
  const configured = process.env.ECHO_HOME || '~/.echo';
  if (configured === '~' || configured.startsWith('~/')) {
    return path.join(os.homedir(), configured.slice(2));
  }
  return path.resolve(configured);
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, HOST, () => {
      const address = server.address();
      server.close((error) => (error ? reject(error) : resolve(address.port)));
    });
  });
}

function resolveBackend(app, port, resourcesPath = process.resourcesPath || '') {
  if (!app || !app.isPackaged) {
    const root = path.resolve(__dirname, '../../..');
    const python = process.env.ECHO_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
    return {
      command: python,
      args: ['-m', 'echo_agent.sidecar', '--host', HOST, '--port', String(port)],
      cwd: root,
    };
  }

  const name = process.platform === 'win32' ? 'echo-sidecar.exe' : 'echo-sidecar';
  const command = path.join(resourcesPath, 'backend', name);
  if (!fs.existsSync(command)) {
    throw new Error(`Bundled backend is missing: ${command}. Rebuild it with npm run sidecar.`);
  }
  return { command, args: ['--host', HOST, '--port', String(port)], cwd: echoHome() };
}

function streamLogger(level, onLog) {
  let pending = '';
  return (chunk) => {
    pending += chunk.toString('utf8');
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() || '';
    for (const text of lines) {
      if (text.trim()) onLog?.({ level, text: text.trim() });
    }
  };
}

async function startBackend({ app, onLog, resourcesPath } = {}) {
  const port = await getFreePort();
  const spec = resolveBackend(app, port, resourcesPath);
  const baseUrl = `http://${HOST}:${port}`;
  let child;

  try {
    child = spawn(spec.command, spec.args, {
      cwd: spec.cwd,
      env: { ...process.env, ECHO_HOME: process.env.ECHO_HOME || echoHome(), PYTHONUNBUFFERED: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (error) {
    throw new Error(`Cannot start Echo backend (${spec.command}): ${error.message}`);
  }

  child.stdout.on('data', streamLogger('info', onLog));
  child.stderr.on('data', streamLogger('error', onLog));

  let failed = null;
  let exited = false;
  child.once('error', (error) => {
    failed = error;
    onLog?.({ level: 'error', text: error.message });
  });
  child.once('exit', () => {
    exited = true;
  });

  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (failed) throw new Error(`Cannot start Echo backend: ${failed.message}`);
    if (exited) throw new Error('Echo backend exited before it was ready. Check the log in Settings.');
    try {
      const response = await fetch(`${baseUrl}/api/state`, { signal: AbortSignal.timeout(1000) });
      if (response.status < 600) return { child, baseUrl, port };
    } catch (_) {
      // Not listening yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }

  stopBackend(child);
  throw new Error('Echo backend did not become ready within 30 seconds. Check the log in Settings.');
}

function stopBackend(child) {
  if (!child || !child.pid || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    return;
  }
  child.kill('SIGTERM');
  const timer = setTimeout(() => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  }, 3000);
  timer.unref();
}

async function apiRequest(baseUrl, apiPath, body) {
  if (!apiPath || !apiPath.startsWith('/api/')) {
    throw new Error('Invalid API path.');
  }
  const isPost = body !== undefined && body !== null;
  const response = await fetch(`${baseUrl}${apiPath}`, {
    method: isPost ? 'POST' : 'GET',
    headers: isPost ? { 'Content-Type': 'application/json' } : {},
    body: isPost ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(isPost ? 180000 : 20000),
  });
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, data };
}

function readEnvFile(envPath) {
  const values = {};
  if (!fs.existsSync(envPath)) return values;
  const text = fs.readFileSync(envPath, 'utf8');
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const index = line.indexOf('=');
    const key = line.slice(0, index).trim();
    const value = line.slice(index + 1).trim().replace(/^["']|["']$/g, '');
    if (key) values[key] = value;
  }
  return values;
}

function saveSetup({ provider, baseUrl, base_url, model, apiKey, api_key } = {}) {
  const home = echoHome();
  fs.mkdirSync(home, { recursive: true });
  const envPath = path.join(home, '.env');
  const values = readEnvFile(envPath);
  const endpoint = baseUrl || base_url;
  const key = apiKey !== undefined && apiKey !== null ? apiKey : api_key;

  if (provider) values.ECHO_PROVIDER = String(provider).trim();
  if (endpoint) values.ECHO_BASE_URL = String(endpoint).trim().replace(/\/+$/, '');
  if (model) values.ECHO_MODEL = String(model).trim();
  if (key !== undefined && key !== null) values.ECHO_API_KEY = String(key).trim();

  const body = Object.keys(values)
    .sort()
    .map((key) => `${key}=${values[key]}`)
    .join('\n') + '\n';
  const temp = `${envPath}.desktop-${process.pid}.tmp`;
  fs.writeFileSync(temp, body, 'utf8');
  fs.renameSync(temp, envPath);
  return envPath;
}

async function probeModels(baseUrl) {
  try {
    const response = await fetch(`${baseUrl.replace(/\/+$/, '')}/models`, {
      signal: AbortSignal.timeout(2000),
    });
    if (!response.ok) return { available: false, models: [] };
    const data = await response.json();
    const models = Array.isArray(data?.data)
      ? data.data.map((item) => String(item?.id || '')).filter(Boolean)
      : [];
    return { available: true, models };
  } catch (_) {
    return { available: false, models: [] };
  }
}

async function readSetupInfo() {
  const home = echoHome();
  const envPath = path.join(home, '.env');
  const values = readEnvFile(envPath);
  const configured = Boolean(values.ECHO_PROVIDER && values.ECHO_BASE_URL && values.ECHO_MODEL);
  const lmStudio = await probeModels(LM_STUDIO_URL);
  return {
    firstRun: !configured,
    home,
    envPath,
    lmStudio: { baseUrl: LM_STUDIO_URL, ...lmStudio },
  };
}

function isSafeExternalUrl(value) {
  try {
    const url = new URL(String(value));
    return url.protocol === 'http:' || url.protocol === 'https:';
  } catch (_) {
    return false;
  }
}

module.exports = {
  LM_STUDIO_URL,
  apiRequest,
  echoHome,
  getFreePort,
  isSafeExternalUrl,
  readEnvFile,
  readSetupInfo,
  resolveBackend,
  saveSetup,
  startBackend,
  stopBackend,
};
