'use strict';

const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const backend = require('../src/main/backend');

function withTempHome(fn) {
  const previous = process.env.ECHO_HOME;
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'echo-desktop-test-'));
  process.env.ECHO_HOME = home;
  return Promise.resolve()
    .then(() => fn(home), (error) => { process.env.ECHO_HOME = previous; throw error; })
    .finally(() => {
      if (previous === undefined) delete process.env.ECHO_HOME;
      else process.env.ECHO_HOME = previous;
      fs.rmSync(home, { recursive: true, force: true });
    });
}

test('development backend runs the Python sidecar module', () => {
  const spec = backend.resolveBackend({ isPackaged: false }, 9123);
  assert.match(spec.command, /python/);
  assert.deepEqual(spec.args.slice(-2), ['--port', '9123']);
  assert.ok(spec.args.includes('echo_agent.sidecar'));
});

test('packaged backend uses the bundled executable', () => {
  const resources = fs.mkdtempSync(path.join(os.tmpdir(), 'echo-resources-'));
  const dir = path.join(resources, 'backend');
  const name = process.platform === 'win32' ? 'echo-sidecar.exe' : 'echo-sidecar';
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, name), '');
  try {
    const spec = backend.resolveBackend({ isPackaged: true }, 9124, resources);
    assert.equal(spec.command, path.join(dir, name));
    assert.deepEqual(spec.args, ['--host', '127.0.0.1', '--port', '9124']);
  } finally {
    fs.rmSync(resources, { recursive: true, force: true });
  }
});

test('first-run setup writes an OpenAI-compatible config and preserves other keys', () => withTempHome(async (home) => {
  const envPath = path.join(home, '.env');
  fs.writeFileSync(envPath, 'ECHO_UI_PORT=9999\nECHO_TEMPERATURE=0.4\n');

  backend.saveSetup({
    provider: 'openai',
    baseUrl: 'http://127.0.0.1:1234/v1/',
    model: 'my-model',
    apiKey: '',
  });

  const values = backend.readEnvFile(envPath);
  assert.equal(values.ECHO_PROVIDER, 'openai');
  assert.equal(values.ECHO_BASE_URL, 'http://127.0.0.1:1234/v1');
  assert.equal(values.ECHO_MODEL, 'my-model');
  assert.equal(values.ECHO_UI_PORT, '9999');
  assert.equal(values.ECHO_TEMPERATURE, '0.4');
}));

test('setup also accepts snake_case keys from the renderer', () => withTempHome(async (home) => {
  backend.saveSetup({
    provider: 'ollama',
    base_url: 'http://localhost:11434',
    model: 'qwen2.5',
    api_key: 'secret',
  });
  const values = backend.readEnvFile(path.join(home, '.env'));
  assert.equal(values.ECHO_BASE_URL, 'http://localhost:11434');
  assert.equal(values.ECHO_MODEL, 'qwen2.5');
  assert.equal(values.ECHO_API_KEY, 'secret');
}));

test('readEnvFile ignores comments and strips quotes', () => withTempHome(async (home) => {
  const envPath = path.join(home, '.env');
  fs.writeFileSync(envPath, '# comment\nECHO_MODEL="llama"\nnot a kv\nECHO_API_KEY=\'secret\'\n');
  const values = backend.readEnvFile(envPath);
  assert.deepEqual(values, { ECHO_MODEL: 'llama', ECHO_API_KEY: 'secret' });
}));

test('apiRequest proxies GET and POST JSON', async () => {
  const server = http.createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({
        method: request.method,
        url: request.url,
        body: body ? JSON.parse(body) : null,
      }));
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  try {
    const baseUrl = `http://127.0.0.1:${port}`;
    const get = await backend.apiRequest(baseUrl, '/api/health');
    assert.deepEqual(get.data, { method: 'GET', url: '/api/health', body: null });

    const post = await backend.apiRequest(baseUrl, '/api/chat', { message: 'hello' });
    assert.deepEqual(post.data, { method: 'POST', url: '/api/chat', body: { message: 'hello' } });

    await assert.rejects(() => backend.apiRequest(baseUrl, '/etc/passwd'));
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('external links accept only http(s)', () => {
  assert.equal(backend.isSafeExternalUrl('https://example.com'), true);
  assert.equal(backend.isSafeExternalUrl('http://localhost:1234'), true);
  assert.equal(backend.isSafeExternalUrl('file:///etc/passwd'), false);
  assert.equal(backend.isSafeExternalUrl('not a url'), false);
});

test('getFreePort returns a usable localhost port', async () => {
  const port = await backend.getFreePort();
  assert.ok(port > 1024 && port < 65536);
});
