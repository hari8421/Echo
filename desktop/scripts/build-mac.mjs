import { spawnSync } from 'node:child_process';

// Build only the architecture of the current Mac: PyInstaller cannot cross-
// compile the sidecar, so each DMG must bundle a native sidecar for that CPU.
const arch = process.arch === 'x64' ? 'x64' : 'arm64';
console.log(`Building macOS ${arch} app (matching native backend).`);

const result = spawnSync('npx', ['--no-install', 'electron-builder', '--mac', `--${arch}`], {
  stdio: 'inherit',
});
if (result.error) throw result.error;
if (result.status !== 0) throw new Error(`electron-builder exited with ${result.status}`);
