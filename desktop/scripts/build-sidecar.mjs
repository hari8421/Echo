import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '../..');
const outDir = path.join(repoRoot, 'dist', 'backend');
const workDir = path.join(repoRoot, '.pyinstaller');
const python = process.env.ECHO_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

function run(command, args) {
  const result = spawnSync(command, args, { cwd: repoRoot, stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} exited with ${result.status}`);
}

const available = spawnSync(python, ['-c', 'import PyInstaller'], { cwd: repoRoot, stdio: 'ignore' });
if (available.status !== 0) {
  throw new Error(`PyInstaller is required to build the backend. Run: ${python} -m pip install pyinstaller`);
}

// The bundled backend must be able to install Chromium through the Playwright
// CLI on first launch, so its node driver and package data are collected too.
const playwright = spawnSync(python, ['-c', 'import playwright'], { cwd: repoRoot, stdio: 'ignore' });
if (playwright.status !== 0) {
  throw new Error(`Playwright is required to build the backend. Run: ${python} -m pip install playwright`);
}

fs.mkdirSync(outDir, { recursive: true });
fs.mkdirSync(workDir, { recursive: true });

run(python, [
  '-m', 'PyInstaller',
  '--noconfirm',
  '--clean',
  '--onefile',
  '--collect-all', 'playwright',
  '--distpath', outDir,
  '--workpath', workDir,
  '--specpath', workDir,
  '--paths', repoRoot,
  '--name', 'echo-sidecar',
  path.join(repoRoot, 'echo_agent', 'sidecar.py'),
]);

console.log(`Backend written to ${outDir}`);
